#!/usr/bin/env python3
"""End-to-end test of the Agri Stack composite against a cluster namespace.

Runs from a developer laptop with the current kubectl context. It reaches the
services through `kubectl port-forward` (closed on exit), sets up a test partner
and the composite's signing key, picks a farmer who has crop seasons, calls the
`loan-profile` use case and prints the composite's JSON answer.

  python composite/scripts/e2e.py --namespace trial --partner bank-a --dry-run   # look only: GETs and SELECTs
  python composite/scripts/e2e.py --namespace trial --partner bank-a             # set up (asks first), then call
  python composite/scripts/e2e.py --namespace trial --partner bank-a --call-only --fan <FAN> \\
         --crop-year 2018 --season SEASON_MEHER --emit-curl --emit-postman loan-profile.postman.json

--partner is required: it must be one of the use case's allowed_partners
(loan-profile allows bank-a). The script registers a TEST key for it in PM.

Steps
  1. Discover   the composite, Partner Management (PM), Consent Manager (CM), Keycloak and
                Postgres from the namespace; check the test partner is in the use case's
                allowed_partners (if not, print the Helm value change and stop).
  2. Keys       a test partner key and a composite key (.p12), kept in --state-dir and reused.
  3. Setup      (idempotent) PM: onboard and approve both partners, or add the key (key-update);
                CM: a binding and policy per registry for the test partner's audience;
                Kubernetes: Secret agri-composite-signing + restart of the composite.
                Every write is listed first and needs --yes or a typed "yes".
  4. Farmer     --fan, or a FAN that is in the Farmer Registry and has crop seasons in the
                Crop Sown Registry (read-only SELECTs through `kubectl exec ... psql`).
  5. Call       consent with a grant per registry, signed envelope, POST, response signature
                checked against the composite's PM key; JSON on stdout, summary on stderr.

Exit codes: 0 ok, 1 error, 2 a decision is needed (allowed_partners, AWE approval, a key
conflict, writes not confirmed), 3 the call failed or a source did not answer ok.

Secrets (admin client secrets, the .p12 password) are read with kubectl into memory and never
printed. Logs mask the FAN; the composite's answer is printed as returned.
Needs: pip install -r composite/scripts/requirements.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

import partner_kit as kit  # noqa: E402

USE_CASE = "loan-profile"
FR_TABLE = "g2p_register_farmers"
CSR_TABLE = "g2p_activity_projection_crop_sown"
CONSENT_VALID_DAYS = 1
SIGNATURE_WINDOW_S = 300  # composite request_max_skew_seconds and CM replay_freshness_window_sec
MANAGED_BY = "agri-composite-e2e"

EXIT_OK, EXIT_ERROR, EXIT_DECISION, EXIT_CALL_FAILED = 0, 1, 2, 3


class E2EError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ── logging (stderr; stdout carries only the composite's JSON) ───────────────

def step(title: str):
    print(f"\n== {title}", file=sys.stderr, flush=True)


def info(msg: str = ""):
    print(f"   {msg}", file=sys.stderr, flush=True)


def warn(msg: str):
    print("   ! " + str(msg).replace("\n", "\n     "), file=sys.stderr, flush=True)


def mask(value: Optional[str]) -> str:
    value = str(value or "")
    return ("…" + value[-4:]) if len(value) > 4 else "…"


# ── kubectl ──────────────────────────────────────────────────────────────────

class Kube:
    def __init__(self, namespace: str, context: Optional[str] = None):
        self.namespace = namespace
        self.context = context

    def base(self) -> List[str]:
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        return cmd + ["-n", self.namespace]

    def run(self, *args: str, input: Optional[str] = None, check: bool = True, timeout: int = 120) -> str:
        proc = subprocess.run(self.base() + list(args), input=input, capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            # kubectl's own errors never contain secret values.
            raise E2EError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def get_json(self, *args: str) -> dict:
        return json.loads(self.run("get", *args, "-o", "json"))

    def get_optional(self, kind: str, name: str) -> Optional[dict]:
        proc = subprocess.run(self.base() + ["get", kind, name, "-o", "json"], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            if "NotFound" in proc.stderr or "not found" in proc.stderr:
                return None
            raise E2EError(f"kubectl get {kind} {name} failed: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout)

    def secret(self, name: str) -> Optional[Dict[str, bytes]]:
        """Secret data, decoded, in memory only. None if the Secret does not exist."""
        obj = self.get_optional("secret", name)
        if obj is None:
            return None
        return {k: base64.b64decode(v) for k, v in (obj.get("data") or {}).items()}

    def sql(self, pod: str, container: Optional[str], db: str, query: str) -> List[List[str]]:
        """Run one read-only query with psql inside the Postgres pod; the password stays in the pod."""
        script = ('PGOPTIONS="-c default_transaction_read_only=on" '
                  'PGPASSWORD="${POSTGRES_POSTGRES_PASSWORD:-$POSTGRES_PASSWORD}" '
                  'exec psql -U postgres -d "$1" -qAt -F "|" -v ON_ERROR_STOP=1')
        args = ["exec", "-i", pod] + (["-c", container] if container else []) + ["--", "sh", "-c", script, "sh", db]
        out = self.run(*args, input=query)
        return [line.split("|") for line in out.splitlines() if line.strip()]

    def apply(self, manifest: dict):
        self.run("apply", "-f", "-", input=json.dumps(manifest))


def container_env(deploy: dict) -> Dict[str, dict]:
    env = {}
    for c in deploy["spec"]["template"]["spec"].get("containers", [])[:1]:
        for e in c.get("env") or []:
            env[e["name"]] = e
    return env


def env_value(env: Dict[str, dict], name: str, default: str = "") -> str:
    return (env.get(name) or {}).get("value") or default


def env_secret_key(env: Dict[str, dict], name: str) -> Optional[Tuple[str, str]]:
    ref = ((env.get(name) or {}).get("valueFrom") or {}).get("secretKeyRef")
    return (ref["name"], ref["key"]) if ref else None


def json_list(value: str) -> List[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]
    except ValueError:
        return [v.strip() for v in value.split(",") if v.strip()]


def host_port(url: str) -> Tuple[str, int]:
    u = urlparse(url)
    return (u.hostname or "").split(".")[0], (u.port or (443 if u.scheme == "https" else 80))


# ── port-forwards ────────────────────────────────────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForwards:
    def __init__(self, kube: Kube):
        self.kube = kube
        self.procs: Dict[Tuple[str, int], Tuple[subprocess.Popen, str]] = {}

    def url(self, service: str, port: int) -> str:
        key = (service, port)
        if key in self.procs and self.procs[key][0].poll() is None:
            return self.procs[key][1]
        local = free_port()
        proc = subprocess.Popen(self.kube.base() + ["port-forward", f"svc/{service}", f"{local}:{port}"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise E2EError(f"port-forward to svc/{service}:{port} exited: {(proc.stderr.read() or '').strip()[:300]}")
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            proc.kill()
            raise E2EError(f"port-forward to svc/{service}:{port} did not come up")
        base = f"http://127.0.0.1:{local}"
        self.procs[key] = (proc, base)
        info(f"port-forward svc/{service}:{port} → {base}")
        return base

    def drop(self, service: str, port: int):
        entry = self.procs.pop((service, port), None)
        if entry:
            entry[0].terminate()

    def close(self):
        for proc, _base in self.procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc, _base in self.procs.values():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs.clear()


# ── use-case YAML (only the two keys this script needs; no YAML dependency) ──

def _unquote(v: str) -> str:
    return v.strip().strip("'\"")


def parse_allowed_partners(text: str) -> List[str]:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^allowed_partners:\s*(.*?)\s*(#.*)?$", line)
        if not m:
            continue
        rest = m.group(1)
        if rest.startswith("["):
            return [_unquote(x) for x in rest.strip("[]").split(",") if x.strip()]
        if rest:
            return [_unquote(rest)]
        out = []
        for nxt in lines[i + 1:]:
            item = re.match(r"^\s+-\s*(.+?)\s*(#.*)?$", nxt)
            if item:
                out.append(_unquote(item.group(1)))
            elif not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            else:
                break
        return out
    return []


def parse_top_scalar(text: str, key: str) -> Optional[str]:
    m = re.search(rf"^{re.escape(key)}:\s*([^#\n]+?)\s*(#.*)?$", text, re.M)
    return _unquote(m.group(1)) if m else None


def allowed_partners_change(partner: str, allowed: List[str], release: str, namespace: str) -> str:
    new = allowed + [partner]
    return "\n".join([
        f"The test partner '{partner}' is not in {USE_CASE}'s allowed_partners {allowed}.",
        "Change the composite release's values (Rancher → Apps → Installed Apps → "
        f"{release} → Edit YAML), key composite.useCases.{USE_CASE}:",
        f"-   allowed_partners: [{', '.join(allowed)}]",
        f"+   allowed_partners: [{', '.join(new)}]",
        "Pods reload the use case within about 30 s, no restart needed. "
        f"(With the helm CLI: helm -n {namespace} get values {release} --all -o yaml > values.yaml, "
        f"edit that line, then helm -n {namespace} upgrade {release} <chart> -f values.yaml.)",
        f"Or rerun with --partner <one of {allowed}> if that test partner is free to use.",
    ])


# ── farmer choice (pure) ─────────────────────────────────────────────────────

def choose_farmer(fr_rows, csr_rows, crop_year: Optional[int] = None, season: Optional[str] = None):
    """Pick a FAN in FR (active) with crop seasons in CSR.

    fr_rows: [(fan, functional_record_id, record_status)], csr_rows: [(fan, farmer_id, crop_year, season, n)].
    Returns (fan, [(crop_year, season, n), ...]) or None. Prefers a CSR farmer_id equal to FR's record ID
    (the composite queries CSR by the FARMER_ID it reads from the FR record), then a match on
    --crop-year/--season, then the most seasons.
    """
    fr = {}
    for fan, frid, status in fr_rows:
        if fan and (status or "ACTIVE").upper() == "ACTIVE":
            fr[fan] = frid
    seasons: Dict[str, list] = {}
    id_match: Dict[str, bool] = {}
    for fan, farmer_id, year, sea, n in csr_rows:
        if fan not in fr:
            continue
        seasons.setdefault(fan, []).append((int(year) if str(year).isdigit() else year, sea, int(n)))
        id_match[fan] = id_match.get(fan, False) or (farmer_id == fr[fan])
    if not seasons:
        return None

    def wanted(fan):
        return any((crop_year is None or y == crop_year) and (season is None or s == season) for y, s, _ in seasons[fan])

    ranked = sorted(seasons, key=lambda f: (not id_match[f], not wanted(f), -len(seasons[f]), f))
    best = ranked[0]
    return best, sorted(seasons[best], key=lambda t: (str(t[0]), t[1]), reverse=True)


# ── keys and comparison helpers ──────────────────────────────────────────────

def load_public(pem: str):
    data = pem.encode() if isinstance(pem, str) else pem
    if b"BEGIN CERTIFICATE" in data:
        return x509.load_pem_x509_certificate(data).public_key()
    return serialization.load_pem_public_key(data)


def spki(pub) -> bytes:
    return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def same_public_key(pem_a: Optional[str], pem_b: Optional[str]) -> bool:
    try:
        return bool(pem_a and pem_b) and spki(load_public(pem_a)) == spki(load_public(pem_b))
    except Exception:
        return False


def new_kid(owner: str) -> str:
    return f"{owner}-e2e-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2)}"


# Responses are saved here by default (git-ignored: they hold a farmer's personal data).
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


class _Tee:
    """A text stream that also writes to the run's log file."""

    def __init__(self, stream, log):
        self._stream, self._log = stream, log

    def write(self, text):
        self._stream.write(text)
        try:
            self._log.write(text)
        except ValueError:  # log closed at exit
            pass
        return len(text)

    def flush(self):
        self._stream.flush()
        try:
            self._log.flush()
        except ValueError:
            pass

    def isatty(self):
        return self._stream.isatty()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _write_private(path: str, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o600)


class State:
    """Test keys under ~/.agri-composite-e2e/<namespace>/ (0700), reused across runs.

    state.json: {"partners": {id: {"kid"}}, "composite": {"id", "kid", "p12_password"}}
    <partner>.key.pem, composite.p12 (0600). In --dry-run nothing is written: missing keys are
    generated in memory only.
    """

    def __init__(self, directory: str, dry_run: bool):
        self.dir = os.path.expanduser(directory)
        self.dry_run = dry_run
        self.data = {"partners": {}, "composite": {}}
        path = os.path.join(self.dir, "state.json")
        if os.path.exists(path):
            with open(path) as fh:
                self.data.update(json.load(fh))

    def _save(self):
        if self.dry_run:
            return
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        _write_private(os.path.join(self.dir, "state.json"), json.dumps(self.data, indent=2).encode())

    def partner_key(self, partner: str, renew: bool = False):
        """(private key, kid, created now?)"""
        path = os.path.join(self.dir, f"{partner}.key.pem")
        entry = self.data["partners"].get(partner)
        if entry and os.path.exists(path) and not renew:
            with open(path, "rb") as fh:
                return serialization.load_pem_private_key(fh.read(), password=None), entry["kid"], False
        key, kid = kit.generate_partner_key(), new_kid(partner)
        if not self.dry_run:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            _write_private(path, kit.private_pem(key))
            self.data["partners"][partner] = {"kid": kid}
            self._save()
        return key, kid, True

    def composite_key(self, composite_id: str, renew: bool = False):
        """(private key, p12 bytes, p12 password, kid, created now?)"""
        path = os.path.join(self.dir, "composite.p12")
        entry = self.data.get("composite") or {}
        if entry.get("id") == composite_id and os.path.exists(path) and not renew:
            with open(path, "rb") as fh:
                p12 = fh.read()
            return kit.load_p12_key(p12, entry["p12_password"]), p12, entry["p12_password"], entry["kid"], False
        password = secrets.token_urlsafe(24)
        key, p12 = kit.generate_composite_p12(composite_id, password)
        kid = new_kid(composite_id)
        if not self.dry_run:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            _write_private(path, p12)
            self.data["composite"] = {"id": composite_id, "kid": kid, "p12_password": password}
            self._save()
        return key, p12, password, kid, True


# ── planning (pure; unit-tested) ─────────────────────────────────────────────

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
    for field, value in (("allowed_purposes", needed["allowed_purposes"][0]),
                         ("allowed_subject_id_types", subject_type),
                         ("allowed_signing_algs", kit.SIGNING_ALG)):
        allowed = policy.get(field) or []
        if allowed and value not in allowed:
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
        return f"CM: create binding {audience} → {controller} (partner_mgmt_id {kit.pm_reference(audience)})"
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


# ── curl / Postman (pure) ────────────────────────────────────────────────────

def query_path(use_case: str = USE_CASE) -> str:
    return f"/composite/v1/use-cases/{use_case}/query"


def curl_command(base_url: str, envelope: dict, use_case: str = USE_CASE) -> str:
    body = json.dumps(envelope, ensure_ascii=False)
    return (f"curl -sS -X POST '{base_url.rstrip('/')}{query_path(use_case)}' \\\n"
            f"  -H 'Content-Type: application/json' --data-binary @- <<'JSON'\n{body}\nJSON")


def postman_collection(envelope: dict, ingress_url: str, forward_url: str, expires_at: str,
                       use_case: str = USE_CASE) -> dict:
    note = (f"Pre-signed by composite/scripts/e2e.py. The signature and the consent expire at {expires_at} "
            f"(about {SIGNATURE_WINDOW_S // 60} minutes after signing: header.message_ts and the consent's "
            "issued_at must be close to now). Do not edit the body: the signature covers it. Rerun the script "
            "with --emit-postman for a fresh request.")
    path = ["composite", "v1", "use-cases", use_case, "query"]

    def request(var):
        return {"method": "POST", "header": [{"key": "Content-Type", "value": "application/json"}],
                "body": {"mode": "raw", "raw": json.dumps(envelope, indent=2, ensure_ascii=False),
                         "options": {"raw": {"language": "json"}}},
                "url": {"raw": "{{" + var + "}}" + query_path(use_case), "host": ["{{" + var + "}}"], "path": path},
                "description": note}

    return {
        "info": {"name": f"Agri composite {use_case} (pre-signed, expires {expires_at})", "description": note,
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "baseUrl", "value": ingress_url}, {"key": "portForwardUrl", "value": forward_url}],
        "item": [
            {"name": f"{use_case} query (ingress)", "request": request("baseUrl")},
            {"name": f"{use_case} query (port-forward)", "request": request("portForwardUrl")},
            {"name": "List use cases", "request": {"method": "GET", "url": {
                "raw": "{{baseUrl}}/composite/v1/use-cases", "host": ["{{baseUrl}}"],
                "path": ["composite", "v1", "use-cases"]}}},
        ],
    }


# ── cluster discovery ────────────────────────────────────────────────────────

class Topology:
    """Names and settings found in the namespace (no secret values)."""

    def __init__(self, kube: Kube, args):
        self.kube = kube
        self.ns = kube.namespace
        deploys = kube.get_json("deploy")["items"]
        services = {s["metadata"]["name"]: s for s in kube.get_json("svc")["items"]}

        def find_deploy(predicate, what, override=None):
            if override:
                for d in deploys:
                    if d["metadata"]["name"] == override:
                        return d
                raise E2EError(f"deployment {override} not found in {self.ns}")
            hits = [d for d in deploys if predicate(d["metadata"]["name"], container_env(d))]
            if not hits:
                raise E2EError(f"no {what} deployment found in {self.ns}")
            return sorted(hits, key=lambda d: d["metadata"]["name"])[0]

        def service_for(deploy) -> Tuple[str, int]:
            name = deploy["metadata"]["name"]
            labels = deploy["spec"]["template"]["metadata"].get("labels") or {}
            svc = services.get(name) or next(
                (s for s in services.values() if s["spec"].get("selector")
                 and all(labels.get(k) == v for k, v in s["spec"]["selector"].items())), None)
            if not svc:
                raise E2EError(f"no service selects deployment {name}")
            return svc["metadata"]["name"], svc["spec"]["ports"][0]["port"]

        # Composite
        comp = find_deploy(lambda n, e: "AGRI_COMPOSITE_COMPOSITE_PARTNER_ID" in e or "AGRI_COMPOSITE_USE_CASES_DIR" in e,
                           "composite", args.composite_deployment)
        cenv = container_env(comp)
        self.composite_deploy = comp["metadata"]["name"]
        self.composite_release = (comp["metadata"].get("labels") or {}).get("app.kubernetes.io/instance", "composite")
        self.composite_svc, self.composite_port = service_for(comp)
        self.composite_id = env_value(cenv, "AGRI_COMPOSITE_COMPOSITE_PARTNER_ID", "agri-composite")
        volumes = comp["spec"]["template"]["spec"].get("volumes") or []
        cm_vol = next((v for v in volumes if "configMap" in v and "use-case" in v["name"]), None)
        self.use_cases_cm = cm_vol["configMap"]["name"] if cm_vol else None
        sec_vol = next((v for v in volumes if "secret" in v and "sign" in v["name"]), None)
        self.signing_secret = sec_vol["secret"]["secretName"] if sec_vol else "agri-composite-signing"
        p12_key = ((sec_vol or {}).get("secret", {}).get("items") or [{}])[0].get("key", "composite.p12")
        self.signing_keys = {
            "p12": p12_key,
            "password": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_P12_PASSWORD") or ("", "password"))[1],
            "kid": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_KID") or ("", "kid"))[1],
            "algorithm": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_ALGORITHM") or ("", "algorithm"))[1],
        }
        self.pm_partner_svc, self.pm_partner_port = host_port(
            env_value(cenv, "AGRI_COMPOSITE_PARTNER_MGMT_API_URL", "http://commons-services-pm-partner-api"))
        try:
            self.registries = json.loads(env_value(cenv, "AGRI_COMPOSITE_REGISTRIES", "{}"))
        except ValueError:
            self.registries = {}
        self.ingress_host = self._ingress_host() or f"agri-composite.{self.ns}.openg2p.org"

        # Partner Management staff API
        pm = find_deploy(lambda n, e: "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID" in e or "PARTNER_MANAGER_AUTH_ADMIN_ROLE" in e,
                         "Partner Management staff API", args.pm_deployment)
        penv = container_env(pm)
        self.pm_staff_svc, self.pm_staff_port = service_for(pm)
        self.pm_issuers = json_list(env_value(penv, "PARTNER_MANAGER_AUTH_DEFAULT_ISSUERS")
                                    or env_value(penv, "COMMON_AUTH_DEFAULT_ISSUERS"))
        jwks = json_list(env_value(penv, "PARTNER_MANAGER_AUTH_DEFAULT_JWKS_URLS")
                         or env_value(penv, "COMMON_AUTH_DEFAULT_JWKS_URLS"))
        self.pm_client_id = args.pm_client_id or env_value(penv, "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID",
                                                           "commons-services-staff-portal")
        self.pm_auth_client = env_value(penv, "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID", "partner-management")
        self.pm_role = env_value(penv, "PARTNER_MANAGER_AUTH_ADMIN_ROLE", "partner_manager")

        # Consent Manager admin API (the one with auth on, not the partner/validate API)
        cm = find_deploy(lambda n, e: "CONSENT_MANAGER_AUTH_ISSUER" in e and "partner" not in n
                         and env_value(e, "CONSENT_MANAGER_AUTH_ENABLED", "true").lower() == "true",
                         "Consent Manager admin API", args.cm_deployment)
        menv = container_env(cm)
        self.cm_svc, self.cm_port = service_for(cm)
        self.cm_issuer = env_value(menv, "CONSENT_MANAGER_AUTH_ISSUER")
        self.cm_role = env_value(menv, "CONSENT_MANAGER_AUTH_ADMIN_ROLE", "CONSENT_MANAGER_ADMIN")
        self.cm_awe_enabled = env_value(menv, "CONSENT_MANAGER_AWE_ENABLED", "false").lower() == "true"
        self.cm_client_id = args.cm_client_id

        # Keycloak service (for a port-forward when the issuer is not reachable from the laptop)
        kc_url = (jwks[0] if jwks else "") or env_value(menv, "CONSENT_MANAGER_AUTH_JWKS_URL")
        self.keycloak_svc, self.keycloak_port = host_port(kc_url) if kc_url else ("", 80)

        # Postgres
        self.pg_pod = args.postgres_pod
        pod = kube.get_optional("pod", self.pg_pod)
        if pod is None:
            raise E2EError(f"Postgres pod {self.pg_pod} not found (use --postgres-pod)")
        names = [c["name"] for c in pod["spec"]["containers"]]
        self.pg_container = "postgresql" if "postgresql" in names else names[0]

    def _ingress_host(self) -> Optional[str]:
        try:
            items = json.loads(self.kube.run("get", "virtualservices.networking.istio.io", "-o", "json", check=False)
                               or "{}").get("items") or []
        except ValueError:
            return None
        for vs in items:
            for route in vs.get("spec", {}).get("http") or []:
                for dest in route.get("route") or []:
                    if (dest.get("destination") or {}).get("host", "").split(".")[0] == self.composite_svc:
                        hosts = vs["spec"].get("hosts") or []
                        if hosts:
                            return hosts[0]
        return None

    def use_case_text(self) -> str:
        if not self.use_cases_cm:
            raise E2EError("the composite deployment mounts no use-case ConfigMap")
        cm = self.kube.get_json("configmap", self.use_cases_cm)
        text = (cm.get("data") or {}).get(f"{USE_CASE}.yaml")
        if text is None:
            raise E2EError(f"ConfigMap {self.use_cases_cm} has no {USE_CASE}.yaml")
        return text


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def token_claims(token: str) -> dict:
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def client_roles(claims: dict, client: Optional[str] = None) -> set:
    roles = set((claims.get("realm_access") or {}).get("roles") or [])
    for name, entry in (claims.get("resource_access") or {}).items():
        if client is None or name == client:
            roles |= set((entry or {}).get("roles") or [])
    return roles


def _detail(r: httpx.Response) -> str:
    return f"HTTP {r.status_code} {r.request.method} {r.request.url.path} → {(r.text or '')[:400]}"


def check(r: httpx.Response, what: str, ok=(200, 201)) -> httpx.Response:
    if r.status_code in (401, 403):
        raise E2EError(f"{what}: {_detail(r)} — the admin client lacks the role or the token's issuer is not accepted")
    if r.status_code not in ok:
        raise E2EError(f"{what}: {_detail(r)}")
    return r


# ── the run ──────────────────────────────────────────────────────────────────

class Run:
    def __init__(self, args):
        self.a = args
        self.kube = Kube(args.namespace, args.context)
        self.pf = PortForwards(self.kube)
        self.http = httpx.Client(timeout=30, verify=not args.insecure)
        self.wrote = False  # PM/CM/k8s changed in this run (caches need time)
        self.composite_key_source = "local"

    def close(self):
        self.pf.close()
        self.http.close()

    # 1. discovery ------------------------------------------------------------
    def discover(self):
        step(f"1. Discover ({self.a.namespace}, context {self.kube.context or self._current_context()})")
        t = self.t = Topology(self.kube, self.a)
        info(f"composite      deploy/{t.composite_deploy}, svc/{t.composite_svc}:{t.composite_port}, "
             f"release {t.composite_release}, partner ID {t.composite_id} (PM {kit.pm_reference(t.composite_id)})")
        info(f"               ingress https://{t.ingress_host}, use cases ConfigMap {t.use_cases_cm}")
        info(f"               registries: " + ", ".join(f"{k} → {v.get('url')}" for k, v in t.registries.items()))
        info(f"PM             staff API svc/{t.pm_staff_svc}:{t.pm_staff_port}, partner API "
             f"svc/{t.pm_partner_svc}:{t.pm_partner_port}, role {t.pm_role}, issuer {', '.join(t.pm_issuers)}")
        info(f"CM             admin API svc/{t.cm_svc}:{t.cm_port}, role {t.cm_role}, issuer {t.cm_issuer}, "
             f"AWE approval of policies {'ON' if t.cm_awe_enabled else 'off'}")
        info(f"Keycloak       svc/{t.keycloak_svc}:{t.keycloak_port} (fallback when the issuer is unreachable)")
        info(f"Postgres       pod/{t.pg_pod} (container {t.pg_container}), DBs {self.a.fr_db}, {self.a.csr_db}")

        text = t.use_case_text()
        self.allowed = parse_allowed_partners(text)
        self.purpose = self.a.purpose or parse_top_scalar(text, "purpose") or kit.DEFAULT_PURPOSE
        info(f"use case       {USE_CASE}: allowed_partners {self.allowed}, purpose {self.purpose}")
        self.partner_allowed = "*" in self.allowed or self.a.partner in self.allowed

        step("Secrets (names and keys only)")
        self.secret_names = {"pm": self.t.pm_client_id, "cm": self.t.cm_client_id, "signing": t.signing_secret}
        for label, name in (("PM admin client", t.pm_client_id), ("CM admin client", t.cm_client_id),
                            ("composite signing", t.signing_secret)):
            data = self.kube.secret(name)
            if data is None:
                info(f"{label:18} secret/{name}: MISSING")
            else:
                info(f"{label:18} secret/{name}: keys {sorted(data)}")
        if t.cm_awe_enabled:
            warn("CM sends new or wider policies to AWE for approval; a new binding's policy stays pending until approved")

    def _current_context(self) -> str:
        try:
            return subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except Exception:
            return "?"

    # tokens ------------------------------------------------------------------
    def _token(self, issuer: str, client_id: str, label: str) -> str:
        secret = (self.kube.secret(client_id) or {}).get("client_secret")
        if not secret:
            raise E2EError(f"{label}: secret/{client_id} (key client_secret) not found; pass --{label.lower()}-client-id")
        token_path = urlparse(issuer).path.rstrip("/") + "/protocol/openid-connect/token"
        try:
            self.http.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=8).raise_for_status()
            token_url = issuer.rstrip("/") + "/protocol/openid-connect/token"
        except Exception:
            if not self.t.keycloak_svc:
                raise E2EError(f"{label}: {issuer} is not reachable and no Keycloak service is known") from None
            token_url = self.pf.url(self.t.keycloak_svc, self.t.keycloak_port) + token_path
        r = self.http.post(token_url, data={"grant_type": "client_credentials", "client_id": client_id,
                                            "client_secret": secret.decode()})
        if r.status_code != 200:
            raise E2EError(f"{label}: token request for client {client_id} failed: HTTP {r.status_code} "
                           f"{(r.json() if 'json' in r.headers.get('content-type', '') else {}).get('error', '')}")
        token = r.json()["access_token"]
        if token_claims(token).get("iss") != issuer:
            raise E2EError(f"{label}: the token's issuer {token_claims(token).get('iss')} is not {issuer}; "
                           "the service would reject it (Keycloak is reached by a different hostname)")
        return token

    def tokens(self):
        step("2. Admin tokens (Keycloak client credentials)")
        t = self.t
        pm_issuer = t.pm_issuers[0] if t.pm_issuers else t.cm_issuer
        self.pm_token = self._token(pm_issuer, t.pm_client_id, "PM")
        # PM accepts its role as a realm role or a client role of its own admin client.
        pm_ok = t.pm_role in client_roles(token_claims(self.pm_token), t.pm_auth_client)
        info(f"PM  client {t.pm_client_id}: role {t.pm_role} {'present' if pm_ok else 'MISSING'}")
        self.cm_token = self._token(t.cm_issuer, t.cm_client_id, "CM")
        # CM accepts its role from the realm or any client.
        cm_ok = t.cm_role in client_roles(token_claims(self.cm_token))
        info(f"CM  client {t.cm_client_id}: role {t.cm_role} {'present' if cm_ok else 'MISSING'}")
        if not pm_ok or not cm_ok:
            raise E2EError("an admin client lacks its role (see above); grant it in Keycloak's staff realm or pass "
                           "--pm-client-id / --cm-client-id", EXIT_DECISION)

    def _pm_admin(self):
        return self.pf.url(self.t.pm_staff_svc, self.t.pm_staff_port), {"Authorization": f"Bearer {self.pm_token}"}

    def _cm_admin(self):
        return self.pf.url(self.t.cm_svc, self.t.cm_port), {"Authorization": f"Bearer {self.cm_token}"}

    def pm_servable(self, ref: str) -> List[dict]:
        r = self.http.get(f"{self.pf.url(self.t.pm_partner_svc, self.t.pm_partner_port)}/keys/{ref}")
        if r.status_code == 404:
            return []
        return check(r, f"PM GET /keys/{ref}").json().get("keys") or []

    # 3. setup ----------------------------------------------------------------
    def keys(self):
        step(f"3. Keys (state dir {self.a.state_dir})")
        self.partner_key, self.partner_kid, new_p = self.state.partner_key(self.a.partner, self.a.new_keys)
        info(f"partner   {self.a.partner} ({kit.pm_reference(self.a.partner)}): kid {self.partner_kid}"
             + (" (new" + (", not saved: dry run)" if self.a.dry_run else ", saved)") if new_p else ""))
        (self.comp_key, self.comp_p12, self.comp_password, self.comp_kid,
         new_c) = self.state.composite_key(self.t.composite_id, self.a.new_keys)
        info(f"composite {self.t.composite_id} ({kit.pm_reference(self.t.composite_id)}): kid {self.comp_kid}"
             + (" (new" + (", not saved: dry run)" if self.a.dry_run else ", saved)") if new_c else ""))

    def _signing_secret_state(self) -> Optional[dict]:
        data = self.kube.secret(self.t.signing_secret)
        if data is None:
            return None
        k = self.t.signing_keys
        out = {"kid": (data.get(k["kid"]) or b"").decode(), "pub_pem": None, "error": None}
        try:
            key = kit.load_p12_key(data[k["p12"]], (data.get(k["password"]) or b"").decode())
            out["pub_pem"] = kit.public_pem(key)
        except KeyError:
            out["error"] = f"no key {k['p12']}"
        except Exception as e:
            out["error"] = type(e).__name__
        return out

    def _pm_state(self, ref: str, base: str, headers: dict) -> Tuple[Optional[dict], List[dict]]:
        r = self.http.get(f"{base}/partners/{ref}", headers=headers)
        partner = None if r.status_code == 404 else check(r, f"PM GET /partners/{ref}").json()
        r = self.http.get(f"{base}/partners/requests", headers=headers, params={"partner_id": ref, "status": "created"})
        pending = check(r, "PM GET /partners/requests").json().get("requests") or []
        return partner, pending

    def plan(self) -> List[str]:
        """Read PM, CM and the signing Secret (GET only) and work out the writes. Returns their descriptions."""
        step("4. Plan (PM, CM, signing Secret: GETs only)")
        t, a = self.t, self.a
        pm_base, pm_h = self._pm_admin()
        self.pm_plans = {}
        conflicts: List[str] = []

        # Composite key: keep a cluster key PM already serves, else use (and install) the local one.
        comp_ref = kit.pm_reference(t.composite_id)
        comp_servable = self.pm_servable(comp_ref)
        secret_state = self._signing_secret_state()
        self.secret_plan = plan_composite_secret(secret=secret_state, local_pub_pem=kit.public_pem(self.comp_key),
                                                 local_kid=self.comp_kid, pm_servable=comp_servable)
        self.composite_key_source = self.secret_plan["use"]
        info(f"secret/{t.signing_secret}: {self.secret_plan['reason']}")

        owners = [(a.partner, self.partner_key, self.partner_kid)]
        if self.secret_plan["use"] == "local":
            owners.append((t.composite_id, self.comp_key, self.comp_kid))
        for owner, key, kid in owners:
            ref = kit.pm_reference(owner)
            servable = comp_servable if owner == t.composite_id else self.pm_servable(ref)
            partner, pending = self._pm_state(ref, pm_base, pm_h)
            p = plan_pm_partner(ref=ref, kid=kid, pub_pem=kit.public_pem(key), servable=servable,
                                partner=partner, pending=pending)
            self.pm_plans[owner] = (p, key, kid)
            info(f"PM {ref}: {'status ' + partner['status'] if partner else 'absent'}, "
                 f"{len(servable)} servable key(s), {len(pending)} open request(s) → {p['state']}: {p['reason']}")
            if p["state"] == "conflict":
                conflicts.append(p["reason"])

        cm_base, cm_h = self._cm_admin()
        r = self.http.get(f"{cm_base}/consent/v1/partners", headers=cm_h, params={"audience": a.partner})
        bindings = check(r, "CM GET /consent/v1/partners").json()
        policies = {}
        for b in bindings:
            r = self.http.get(f"{cm_base}/consent/v1/partners/{b['id']}/policies", headers=cm_h)
            policies[b["id"]] = check(r, "CM GET policies").json() if r.status_code != 404 else []
        needed = {c: kit.policy_payload(c, self.purpose) for c in kit.GRANTS}
        self.cm_plan = plan_cm(audience=a.partner, pm_ref=kit.pm_reference(a.partner), needed=needed,
                               bindings=bindings, policies=policies)
        for b in bindings:
            active = next((p for p in policies.get(b["id"], []) if p.get("status") == "active"), None)
            info(f"CM {b['audience']} → {b['controller_id']}: binding {b['status']}, "
                 f"active policy {'v' + str(active['version']) if active else 'none'}")
        if not bindings:
            info(f"CM audience {a.partner}: no bindings")
        conflicts += self.cm_plan["conflicts"]

        writes = []
        for owner, (p, _key, kid) in self.pm_plans.items():
            writes += [describe_pm_action(kit.pm_reference(owner), act, kid) for act in p["actions"]]
        writes += [describe_cm_action(a.partner, act) for act in self.cm_plan["actions"]]
        if self.secret_plan["write_secret"]:
            writes.append(f"Kubernetes: {'replace' if secret_state else 'create'} secret/{t.signing_secret} "
                          f"({', '.join(t.signing_keys.values())}; kid {self.comp_kid})")
            writes.append(f"Kubernetes: rollout restart deploy/{t.composite_deploy} and wait for it")
        self.conflicts = conflicts
        return writes

    def confirm(self, writes: List[str]):
        if not writes:
            info("nothing to change")
            return
        print("\n   These writes are needed:", file=sys.stderr)
        for w in writes:
            print(f"     - {w}", file=sys.stderr)
        if self.a.yes:
            return
        if not sys.stdin.isatty():
            raise E2EError("writes need confirmation: rerun with --yes", EXIT_DECISION)
        print("   Proceed? Type 'yes': ", end="", file=sys.stderr, flush=True)  # stdout is for the JSON only
        answer = sys.stdin.readline().strip().lower()
        if answer != "yes":
            raise E2EError("not confirmed; nothing was changed", EXIT_DECISION)

    def apply(self):
        step("5. Apply")
        t, a = self.t, self.a
        pm_base, pm_h = self._pm_admin()
        for owner, (p, key, kid) in self.pm_plans.items():
            ref = kit.pm_reference(owner)
            label = f"TEST {owner}" if owner != t.composite_id else "Agri Stack composite"
            for act in p["actions"]:
                self.wrote = True
                if act["op"] == "enable":
                    check(self.http.post(f"{pm_base}/partners/{ref}/enable", headers=pm_h), f"PM enable {ref}")
                    info(f"PM {ref}: enabled")
                    continue
                if act["op"] == "approve":
                    request_id = act["request_id"]
                elif act["op"] == "onboard":
                    r = self.http.post(f"{pm_base}/partners/requests/onboarding", headers=pm_h,
                                       json=kit.onboarding_payload(owner, label, key, kid))
                    request_id = check(r, f"PM onboard {ref}").json()["id"]
                    info(f"PM {ref}: onboarding request {request_id}")
                else:  # key_update
                    body = {"partner_id": ref, "description": f"{MANAGED_BY}: test key {kid}",
                            "keys": kit.onboarding_payload(owner, label, key, kid)["keys"]}
                    r = self.http.post(f"{pm_base}/partners/requests/key-update", headers=pm_h, json=body)
                    request_id = check(r, f"PM key-update {ref}").json()["id"]
                    info(f"PM {ref}: key-update request {request_id}")
                check(self.http.post(f"{pm_base}/partners/requests/{request_id}/approve", headers=pm_h,
                                     json={"notes": f"approved by {MANAGED_BY}"}), f"PM approve {request_id}")
                info(f"PM {ref}: request {request_id} approved")
            if p["actions"]:
                if not any(k.get("kid") == kid for k in self.pm_servable(ref)):
                    raise E2EError(f"PM still does not serve {ref} kid {kid} after the changes")
                info(f"PM {ref}: key {kid} is served")

        cm_base, cm_h = self._cm_admin()
        binding_ids: Dict[str, str] = {}
        for act in self.cm_plan["actions"]:
            self.wrote = True
            controller = act["controller"]
            if act["op"] == "create_binding":
                r = self.http.post(f"{cm_base}/consent/v1/partners", headers=cm_h,
                                   json=kit.binding_payload(a.partner, controller))
                if r.status_code == 409:
                    found = check(self.http.get(f"{cm_base}/consent/v1/partners", headers=cm_h,
                                                params={"audience": a.partner}), "CM list").json()
                    match = next((b for b in found if b["controller_id"] == controller), None)
                    if not match:
                        raise E2EError(f"CM create binding: {_detail(r)}")
                    binding_ids[controller] = match["id"]
                else:
                    binding_ids[controller] = check(r, "CM create binding").json()["id"]
                info(f"CM {a.partner} → {controller}: binding {binding_ids[controller]}")
            elif act["op"] == "activate_binding":
                check(self.http.patch(f"{cm_base}/consent/v1/partners/{act['binding_id']}", headers=cm_h,
                                      json={"status": "active"}), "CM activate binding")
                info(f"CM {a.partner} → {controller}: binding set active")
            else:
                bid = act.get("binding_id") or binding_ids[controller]
                r = self.http.put(f"{cm_base}/consent/v1/partners/{bid}/policy", headers=cm_h, json=act["body"])
                pol = check(r, "CM put policy").json()
                info(f"CM {a.partner} → {controller}: policy v{pol.get('version')} {pol.get('status')}")
                if pol.get("status") == "pending":
                    raise E2EError(f"CM policy for {a.partner} → {controller} is pending AWE approval "
                                   f"(request {pol.get('awe_request_id')}); approve it in AWE, then rerun",
                                   EXIT_DECISION)

        if self.secret_plan["write_secret"]:
            self.wrote = True
            manifest = signing_secret_manifest(t.signing_secret, t.ns, t.signing_keys, self.comp_p12,
                                               self.comp_password, self.comp_kid)
            self.kube.apply(manifest)
            info(f"secret/{t.signing_secret} applied (kid {self.comp_kid})")
            self.pf.drop(t.composite_svc, t.composite_port)
            self.kube.run("rollout", "restart", f"deploy/{t.composite_deploy}")
            info(f"deploy/{t.composite_deploy}: restarting …")
            self.kube.run("rollout", "status", f"deploy/{t.composite_deploy}", "--timeout=300s", timeout=320)
            info(f"deploy/{t.composite_deploy}: rolled out")

    # farmer ------------------------------------------------------------------
    def farmer(self) -> str:
        step("6. Farmer")
        a, t = self.a, self.t
        fr_rows = self.kube.sql(t.pg_pod, t.pg_container, a.fr_db,
                                f"select f.foundational_id, f.functional_record_id, to_jsonb(f)->>'record_status' "
                                f"from {FR_TABLE} f where f.foundational_id is not null")
        csr_rows = self.kube.sql(t.pg_pod, t.pg_container, a.csr_db,
                                 f"select fayda_fan, farmer_id, crop_year, season, count(*) from {CSR_TABLE} "
                                 f"where fayda_fan is not null group by 1, 2, 3, 4")
        info(f"FR {len(fr_rows)} farmers with a FAN; CSR {len(csr_rows)} (farmer, crop year, season) groups")
        if a.fan:
            in_fr = any(r[0] == a.fan for r in fr_rows)
            seasons = sorted({(r[2], r[3]) for r in csr_rows if r[0] == a.fan}, reverse=True)
            info(f"--fan {mask(a.fan)}: {'in FR' if in_fr else 'NOT in FR'}, CSR seasons {seasons or 'none'}")
            if not in_fr:
                warn("the farmer source will answer no_record")
            return a.fan
        picked = choose_farmer(fr_rows, csr_rows, a.crop_year, a.season)
        if not picked:
            raise E2EError("no FAN is in both the Farmer Registry and the Crop Sown Registry; pass --fan")
        fan, seasons = picked
        info(f"picked FAN {mask(fan)}; CSR seasons " + ", ".join(f"{y} {s} ({n})" for y, s, n in seasons))
        if (a.crop_year or a.season) and not any(
                (a.crop_year is None or y == a.crop_year) and (a.season is None or s == a.season) for y, s, _ in seasons):
            warn("no farmer has the requested crop year/season; the crop sources will answer no_record")
        return fan

    # call --------------------------------------------------------------------
    def envelope(self, fan: str) -> dict:
        subject = {"type": "FAYDA_FAN", "value": fan}
        params = {}
        if self.a.crop_year is not None:
            params["crop_year"] = self.a.crop_year
        if self.a.season:
            params["season"] = self.a.season
        consent = kit.make_consent(self.partner_key, partner=self.a.partner, kid=self.partner_kid, subject=subject,
                                   purpose=self.purpose, valid_days=CONSENT_VALID_DAYS)
        return kit.build_query_envelope(self.partner_key, partner=self.a.partner, kid=self.partner_kid,
                                        composite=self.t.composite_id, subject=subject, parameters=params,
                                        consent_jws=consent)

    def _verify(self, body) -> str:
        if not isinstance(body, dict) or "signature" not in body:
            return "MISSING"
        kid = kit.jws_header(body["signature"]).get("kid")
        keys = self.pm_servable(kit.pm_reference(self.t.composite_id))
        key = next((k for k in keys if k.get("kid") == kid), None)
        if key:
            return kit.verify_response_with_key(body, load_public(key["public_key"])) + f" (PM key {kid})"
        if self.composite_key_source == "local":
            return kit.verify_response_with_key(body, self.comp_key.public_key()) + f" (local key; PM has no kid {kid})"
        return f"not checked (PM has no kid {kid})"

    def call_once(self, fan: str):
        base = self.pf.url(self.t.composite_svc, self.t.composite_port)
        env = self.envelope(fan)
        started = time.monotonic()
        r = self.http.post(base + query_path(), json=env, timeout=60)
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body, elapsed

    def call(self, fan: str) -> int:
        step(f"7. Call {USE_CASE} as {self.a.partner}")
        params = {k: v for k, v in (("crop_year", self.a.crop_year), ("season", self.a.season)) if v is not None}
        info(f"subject FAYDA_FAN {mask(fan)}, parameters {params or '{}'}, consent grants {list(kit.GRANTS)}")
        status, body, elapsed = self.call_once(fan)
        if self.wrote and self._retryable(status, body):
            warn(f"HTTP {status} right after setup; PM/CM caches can hold the old state for up to "
                 f"{self.a.settle}s. Retrying once after {self.a.settle}s …")
            time.sleep(self.a.settle)
            status, body, elapsed = self.call_once(fan)
        sig = self._verify(body)
        text = json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body
        # The answer holds a farmer's personal data: written to a git-ignored folder
        # (owner-only file), and printed only when asked for.
        path = os.path.join(self.a.out_dir, f"{USE_CASE}-{self.a.namespace}-{self.a.run_stamp}.json")
        _write_private(path, (text + "\n").encode())
        info(f"response saved to {path}")
        if self.a.print:
            print(text, flush=True)
        return self.summary(status, body, elapsed, sig)

    @staticmethod
    def _retryable(status, body) -> bool:
        if status == 401:
            return True
        sources = ((body or {}).get("message") or {}).get("sources") if isinstance(body, dict) else None
        return any(s.get("status") in ("denied", "error") for s in (sources or {}).values())

    def summary(self, status, body, elapsed, sig) -> int:
        step("Summary")
        info(f"HTTP {status} in {elapsed} ms (round trip through the port-forward); response signature: {sig}")
        if not isinstance(body, dict):
            info("the composite returned no signed envelope (null body: it could not sign; check "
                 f"secret/{self.t.signing_secret} and the composite logs)" if body in (None, "null") else "non-JSON body")
            return EXIT_CALL_FAILED
        header, msg = body.get("header") or {}, body.get("message") or {}
        info(f"header.status {header.get('status')}"
             + (f", {header.get('status_reason_code')}: {header.get('status_reason_message')}"
                if header.get("status_reason_code") else ""))
        sources = msg.get("sources") or {}
        for sid, s in sources.items():
            info(f"source {sid:17} {s.get('status')}" + (f" — {s['detail']}" if s.get("detail") else ""))
        data = msg.get("data") or {}
        if data:
            land, crops = data.get("land") or {}, data.get("crops") or {}
            info(f"data: {land.get('parcel_count')} parcel(s), land total {land.get('total_size')}, "
                 f"{len(crops.get('seasons') or [])} crop season(s), {len(crops.get('season_summaries') or [])} "
                 f"season summary(ies), area sown {crops.get('total_area_sown_ha')} ha")
        info("(the composite reports no per-source timings; see its logs or the Audit Manager for those)")
        if "INVALID" in sig or sig == "MISSING":
            warn("the response signature does not verify")
            return EXIT_CALL_FAILED
        if status != 200 or header.get("status") != "succ":
            err = msg.get("error") or {}
            warn(f"FAILED: {err.get('code')}: {err.get('message')}" + self._hint(status, err.get("code")))
            return EXIT_CALL_FAILED
        bad = {sid: s for sid, s in sources.items() if s.get("status") not in ("ok", "no_record")}
        if bad:
            warn("FAILED: " + "; ".join(f"{sid} {s.get('status')} ({s.get('detail') or 'no detail'})"
                                        for sid, s in bad.items()))
            return EXIT_CALL_FAILED
        empty = [sid for sid, s in sources.items() if s.get("status") == "no_record"]
        if empty:
            warn(f"no records from {empty}")
        info("OK")
        return EXIT_OK

    def _hint(self, status, code) -> str:
        if status == 401:
            return (f" — is {kit.pm_reference(self.a.partner)} kid {getattr(self, 'partner_kid', '?')} served by PM? "
                    "Run without --call-only to set it up.")
        if code == "partner_not_allowed":
            return " — see allowed_partners above"
        if code and code.startswith("consent_"):
            return " — check the CM bindings/policies (run without --call-only)"
        return ""

    def emit(self, fan: str):
        if not (self.a.emit_curl or self.a.emit_postman):
            return
        step("8. Pre-signed request")
        env = self.envelope(fan)
        expires = (datetime.now(timezone.utc) + timedelta(seconds=SIGNATURE_WINDOW_S)).isoformat(timespec="seconds")
        ingress = f"https://{self.t.ingress_host}"
        fwd_port = 18080
        forward = f"http://127.0.0.1:{fwd_port}"
        pf_cmd = " ".join(self.kube.base() + ["port-forward", f"svc/{self.t.composite_svc}",
                                              f"{fwd_port}:{self.t.composite_port}"])
        info(f"signed now; valid until about {expires} (message_ts and consent issued_at must be within "
             f"{SIGNATURE_WINDOW_S}s of the server's clock)")
        if self.a.emit_curl:
            print(f"\n# Through a port-forward (start it first, in another terminal):\n#   {pf_cmd}\n"
                  f"{curl_command(forward, env)}\n\n# Through the ingress:\n{curl_command(ingress, env)}\n",
                  file=sys.stderr, flush=True)
        if self.a.emit_postman:
            path = os.path.abspath(self.a.emit_postman)
            _write_private(path, json.dumps(postman_collection(env, ingress, forward, expires), indent=2,
                                            ensure_ascii=False).encode())
            info(f"Postman collection written to {path} (port-forward variant needs: {pf_cmd})")

    # dry run extras -----------------------------------------------------------
    def probe_composite(self):
        step("Composite API (GET)")
        base = self.pf.url(self.t.composite_svc, self.t.composite_port)
        r = self.http.get(base + "/ping")
        info(f"GET /ping → HTTP {r.status_code}")
        r = self.http.get(base + f"/composite/v1/use-cases/{USE_CASE}")
        if r.status_code == 200:
            d = r.json()
            info(f"GET /composite/v1/use-cases/{USE_CASE} → {d.get('use_case', USE_CASE)} v{d.get('version')}, "
                 f"sources {[x['id'] for x in d.get('sources', [])]}, grants needed {d.get('consent_grants_needed')}")
        else:
            info(f"GET /composite/v1/use-cases/{USE_CASE} → HTTP {r.status_code}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--namespace", "-n", required=True)
    p.add_argument("--context", help="kubectl context (default: the current one)")
    p.add_argument("--fan", help="FAYDA_FAN of the farmer (default: pick one with crop seasons)")
    p.add_argument("--crop-year", type=int)
    p.add_argument("--season", choices=["SEASON_MEHER", "SEASON_BELG", "SEASON_IRRIGATION"])
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--setup-only", action="store_true", help="set up, do not call")
    mode.add_argument("--call-only", action="store_true", help="call with the saved keys, no setup")
    mode.add_argument("--dry-run", action="store_true", help="discover and plan with GETs and SELECTs only; no writes")
    p.add_argument("--no-call", action="store_true", help="with --emit-*: only emit the pre-signed request")
    p.add_argument("--yes", "-y", action="store_true", help="make the listed writes without asking")
    p.add_argument("--emit-curl", action="store_true", help="print a pre-signed curl (valid ~5 min)")
    p.add_argument("--emit-postman", metavar="FILE", help="write a Postman collection with a pre-signed request")
    p.add_argument("--out-dir", help="where to save the response JSON (default composite/scripts/out/, git-ignored)")
    p.add_argument("--print", action="store_true", help="also print the response JSON to stdout")
    p.add_argument("--partner", required=True,
                   help="REQUIRED. Test partner ID (DCI sender_id); must be in the use case's allowed_partners, "
                        "e.g. bank-a for loan-profile. A TEST key for it is registered in PM")
    p.add_argument("--purpose", help="consent purpose (default: the use case's purpose)")
    p.add_argument("--state-dir", help="keys and state (default ~/.agri-composite-e2e/<namespace>)")
    p.add_argument("--new-keys", action="store_true", help="generate new test keys (new kids) instead of reusing")
    p.add_argument("--postgres-pod", default="commons-postgresql-0")
    p.add_argument("--fr-db", default="fr")
    p.add_argument("--csr-db", default="csr")
    p.add_argument("--composite-deployment", help="default: discovered")
    p.add_argument("--pm-deployment", help="PM staff API deployment (default: discovered)")
    p.add_argument("--cm-deployment", help="CM admin API deployment (default: discovered)")
    p.add_argument("--pm-client-id", help="Keycloak client with PM's admin role; its Secret has the same name "
                                          "(default: PM's PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID)")
    p.add_argument("--cm-client-id", default="consent-manager",
                   help="Keycloak client with CM's admin role; its Secret has the same name")
    p.add_argument("--settle", type=int, default=65,
                   help="seconds to wait before one retry when the first call after setup fails (caches)")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification towards Keycloak")
    a = p.parse_args(argv)
    a.state_dir = a.state_dir or os.path.join("~", ".agri-composite-e2e", a.namespace)
    # Everything the script prints (log, prompts, emitted requests, the JSON with
    # --print) is also written to one log file per run, next to the saved
    # response and with the same timestamp. Git-ignored, owner-only.
    a.out_dir = a.out_dir or OUT_DIR
    a.run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(a.out_dir, exist_ok=True)
    log_path = os.path.join(a.out_dir, f"e2e-{a.namespace}-{a.run_stamp}.log")
    log_file = os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8")
    sys.stdout, sys.stderr = _Tee(sys.stdout, log_file), _Tee(sys.stderr, log_file)
    print(f"log: {log_path}", file=sys.stderr, flush=True)
    if a.no_call and not (a.emit_curl or a.emit_postman):
        p.error("--no-call needs --emit-curl or --emit-postman")

    run = Run(a)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        run.state = State(a.state_dir, a.dry_run)
        run.discover()

        if a.dry_run:
            if not run.partner_allowed:
                warn(allowed_partners_change(a.partner, run.allowed, run.t.composite_release, a.namespace))
            run.tokens()
            run.keys()
            writes = run.plan()
            step("Planned writes (dry run: none made)")
            for w in writes or ["none"]:
                info(f"- {w}")
            for c in run.conflicts:
                warn(f"needs a decision: {c}")
            run.farmer()
            run.probe_composite()
            return EXIT_OK

        if not run.partner_allowed:
            raise E2EError(allowed_partners_change(a.partner, run.allowed, run.t.composite_release, a.namespace),
                           EXIT_DECISION)

        if a.call_only:
            step(f"3. Keys (state dir {a.state_dir})")
            if a.partner not in run.state.data["partners"]:
                raise E2EError(f"no saved key for {a.partner}; run without --call-only first")
            run.partner_key, run.partner_kid, _ = run.state.partner_key(a.partner)
            comp = run.state.data.get("composite") or {}
            if comp.get("id") == run.t.composite_id:
                run.comp_key, *_ = run.state.composite_key(run.t.composite_id)
            else:
                run.composite_key_source = "cluster"
            info(f"partner {a.partner}: kid {run.partner_kid}")
        else:
            run.tokens()
            run.keys()
            writes = run.plan()
            if run.conflicts:
                raise E2EError("needs a decision:\n   - " + "\n   - ".join(run.conflicts), EXIT_DECISION)
            run.confirm(writes)
            if writes:
                run.apply()
            if a.setup_only:
                step("Setup done")
                return EXIT_OK

        fan = run.farmer()
        code = EXIT_OK if a.no_call else run.call(fan)
        run.emit(fan)
        return code
    except E2EError as e:
        print("\nERROR: " + str(e).replace("\n", "\n       "), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        run.close()


if __name__ == "__main__":
    sys.exit(main())
