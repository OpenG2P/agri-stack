from typing import Dict

from openg2p_fastapi_common.config import Settings as BaseSettings
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="agri_composite_", env_file=".env", extra="allow")

    openapi_title: str = "OpenG2P Agri Stack Composite"
    openapi_description: str = """
        Agri Stack use-case composite.

        Serves approved use cases (configuration, not code) by querying several
        registries over DCI with the partner's consent and returning one signed
        response. Nothing is stored.
        """
    openapi_version: str = __version__

    # No database. The base Settings would otherwise build a datasource URL from
    # its defaults; the Initializer also skips init_db.
    db_datasource: str = "none://"

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

    # ── Audit Manager (CloudEvents, fire-and-forget). Empty URL → off. ────────
    audit_manager_url: str = ""
    audit_timeout_seconds: float = 2.0
    audit_source: str = "/openg2p/agri-composite"
