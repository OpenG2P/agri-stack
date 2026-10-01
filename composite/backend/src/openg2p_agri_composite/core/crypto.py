"""Signing and verification through openg2p-fastapi-common's crypto helper.

* Partner keys: the ``partner-mgmt`` backend (PartnerMgmtKeyStore — PM key API,
  per-pod cache with soft/hard/negative TTL, unknown-kid refresh, single-flight).
  Keys are looked up as ``PARTNER_<SENDER_ID>`` (upper-case, '-' → '_'), as the
  registries do.
* Own key: a PKCS#12 (.p12) file; detached JWS over ``{header, message}``
  (canonical JSON, sorted keys), the DCI convention.
"""

import logging
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from openg2p_fastapi_common.utils.crypto import PartnerMgmtKeyStore, build_crypto_helper

_logger = logging.getLogger("agri_composite.crypto")


def partner_reference_id(sender_id: str) -> str:
    return f"PARTNER_{(sender_id or '').replace('-', '_').upper()}"


def algorithm_for_p12(path: str, password: str) -> Optional[str]:
    """JWS algorithm matching the .p12's key type (EdDSA / ES256 / RS256)."""
    with open(path, "rb") as fh:
        key, _cert, _ = pkcs12.load_key_and_certificates(fh.read(), password.encode() if password else None)
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return "EdDSA"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return "ES256"
    if isinstance(key, rsa.RSAPrivateKey):
        return "RS256"
    return None


class CompositeCrypto:
    def __init__(self, helper, can_sign: bool):
        self.helper = helper
        self.can_sign = can_sign

    async def sign_detached(self, payload: Dict[str, Any]) -> str:
        return await self.helper.create_jwt_token(payload=payload, include_payload=False)

    async def verify_detached(self, signature: str, payload: Dict[str, Any], sender_id: str) -> bool:
        try:
            return bool(await self.helper.verify_jwt(signature, payload=payload, km_ref_id=partner_reference_id(sender_id)))
        except Exception:
            _logger.exception("Signature verification error for sender '%s'", sender_id)
            return False

    async def verify_compact(self, jws: str, sender_id: str) -> bool:
        try:
            return bool(await self.helper.verify_jwt(jws, payload=None, km_ref_id=partner_reference_id(sender_id)))
        except Exception:
            _logger.exception("Consent signature verification error for partner '%s'", sender_id)
            return False


def build_composite_crypto(
    *,
    partner_mgmt_api_url: str,
    signing_p12_path: str,
    signing_p12_password: str,
    signing_kid: str,
    signing_algorithm: str,
    allowed_algorithms: str,
    soft_ttl: int = 300,
    hard_ttl: int = 21600,
    negative_ttl: int = 30,
    refresh_cooldown: int = 10,
    fetch_timeout: float = 3.0,
    pm_transport=None,
) -> CompositeCrypto:
    store = PartnerMgmtKeyStore(
        api_url=partner_mgmt_api_url or "",
        soft_ttl=soft_ttl,
        hard_ttl=hard_ttl,
        negative_ttl=negative_ttl,
        refresh_cooldown=refresh_cooldown,
        timeout=fetch_timeout,
        transport=pm_transport,
    )
    allowed = [a.strip() for a in (allowed_algorithms or "").split(",") if a.strip()]
    alg = (signing_algorithm or "").strip()
    can_sign = False
    if signing_p12_path:
        try:
            detected = algorithm_for_p12(signing_p12_path, signing_p12_password)
            if alg in ("", "auto"):
                alg = detected or "RS256"
            elif detected and detected != alg:
                _logger.warning("signing_algorithm %s does not match the .p12 key (%s); using %s", alg, detected, detected)
                alg = detected
            can_sign = True
        except Exception as e:
            _logger.error("Cannot load the composite signing key %s: %s", signing_p12_path, e)
    else:
        _logger.error("No composite signing key configured (signing_p12_path); queries will fail until one is set")
    if alg in ("", "auto"):
        alg = "RS256"
    if alg not in allowed:
        allowed.append(alg)
    helper = build_crypto_helper(
        name="agri-composite-crypto",
        backend="partner-mgmt",
        partner_key_store=store,
        signing_key_path=signing_p12_path or "",
        signing_key_password=signing_p12_password or "",
        signing_key_kid=signing_kid or "",
        signing_algorithm=alg,
        allowed_algorithms=allowed,
    )
    return CompositeCrypto(helper, can_sign)
