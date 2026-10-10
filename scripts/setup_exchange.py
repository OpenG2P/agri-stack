#!/usr/bin/env python3
"""One-time (idempotent) operator setup of an Agri Exchange after install.

Run once after installing the Agri Exchange bundle (and again after adding a registry or a use
case that needs new data scopes). Uses the current kubectl context: reads Deployments, Services
and Secrets, and reaches the services through `kubectl port-forward` (closed on exit).

  python scripts/setup_exchange.py --namespace agrix \\
         --registry farmer-registry=trial --registry crop-sown-registry=dept1 --dry-run
  python scripts/setup_exchange.py --namespace agrix \\
         --registry farmer-registry=trial --registry crop-sown-registry=dept1

--namespace is the exchange (the composite and its commons); each --registry names a registry
(data controller) and the namespace of the department install that serves it.

Steps
  1. Discover   the composite, Partner Management (PM) and Consent Manager (CM) of the exchange
                and of each department namespace.
  2. Tokens     admin tokens for each PM and CM (Keycloak client credentials; the client
                secrets are read from the cluster into memory, never printed).
  3. Use cases  the composite's published use cases: purposes, and the data scopes each one
                may need per registry (consent_scopes, required + optional).
  4. Plan       (GETs only) what is missing:
                  the composite's signing key: Secret agri-composite-signing (generated .p12),
                    kept if the exchange PM already serves its key;
                  that key in the exchange PM and in each department PM (registries check the
                    composite's signature at their own PM);
                  exchange consent mode: in each department CM, a binding and policy for the
                    composite (audience = its partner ID) per registry, with the use cases'
                    scopes and purposes; and the department CM must trust the exchange CM's
                    receipts (checked, not set: it is a Helm value).
  5. Apply      after a typed "yes" (or --yes): the PM requests (raised and approved), the CM
                bindings and policies, the Secret and a restart of the composite.

Partners (e.g. a bank) are not set up here: they onboard through PM and the CM, as
scripts/partner_test.py shows.

Exit codes: 0 ok, 1 error, 2 a decision is needed (receipt trust, AWE approval, a key conflict,
writes not confirmed).
Needs: pip install -r scripts/requirements.txt
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

import partner_kit as kit  # noqa: E402

KitError = kit.KitError


# ── logging (stderr) ─────────────────────────────────────────────────────────

def step(title: str):
    print(f"\n== {title}", file=sys.stderr, flush=True)


def info(msg: str = ""):
    print(f"   {msg}", file=sys.stderr, flush=True)


def warn(msg: str):
    print("   ! " + str(msg).replace("\n", "\n     "), file=sys.stderr, flush=True)


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
            raise KitError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def get_json(self, *args: str) -> dict:
        return json.loads(self.run("get", *args, "-o", "json"))

    def get_optional(self, kind: str, name: str) -> Optional[dict]:
        proc = subprocess.run(self.base() + ["get", kind, name, "-o", "json"], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            if "NotFound" in proc.stderr or "not found" in proc.stderr:
                return None
            raise KitError(f"kubectl get {kind} {name} failed: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout)

    def secret(self, name: str) -> Optional[Dict[str, bytes]]:
        """Secret data, decoded, in memory only. None if the Secret does not exist."""
        obj = self.get_optional("secret", name)
        if obj is None:
            return None
        return {k: base64.b64decode(v) for k, v in (obj.get("data") or {}).items()}

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
                raise KitError(f"port-forward to svc/{service}:{port} exited: {(proc.stderr.read() or '').strip()[:300]}")
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            proc.kill()
            raise KitError(f"port-forward to svc/{service}:{port} did not come up")
        base = f"http://127.0.0.1:{local}"
        self.procs[key] = (proc, base)
        info(f"port-forward {self.kube.namespace}/svc/{service}:{port} → {base}")
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


# ── pure helpers (unit-tested) ───────────────────────────────────────────────

def parse_registries(items: List[str]) -> Dict[str, str]:
    """--registry controller=namespace (repeatable) → {controller: namespace}."""
    out: Dict[str, str] = {}
    for item in items or []:
        controller, sep, ns = item.partition("=")
        if not sep or not controller.strip() or not ns.strip():
            raise KitError(f"--registry must be <controller>=<namespace> (got '{item}')")
        out[controller.strip()] = ns.strip()
    return out


def needed_policies(use_cases: List[dict], controllers: List[str]) -> Dict[str, dict]:
    """The composite's CM policy per registry: what all published use cases may ask that registry for.

    Scopes: each use case's consent_scopes (required + optional) for the registry; a use case
    that reads the registry but declares no scopes for it takes the kit's scopes (GRANTS), if any.
    Purposes: those of the use cases that read the registry.
    """
    scopes: Dict[str, set] = {c: set() for c in controllers}
    purposes: Dict[str, set] = {c: set() for c in controllers}
    for uc in use_cases:
        declared = uc.get("consent_scopes") or {}
        for c in uc.get("consent_grants_needed") or []:
            if c not in scopes:
                continue
            if c in declared:
                scopes[c] |= set(declared[c].get("required") or []) | set(declared[c].get("optional") or [])
            else:
                scopes[c] |= set(kit.GRANTS.get(c) or [])
            purposes[c].add(uc.get("purpose") or kit.DEFAULT_PURPOSE)
    out = {}
    for c in controllers:
        if not scopes[c]:
            continue
        body = kit.policy_payload(c, sorted(purposes[c])[0], sorted(scopes[c]))
        body["allowed_purposes"] = sorted(purposes[c])
        out[c] = body
    return out


def find_deploy(deploys: List[dict], ns: str, predicate, what: str, override: Optional[str] = None) -> dict:
    if override:
        for d in deploys:
            if d["metadata"]["name"] == override:
                return d
        raise KitError(f"deployment {override} not found in {ns}")
    hits = [d for d in deploys if predicate(d["metadata"]["name"], container_env(d))]
    if not hits:
        raise KitError(f"no {what} deployment found in {ns}")
    return sorted(hits, key=lambda d: d["metadata"]["name"])[0]


def service_for(services: Dict[str, dict], deploy: dict) -> Tuple[str, int]:
    name = deploy["metadata"]["name"]
    labels = deploy["spec"]["template"]["metadata"].get("labels") or {}
    svc = services.get(name) or next(
        (s for s in services.values() if s["spec"].get("selector")
         and all(labels.get(k) == v for k, v in s["spec"]["selector"].items())), None)
    if not svc:
        raise KitError(f"no service selects deployment {name}")
    return svc["metadata"]["name"], svc["spec"]["ports"][0]["port"]


def trusted_issuers(value: str) -> List[dict]:
    """CONSENT_MANAGER_TRUSTED_RECEIPT_ISSUERS ([{issuer, jwks_url, presenter}]) as a list."""
    try:
        items = json.loads(value or "[]")
    except ValueError:
        return []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def receipt_trust_gap(*, exchange_issuer: str, presenters: List[str], composite_id: str,
                      dept_trusted: List[dict], dept_ns: str, exchange_ns: str) -> Optional[str]:
    """Why a department CM would refuse the exchange CM's receipts, or None when it accepts them."""
    if not exchange_issuer or composite_id not in presenters:
        return (f"the exchange CM in {exchange_ns} does not issue receipts to {composite_id} (needs "
                f"global.agriStackExchange.issuer and receiptPresenters [{composite_id}] in its CM values)")
    for t in dept_trusted:
        if t.get("issuer") == exchange_issuer:
            if t.get("presenter") and t["presenter"] != composite_id:
                return (f"the CM in {dept_ns} trusts {exchange_issuer} only for presenter {t['presenter']}, "
                        f"not {composite_id}")
            return None
    return (f"the CM in {dept_ns} does not trust receipts from {exchange_issuer} (set "
            f"global.agriStackExchange.trustedIssuer: issuer {exchange_issuer}, jwksUrl = the {exchange_ns} CM "
            f"partner API's /.well-known/jwks.json, presenter {composite_id})")


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
        raise KitError(f"{what}: {_detail(r)} — the admin client lacks the role or the token's issuer is not accepted")
    if r.status_code not in ok:
        raise KitError(f"{what}: {_detail(r)}")
    return r


# ── cluster discovery ────────────────────────────────────────────────────────

class Commons:
    """PM, CM and Keycloak of one commons install (one namespace; no secret values)."""

    def __init__(self, kube: Kube, args, *, pm_partner_url: Optional[str] = None):
        self.kube = kube
        self.ns = kube.namespace
        self.deploys = deploys = kube.get_json("deploy")["items"]
        self.services = services = {s["metadata"]["name"]: s for s in kube.get_json("svc")["items"]}

        # Partner Management staff API, and the partner API (public keys)
        pm = find_deploy(deploys, self.ns,
                         lambda n, e: "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID" in e or "PARTNER_MANAGER_AUTH_ADMIN_ROLE" in e,
                         "Partner Management staff API")
        penv = container_env(pm)
        self.pm_staff_svc, self.pm_staff_port = service_for(services, pm)
        if pm_partner_url:
            self.pm_partner_svc, self.pm_partner_port = host_port(pm_partner_url)
        else:
            pp = find_deploy(deploys, self.ns, lambda n, e: n.endswith("pm-partner-api"), "Partner Management partner API")
            self.pm_partner_svc, self.pm_partner_port = service_for(services, pp)
        self.pm_issuers = json_list(env_value(penv, "PARTNER_MANAGER_AUTH_DEFAULT_ISSUERS")
                                    or env_value(penv, "COMMON_AUTH_DEFAULT_ISSUERS"))
        jwks = json_list(env_value(penv, "PARTNER_MANAGER_AUTH_DEFAULT_JWKS_URLS")
                         or env_value(penv, "COMMON_AUTH_DEFAULT_JWKS_URLS"))
        self.pm_client_id = args.pm_client_id or env_value(
            penv, "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID", "commons-services-staff-portal")
        self.pm_auth_client = env_value(penv, "PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID", "partner-management")
        self.pm_role = env_value(penv, "PARTNER_MANAGER_AUTH_ADMIN_ROLE", "partner_manager")

        # Consent Manager admin API (the one with auth on, not the partner/validate API)
        cm = find_deploy(deploys, self.ns,
                         lambda n, e: "CONSENT_MANAGER_AUTH_ISSUER" in e and "partner" not in n
                         and env_value(e, "CONSENT_MANAGER_AUTH_ENABLED", "true").lower() == "true",
                         "Consent Manager admin API")
        menv = container_env(cm)
        self.cm_svc, self.cm_port = service_for(services, cm)
        self.cm_issuer = env_value(menv, "CONSENT_MANAGER_AUTH_ISSUER")
        self.cm_role = env_value(menv, "CONSENT_MANAGER_AUTH_ADMIN_ROLE", "CONSENT_MANAGER_ADMIN")
        self.cm_awe_enabled = env_value(menv, "CONSENT_MANAGER_AWE_ENABLED", "false").lower() == "true"
        self.cm_client_id = args.cm_client_id
        # Agri Stack exchange settings, read from the CM partner API (it serves /validate)
        cmp = next((d for d in deploys if d["metadata"]["name"].endswith("cm-partner-api")), cm)
        xenv = container_env(cmp)
        self.cm_receipt_issuer = env_value(xenv, "CONSENT_MANAGER_RECEIPT_ISSUER")
        self.cm_receipt_presenters = json_list(env_value(xenv, "CONSENT_MANAGER_RECEIPT_PRESENTERS"))
        self.cm_trusted_issuers = trusted_issuers(env_value(xenv, "CONSENT_MANAGER_TRUSTED_RECEIPT_ISSUERS"))

        # Keycloak service (for a port-forward when the issuer is not reachable from the laptop)
        kc_url = (jwks[0] if jwks else "") or env_value(menv, "CONSENT_MANAGER_AUTH_JWKS_URL")
        self.keycloak_svc, self.keycloak_port = host_port(kc_url) if kc_url else ("", 80)


class Topology(Commons):
    """The composite and the commons of its namespace (the exchange)."""

    def __init__(self, kube: Kube, args):
        deploys = kube.get_json("deploy")["items"]
        services = {s["metadata"]["name"]: s for s in kube.get_json("svc")["items"]}
        comp = find_deploy(deploys, kube.namespace,
                           lambda n, e: "AGRI_COMPOSITE_COMPOSITE_PARTNER_ID" in e or "AGRI_COMPOSITE_USE_CASES_DIR" in e,
                           "composite", args.composite_deployment)
        cenv = container_env(comp)
        self.composite_deploy = comp["metadata"]["name"]
        self.composite_svc, self.composite_port = service_for(services, comp)
        self.composite_id = env_value(cenv, "AGRI_COMPOSITE_COMPOSITE_PARTNER_ID", "agri-composite")
        self.consent_mode = env_value(cenv, "AGRI_COMPOSITE_CONSENT_MODE", "passthrough").lower() or "passthrough"
        volumes = comp["spec"]["template"]["spec"].get("volumes") or []
        sec_vol = next((v for v in volumes if "secret" in v and "sign" in v["name"]), None)
        self.signing_secret = sec_vol["secret"]["secretName"] if sec_vol else "agri-composite-signing"
        p12_key = ((sec_vol or {}).get("secret", {}).get("items") or [{}])[0].get("key", "composite.p12")
        self.signing_keys = {
            "p12": p12_key,
            "password": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_P12_PASSWORD") or ("", "password"))[1],
            "kid": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_KID") or ("", "kid"))[1],
            "algorithm": (env_secret_key(cenv, "AGRI_COMPOSITE_SIGNING_ALGORITHM") or ("", "algorithm"))[1],
        }
        try:
            self.registries = json.loads(env_value(cenv, "AGRI_COMPOSITE_REGISTRIES", "{}"))
        except ValueError:
            self.registries = {}
        super().__init__(kube, args, pm_partner_url=env_value(
            cenv, "AGRI_COMPOSITE_PARTNER_MGMT_API_URL", "http://commons-services-pm-partner-api"))


# ── the composite's key on disk ──────────────────────────────────────────────

class State:
    """The composite's key under ~/.agri-stack-setup/<namespace>/ (0700), reused across runs.

    state.json: {"composite": {"id", "kid", "p12_password"}}; composite.p12 (0600). In --dry-run
    nothing is written: a missing key is generated in memory only.
    """

    def __init__(self, directory: str, dry_run: bool):
        self.dir = os.path.expanduser(directory)
        self.dry_run = dry_run
        self.data = {"composite": {}}
        path = os.path.join(self.dir, "state.json")
        if os.path.exists(path):
            with open(path) as fh:
                self.data.update(json.load(fh))

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
        kid = kit.new_kid(composite_id)
        if not self.dry_run:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            os.chmod(self.dir, 0o700)
            kit._write_private(path, p12)
            self.data["composite"] = {"id": composite_id, "kid": kid, "p12_password": password}
            kit._write_private(os.path.join(self.dir, "state.json"), json.dumps(self.data, indent=2).encode())
        return key, p12, password, kid, True


# ── the run ──────────────────────────────────────────────────────────────────

class Site:
    """One namespace's PM and CM: the exchange's own, or a department's."""

    def __init__(self, c: Commons, kube: Kube, pf: PortForwards, controllers: List[str], own: bool):
        self.c, self.kube, self.pf = c, kube, pf
        self.ns = kube.namespace
        self.controllers = controllers  # registries (data controllers) served from this namespace
        self.own = own  # the exchange namespace
        self.pm_token = self.cm_token = ""


class Setup:
    def __init__(self, args):
        self.a = args
        self.kube = Kube(args.namespace, args.context)
        self.pf = PortForwards(self.kube)
        self.http = httpx.Client(timeout=30, verify=not args.insecure)
        self.sites: List[Site] = []
        self.conflicts: List[str] = []

    def close(self):
        for site in self.sites:
            if site.pf is not self.pf:
                site.pf.close()
        self.pf.close()
        self.http.close()

    # 1. discovery ------------------------------------------------------------
    def discover(self):
        a = self.a
        step(f"1. Discover ({a.namespace}, context {self.kube.context or self._current_context()})")
        t = self.t = Topology(self.kube, a)
        info(f"composite      deploy/{t.composite_deploy}, svc/{t.composite_svc}:{t.composite_port}, "
             f"partner ID {t.composite_id} (PM {kit.pm_reference(t.composite_id)}), consent mode {t.consent_mode}")
        info("               registries: " + (", ".join(f"{k} → {v.get('url')}" for k, v in t.registries.items())
                                              or "none configured"))

        ns_of = parse_registries(a.registry)
        unknown = [c for c in ns_of if t.registries and c not in t.registries]
        if unknown:
            warn(f"the composite has no registry {unknown}; set up anyway")
        for c in t.registries:
            if c not in ns_of:
                warn(f"no --registry {c}=<namespace>: its department install is not set up")
        own = [c for c, ns in ns_of.items() if ns == a.namespace]
        self.main = Site(t, self.kube, self.pf, own, True)
        self.sites = [self.main]
        for ns in sorted({n for n in ns_of.values() if n != a.namespace}):
            kube = Kube(ns, a.context)
            self.sites.append(Site(Commons(kube, a), kube, PortForwards(kube),
                                   [c for c, n in ns_of.items() if n == ns], False))

        for site in self.sites:
            c = site.c
            info(f"--- {site.ns} ({'composite' if site.own else 'department'}"
                 + (f"; registries {', '.join(site.controllers)})" if site.controllers else ")"))
            info(f"PM             staff API svc/{c.pm_staff_svc}:{c.pm_staff_port}, partner API "
                 f"svc/{c.pm_partner_svc}:{c.pm_partner_port}, role {c.pm_role}, issuer {', '.join(c.pm_issuers)}")
            info(f"CM             admin API svc/{c.cm_svc}:{c.cm_port}, role {c.cm_role}, issuer {c.cm_issuer}, "
                 f"AWE approval of policies {'ON' if c.cm_awe_enabled else 'off'}")
            if c.cm_receipt_issuer or c.cm_trusted_issuers:
                info(f"               receipts: issues as {c.cm_receipt_issuer or '-'} to {c.cm_receipt_presenters or '-'}; "
                     f"trusts {[x.get('issuer') for x in c.cm_trusted_issuers] or '-'}")
            for label, name in (("PM admin client", c.pm_client_id), ("CM admin client", c.cm_client_id)):
                data = site.kube.secret(name)
                info(f"{label:16}{site.ns}/secret/{name}: {'MISSING' if data is None else 'keys ' + str(sorted(data))}")

        if t.consent_mode == "exchange":
            # Every department must accept the exchange CM's receipts.
            for site in self.sites:
                if site.controllers and not site.own:
                    gap = receipt_trust_gap(exchange_issuer=t.cm_receipt_issuer, presenters=t.cm_receipt_presenters,
                                            composite_id=t.composite_id, dept_trusted=site.c.cm_trusted_issuers,
                                            dept_ns=site.ns, exchange_ns=t.ns)
                    if gap:
                        self.conflicts.append(gap)
                        warn(gap)
        else:
            warn(f"consent mode {t.consent_mode}: registries check each PARTNER's consent at their own CM, so "
                 "each partner needs a binding there; this script sets up only the composite's key")

    def _current_context(self) -> str:
        try:
            return subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except Exception:
            return "?"

    # 2. tokens ---------------------------------------------------------------
    def _token(self, issuer: str, client_id: str, label: str, site: Site) -> str:
        secret = (site.kube.secret(client_id) or {}).get("client_secret")
        if not secret:
            raise KitError(f"{label}: {site.ns}/secret/{client_id} (key client_secret) not found; "
                           f"pass --{label.lower()}-client-id")
        token_path = urlparse(issuer).path.rstrip("/") + "/protocol/openid-connect/token"
        try:
            self.http.get(issuer.rstrip("/") + "/.well-known/openid-configuration", timeout=8).raise_for_status()
            token_url = issuer.rstrip("/") + "/protocol/openid-connect/token"
        except Exception:
            if not site.c.keycloak_svc:
                raise KitError(f"{label}: {issuer} is not reachable and no Keycloak service is known") from None
            token_url = site.pf.url(site.c.keycloak_svc, site.c.keycloak_port) + token_path
        r = self.http.post(token_url, data={"grant_type": "client_credentials", "client_id": client_id,
                                            "client_secret": secret.decode()})
        if r.status_code != 200:
            raise KitError(f"{label}: token request for client {client_id} failed: HTTP {r.status_code} "
                           f"{(r.json() if 'json' in r.headers.get('content-type', '') else {}).get('error', '')}")
        token = r.json()["access_token"]
        if token_claims(token).get("iss") != issuer:
            raise KitError(f"{label}: the token's issuer {token_claims(token).get('iss')} is not {issuer}; "
                           "the service would reject it (Keycloak is reached by a different hostname)")
        return token

    def tokens(self):
        step("2. Admin tokens (Keycloak client credentials)")
        missing = False
        for site in self.sites:
            c = site.c
            site.pm_token = self._token(c.pm_issuers[0] if c.pm_issuers else c.cm_issuer, c.pm_client_id, "PM", site)
            # PM accepts its role as a realm role or a client role of its own admin client.
            pm_ok = c.pm_role in client_roles(token_claims(site.pm_token), c.pm_auth_client)
            info(f"{site.ns}: PM  client {c.pm_client_id}: role {c.pm_role} {'present' if pm_ok else 'MISSING'}")
            missing = missing or not pm_ok
            if self._cm_needed(site):
                site.cm_token = self._token(c.cm_issuer, c.cm_client_id, "CM", site)
                # CM accepts its role from the realm or any client.
                cm_ok = c.cm_role in client_roles(token_claims(site.cm_token))
                info(f"{site.ns}: CM  client {c.cm_client_id}: role {c.cm_role} {'present' if cm_ok else 'MISSING'}")
                missing = missing or not cm_ok
        if missing:
            raise KitError("an admin client lacks its role (see above); grant it in Keycloak's staff realm or pass "
                           "--pm-client-id / --cm-client-id", kit.EXIT_DECISION)

    def _cm_needed(self, site: Site) -> bool:
        """A namespace that serves registries checks the exchange's receipts for the composite at its CM."""
        return self.t.consent_mode == "exchange" and bool(site.controllers)

    # 3. use cases ------------------------------------------------------------
    def use_cases(self):
        step("3. Use cases (the composite's published use cases)")
        base = self.pf.url(self.t.composite_svc, self.t.composite_port)
        items = check(self.http.get(f"{base}/composite/v1/use-cases"), "composite GET use cases").json()
        self.published = items.get("use_cases") or []
        for uc in self.published:
            info(f"{uc.get('use_case')}: purpose {uc.get('purpose')}, registries {uc.get('consent_grants_needed')}")
        if not self.published:
            warn("no published use cases: no CM policies are planned")
        controllers = [c for s in self.sites if self._cm_needed(s) for c in s.controllers]
        self.policies = needed_policies(self.published, controllers)
        for c in controllers:
            body = self.policies.get(c)
            info(f"policy {c}: " + (f"scopes {body['allowed_data_scopes']}, purposes {body['allowed_purposes']}"
                                    if body else "no use case reads it; nothing planned"))

    # 4. plan -----------------------------------------------------------------
    def _servable(self, ref: str, site: Site) -> List[dict]:
        r = self.http.get(f"{site.pf.url(site.c.pm_partner_svc, site.c.pm_partner_port)}/keys/{ref}")
        if r.status_code == 404:
            return []
        return check(r, f"{site.ns}: PM GET /keys/{ref}").json().get("keys") or []

    def _pm_admin(self, site: Site):
        return site.pf.url(site.c.pm_staff_svc, site.c.pm_staff_port), {"Authorization": f"Bearer {site.pm_token}"}

    def _cm_admin(self, site: Site):
        return site.pf.url(site.c.cm_svc, site.c.cm_port), {"Authorization": f"Bearer {site.cm_token}"}

    def _signing_secret_state(self) -> Optional[dict]:
        data = self.kube.secret(self.t.signing_secret)
        if data is None:
            return None
        k = self.t.signing_keys
        out = {"kid": (data.get(k["kid"]) or b"").decode(), "pub_pem": None, "error": None}
        try:
            out["pub_pem"] = kit.public_pem(kit.load_p12_key(data[k["p12"]], (data.get(k["password"]) or b"").decode()))
        except KeyError:
            out["error"] = f"no key {k['p12']}"
        except Exception as e:
            out["error"] = type(e).__name__
        return out

    def plan(self) -> List[str]:
        """Read PM, CM and the signing Secret (GETs only) and work out the writes. Returns their descriptions."""
        a, t = self.a, self.t
        step(f"4. Plan (GETs only; composite key in {a.state_dir})")
        (self.comp_key, self.comp_p12, self.comp_password, self.comp_kid,
         new) = State(a.state_dir, a.dry_run).composite_key(t.composite_id, a.new_key)
        if new:
            info(f"local composite key: kid {self.comp_kid} (new{', not saved: dry run' if a.dry_run else ', saved'})")

        # The composite's key: keep a cluster key the exchange PM already serves, else install the local one.
        comp_ref = kit.pm_reference(t.composite_id)
        comp_servable = self._servable(comp_ref, self.main)
        secret_state = self._signing_secret_state()
        self.secret_plan = kit.plan_composite_secret(secret=secret_state, local_pub_pem=kit.public_pem(self.comp_key),
                                                     local_kid=self.comp_kid, pm_servable=comp_servable)
        info(f"secret/{t.signing_secret}: {self.secret_plan['reason']}")
        if self.secret_plan["use"] == "local":
            key, kid = self.comp_key, self.comp_kid
        else:
            kid = secret_state.get("kid") or next(
                (k.get("kid") for k in comp_servable if kit.same_public_key(k.get("public_key"), secret_state["pub_pem"])), "")
            key = kit._PublicOnly(kit.load_public(secret_state["pub_pem"]))
        self.key_in_use = (key, kid)

        writes: List[str] = []
        self.site_plans = []
        for site in self.sites:
            pm_base, pm_h = self._pm_admin(site)
            servable = comp_servable if site.own else self._servable(comp_ref, site)
            r = self.http.get(f"{pm_base}/partners/{comp_ref}", headers=pm_h)
            partner = None if r.status_code == 404 else check(r, f"{site.ns}: PM GET /partners/{comp_ref}").json()
            r = self.http.get(f"{pm_base}/partners/requests", headers=pm_h,
                              params={"partner_id": comp_ref, "status": "created"})
            pending = check(r, f"{site.ns}: PM GET /partners/requests").json().get("requests") or []
            pm_plan = kit.plan_pm_partner(ref=comp_ref, kid=kid, pub_pem=kit.public_pem(key), servable=servable,
                                          partner=partner, pending=pending)
            info(f"{site.ns}: PM {comp_ref}: {'status ' + partner['status'] if partner else 'absent'}, "
                 f"{len(servable)} servable key(s), {len(pending)} open request(s) → {pm_plan['state']}: {pm_plan['reason']}")
            if pm_plan["state"] == "conflict":
                self.conflicts.append(f"{site.ns}: {pm_plan['reason']}")

            cm_plan = None
            needed = {c: self.policies[c] for c in site.controllers if c in self.policies}
            if self._cm_needed(site) and needed:
                cm_base, cm_h = self._cm_admin(site)
                r = self.http.get(f"{cm_base}/consent/v1/partners", headers=cm_h, params={"audience": t.composite_id})
                bindings = check(r, f"{site.ns}: CM GET /consent/v1/partners").json()
                policies = {}
                for b in bindings:
                    r = self.http.get(f"{cm_base}/consent/v1/partners/{b['id']}/policies", headers=cm_h)
                    policies[b["id"]] = check(r, "CM GET policies").json() if r.status_code != 404 else []
                    active = next((p for p in policies[b["id"]] if p.get("status") == "active"), None)
                    info(f"{site.ns}: CM {b['audience']} → {b['controller_id']}: binding {b['status']}, "
                         f"active policy {'v' + str(active['version']) if active else 'none'}")
                if not bindings:
                    info(f"{site.ns}: CM audience {t.composite_id}: no bindings")
                cm_plan = kit.plan_cm(audience=t.composite_id, pm_ref=comp_ref, needed=needed,
                                      bindings=bindings, policies=policies)
                self.conflicts += [f"{site.ns}: {c}" for c in cm_plan["conflicts"]]
                if site.c.cm_awe_enabled:
                    warn(f"CM in {site.ns} sends new or wider policies to AWE for approval")

            self.site_plans.append((site, pm_plan, cm_plan))
            prefix = f"[{site.ns}] "
            writes += [prefix + kit.describe_pm_action(comp_ref, act, kid) for act in pm_plan["actions"]]
            if cm_plan:
                writes += [prefix + kit.describe_cm_action(t.composite_id, act) for act in cm_plan["actions"]]

        if self.secret_plan["write_secret"]:
            writes.append(f"[{t.ns}] Kubernetes: {'replace' if secret_state else 'create'} secret/{t.signing_secret} "
                          f"({', '.join(t.signing_keys.values())}; kid {self.comp_kid})")
            writes.append(f"[{t.ns}] Kubernetes: rollout restart deploy/{t.composite_deploy} and wait for it")
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
            raise KitError("writes need confirmation: rerun with --yes", kit.EXIT_DECISION)
        print("   Proceed? Type 'yes': ", end="", file=sys.stderr, flush=True)
        if sys.stdin.readline().strip().lower() != "yes":
            raise KitError("not confirmed; nothing was changed", kit.EXIT_DECISION)

    # 5. apply ----------------------------------------------------------------
    def _apply_pm(self, site: Site, plan: dict):
        t = self.t
        ref = kit.pm_reference(t.composite_id)
        key, kid = self.key_in_use
        pm_base, pm_h = self._pm_admin(site)
        for act in plan["actions"]:
            if act["op"] == "enable":
                check(self.http.post(f"{pm_base}/partners/{ref}/enable", headers=pm_h), f"PM enable {ref}")
                info(f"{site.ns}: PM {ref}: enabled")
                continue
            if act["op"] == "approve":
                request_id = act["request_id"]
            elif act["op"] == "onboard":
                r = self.http.post(f"{pm_base}/partners/requests/onboarding", headers=pm_h,
                                   json=kit.onboarding_payload(t.composite_id, "Agri Stack composite", key, kid))
                request_id = check(r, f"PM onboard {ref}").json()["id"]
                info(f"{site.ns}: PM {ref}: onboarding request {request_id}")
            else:  # key_update
                body = {"partner_id": ref, "description": f"{kit.MANAGED_BY}: composite key {kid}",
                        "keys": kit.onboarding_payload(t.composite_id, "Agri Stack composite", key, kid)["keys"]}
                r = self.http.post(f"{pm_base}/partners/requests/key-update", headers=pm_h, json=body)
                request_id = check(r, f"PM key-update {ref}").json()["id"]
                info(f"{site.ns}: PM {ref}: key-update request {request_id}")
            check(self.http.post(f"{pm_base}/partners/requests/{request_id}/approve", headers=pm_h,
                                 json={"notes": f"approved by {kit.MANAGED_BY}"}), f"PM approve {request_id}")
            info(f"{site.ns}: PM {ref}: request {request_id} approved")
        if plan["actions"]:
            if not any(k.get("kid") == kid for k in self._servable(ref, site)):
                raise KitError(f"{site.ns}: PM still does not serve {ref} kid {kid} after the changes")
            info(f"{site.ns}: PM {ref}: key {kid} is served")

    def _apply_cm(self, site: Site, plan: dict):
        audience = self.t.composite_id
        cm_base, cm_h = self._cm_admin(site)
        binding_ids: Dict[str, str] = {}
        for act in plan["actions"]:
            controller = act["controller"]
            if act["op"] == "create_binding":
                r = self.http.post(f"{cm_base}/consent/v1/partners", headers=cm_h,
                                   json=kit.binding_payload(audience, controller))
                if r.status_code == 409:
                    found = check(self.http.get(f"{cm_base}/consent/v1/partners", headers=cm_h,
                                                params={"audience": audience}), "CM list").json()
                    match = next((b for b in found if b["controller_id"] == controller), None)
                    if not match:
                        raise KitError(f"CM create binding: {_detail(r)}")
                    binding_ids[controller] = match["id"]
                else:
                    binding_ids[controller] = check(r, "CM create binding").json()["id"]
                info(f"{site.ns}: CM {audience} → {controller}: binding {binding_ids[controller]}")
            elif act["op"] == "activate_binding":
                check(self.http.patch(f"{cm_base}/consent/v1/partners/{act['binding_id']}", headers=cm_h,
                                      json={"status": "active"}), "CM activate binding")
                info(f"{site.ns}: CM {audience} → {controller}: binding set active")
            else:
                bid = act.get("binding_id") or binding_ids[controller]
                pol = check(self.http.put(f"{cm_base}/consent/v1/partners/{bid}/policy", headers=cm_h,
                                          json=act["body"]), "CM put policy").json()
                info(f"{site.ns}: CM {audience} → {controller}: policy v{pol.get('version')} {pol.get('status')}")
                if pol.get("status") == "pending":
                    raise KitError(f"{site.ns}: CM policy for {audience} → {controller} is pending AWE approval "
                                   f"(request {pol.get('awe_request_id')}); approve it in AWE, then rerun",
                                   kit.EXIT_DECISION)

    def apply(self):
        step("5. Apply")
        t = self.t
        for site, pm_plan, cm_plan in self.site_plans:
            self._apply_pm(site, pm_plan)
            if cm_plan:
                self._apply_cm(site, cm_plan)
        if self.secret_plan["write_secret"]:
            self.kube.apply(kit.signing_secret_manifest(t.signing_secret, t.ns, t.signing_keys, self.comp_p12,
                                                        self.comp_password, self.comp_kid))
            info(f"secret/{t.signing_secret} applied (kid {self.comp_kid})")
            self.pf.drop(t.composite_svc, t.composite_port)
            self.kube.run("rollout", "restart", f"deploy/{t.composite_deploy}")
            info(f"deploy/{t.composite_deploy}: restarting …")
            self.kube.run("rollout", "status", f"deploy/{t.composite_deploy}", "--timeout=300s", timeout=320)
            info(f"deploy/{t.composite_deploy}: rolled out")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--namespace", "-n", required=True, help="the exchange's namespace (composite and its commons)")
    p.add_argument("--registry", action="append", default=[], metavar="CONTROLLER=NAMESPACE",
                   help="a registry and the namespace of its department install, e.g. farmer-registry=trial "
                        "(repeatable)")
    p.add_argument("--context", help="kubectl context (default: the current one)")
    p.add_argument("--dry-run", action="store_true", help="discover and plan with GETs only; no writes")
    p.add_argument("--yes", "-y", action="store_true", help="make the listed writes without asking")
    p.add_argument("--state-dir", help="the composite's key (default ~/.agri-stack-setup/<namespace>)")
    p.add_argument("--new-key", action="store_true", help="generate a new composite key (new kid) instead of reusing")
    p.add_argument("--composite-deployment", help="default: discovered")
    p.add_argument("--pm-client-id", help="Keycloak client with PM's admin role; its Secret has the same name "
                                          "(default: PM's PARTNER_MANAGER_AUTH_ADMIN_CLIENT_ID)")
    p.add_argument("--cm-client-id", default="consent-manager",
                   help="Keycloak client with CM's admin role; its Secret has the same name")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification towards Keycloak")
    a = p.parse_args(argv)
    a.state_dir = a.state_dir or os.path.join("~", ".agri-stack-setup", a.namespace)

    run = Setup(a)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        run.discover()
        run.tokens()
        run.use_cases()
        writes = run.plan()
        if a.dry_run:
            step("Planned writes (dry run: none made)")
            for w in writes or ["none"]:
                info(f"- {w}")
            for c in run.conflicts:
                warn(f"needs a decision: {c}")
            return kit.EXIT_DECISION if run.conflicts else kit.EXIT_OK
        if run.conflicts:
            raise KitError("needs a decision:\n   - " + "\n   - ".join(run.conflicts), kit.EXIT_DECISION)
        run.confirm(writes)
        if writes:
            run.apply()
        step("Done")
        return kit.EXIT_OK
    except KitError as e:
        print("\nERROR: " + str(e).replace("\n", "\n       "), file=sys.stderr)
        return e.code
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        run.close()


if __name__ == "__main__":
    sys.exit(main())
