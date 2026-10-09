from typing import Dict, Literal

from iam_core.user_auth.config import Settings as IamSettings
from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from . import __version__


class RegistryEndpoint(BaseModel):
    """Where one data controller (registry) answers DCI searches.

    Partner Management has no registry endpoints yet, so they are configured
    here. ``partner_id`` is the registry's PM partner ID: when set, the
    registry's response signature is verified against its PM key.
    """

    url: str  # DCI sync search URL, e.g. http://fr-partner-api/dci/registry/sync/search
    partner_id: str = ""
    receiver_id: str = ""  # header.receiver_id sent to it; defaults to the controller ID
    # Its data scope catalogue (signed POST); empty → the search URL with /dci/... → /partner/data_scopes.
    catalogue_url: str = ""


class Settings(IamSettings):
    """The composite's settings. The IAM (staff login) fields come from iam-core's
    Settings, read from AGRI_COMPOSITE_* like the rest; they matter only when the
    console is on (``console_enabled``)."""

    model_config = SettingsConfigDict(
        env_prefix="agri_composite_", env_file=".env", extra="allow", env_nested_delimiter="__"
    )

    openapi_title: str = "OpenG2P Agri Stack Composite"
    openapi_description: str = """
        Agri Stack use-case composite.

        Serves approved use cases (configuration, not code) by querying several
        registries over DCI with the partner's consent and returning one signed
        response. No partner data is stored (the console's call log holds
        outcomes and timings only).
        """
    openapi_version: str = __version__

    # No database unless the console is on. "none://" stops the base Settings
    # building a datasource URL from its defaults; set AGRI_COMPOSITE_DB_DATASOURCE
    # (or "" plus the DB_* parts) for the console's call log.
    db_datasource: str = "none://"

    # ── Console (staff admin API under /composite/v1/admin, IAM login) ─────────
    # Off: no admin routes, no IAM middleware, no database — the partner API only.
    # On: needs a database (the call log), the IAM staff API
    # (auth_provider_api_url), Redis (auth_redis_url) and keycloak_client_id.
    console_enabled: bool = False
    # Portal links shown in the console (Partner Management, Consent Manager).
    console_pm_portal_url: str = ""
    console_cm_portal_url: str = ""
    # Call log rows older than this are deleted (0 keeps everything).
    activity_retention_days: int = 90
    # Registry data scope catalogues are cached this long (seconds).
    catalogue_cache_seconds: int = 300

    # ── Use cases ─────────────────────────────────────────────────────────────
    # Directory of use-case YAML files (a Helm-mounted ConfigMap in a cluster).
    use_cases_dir: str = "use-cases"
    # Seconds between checks for changed files (0 disables reloading).
    use_cases_reload_seconds: int = 30

    # ── Registries (data controllers) ─────────────────────────────────────────
    # JSON in the env: {"farmer-registry": {"url": "...", "partner_id": ""}, ...}
    registries: Dict[str, RegistryEndpoint] = {}

    # ── Identity and keys ─────────────────────────────────────────────────────
    # The composite's own PM partner ID: header.sender_id towards registries
    # and to partners. Partners must send it as header.receiver_id.
    composite_partner_id: str = "agri-composite"
    # Composite signing key (.p12). Empty → the service cannot sign, so every
    # query fails with a clear error; listing use cases still works.
    signing_p12_path: str = ""
    signing_p12_password: str = ""
    signing_kid: str = ""  # blank → the certificate's SHA-256 thumbprint
    signing_algorithm: str = "auto"  # auto = from the key type (EC → ES256, Ed25519 → EdDSA, RSA → RS256)

    # Partner Management (partner public keys, cached per pod).
    partner_mgmt_api_url: str = "http://commons-services-pm-partner-api"
    crypto_allowed_algorithms: str = "EdDSA,ES256,RS256"
    partner_key_cache_ttl_seconds: int = 300
    partner_key_hard_ttl_seconds: int = 21600
    partner_key_negative_ttl_seconds: int = 30
    partner_key_refresh_cooldown_seconds: int = 10
    partner_key_fetch_timeout_seconds: float = 3.0

    # Partner request freshness (header.message_ts within now ± this). 0 disables.
    request_max_skew_seconds: int = 300

    # ── Outbound HTTP (one pooled client per worker) ──────────────────────────
    http_max_connections: int = 200
    http_max_keepalive_connections: int = 50
    http_keepalive_expiry_seconds: float = 30.0
    default_source_timeout_ms: int = 5000
    default_overall_timeout_ms: int = 10000

    # ── Consent mode ──────────────────────────────────────────────────────────
    # passthrough (default): the partner's consent JWS goes to each registry
    # unchanged; each registry validates it with its own Consent Manager.
    # exchange: the composite first validates the partner's consent at the
    # exchange Consent Manager (consent_exchange_cm_url) asking for consent
    # receipts, and sends each registry its own controller's receipt instead.
    consent_mode: Literal["passthrough", "exchange"] = "passthrough"
    # Exchange Consent Manager partner-api base URL (…/consent/v1/validate is
    # appended), e.g. http://commons-services-consent-manager-partner-api.
    consent_exchange_cm_url: str = ""
    consent_exchange_cm_timeout_seconds: float = 5.0

    # ── Audit Manager (CloudEvents, fire-and-forget). Empty URL → off. ────────
    audit_manager_url: str = ""
    audit_timeout_seconds: float = 2.0
    audit_source: str = "/openg2p/agri-composite"
