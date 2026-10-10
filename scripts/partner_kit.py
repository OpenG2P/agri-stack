#!/usr/bin/env python3
"""Partner test kit for the Agri Stack composite.

Needs only the standard library plus ``cryptography`` and ``pyjwt``
(``pip install cryptography pyjwt``). Test use only — the keys it writes are
plain files.

  python partner_kit.py keys                      # partner key + composite .p12, and what to onboard
  python partner_kit.py consent --subject FAYDA_FAN:123456789012
  python partner_kit.py call --url https://agri-composite.<ns>.openg2p.org \\
         --subject FAYDA_FAN:123456789012 --param crop_year=2019 --param season=SEASON_MEHER
  python partner_kit.py describe --url https://agri-composite.<ns>.openg2p.org

Identifiers: a partner signs DCI envelopes with ``header.sender_id`` = its
partner ID (e.g. ``bank-a``); Partner Management holds its key under
``PARTNER_<ID>`` upper-cased with '-' → '_' (e.g. ``PARTNER_BANK_A``), which is
where the composite and the registries look it up.
"""

import argparse
import base64
import json
import os
import re
import secrets
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from jwt import PyJWS

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kit-out")

# Data scopes per registry for loan-profile: scope IDs (<registry>.<name>) from
# each registry's scope catalogue (signed POST /partner/data_scopes on the registry).
# The Farmer Registry grant must include farmer-registry.farmer_identifiers: the
# farmer ID the Crop Sown Registry is queried by is read from it.
GRANTS = {
    "farmer-registry": [f"farmer-registry.{name}" for name in (
        "farmer_identifiers", "personal_details", "household_location", "land", "land_location", "main_crops")],
    "crop-sown-registry": [f"crop-sown-registry.{name}" for name in (
        "farmer_reference", "crop_season", "measures", "location")],
}


def pm_reference(partner_id: str) -> str:
    return f"PARTNER_{partner_id.replace('-', '_').upper()}"


def canonical(obj) -> bytes:
    # Same bytes as the services' orjson OPT_SORT_KEYS: compact, sorted, UTF-8.
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def public_pem(key) -> str:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def private_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def utc_ts(now=None) -> str:
    """DCI header timestamp, e.g. 2026-10-01T10:00:00.000Z."""
    now = now or datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ── pure helpers (also used by partner_test.py and setup_exchange.py) ─────────

DEFAULT_PURPOSE = "credit-assessment"
SUBJECT_ID_TYPES = ["FAYDA_FAN", "FARMER_ID"]
SIGNING_ALG = "ES256"  # the kit's keys are EC P-256
POLICY_MAX_VALIDITY = "P90D"


def generate_partner_key():
    return ec.generate_private_key(ec.SECP256R1())


def generate_composite_p12(composite_id: str, password: str, days: int = 365):
    """A new EC key and self-signed certificate as a .p12. Returns (key, p12 bytes)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, composite_id)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
    p12 = pkcs12.serialize_key_and_certificates(
        composite_id.encode(), key, cert, None, serialization.BestAvailableEncryption(password.encode()))
    return key, p12


def load_p12_key(p12: bytes, password: str):
    key, _cert, _extra = pkcs12.load_key_and_certificates(p12, password.encode() if password else None)
    return key


def onboarding_payload(partner_id: str, label: str, key, kid: str) -> dict:
    """PM staff API body for POST /partners/requests/onboarding."""
    return {"partner_id": pm_reference(partner_id), "name": label, "org_name": label,
            "description": "Agri Stack composite test (TEST)",
            "keys": [{"public_key": public_pem(key), "kid": kid, "algorithm": SIGNING_ALG}]}


def binding_payload(audience: str, controller: str) -> dict:
    """CM staff API body for POST /consent/v1/partners (one binding per controller)."""
    return {"audience": audience, "controller_id": controller, "partner_mgmt_id": pm_reference(audience),
            "name": f"TEST {audience} → {controller}"}


def policy_payload(controller: str, purpose: str = DEFAULT_PURPOSE, scopes=None) -> dict:
    """CM staff API body for PUT /consent/v1/partners/{id}/policy (scopes: default GRANTS[controller])."""
    return {"allowed_data_scopes": list(GRANTS[controller] if scopes is None else scopes), "allowed_purposes": [purpose],
            "allowed_subject_id_types": list(SUBJECT_ID_TYPES), "allowed_signing_algs": [SIGNING_ALG],
            "max_validity_duration": POLICY_MAX_VALIDITY, "fetch_type": "oneshot"}


def make_consent(key, *, partner: str, kid: str, subject: dict, purpose: str = DEFAULT_PURPOSE,
                 controllers=None, valid_days: float = 30, now=None, scopes=None) -> str:
    """Partner-signed consent (compact JWS) with one grant per controller.

    scopes: {controller: [scope IDs]} to grant (e.g. a use case's consent_scopes); default GRANTS."""
    now = now or datetime.now(timezone.utc)
    grants_by_controller = GRANTS if scopes is None else scopes
    controllers = list(grants_by_controller) if controllers is None else list(controllers)
    claims = {
        "jti": str(uuid.uuid4()),
        "aud": partner,
        "subject_id": dict(subject),
        "purpose": {"code": purpose},
        "grants": [{"data_controller": c, "data_scopes": list(s)} for c, s in grants_by_controller.items()
                   if c in controllers],
        "fetch_type": "oneshot",
        "validity": {"valid_from": now.isoformat(timespec="seconds"),
                     "valid_until": (now + timedelta(days=valid_days)).isoformat(timespec="seconds")},
        "issued_at": now.isoformat(timespec="seconds"),
    }
    return PyJWS().encode(canonical(claims), key, algorithm=SIGNING_ALG, headers={"kid": kid})


def sign_detached(payload: dict, key, kid: str) -> str:
    """Detached JWS (header..signature) over the canonical JSON of payload."""
    p1, _p2, p3 = PyJWS().encode(canonical(payload), key, algorithm=SIGNING_ALG, headers={"kid": kid}).split(".")
    return f"{p1}..{p3}"


def build_query_envelope(key, *, partner: str, kid: str, composite: str, subject: dict, parameters=None,
                         consent_jws=None, now=None, consent_id=None) -> dict:
    """The signed request envelope a partner posts to .../use-cases/{use_case}/query.

    consent_jws: a consent the partner signs; consent_id: instead, the ID of a consent the
    subject gave through the Consent Manager (e.g. collected in the partner portal)."""
    header = {"version": "1.0.0", "message_id": str(uuid.uuid4()), "message_ts": utc_ts(now),
              "action": "query", "sender_id": partner, "receiver_id": composite}
    message = {"subject": dict(subject), "parameters": dict(parameters or {})}
    if consent_jws:
        message["consent_jws"] = consent_jws
    if consent_id:
        message["consent_id"] = consent_id
    return {"signature": sign_detached({"header": header, "message": message}, key, kid),
            "header": header, "message": message}


def jws_header(jws: str) -> dict:
    first = (jws or "").split(".")[0]
    try:
        return json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
    except ValueError:
        return {}


def verify_response_with_key(body, public_key) -> str:
    """'valid', 'MISSING' or 'INVALID (...)' for the composite's detached signature."""
    sig = (body or {}).get("signature") or ""
    parts = sig.split(".")
    if len(parts) != 3:
        return "MISSING"
    full = f"{parts[0]}.{b64u(canonical({'header': body['header'], 'message': body['message']}))}.{parts[2]}"
    try:
        PyJWS().decode(full, public_key, algorithms=["ES256", "RS256", "EdDSA"])
        return "valid"
    except Exception as e:
        return f"INVALID ({e})"


# ── keys ─────────────────────────────────────────────────────────────────────


# ── Shared by partner_test.py and setup_exchange.py: errors, key comparison, and the plans
#    for Partner Management, the Consent Manager and the composite's signing Secret. ──────

EXIT_OK, EXIT_ERROR, EXIT_DECISION, EXIT_CALL_FAILED = 0, 1, 2, 3
MANAGED_BY = "agri-stack-scripts"
CONSENT_VALID_DAYS = 1  # the test consents run for a day; a policy must allow at least that


class KitError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


def mask(value: Optional[str]) -> str:
    value = str(value or "")
    return ("…" + value[-4:]) if len(value) > 4 else "…"


def load_public(pem: str):
    data = pem.encode() if isinstance(pem, str) else pem
    if b"BEGIN CERTIFICATE" in data:
        return x509.load_pem_x509_certificate(data).public_key()
    return serialization.load_pem_public_key(data)


class _PublicOnly:
    """A public key where the kit expects a private one (it only calls .public_key())."""

    def __init__(self, pub):
        self._pub = pub

    def public_key(self):
        return self._pub


def spki(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def same_public_key(pem_a: Optional[str], pem_b: Optional[str]) -> bool:
    try:
        return bool(pem_a and pem_b) and spki(load_public(pem_a)) == spki(load_public(pem_b))
    except Exception:
        return False


def new_kid(owner: str) -> str:
    return f"{owner}-test-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2)}"


def _write_private(path: str, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


def plan_pm_partner(*, ref: str, kid: str, pub_pem: str, servable: List[dict], partner: Optional[dict],
                    pending: List[dict]) -> dict:
    """What PM needs so that `ref` serves (kid, pub_pem).

    servable: GET /keys/{ref} keys (only an active partner's valid keys); partner: GET /partners/{ref}
    or None; pending: open requests (status 'created') for ref.
    Returns {"state": "ok"|"actions"|"conflict", "actions": [...], "reason": str}. Actions:
    {"op": "onboard"} | {"op": "key_update"} (each then approved) | {"op": "approve", "request_id"} | {"op": "enable"}.
    """
    for k in servable:
        if k.get("kid") == kid:
            if same_public_key(k.get("public_key"), pub_pem):
                return {"state": "ok", "actions": [], "reason": f"PM serves kid {kid}"}
            return {"state": "conflict", "actions": [],
                    "reason": f"PM serves kid {kid} for {ref} with a different public key; rerun with --new-keys"}
    if partner is None:
        return {"state": "actions", "actions": [{"op": "onboard"}], "reason": f"{ref} is not in PM"}
    status = partner.get("status")
    mine = [r for r in pending if kid in [(k or {}).get("kid") for k in r.get("proposed_keys") or []]]
    actions: List[dict] = []
    if status == "disabled":
        actions.append({"op": "enable"})
    if mine:
        actions.append({"op": "approve", "request_id": mine[0]["id"], "request_type": mine[0].get("request_type")})
        return {"state": "actions", "actions": actions, "reason": f"a {mine[0].get('request_type')} request with kid {kid} is open"}
    if status == "created":
        other = next((r for r in pending if r.get("request_type") == "onboarding"), None)
        hint = f"open onboarding request {other['id']} has other keys" if other else "no open onboarding request"
        return {"state": "conflict", "actions": [],
                "reason": f"{ref} is onboarded but never approved ({hint}); approve or reject it in PM, then rerun"}
    actions.append({"op": "key_update"})
    return {"state": "actions", "actions": actions, "reason": f"{ref} is {status} without kid {kid}"}


def plan_composite_secret(*, secret: Optional[dict], local_pub_pem: str, local_kid: str,
                          pm_servable: List[dict]) -> dict:
    """Which composite key to use, and whether Secret agri-composite-signing must be (re)written.

    secret: None (missing) or {"pub_pem": str|None, "kid": str, "error": str|None}.
    Returns {"use": "local"|"cluster", "write_secret": bool, "reason": str}.
    """
    if secret is None:
        return {"use": "local", "write_secret": True, "reason": "the Secret does not exist"}
    pub = secret.get("pub_pem")
    if not pub:
        return {"use": "local", "write_secret": True,
                "reason": f"the Secret's .p12 cannot be read ({secret.get('error') or 'missing key'}); it will be replaced"}
    if same_public_key(pub, local_pub_pem) and (secret.get("kid") or "") == local_kid:
        return {"use": "local", "write_secret": False, "reason": "the Secret holds this script's composite key"}
    skid = secret.get("kid") or ""
    for k in pm_servable:
        if same_public_key(k.get("public_key"), pub) and (not skid or k.get("kid") == skid):
            return {"use": "cluster", "write_secret": False,
                    "reason": f"the Secret's key (kid {skid or k.get('kid')}) is already served by PM; left as is"}
    return {"use": "local", "write_secret": True,
            "reason": f"the Secret's key (kid {skid or 'thumbprint'}) is not served by PM; it will be replaced"}


_DURATION = re.compile(r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?$")


def duration_days(value: Optional[str]) -> Optional[float]:
    """Days in a simple ISO-8601 date duration (PnYnMnWnD); None = no cap or not parsable."""
    m = _DURATION.match(value or "")
    if not value or not m:
        return None
    y, mo, w, d = (int(x or 0) for x in m.groups())
    return y * 365 + mo * 30 + w * 7 + d


def policy_covers(policy: Optional[dict], needed: dict, subject_type: str) -> bool:
    if not policy or policy.get("status", "active") != "active":
        return False
    if not set(needed["allowed_data_scopes"]) <= set(policy.get("allowed_data_scopes") or []):
        return False
    for field, values in (("allowed_purposes", needed["allowed_purposes"]),
                          ("allowed_subject_id_types", [subject_type]),
                          ("allowed_signing_algs", [SIGNING_ALG])):
        allowed = policy.get(field) or []
        if allowed and not set(values) <= set(allowed):
            return False
    cap = duration_days(policy.get("max_validity_duration"))
    return cap is None or cap >= CONSENT_VALID_DAYS


def merged_policy(active: Optional[dict], needed: dict) -> dict:
    """The needed policy, widened by what the active policy already allows (never narrows)."""
    if not active:
        return dict(needed)
    out = dict(needed)
    for field in ("allowed_data_scopes", "allowed_purposes", "allowed_subject_id_types", "allowed_signing_algs"):
        out[field] = sorted(set(needed[field]) | set(active.get(field) or []))
    old, new = duration_days(active.get("max_validity_duration")), duration_days(needed.get("max_validity_duration"))
    if active.get("max_validity_duration") and old is not None and new is not None and old > new:
        out["max_validity_duration"] = active["max_validity_duration"]
    return out


def plan_cm(*, audience: str, pm_ref: str, needed: Dict[str, dict], bindings: List[dict],
            policies: Dict[str, List[dict]], subject_type: str = "FAYDA_FAN") -> dict:
    """CM bindings + policies for `audience`, one per controller in `needed` ({controller: policy body}).

    bindings: GET /consent/v1/partners?audience=; policies: {binding id: GET .../{id}/policies}.
    Returns {"actions": [...], "conflicts": [...], "ok": [controllers]}.
    """
    actions, conflicts, ok = [], [], []
    for controller, policy in needed.items():
        b = next((x for x in bindings if x.get("audience") == audience and x.get("controller_id") == controller), None)
        if b is None:
            actions.append({"op": "create_binding", "controller": controller})
            actions.append({"op": "put_policy", "controller": controller, "body": policy})
            continue
        if b.get("partner_mgmt_id") and b["partner_mgmt_id"] != pm_ref:
            conflicts.append(f"{audience} → {controller}: binding {b['id']} uses partner_mgmt_id "
                             f"{b['partner_mgmt_id']}, not {pm_ref}")
            continue
        if b.get("status") != "active":
            actions.append({"op": "activate_binding", "controller": controller, "binding_id": b["id"]})
        versions = policies.get(b["id"]) or []
        active = next((p for p in versions if p.get("status") == "active"), None)
        if policy_covers(active, policy, subject_type):
            if b.get("status") == "active":
                ok.append(controller)
            continue
        pending = [p for p in versions if p.get("status") == "pending"]
        if pending:
            conflicts.append(f"{audience} → {controller}: policy v{pending[0].get('version')} is pending AWE "
                             f"approval (request {pending[0].get('awe_request_id')}); approve it, then rerun")
            continue
        actions.append({"op": "put_policy", "controller": controller, "binding_id": b["id"],
                        "body": merged_policy(active, policy)})
    return {"actions": actions, "conflicts": conflicts, "ok": ok}


def describe_pm_action(ref: str, action: dict, kid: str) -> str:
    op = action["op"]
    if op == "onboard":
        return f"PM: onboard {ref} with key {kid}, then approve the request"
    if op == "key_update":
        return f"PM: key-update request adding key {kid} to {ref}, then approve it"
    if op == "approve":
        return f"PM: approve open {action.get('request_type')} request {action['request_id']} for {ref}"
    if op == "enable":
        return f"PM: enable partner {ref}"
    return f"PM: {op} {ref}"


def describe_cm_action(audience: str, action: dict) -> str:
    op, controller = action["op"], action["controller"]
    if op == "create_binding":
        return f"CM: create binding {audience} → {controller} (partner_mgmt_id {pm_reference(audience)})"
    if op == "activate_binding":
        return f"CM: set binding {action['binding_id']} ({audience} → {controller}) active"
    body = action["body"]
    return (f"CM: put policy for {audience} → {controller}: scopes {body['allowed_data_scopes']}, "
            f"purposes {body['allowed_purposes']}, id types {body['allowed_subject_id_types']}")


def signing_secret_manifest(name: str, namespace: str, keys: Dict[str, str], p12: bytes, password: str,
                            kid: str) -> dict:
    def b64(v):
        return base64.b64encode(v if isinstance(v, bytes) else v.encode()).decode()

    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": MANAGED_BY}},
            "data": {keys["p12"]: b64(p12), keys["password"]: b64(password), keys["kid"]: b64(kid),
                     keys["algorithm"]: b64("auto")}}


def cmd_keys(a):
    os.makedirs(a.out, exist_ok=True)
    partner_key_path = os.path.join(a.out, f"{a.partner}.key.pem")
    if os.path.exists(partner_key_path) and not a.force:
        sys.exit(f"{partner_key_path} exists; use --force to replace the keys")

    partner = generate_partner_key()
    with open(partner_key_path, "wb") as fh:
        fh.write(private_pem(partner))
    os.chmod(partner_key_path, 0o600)

    composite, p12 = generate_composite_p12(a.composite, a.p12_password, a.days)
    p12_path = os.path.join(a.out, "composite.p12")
    with open(p12_path, "wb") as fh:
        fh.write(p12)
    os.chmod(p12_path, 0o600)
    with open(os.path.join(a.out, "composite.pub.pem"), "w") as fh:
        fh.write(public_pem(composite))
    with open(os.path.join(a.out, "kit.json"), "w") as fh:
        json.dump({"partner": a.partner, "partner_kid": a.partner_kid, "composite": a.composite,
                   "composite_kid": a.composite_kid}, fh, indent=2)

    print(f"Wrote {partner_key_path}, {p12_path} (password '{a.p12_password}'), composite.pub.pem, kit.json in {a.out}\n")
    print("1. Partner Management — onboard both, then approve each request")
    print("   (POST {pm-staff-portal-api}/partners/requests/onboarding, then POST /partners/requests/{id}/approve,")
    print("    as a client with PM's partner_manager role):\n")
    print(json.dumps(onboarding_payload(a.partner, f"TEST {a.partner}", partner, a.partner_kid), indent=2))
    print(json.dumps(onboarding_payload(a.composite, "Agri Stack composite", composite, a.composite_kid), indent=2))
    print("\n2. The composite's signing key Secret (namespace of the composite):\n")
    print(f"   kubectl -n <ns> create secret generic agri-composite-signing \\\n"
          f"     --from-file=composite.p12={p12_path} --from-literal=password='{a.p12_password}' \\\n"
          f"     --from-literal=kid={a.composite_kid} --from-literal=algorithm=auto")
    print("\n3. Consent Manager — bind the partner's audience to each registry with a policy")
    print("   (POST {cm-staff-api}/consent/v1/partners, then PUT /consent/v1/partners/{id}/policy, admin role):\n")
    for controller in GRANTS:
        print(f"   {controller}:")
        print("   binding " + json.dumps(binding_payload(a.partner, controller)))
        print("   policy  " + json.dumps(policy_payload(controller, a.purpose)))
    print(f"\n4. The use case must allow the partner: allowed_partners: [{a.partner}] (loan-profile has bank-a).")


def _load_partner_key(a):
    path = os.path.join(a.out, f"{a.partner}.key.pem")
    try:
        with open(path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)
    except FileNotFoundError:
        sys.exit(f"No partner key at {path}; run 'partner_kit.py keys' first")


# ── consent ──────────────────────────────────────────────────────────────────

def parse_subject(text):
    typ, sep, value = (text or "").partition(":")
    if not sep or not typ or not value:
        sys.exit("--subject must be TYPE:VALUE, e.g. FAYDA_FAN:123456789012")
    return {"type": typ, "value": value}


def build_consent(a, key) -> str:
    return make_consent(key, partner=a.partner, kid=a.partner_kid, subject=parse_subject(a.subject),
                        purpose=a.purpose, controllers=a.controllers, valid_days=a.valid_days)


def cmd_consent(a):
    jws = build_consent(a, _load_partner_key(a))
    print(jws)
    payload = jws.split(".")[1]
    print("\nclaims:", json.dumps(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))), indent=2),
          file=sys.stderr)


# ── call ─────────────────────────────────────────────────────────────────────

def _http(method, url, body=None, insecure=False, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")


def _parse_params(items):
    out = {}
    for item in items or []:
        k, sep, v = item.partition("=")
        if not sep:
            sys.exit(f"--param {item!r}: expected name=value")
        try:
            out[k] = json.loads(v)  # numbers, booleans
        except ValueError:
            out[k] = v
    return out


def verify_response(body, pub_path) -> str:
    try:
        with open(pub_path, "rb") as fh:
            pub = serialization.load_pem_public_key(fh.read())
    except FileNotFoundError:
        return f"not checked (no {pub_path})"
    return verify_response_with_key(body, pub)


def cmd_call(a):
    key = _load_partner_key(a)
    envelope = build_query_envelope(
        key, partner=a.partner, kid=a.partner_kid, composite=a.composite, subject=parse_subject(a.subject),
        parameters=_parse_params(a.param), consent_jws=None if a.no_consent else build_consent(a, key))
    url = f"{a.url.rstrip('/')}/composite/v1/use-cases/{a.use_case}/query"
    print(f"POST {url}", file=sys.stderr)
    status, body = _http("POST", url, envelope, a.insecure)
    print(f"HTTP {status}", file=sys.stderr)
    if isinstance(body, dict) and "signature" in body:
        print(f"response signature: {verify_response(body, os.path.join(a.out, 'composite.pub.pem'))}", file=sys.stderr)
    print(json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body)


def cmd_describe(a):
    path = "/composite/v1/use-cases" + (f"/{a.use_case}" if a.use_case else "")
    status, body = _http("GET", a.url.rstrip("/") + path, insecure=a.insecure)
    print(f"HTTP {status}", file=sys.stderr)
    print(json.dumps(body, indent=2, ensure_ascii=False))


def main():
    saved = {}
    out_default = os.environ.get("KIT_OUT", DEFAULT_OUT)
    try:
        with open(os.path.join(out_default, "kit.json")) as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        pass

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=out_default, help="where keys are written/read (default: scripts/kit-out)")
    p.add_argument("--partner", default=saved.get("partner", "bank-a"), help="partner ID (DCI sender_id)")
    p.add_argument("--partner-kid", default=saved.get("partner_kid", "bank-a-test-1"))
    p.add_argument("--composite", default=saved.get("composite", "agri-composite"), help="composite partner ID")
    p.add_argument("--composite-kid", default=saved.get("composite_kid", "agri-composite-test-1"))
    p.add_argument("--purpose", default="credit-assessment")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keys", help="generate test keys and print the onboarding steps")
    k.add_argument("--p12-password", default="openg2p-test")
    k.add_argument("--days", type=int, default=365)
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keys)

    for name, func, helptext in (("consent", cmd_consent, "print a partner-signed consent JWS"),
                                 ("call", cmd_call, "sign an envelope, call the composite, verify the answer")):
        c = sub.add_parser(name, help=helptext)
        c.add_argument("--subject", required=True, help="TYPE:VALUE, e.g. FAYDA_FAN:123456789012 or FARMER_ID:FR-0007")
        c.add_argument("--controllers", nargs="+", default=list(GRANTS), help="registries to grant (default: both)")
        c.add_argument("--valid-days", type=int, default=30)
        c.set_defaults(func=func)
        if name == "call":
            c.add_argument("--url", required=True, help="composite base URL")
            c.add_argument("--use-case", default="loan-profile", help="name or name@major")
            c.add_argument("--param", action="append", help="name=value (repeatable)")
            c.add_argument("--no-consent", action="store_true")
            c.add_argument("--insecure", action="store_true", help="skip TLS verification")

    d = sub.add_parser("describe", help="list published use cases (or one)")
    d.add_argument("--url", required=True)
    d.add_argument("--use-case", default="")
    d.add_argument("--insecure", action="store_true")
    d.set_defaults(func=cmd_describe)

    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
