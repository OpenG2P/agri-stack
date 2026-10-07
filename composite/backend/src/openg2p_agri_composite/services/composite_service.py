import asyncio
import logging
from typing import Optional

import httpx
from openg2p_fastapi_common.service import BaseService

from ..config import Settings
from ..core.audit import AuditEmitter
from ..core.crypto import build_composite_crypto
from ..core.engine import CompositeEngine, EngineSettings
from ..core.loader import UseCaseRegistry

_config = Settings.get_config()
_logger = logging.getLogger(_config.logging_default_logger_name)


class CompositeService(BaseService):
    """Wires the engine to the service configuration.

    One pooled ``httpx.AsyncClient`` per worker process, created lazily on the
    worker's event loop (after gunicorn forks) and closed at shutdown. Startup
    loads the use-case files but never calls a registry.
    """

    def __init__(self, name="", http_transport: Optional[httpx.AsyncBaseTransport] = None, pm_transport=None, **kwargs):
        super().__init__(name=name, **kwargs)
        self._transport = http_transport
        self._client: Optional[httpx.AsyncClient] = None
        self._reload_task: Optional[asyncio.Task] = None

        registries = {cid: ep.model_dump() for cid, ep in _config.registries.items()}
        if not registries:
            _logger.warning("No registries configured (AGRI_COMPOSITE_REGISTRIES); every source will fail")
        self.registry = UseCaseRegistry(_config.use_cases_dir, known_controllers=registries.keys())
        self.registry.load()

        self.crypto = build_composite_crypto(
            partner_mgmt_api_url=_config.partner_mgmt_api_url,
            signing_p12_path=_config.signing_p12_path,
            signing_p12_password=_config.signing_p12_password,
            signing_kid=_config.signing_kid,
            signing_algorithm=_config.signing_algorithm,
            allowed_algorithms=_config.crypto_allowed_algorithms,
            soft_ttl=_config.partner_key_cache_ttl_seconds,
            hard_ttl=_config.partner_key_hard_ttl_seconds,
            negative_ttl=_config.partner_key_negative_ttl_seconds,
            refresh_cooldown=_config.partner_key_refresh_cooldown_seconds,
            fetch_timeout=_config.partner_key_fetch_timeout_seconds,
            pm_transport=pm_transport,
        )
        if _config.consent_mode == "exchange" and not _config.consent_exchange_cm_url:
            _logger.error("Consent mode is exchange but AGRI_COMPOSITE_CONSENT_EXCHANGE_CM_URL is empty; "
                          "every query that needs consent will fail")
        self.audit = AuditEmitter(
            _config.audit_manager_url, self.http_client, timeout=_config.audit_timeout_seconds, source=_config.audit_source
        )
        self.engine = CompositeEngine(
            EngineSettings(
                composite_partner_id=_config.composite_partner_id,
                request_max_skew_seconds=_config.request_max_skew_seconds,
                default_source_timeout_ms=_config.default_source_timeout_ms,
                default_overall_timeout_ms=_config.default_overall_timeout_ms,
                registries=registries,
                consent_mode=_config.consent_mode,
                exchange_cm_url=_config.consent_exchange_cm_url,
                exchange_cm_timeout_seconds=_config.consent_exchange_cm_timeout_seconds,
            ),
            self.registry,
            self.crypto,
            self.http_client,
            audit=self.audit,
        )

    def http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                limits=httpx.Limits(
                    max_connections=_config.http_max_connections,
                    max_keepalive_connections=_config.http_max_keepalive_connections,
                    keepalive_expiry=_config.http_keepalive_expiry_seconds,
                ),
                timeout=_config.default_source_timeout_ms / 1000.0,
            )
        return self._client

    async def start(self):
        if _config.use_cases_reload_seconds > 0 and self._reload_task is None:
            self._reload_task = asyncio.get_running_loop().create_task(self._reload_loop())

    async def stop(self):
        if self._reload_task:
            self._reload_task.cancel()
            self._reload_task = None
        await self.audit.drain()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _reload_loop(self):
        while True:
            await asyncio.sleep(_config.use_cases_reload_seconds)
            try:
                if self.registry.reload_if_changed():
                    _logger.info("Use-case files changed; reloaded")
            except Exception:
                _logger.exception("Use-case reload failed; keeping the loaded set")
