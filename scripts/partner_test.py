#!/usr/bin/env python3
"""Test the Agri Stack composite the way a partner (e.g. a bank) uses it — public APIs only.

No kubectl, no database: everything goes through the services' public URLs, as in a real
deployment. Run it from any machine that can reach the environment's hostnames.

  python scripts/partner_test.py --base-domain agrix.openg2p.org --partner bank-a

The farmer defaults to a sample farmer every demo install has (--fan to pick another).

Steps
  1. Use case    GET the use case from the composite (name@major, purpose, inputs, consent grants needed).
  2. Key         the partner's signing key (EC P-256), kept in --state-dir and reused.
  3. Onboard     the partner in the exchange's Partner Management (PM): its key, and its
                 policy for the use case under test (name@major) in the same onboarding
                 request (a partner has one policy per use case).
                   with PM admin credentials: raise the onboarding (or key-update) request,
                   then wait for a PM admin to approve it in the PM portal; --auto-approve
                   approves it with the same credentials (no manual step);
                   without them: print the partner ID, public key and policy for a PM admin
                   to onboard and approve, then wait.
                 Either way, the script waits until PM's public key API serves the key.
  4. Access      the partner's policy for the use case in PM: an already onboarded partner
                 without it gets a PM policy request adding it (approved as in 3; an existing
                 policy is widened, never narrowed; other use cases' policies are untouched).
                 The script waits until PM's policy API serves it.
  5. Consent     a consent for the farmer with a grant per registry, signed by the partner. The
                 farmer is --fan, or by default the sample farmer FR-0007 (sample person
                 ETH-IND-0007 of the country pack in openg2p-data, with land and crop
                 seasons in a demo install's Farmer and Crop Sown Registries).
                 Instead: --consent-id (a consent the Consent Manager holds) or --consent-lookup
                 (no consent sent; the exchange CM finds the farmer's consent for this partner
                 and use case, e.g. one collected in the partner portal).
  6. Query       the signed request, POSTed to the composite's public URL; the response
                 signature is checked against the composite's key served by PM.

Admin credentials (optional; Keycloak client credentials of the staff realm), from the
environment so they never appear on a command line:
  PM_ADMIN_CLIENT_SECRET   (client --pm-client-id, default commons-services-staff-portal)
With it set and --auto-approve, the whole run needs no manual step.

Exit codes: 0 ok, 1 error, 2 a step was not completed (timed out or refused), 3 the query
failed or a source did not answer ok.
Needs: pip install -r scripts/requirements.txt
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

import partner_kit as kit  # noqa: E402

# Sample person ETH-IND-0007 (openg2p-data packs/ETH/samples/individuals.json): farmer FR-0007 in
# the Farmer Registry's sample data, and crop seasons in the Crop Sown Registry's samples (both
# derive the farmer from the same sample person; the FAN is the person's national ID).
SAMPLE_FAN = "946053125409"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
MANAGED_BY = "agri-partner-test"


def step(title: str):
    print(f"\n== {title}", file=sys.stderr, flush=True)


def info(msg: str = ""):
    print(f"   {msg}", file=sys.stderr, flush=True)


def warn(msg: str):
    print(f"   ! {msg}", file=sys.stderr, flush=True)


def _detail(r: httpx.Response) -> str:
    return f"HTTP {r.status_code} {r.request.method} {r.request.url} → {(r.text or '')[:300]}"


def check(r: httpx.Response, what: str, ok=(200, 201)) -> httpx.Response:
    if r.status_code in (401, 403):
        raise kit.KitError(f"{what}: {_detail(r)} — the admin client lacks the role, or its token is not accepted")
    if r.status_code not in ok:
        raise kit.KitError(f"{what}: {_detail(r)}")
    return r


class Urls:
    """Public URLs of one environment; each defaults from --base-domain and can be overridden."""

    def __init__(self, a):
        bd = a.base_domain

        def pick(value, host):
            return (value or (f"https://{host}.{bd}" if bd else "")).rstrip("/")

        self.composite = pick(a.composite_url, "agri-composite")
        self.pm_partner = pick(a.pm_partner_url, "partner-management-partner-api")
        self.pm_staff = pick(a.pm_staff_url, "pm-staff-portal")
        self.pm_portal = pick(a.pm_portal_url, "partner-management")
        self.issuer = (a.issuer or (f"https://keycloak.{bd}/realms/staff" if bd else "")).rstrip("/")
        missing = [n for n, v in vars(self).items() if not v]
        if missing:
            raise kit.KitError(f"no URL for {', '.join(missing)}: pass --base-domain or the --*-url options")


class Keys:
    """The partner's test key under --state-dir (0700), reused across runs."""

    def __init__(self, directory: str):
        self.dir = os.path.expanduser(directory)
        self.meta_path = os.path.join(self.dir, "state.json")
        try:
            with open(self.meta_path) as fh:
                self.meta = json.load(fh)
        except (OSError, ValueError):
            self.meta = {}

    def partner(self, partner: str, renew: bool):
        path = os.path.join(self.dir, f"{partner}.key.pem")
        kid = (self.meta.get(partner) or {}).get("kid")
        if kid and os.path.exists(path) and not renew:
            with open(path, "rb") as fh:
                return serialization.load_pem_private_key(fh.read(), password=None), kid, False
        key, kid = kit.generate_partner_key(), kit.new_kid(partner)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        kit._write_private(path, kit.private_pem(key))
        self.meta[partner] = {"kid": kid}
        kit._write_private(self.meta_path, json.dumps(self.meta, indent=2).encode())
        return key, kid, True


class Run:
    def __init__(self, a):
        self.a = a
        self.u = Urls(a)
        self.http = httpx.Client(timeout=30, verify=not a.insecure)
        self.ref = kit.pm_reference(a.partner)
        # Set when this run onboarded the partner or changed its policy: the Composite and
        # the exchange CM may still hold the old answer from PM in their caches.
        self.changed = False

    def close(self):
        self.http.close()

    # helpers -----------------------------------------------------------------
    def _token(self, client_id: str, secret: str, label: str) -> str:
        r = self.http.post(f"{self.u.issuer}/protocol/openid-connect/token",
                           data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
        if r.status_code != 200:
            raise kit.KitError(f"{label} admin token for client {client_id}: HTTP {r.status_code} "
                           f"{(r.json() if 'json' in r.headers.get('content-type', '') else {}).get('error', '')}")
        return r.json()["access_token"]

    def served_keys(self, ref: str) -> List[dict]:
        r = self.http.get(f"{self.u.pm_partner}/keys/{ref}")
        if r.status_code == 404:
            return []
        return check(r, f"PM GET /keys/{ref}").json().get("keys") or []

    def _serves(self, kid: str) -> bool:
        pub = kit.public_pem(self.key)
        return any(k.get("kid") == kid and kit.same_public_key(k.get("public_key"), pub) for k in self.served_keys(self.ref))

    def _wait(self, what: str, done) -> None:
        deadline = time.monotonic() + self.a.wait
        info(f"waiting for {what} (up to {self.a.wait}s; Ctrl-C to stop) …")
        while time.monotonic() < deadline:
            if done():
                return
            time.sleep(self.a.poll)
        raise kit.KitError(f"timed out waiting for {what}", kit.EXIT_DECISION)

    # 1. use case -------------------------------------------------------------
    def use_case(self):
        step(f"1. Use case {self.a.use_case} ({self.u.composite})")
        r = check(self.http.get(f"{self.u.composite}/composite/v1/use-cases/{self.a.use_case}"), "GET use case")
        d = r.json()
        self.use_case_ref = d.get("use_case") or self.a.use_case  # name@major: what the partner's policy grants
        self.purpose = self.a.purpose or d.get("purpose") or kit.DEFAULT_PURPOSE
        grants = d.get("consent_grants_needed") or list(kit.GRANTS)
        # The use case says what the consent must (and may) grant per registry; the
        # test grants both. A registry it lists no scopes for: the kit's test scopes.
        declared = d.get("consent_scopes") or {}
        self.scopes = {c: (declared[c]["required"] + declared[c]["optional"]) if c in declared else kit.GRANTS.get(c)
                       for c in grants}
        self.controllers = [c for c, s in self.scopes.items() if s]
        info(f"{d.get('use_case')} — {d.get('title')}; purpose {self.purpose}; "
             f"sources {[s.get('id') for s in d.get('sources', [])]}")
        for c in grants:
            if c in declared:
                info(f"  {c}: required {declared[c]['required']}, optional {declared[c]['optional']}")
            elif self.scopes.get(c):
                info(f"  {c}: the use case names no scopes; the kit's test scopes {self.scopes[c]}")
            else:
                warn(f"  {c}: no scopes known; the consent leaves it out")

    # 2. key ------------------------------------------------------------------
    def keys(self):
        step(f"2. Partner key (state dir {self.a.state_dir})")
        self.key, self.kid, new = Keys(self.a.state_dir).partner(self.a.partner, self.a.new_key)
        info(f"{self.a.partner} ({self.ref}): kid {self.kid}" + (" (new, saved)" if new else ""))

    def needed_policy(self) -> dict:
        """The partner's policy for the use case under test (PM PolicyInput)."""
        # The test signs its own consent unless it uses one the Exchange CM holds
        # (--consent-id, --consent-lookup): only then must the policy accept it.
        signs = not (self.a.consent_id or getattr(self.a, "consent_lookup", False))
        return kit.use_case_policy_payload(self.use_case_ref, [self.purpose], allow_partner_consents=signs)

    def _pm_admin(self):
        """(staff API base, headers) with PM admin credentials, or None without them."""
        secret = os.environ.get("PM_ADMIN_CLIENT_SECRET")
        if not secret:
            return None
        if not getattr(self, "_pm_headers", None):
            self._pm_headers = {"Authorization": f"Bearer {self._token(self.a.pm_client_id, secret, 'PM')}"}
        return self.u.pm_staff, self._pm_headers

    def _pm_state(self, base: str, h: dict):
        r = self.http.get(f"{base}/partners/{self.ref}", headers=h)
        partner = None if r.status_code == 404 else check(r, f"PM GET /partners/{self.ref}").json()
        r = self.http.get(f"{base}/partners/requests", headers=h, params={"partner_id": self.ref, "status": "created"})
        pending = check(r, "PM GET /partners/requests").json().get("requests") or []
        return partner, pending

    def _granted(self) -> bool:
        """PM's policy API serves the partner's policy for this use case, and it covers the test."""
        r = self.http.get(f"{self.u.pm_partner}/policies/{self.ref}", params={"use_case": self.use_case_ref})
        if r.status_code == 404:
            return False
        served = check(r, f"PM GET /policies/{self.ref}").json().get("policies") or []
        return any(kit.policy_covers(p, self.needed_policy(), self.a.subject_type) for p in served)

    # 3. onboard --------------------------------------------------------------
    def onboard(self):
        step(f"3. Onboard {self.ref} in Partner Management")
        if self._serves(self.kid):
            info(f"PM serves kid {self.kid}: nothing to do")
            return
        admin = self._pm_admin()
        if not admin:
            self._onboard_manually()
            return
        base, h = admin
        partner, pending = self._pm_state(base, h)
        plan = kit.plan_pm_partner(ref=self.ref, kid=self.kid, pub_pem=kit.public_pem(self.key),
                               servable=self.served_keys(self.ref), partner=partner, pending=pending)
        info(f"{self.ref}: {'status ' + partner['status'] if partner else 'not in PM'} → {plan['reason']}")
        if plan["state"] == "conflict":
            raise kit.KitError(plan["reason"], kit.EXIT_DECISION)
        label = f"TEST {self.a.partner}"
        request_id = None
        for act in plan["actions"]:
            if act["op"] == "enable":
                check(self.http.post(f"{base}/partners/{self.ref}/enable", headers=h), "PM enable")
                info(f"{self.ref}: enabled")
            elif act["op"] == "approve":
                request_id = act["request_id"]
                info(f"open request {request_id} already carries kid {self.kid}")
            elif act["op"] == "onboard":
                r = self.http.post(f"{base}/partners/requests/onboarding", headers=h,
                                   json=kit.onboarding_payload(self.a.partner, label, self.key, self.kid,
                                                               [self.needed_policy()]))
                request_id = check(r, "PM onboarding request").json()["id"]
                info(f"onboarding request {request_id} raised, with the policy for {self.use_case_ref}")
            else:  # key_update
                body = {"partner_id": self.ref, "description": f"{MANAGED_BY}: key {self.kid}",
                        "keys": kit.onboarding_payload(self.a.partner, label, self.key, self.kid)["keys"]}
                request_id = check(self.http.post(f"{base}/partners/requests/key-update", headers=h, json=body),
                                   "PM key-update request").json()["id"]
                info(f"key-update request {request_id} raised")
        self.changed = True
        if request_id and self.a.auto_approve:
            check(self.http.post(f"{base}/partners/requests/{request_id}/approve", headers=h,
                                 json={"notes": f"approved by {MANAGED_BY} (--auto-approve)"}), "PM approve")
            info(f"request {request_id} approved (--auto-approve)")
        elif request_id:
            info(f"a PM admin must approve request {request_id} in the PM portal: {self.u.pm_portal}")
        self._wait(f"PM to serve {self.ref} kid {self.kid}", lambda: self._serves(self.kid))
        info(f"PM serves kid {self.kid}")

    def _onboard_manually(self):
        path = os.path.join(os.path.expanduser(self.a.state_dir), f"{self.a.partner}.pub.pem")
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w") as fh:
            fh.write(kit.public_pem(self.key))
        info("no PM admin credentials (PM_ADMIN_CLIENT_SECRET): a PM admin onboards the partner in the PM portal")
        info(f"  portal      {self.u.pm_portal}")
        info(f"  partner ID  {self.ref}")
        info(f"  key         kid {self.kid}, algorithm {kit.SIGNING_ALG}, public key in {path}")
        info(f"  policy      {kit.describe_policy(self.needed_policy())}")
        info("  then approve the onboarding (or key-update) request")
        self.changed = True
        self._wait(f"PM to serve {self.ref} kid {self.kid}", lambda: self._serves(self.kid))
        info(f"PM serves kid {self.kid}")

    # 4. access ---------------------------------------------------------------
    def access(self):
        step(f"4. Use-case grant for {self.a.partner} in Partner Management ({self.use_case_ref})")
        if self._granted():
            info(f"PM serves {self.ref}'s policy for {self.use_case_ref}: nothing to do")
            return
        admin = self._pm_admin()
        if not admin:
            info("no PM admin credentials (PM_ADMIN_CLIENT_SECRET): a PM admin raises and approves a policy "
                 f"request for {self.ref} in {self.u.pm_portal}:")
            info(f"  {kit.describe_policy(self.needed_policy())}")
            self.changed = True
            self._wait(f"PM to serve {self.ref}'s policy for {self.use_case_ref}", self._granted)
            info("granted")
            return
        base, h = admin
        partner, pending = self._pm_state(base, h)
        if partner is None:
            raise kit.KitError(f"{self.ref} is not in PM", kit.EXIT_DECISION)
        r = self.http.get(f"{base}/partners/{self.ref}/policies", headers=h)
        data = [] if r.status_code == 404 else check(r, f"PM GET /partners/{self.ref}/policies").json()
        active = data.get("policies") if isinstance(data, dict) else data
        plan = kit.plan_pm_policies(needed=[self.needed_policy()], active=active or [], pending=pending,
                                    subject_type=self.a.subject_type)
        if plan["conflicts"]:
            raise kit.KitError("; ".join(plan["conflicts"]), kit.EXIT_DECISION)
        request_ids = list(plan["approve"])
        self.changed = True
        for rid in request_ids:
            info(f"open request {rid} already carries the policy for {self.use_case_ref}")
        if plan["request"]:
            body = kit.policy_request_payload(self.ref, plan["request"], f"{MANAGED_BY}: grant {self.use_case_ref}")
            rid = check(self.http.post(f"{base}/partners/requests/policy", headers=h, json=body),
                        "PM policy request").json()["id"]
            info(f"policy request {rid} raised: {kit.describe_policy(plan['request'][0])}")
            request_ids.append(rid)
        for rid in request_ids:
            if self.a.auto_approve:
                check(self.http.post(f"{base}/partners/requests/{rid}/approve", headers=h,
                                     json={"notes": f"approved by {MANAGED_BY} (--auto-approve)"}), "PM approve")
                info(f"request {rid} approved (--auto-approve)")
            else:
                info(f"a PM admin must approve request {rid} in the PM portal: {self.u.pm_portal}")
        self._wait(f"PM to serve {self.ref}'s policy for {self.use_case_ref}", self._granted)
        info("granted")

    # 5–6. consent and query --------------------------------------------------
    # Longest time the Composite and the exchange CM may serve a cached PM answer from
    # before this run's change (their policy cache TTL, 60 s by default), plus a margin.
    CACHE_WAIT = 90

    def query(self) -> int:
        step(f"5. Consent and query {self.a.use_case} as {self.a.partner}")
        deadline = time.monotonic() + (self.CACHE_WAIT if self.changed else 0)
        while True:
            status, body = self._send()
            if not (self._grant_refused(status, body) and time.monotonic() < deadline):
                break
            info(f"access refused, as a cached answer from before this run's PM change would be; "
                 f"retrying in {self.a.poll}s (up to {self.CACHE_WAIT}s)")
            time.sleep(self.a.poll)
        if self.a.print:
            print(json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body, flush=True)
        return self.summary(status, body)

    @staticmethod
    def _grant_refused(status, body) -> bool:
        """The Composite or the exchange CM refused the partner's use-case grant."""
        if not isinstance(body, dict):
            return False
        err = (body.get("message") or {}).get("error") or {}
        text = f"{err.get('code')} {err.get('message')}"
        return (status == 403 and err.get("code") == "partner_not_allowed") or any(
            code in text for code in ("use_case_not_granted", "unknown_partner"))

    def _send(self):
        """Build the consent and the signed request, POST it and save the answer."""
        subject = {"type": self.a.subject_type, "value": self.a.fan}
        params = {k: v for k, v in (("crop_year", self.a.crop_year), ("season", self.a.season)) if v is not None}
        env, consent = self._envelope(subject, params)
        info(f"subject {self.a.subject_type} {kit.mask(self.a.fan)}, parameters {params or '{}'}, {consent}")
        url = f"{self.u.composite}/composite/v1/use-cases/{self.a.use_case}/query"
        started = time.monotonic()
        r = self.http.post(url, json=env, timeout=90)
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        text = json.dumps(body, indent=2, ensure_ascii=False) if not isinstance(body, str) else body
        # The answer holds a farmer's personal data: git-ignored folder, owner-only file.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.a.out_dir, f"{self.a.use_case}-{self.a.base_domain or 'env'}-{stamp}.json")
        kit._write_private(path, (text + "\n").encode())
        info(f"POST {url} → HTTP {r.status_code} in {elapsed} ms; response saved to {path}")
        return r.status_code, body

    def _envelope(self, subject, params):
        """(the signed request, how it carries the consent)."""
        build = dict(key=self.key, partner=self.a.partner, kid=self.kid, composite=self.a.composite_id,
                     subject=subject, parameters=params)
        if self.a.consent_lookup:
            # No consent sent: the exchange CM looks up the one it holds for this partner, farmer
            # and use case (e.g. collected in the partner portal and verified by staff).
            return kit.build_query_envelope(**build), "no consent (looked up by the exchange CM)"
        if self.a.consent_id:
            # A consent the farmer gave through the Consent Manager (partner portal, verified by staff):
            # the exchange CM checks it and what it grants; the partner signs only the request.
            return kit.build_query_envelope(**build, consent_id=self.a.consent_id), \
                f"stored consent {self.a.consent_id}"
        consent = kit.make_consent(self.key, partner=self.a.partner, kid=self.kid, subject=subject,
                                   purpose=self.purpose, controllers=self.controllers, valid_days=1,
                                   scopes=self.scopes)
        return kit.build_query_envelope(**build, consent_jws=consent), f"consent grants {self.controllers}"

    def _verify(self, body) -> str:
        if not isinstance(body, dict) or "signature" not in body:
            return "MISSING"
        kid = kit.jws_header(body["signature"]).get("kid")
        key = next((k for k in self.served_keys(kit.pm_reference(self.a.composite_id)) if k.get("kid") == kid), None)
        if not key:
            return f"not checked (PM serves no kid {kid} for {self.a.composite_id})"
        return kit.verify_response_with_key(body, kit.load_public(key["public_key"])) + f" (PM key {kid})"

    def _policy_hint(self):
        info(f"the partner's policy for {self.use_case_ref} in PM ({self.u.pm_portal}) must allow: "
             f"{kit.describe_policy(self.needed_policy())}, signing {kit.SIGNING_ALG}")

    def summary(self, status, body) -> int:
        step("6. Result")
        sig = self._verify(body)
        info(f"response signature: {sig}")
        if not isinstance(body, dict):
            warn("the composite returned no signed envelope")
            return kit.EXIT_CALL_FAILED
        header, msg = body.get("header") or {}, body.get("message") or {}
        info(f"header.status {header.get('status')}"
             + (f", {header.get('status_reason_code')}: {header.get('status_reason_message')}"
                if header.get("status_reason_code") else ""))
        if msg.get("consent_id"):
            info(f"consent used {msg['consent_id']}")
        sources = msg.get("sources") or {}
        for sid, s in sources.items():
            info(f"source {sid:17} {s.get('status')}" + (f" — {s['detail']}" if s.get("detail") else ""))
        if "INVALID" in sig or sig == "MISSING":
            warn("the response signature does not verify")
            return kit.EXIT_CALL_FAILED
        if status != 200 or header.get("status") != "succ":
            err = msg.get("error") or {}
            warn(f"FAILED: {err.get('code')}: {err.get('message')}")
            if str(err.get("code") or "").startswith("consent") or err.get("code") == "partner_not_allowed":
                self._policy_hint()
            return kit.EXIT_CALL_FAILED
        bad = {sid: s for sid, s in sources.items() if s.get("status") not in ("ok", "no_record")}
        if bad:
            warn("FAILED: " + "; ".join(f"{sid} {s.get('status')}" for sid, s in bad.items()))
            if any(s.get("status") == "denied" for s in bad.values()):
                self._policy_hint()
            return kit.EXIT_CALL_FAILED
        info("OK")
        return kit.EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-domain", help="e.g. agrix.openg2p.org: every URL defaults to <service>.<base domain>")
    p.add_argument("--partner", required=True,
                   help="partner ID, e.g. bank-a (its policy for the use case in PM is set up if absent)")
    p.add_argument("--fan", default=SAMPLE_FAN,
                   help=f"the farmer's ID (FAYDA_FAN by default), as the farmer gives it "
                        f"(default {SAMPLE_FAN}: sample farmer FR-0007 of a demo install)")
    consent = p.add_mutually_exclusive_group()
    consent.add_argument("--consent-id",
                         help="query with a consent the farmer gave through the Consent Manager (e.g. collected in "
                              "the partner portal and verified by staff) instead of a consent this script signs; "
                              "the composite must be in exchange consent mode, and --fan must be that consent's "
                              "farmer")
    consent.add_argument("--consent-lookup", action="store_true",
                         help="query with no consent: the exchange CM looks up the one it holds from this partner "
                              "for --fan and the use case (e.g. collected in the partner portal); the composite "
                              "must be in exchange consent mode")
    p.add_argument("--subject-type", default="FAYDA_FAN", choices=kit.SUBJECT_ID_TYPES)
    p.add_argument("--use-case", default="loan-profile")
    p.add_argument("--crop-year", type=int)
    p.add_argument("--season", choices=["SEASON_MEHER", "SEASON_BELG", "SEASON_IRRIGATION"])
    p.add_argument("--purpose", help="consent purpose (default: the use case's purpose)")
    p.add_argument("--composite-id", default="agri-composite", help="the composite's partner ID (receiver_id)")
    p.add_argument("--auto-approve", action="store_true",
                   help="with PM_ADMIN_CLIENT_SECRET: approve the PM requests too (no manual step)")
    p.add_argument("--pm-client-id", default="commons-services-staff-portal", help="PM admin client (staff realm)")
    p.add_argument("--wait", type=int, default=1800, help="seconds to wait for a manual approval (default 1800)")
    p.add_argument("--poll", type=int, default=10, help="seconds between checks while waiting")
    p.add_argument("--new-key", action="store_true", help="generate a new partner key (new kid)")
    p.add_argument("--state-dir", help="partner keys (default ~/.agri-partner-test/<base domain>)")
    p.add_argument("--out-dir", default=OUT_DIR, help="where the response JSON is saved (git-ignored)")
    p.add_argument("--print", action="store_true", help="also print the response JSON to stdout")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification")
    for name in ("composite", "pm-partner", "pm-staff", "pm-portal"):
        p.add_argument(f"--{name}-url", help=f"override the {name} URL")
    p.add_argument("--issuer", help="Keycloak staff realm issuer URL (default https://keycloak.<base domain>/realms/staff)")
    a = p.parse_args(argv)
    if a.auto_approve and not os.environ.get("PM_ADMIN_CLIENT_SECRET"):
        p.error("--auto-approve needs PM_ADMIN_CLIENT_SECRET")
    a.state_dir = a.state_dir or os.path.join("~", ".agri-partner-test", a.base_domain or "default")
    os.makedirs(a.out_dir, exist_ok=True)

    run = None
    try:
        run = Run(a)
        run.use_case()
        run.keys()
        run.onboard()
        run.access()
        return run.query()
    except kit.KitError as e:
        print("\nERROR: " + str(e).replace("\n", "\n       "), file=sys.stderr)
        return e.code
    except httpx.HTTPError as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return kit.EXIT_ERROR
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if run:
            run.close()


if __name__ == "__main__":
    sys.exit(main())
