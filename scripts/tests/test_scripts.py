"""Unit tests for the pure parts of partner_kit.py and setup_exchange.py (no cluster, no network).

  pip install -r scripts/requirements.txt pytest
  pytest scripts/tests
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from unittest import mock

import pytest
from cryptography.hazmat.primitives.serialization import pkcs12
from jwt import PyJWS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import setup_exchange as setup  # noqa: E402
import partner_kit as kit  # noqa: E402


@pytest.fixture(scope="module")
def partner_key():
    return kit.generate_partner_key()


@pytest.fixture(scope="module")
def other_key():
    return kit.generate_partner_key()


def _claims(jws):
    payload = jws.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


# ── consent and envelope ─────────────────────────────────────────────────────

def test_consent_has_a_grant_per_registry_and_verifies(partner_key):
    now = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)
    jws = kit.make_consent(partner_key, partner="test-bank", kid="k1", subject={"type": "FAYDA_FAN", "value": "123"},
                           purpose="credit-assessment", valid_days=1, now=now)
    assert kit.jws_header(jws)["kid"] == "k1" and kit.jws_header(jws)["alg"] == "ES256"
    PyJWS().decode(jws, partner_key.public_key(), algorithms=["ES256"])
    c = _claims(jws)
    assert c["aud"] == "test-bank"
    assert c["subject_id"] == {"type": "FAYDA_FAN", "value": "123"}
    assert c["purpose"] == {"code": "credit-assessment"}
    assert {g["data_controller"]: g["data_scopes"] for g in c["grants"]} == kit.GRANTS
    assert c["issued_at"] == "2026-10-02T10:00:00+00:00"
    assert c["validity"]["valid_until"] == "2026-10-03T10:00:00+00:00"


def test_consent_can_narrow_controllers(partner_key):
    jws = kit.make_consent(partner_key, partner="p", kid="k", subject={"type": "FAYDA_FAN", "value": "1"},
                           controllers=["farmer-registry"])
    assert [g["data_controller"] for g in _claims(jws)["grants"]] == ["farmer-registry"]


def test_envelope_detached_signature_covers_canonical_header_and_message(partner_key):
    env = kit.build_query_envelope(partner_key, partner="test-bank", kid="k1", composite="agri-composite",
                                   subject={"type": "FAYDA_FAN", "value": "123"},
                                   parameters={"crop_year": 2018, "season": "SEASON_MEHER"}, consent_jws="a.b.c")
    assert env["header"]["sender_id"] == "test-bank" and env["header"]["receiver_id"] == "agri-composite"
    assert env["header"]["message_ts"].endswith("Z")
    assert env["message"]["consent_jws"] == "a.b.c"
    p1, empty, p3 = env["signature"].split(".")
    assert empty == ""
    payload = kit.b64u(kit.canonical({"header": env["header"], "message": env["message"]}))
    PyJWS().decode(f"{p1}.{payload}.{p3}", partner_key.public_key(), algorithms=["ES256"])


def test_verify_response_with_key(partner_key, other_key):
    header, message = {"status": "succ"}, {"data": {"x": 1, "ä": "ü"}}
    body = {"signature": kit.sign_detached({"header": header, "message": message}, partner_key, "k"),
            "header": header, "message": message}
    assert kit.verify_response_with_key(body, partner_key.public_key()) == "valid"
    assert kit.verify_response_with_key(body, other_key.public_key()).startswith("INVALID")
    tampered = dict(body, message={"data": {"x": 2}})
    assert kit.verify_response_with_key(tampered, partner_key.public_key()).startswith("INVALID")
    assert kit.verify_response_with_key({"header": {}, "message": {}}, partner_key.public_key()) == "MISSING"


def test_composite_p12_round_trip():
    key, p12 = kit.generate_composite_p12("agri-composite", "pw")
    assert kit.public_pem(kit.load_p12_key(p12, "pw")) == kit.public_pem(key)
    _k, cert, _ = pkcs12.load_key_and_certificates(p12, b"pw")
    assert "agri-composite" in cert.subject.rfc4514_string()


def test_payloads(partner_key):
    pol = kit.scope_policy_payload("crop-sown-registry", ["credit-assessment"])
    assert pol["data_controller"] == "crop-sown-registry" and pol["use_case"] is None
    assert pol["allowed_data_scopes"] == sorted(["crop-sown-registry.farmer_reference", "crop-sown-registry.crop_season",
                                                 "crop-sown-registry.measures", "crop-sown-registry.location"])
    # Scope IDs are namespaced by the registry they belong to.
    assert all(s.startswith(f"{c}.") for c, scopes in kit.GRANTS.items() for s in scopes)
    # The crop sources query the Crop Sown Registry by the farmer ID read from the
    # farmer record: the Farmer Registry grant must carry it.
    assert "farmer-registry.farmer_identifiers" in kit.GRANTS["farmer-registry"]
    assert "FAYDA_FAN" in pol["allowed_subject_id_types"] and pol["allowed_signing_algs"] == ["ES256"]
    # A use-case policy: one use case (name@major), no data controller, no scopes.
    uc = kit.use_case_policy_payload("loan-profile@1", ["credit-assessment"])
    assert uc["data_controller"] is None and uc["allowed_data_scopes"] == []
    assert uc["use_case"] == "loan-profile@1" and uc["allowed_purposes"] == ["credit-assessment"]
    assert "allowed_use_cases" not in uc
    req = kit.policy_request_payload("test-bank", [], revoke=[uc, pol])
    assert req["revoke"] == [{"use_case": "loan-profile@1"}, {"data_controller": "crop-sown-registry"}]
    assert "revoke" not in kit.policy_request_payload("test-bank", [uc])
    # The onboarding request carries policies only when given; the policy request names the PM partner.
    assert "policies" not in kit.onboarding_payload("test-bank", "T", partner_key, "k1")
    onboard = kit.onboarding_payload("test-bank", "T", partner_key, "k1", [uc])
    assert onboard["partner_id"] == "PARTNER_TEST_BANK" and onboard["policies"] == [uc]
    req = kit.policy_request_payload("test-bank", [pol])
    assert req["partner_id"] == "PARTNER_TEST_BANK" and req["policies"] == [pol]
    assert kit.policy_request_payload("PARTNER_AGRI_COMPOSITE", [pol])["partner_id"] == "PARTNER_AGRI_COMPOSITE"


# ── PM plan ──────────────────────────────────────────────────────────────────

def _pm(key, other, **kw):
    args = dict(ref="PARTNER_TEST_BANK", kid="k1", pub_pem=kit.public_pem(key), servable=[], partner=None, pending=[])
    args.update(kw)
    return kit.plan_pm_partner(**args)


def test_pm_absent_partner_is_onboarded(partner_key, other_key):
    assert _pm(partner_key, other_key)["actions"] == [{"op": "onboard"}]


def test_pm_served_key_is_ok(partner_key, other_key):
    p = _pm(partner_key, other_key, servable=[{"kid": "k1", "public_key": kit.public_pem(partner_key)}],
            partner={"status": "active"})
    assert p["state"] == "ok" and p["actions"] == []


def test_pm_same_kid_other_key_is_conflict(partner_key, other_key):
    p = _pm(partner_key, other_key, servable=[{"kid": "k1", "public_key": kit.public_pem(other_key)}],
            partner={"status": "active"})
    assert p["state"] == "conflict" and "--new-keys" in p["reason"]


def test_pm_active_partner_without_our_key_gets_key_update(partner_key, other_key):
    p = _pm(partner_key, other_key, servable=[{"kid": "old", "public_key": kit.public_pem(other_key)}],
            partner={"status": "active"})
    assert p["actions"] == [{"op": "key_update"}]


def test_pm_disabled_partner_is_enabled_then_key_update(partner_key, other_key):
    assert [a["op"] for a in _pm(partner_key, other_key, partner={"status": "disabled"})["actions"]] == \
        ["enable", "key_update"]


def test_pm_open_request_with_our_kid_is_approved_not_resubmitted(partner_key, other_key):
    pending = [{"id": "r9", "request_type": "onboarding", "proposed_keys": [{"kid": "k1"}]}]
    p = _pm(partner_key, other_key, partner={"status": "created"}, pending=pending)
    assert p["actions"] == [{"op": "approve", "request_id": "r9", "request_type": "onboarding"}]


def test_pm_created_partner_with_foreign_onboarding_is_conflict(partner_key, other_key):
    pending = [{"id": "r1", "request_type": "onboarding", "proposed_keys": [{"kid": "someone-else"}]}]
    p = _pm(partner_key, other_key, partner={"status": "created"}, pending=pending)
    assert p["state"] == "conflict" and "r1" in p["reason"]


# ── composite Secret plan ────────────────────────────────────────────────────

def test_secret_missing_is_written(partner_key):
    p = kit.plan_composite_secret(secret=None, local_pub_pem=kit.public_pem(partner_key), local_kid="k",
                                  pm_servable=[])
    assert p == {"use": "local", "write_secret": True, "reason": "the Secret does not exist"}


def test_secret_with_our_key_is_kept(partner_key):
    pem = kit.public_pem(partner_key)
    p = kit.plan_composite_secret(secret={"pub_pem": pem, "kid": "k"}, local_pub_pem=pem, local_kid="k",
                                  pm_servable=[])
    assert p["use"] == "local" and not p["write_secret"]


def test_secret_with_a_key_pm_serves_is_left_alone(partner_key, other_key):
    other = kit.public_pem(other_key)
    p = kit.plan_composite_secret(secret={"pub_pem": other, "kid": "theirs"}, local_pub_pem=kit.public_pem(partner_key),
                                  local_kid="k", pm_servable=[{"kid": "theirs", "public_key": other}])
    assert p["use"] == "cluster" and not p["write_secret"]


def test_secret_with_an_unknown_key_is_replaced(partner_key, other_key):
    p = kit.plan_composite_secret(secret={"pub_pem": kit.public_pem(other_key), "kid": "x"},
                                  local_pub_pem=kit.public_pem(partner_key), local_kid="k", pm_servable=[])
    assert p["use"] == "local" and p["write_secret"]


def test_secret_unreadable_is_replaced(partner_key):
    p = kit.plan_composite_secret(secret={"pub_pem": None, "kid": "", "error": "ValueError"},
                                  local_pub_pem=kit.public_pem(partner_key), local_kid="k", pm_servable=[])
    assert p["write_secret"] and "ValueError" in p["reason"]


def test_secret_manifest_holds_values_only_as_base64():
    m = kit.signing_secret_manifest("agri-composite-signing", "trial",
                                    {"p12": "composite.p12", "password": "password", "kid": "kid",
                                     "algorithm": "algorithm"}, b"\x00p12", "s3cret", "kid-1")
    assert m["metadata"]["namespace"] == "trial"
    assert base64.b64decode(m["data"]["password"]) == b"s3cret"
    assert "s3cret" not in json.dumps(m)
    assert set(m["data"]) == {"composite.p12", "password", "kid", "algorithm"}


# ── PM policy plan ───────────────────────────────────────────────────────────

NEEDED = {c: kit.scope_policy_payload(c) for c in kit.GRANTS}
USE_CASE = kit.use_case_policy_payload("loan-profile@1")


def _policy(controller, **kw):
    pol = dict(NEEDED[controller] if controller else USE_CASE, status="active", version=1)
    pol.update(kw)
    return pol


def _plan(needed, active=(), pending=()):
    return kit.plan_pm_policies(needed=list(needed), active=list(active), pending=list(pending))


def test_policies_none_active_are_requested():
    p = _plan(NEEDED.values())
    assert [x["data_controller"] for x in p["request"]] == list(kit.GRANTS)
    assert p["approve"] == [] and p["conflicts"] == [] and p["ok"] == []


def test_policies_in_place_are_ok():
    p = _plan(NEEDED.values(), active=[_policy(c) for c in kit.GRANTS])
    assert p["request"] == [] and sorted(p["ok"]) == sorted(kit.GRANTS)


def test_narrow_scope_policy_is_widened_without_dropping_existing_scopes():
    active = [_policy(c) for c in kit.GRANTS]
    active[0] = _policy("farmer-registry", allowed_data_scopes=["farmer-registry.personal_details", "x"])
    (req,) = _plan(NEEDED.values(), active=active)["request"]
    assert req["data_controller"] == "farmer-registry"
    assert set(req["allowed_data_scopes"]) == set(kit.GRANTS["farmer-registry"]) | {"x"}


def test_wrong_purpose_needs_a_wider_policy():
    p = _plan([NEEDED["farmer-registry"]], active=[_policy("farmer-registry", allowed_purposes=["other"])])
    assert set(p["request"][0]["allowed_purposes"]) == {"other", "credit-assessment"}


def test_a_scope_policy_does_not_count_as_a_use_case_policy():
    p = _plan([USE_CASE], active=[_policy("farmer-registry")])
    assert p["request"] == [USE_CASE]


def test_one_policy_per_use_case():
    # Another use case's policy neither covers this one nor is widened: this use case gets its own.
    p = _plan([USE_CASE], active=[_policy(None, use_case="insurance@1")])
    assert p["request"] == [USE_CASE]
    assert _plan([USE_CASE], active=[_policy(None, use_case="insurance@1"), _policy(None)])["ok"] == ["loan-profile@1"]
    # This use case's policy with another purpose is widened (never narrowed).
    (req,) = _plan([USE_CASE], active=[_policy(None, allowed_purposes=["other"])])["request"]
    assert req["use_case"] == "loan-profile@1" and set(req["allowed_purposes"]) == {"other", "credit-assessment"}


def test_open_request_that_covers_is_approved_not_resubmitted():
    pending = [{"id": "r1", "request_type": "policy", "proposed_policies": [NEEDED["farmer-registry"]]}]
    p = _plan([NEEDED["farmer-registry"]], pending=pending)
    assert p["approve"] == ["r1"] and p["request"] == []


def test_open_request_that_does_not_cover_is_a_conflict():
    narrow = dict(NEEDED["farmer-registry"], allowed_data_scopes=["farmer-registry.land"])
    pending = [{"id": "r2", "request_type": "onboarding", "proposed_policies": [narrow]}]
    p = _plan([NEEDED["farmer-registry"]], pending=pending)
    assert p["request"] == [] and p["approve"] == []
    assert len(p["conflicts"]) == 1 and "r2" in p["conflicts"][0]


def test_policy_validity_cap():
    assert kit.duration_days("P90D") == 90 and kit.duration_days("P1Y") == 365 and kit.duration_days(None) is None
    # Not a PnYnMnWnD duration: treated as no cap (CM is the authority).
    assert kit.policy_covers(_policy("farmer-registry", max_validity_duration="PT1H"), NEEDED["farmer-registry"],
                             "FAYDA_FAN")
    assert not kit.policy_covers(_policy("farmer-registry", allowed_subject_id_types=["FARMER_ID"]),
                                 NEEDED["farmer-registry"], "FAYDA_FAN")
    assert kit.policy_covers(_policy("farmer-registry", max_validity_duration="P1D"), NEEDED["farmer-registry"],
                             "FAYDA_FAN")


# ── setup_exchange ───────────────────────────────────────────────────────────


def test_query_envelope_with_a_stored_consent_id():
    key = kit.generate_partner_key()
    env = kit.build_query_envelope(key, partner="bank-a", kid="k1", composite="agri-composite",
                                   subject={"type": "FAYDA_FAN", "value": "123"}, consent_id="c-42")
    assert env["message"]["consent_id"] == "c-42" and "consent_jws" not in env["message"]
    head, _, sig = env["signature"].split(".")
    PyJWS().decode_complete(f"{head}.{kit.b64u(kit.canonical({'header': env['header'], 'message': env['message']}))}.{sig}",
                            key.public_key(), algorithms=["ES256"])


def test_state_dry_run_writes_nothing(tmp_path):
    s = setup.State(str(tmp_path / "st"), dry_run=True)
    _key, _p12, _pw, kid, new = s.composite_key("agri-composite")
    assert new and kid.startswith("agri-composite-test-")
    assert not (tmp_path / "st").exists()


def test_state_is_private_and_reused(tmp_path):
    d = tmp_path / "st"
    c1 = setup.State(str(d), dry_run=False).composite_key("agri-composite")
    assert c1[4]
    assert oct(os.stat(d).st_mode & 0o777) == "0o700"
    for f in ("state.json", "composite.p12"):
        assert oct(os.stat(d / f).st_mode & 0o777) == "0o600"
    c2 = setup.State(str(d), dry_run=False).composite_key("agri-composite")
    assert not c2[4] and c2[3] == c1[3] and kit.public_pem(c2[0]) == kit.public_pem(c1[0])


def test_confirm_requires_yes_without_a_terminal():
    run = setup.Setup.__new__(setup.Setup)
    run.a = mock.Mock(yes=False)
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        with pytest.raises(kit.KitError) as exc:
            run.confirm(["PM: onboard X"])
    assert exc.value.code == kit.EXIT_DECISION
    run.a.yes = True
    run.confirm(["PM: onboard X"])  # no prompt
    run.confirm([])


def test_parse_registries():
    assert setup.parse_registries(["farmer-registry=trial", " crop-sown-registry = dept1 "]) == {
        "farmer-registry": "trial", "crop-sown-registry": "dept1"}
    assert setup.parse_registries([]) == {}
    for bad in ("farmer-registry", "=trial", "farmer-registry="):
        with pytest.raises(kit.KitError):
            setup.parse_registries([bad])


def test_needed_policies_from_published_use_cases():
    use_cases = [
        {"use_case": "loan-profile@1", "purpose": "credit-assessment",
         "consent_grants_needed": ["crop-sown-registry", "farmer-registry"],
         "consent_scopes": {"farmer-registry": {"required": ["farmer-registry.land"],
                                                "optional": ["farmer-registry.main_crops"]}}},
        {"use_case": "insurance@1", "purpose": "insurance", "consent_grants_needed": ["farmer-registry"],
         "consent_scopes": {"farmer-registry": {"required": ["farmer-registry.personal_details"], "optional": []}}},
    ]
    out = setup.needed_policies(use_cases, ["farmer-registry", "crop-sown-registry", "other-registry"])
    # The composite's ceiling: a PM scope policy per registry.
    assert out["farmer-registry"]["data_controller"] == "farmer-registry"
    assert out["farmer-registry"]["use_case"] is None
    assert out["farmer-registry"]["allowed_data_scopes"] == [
        "farmer-registry.land", "farmer-registry.main_crops", "farmer-registry.personal_details"]
    assert out["farmer-registry"]["allowed_purposes"] == ["credit-assessment", "insurance"]
    # Declares no scopes for the Crop Sown Registry: the kit's scopes.
    assert out["crop-sown-registry"]["allowed_data_scopes"] == sorted(kit.GRANTS["crop-sown-registry"])
    # No use case reads it: no policy.
    assert "other-registry" not in out


def test_policy_with_several_purposes_needs_all_of_them():
    needed = setup.needed_policies([
        {"purpose": "a", "consent_grants_needed": ["farmer-registry"]},
        {"purpose": "b", "consent_grants_needed": ["farmer-registry"]}], ["farmer-registry"])["farmer-registry"]
    assert not kit.policy_covers(_policy("farmer-registry", allowed_purposes=["a"]), needed, "FAYDA_FAN")
    assert kit.policy_covers(_policy("farmer-registry", allowed_purposes=["a", "b"]), needed, "FAYDA_FAN")


def test_receipt_trust_gap():
    kw = dict(exchange_issuer="https://cm.agrix", presenters=["agri-composite"], composite_id="agri-composite",
              dept_ns="trial", exchange_ns="agrix")
    assert setup.receipt_trust_gap(dept_trusted=[{"issuer": "https://cm.agrix", "presenter": "agri-composite"}], **kw) is None
    assert "does not trust" in setup.receipt_trust_gap(dept_trusted=[], **kw)
    assert "only for presenter" in setup.receipt_trust_gap(
        dept_trusted=[{"issuer": "https://cm.agrix", "presenter": "someone"}], **kw)
    assert "does not issue consent tokens" in setup.receipt_trust_gap(
        dept_trusted=[], **{**kw, "presenters": []})


# ── partner_test: onboarding and the use-case grant in PM (stubbed PM) ───────

class FakePm:
    """PM staff and partner APIs, enough for partner_test's steps 3 and 4."""

    def __init__(self, partner=None, policies=None):
        self.partner = partner  # None: not onboarded
        self.policies = list(policies or [])  # active
        self.keys = []
        self.requests = {}
        self.posts = []

    def __call__(self, request):
        import httpx

        path, method = request.url.path, request.method
        if path.endswith("/protocol/openid-connect/token"):
            return httpx.Response(200, json={"access_token": "t"})
        body = json.loads(request.content) if request.content else None
        if method == "POST":
            self.posts.append((path, body))
        if path.startswith("/keys/"):
            return httpx.Response(200, json={"keys": self.keys}) if self.keys else httpx.Response(404)
        if path.startswith("/policies/"):
            use_case = request.url.params.get("use_case")
            found = [p for p in self.policies if not p.get("data_controller") and p.get("use_case") == use_case]
            return httpx.Response(200, json={"policies": found}) if found and self.partner else httpx.Response(404)
        if path == "/partners/requests" and method == "GET":
            return httpx.Response(200, json={"requests": []})
        if path in ("/partners/requests/onboarding", "/partners/requests/policy"):
            rid = f"r{len(self.requests) + 1}"
            self.requests[rid] = body
            return httpx.Response(201, json={"id": rid})
        if path.endswith("/approve"):
            req = self.requests[path.split("/")[-2]]
            self.partner = {"status": "active"}
            self.keys += req.get("keys") or []
            for p in req.get("policies") or []:
                self.policies = [x for x in self.policies if kit.policy_key(x) != kit.policy_key(p)]
                self.policies.append({**p, "status": "active", "version": 2})
            return httpx.Response(200, json={})
        if path.endswith("/policies"):
            return httpx.Response(200, json=self.policies) if self.partner else httpx.Response(404)
        if path.startswith("/partners/"):
            return httpx.Response(200, json=self.partner) if self.partner else httpx.Response(404)
        return httpx.Response(404)


def _partner_run(pm, tmp_path, monkeypatch):
    import httpx

    import partner_test

    monkeypatch.setenv("PM_ADMIN_CLIENT_SECRET", "s")
    a = mock.Mock(base_domain="x.test", composite_url=None, pm_partner_url=None, pm_staff_url=None,
                  pm_portal_url=None, issuer=None, insecure=False, partner="test-bank", auto_approve=True,
                  pm_client_id="c", wait=1, poll=0, subject_type="FAYDA_FAN", state_dir=str(tmp_path),
                  new_key=False, use_case="loan-profile")
    run = partner_test.Run(a)
    run.http = httpx.Client(transport=httpx.MockTransport(pm))
    run.use_case_ref, run.purpose = "loan-profile@1", "credit-assessment"
    run.keys()
    return run


def test_partner_test_onboards_with_the_use_case_policy(tmp_path, monkeypatch):
    pm = FakePm()
    run = _partner_run(pm, tmp_path, monkeypatch)
    run.onboard()
    path, body = next(p for p in pm.posts if p[0] == "/partners/requests/onboarding")
    assert body["partner_id"] == "PARTNER_TEST_BANK"
    assert body["policies"] == [kit.use_case_policy_payload("loan-profile@1", ["credit-assessment"])]
    run.access()  # granted by the onboarding: no policy request
    assert not any(p[0] == "/partners/requests/policy" for p in pm.posts)


def test_partner_test_adds_the_use_case_policy_for_an_onboarded_partner(tmp_path, monkeypatch):
    other = {**kit.use_case_policy_payload("insurance@1"), "status": "active", "version": 1}
    pm = FakePm(partner={"status": "active"}, policies=[other])
    run = _partner_run(pm, tmp_path, monkeypatch)
    run.access()
    path, body = next(p for p in pm.posts if p[0] == "/partners/requests/policy")
    assert body["partner_id"] == "PARTNER_TEST_BANK"
    # Only this use case's own policy is added; the other use case's policy is untouched.
    assert body["policies"] == [kit.use_case_policy_payload("loan-profile@1", ["credit-assessment"])]
    assert "revoke" not in body
    assert any(p[0].endswith("/approve") for p in pm.posts)
    assert other in pm.policies


def test_partner_test_retries_only_grant_refusals():
    """A refusal of the use-case grant (possibly a cached PM answer) is retried; other failures are not."""
    import partner_test

    refused = partner_test.Run._grant_refused
    assert refused(403, {"message": {"error": {"code": "partner_not_allowed", "message": "not granted"}}})
    assert refused(403, {"message": {"error": {"code": "consent_denied", "message":
                                               "the exchange Consent Manager denied the consent (use_case_not_granted)"}}})
    assert not refused(403, {"message": {"error": {"code": "consent_denied", "message": "denied (expired)"}}})
    assert not refused(503, {"message": {"error": {"code": "partner_policy_unavailable"}}})
    assert not refused(200, "not json")
