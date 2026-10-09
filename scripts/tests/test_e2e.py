"""Unit tests for the pure parts of e2e.py and partner_kit.py (no cluster, no network).

  pip install -r scripts/requirements.txt pytest
  pytest scripts/tests
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from cryptography.hazmat.primitives.serialization import pkcs12
from jwt import PyJWS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import e2e  # noqa: E402
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
    jws = kit.make_consent(partner_key, partner="e2e-bank", kid="k1", subject={"type": "FAYDA_FAN", "value": "123"},
                           purpose="credit-assessment", valid_days=1, now=now)
    assert kit.jws_header(jws)["kid"] == "k1" and kit.jws_header(jws)["alg"] == "ES256"
    PyJWS().decode(jws, partner_key.public_key(), algorithms=["ES256"])
    c = _claims(jws)
    assert c["aud"] == "e2e-bank"
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
    env = kit.build_query_envelope(partner_key, partner="e2e-bank", kid="k1", composite="agri-composite",
                                   subject={"type": "FAYDA_FAN", "value": "123"},
                                   parameters={"crop_year": 2018, "season": "SEASON_MEHER"}, consent_jws="a.b.c")
    assert env["header"]["sender_id"] == "e2e-bank" and env["header"]["receiver_id"] == "agri-composite"
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


def test_payloads():
    assert kit.binding_payload("e2e-bank", "farmer-registry")["partner_mgmt_id"] == "PARTNER_E2E_BANK"
    pol = kit.policy_payload("crop-sown-registry", "credit-assessment")
    assert pol["allowed_data_scopes"] == ["crop-sown-registry.farmer_reference", "crop-sown-registry.crop_season",
                                          "crop-sown-registry.measures", "crop-sown-registry.location"]
    # Scope IDs are namespaced by the registry they belong to.
    assert all(s.startswith(f"{c}.") for c, scopes in kit.GRANTS.items() for s in scopes)
    # The crop sources query the Crop Sown Registry by the farmer ID read from the
    # farmer record: the Farmer Registry grant must carry it.
    assert "farmer-registry.farmer_identifiers" in kit.GRANTS["farmer-registry"]
    assert "FAYDA_FAN" in pol["allowed_subject_id_types"] and pol["allowed_signing_algs"] == ["ES256"]


# ── PM plan ──────────────────────────────────────────────────────────────────

def _pm(key, other, **kw):
    args = dict(ref="PARTNER_E2E_BANK", kid="k1", pub_pem=kit.public_pem(key), servable=[], partner=None, pending=[])
    args.update(kw)
    return e2e.plan_pm_partner(**args)


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
    p = e2e.plan_composite_secret(secret=None, local_pub_pem=kit.public_pem(partner_key), local_kid="k",
                                  pm_servable=[])
    assert p == {"use": "local", "write_secret": True, "reason": "the Secret does not exist"}


def test_secret_with_our_key_is_kept(partner_key):
    pem = kit.public_pem(partner_key)
    p = e2e.plan_composite_secret(secret={"pub_pem": pem, "kid": "k"}, local_pub_pem=pem, local_kid="k",
                                  pm_servable=[])
    assert p["use"] == "local" and not p["write_secret"]


def test_secret_with_a_key_pm_serves_is_left_alone(partner_key, other_key):
    other = kit.public_pem(other_key)
    p = e2e.plan_composite_secret(secret={"pub_pem": other, "kid": "theirs"}, local_pub_pem=kit.public_pem(partner_key),
                                  local_kid="k", pm_servable=[{"kid": "theirs", "public_key": other}])
    assert p["use"] == "cluster" and not p["write_secret"]


def test_secret_with_an_unknown_key_is_replaced(partner_key, other_key):
    p = e2e.plan_composite_secret(secret={"pub_pem": kit.public_pem(other_key), "kid": "x"},
                                  local_pub_pem=kit.public_pem(partner_key), local_kid="k", pm_servable=[])
    assert p["use"] == "local" and p["write_secret"]


def test_secret_unreadable_is_replaced(partner_key):
    p = e2e.plan_composite_secret(secret={"pub_pem": None, "kid": "", "error": "ValueError"},
                                  local_pub_pem=kit.public_pem(partner_key), local_kid="k", pm_servable=[])
    assert p["write_secret"] and "ValueError" in p["reason"]


def test_secret_manifest_holds_values_only_as_base64():
    m = e2e.signing_secret_manifest("agri-composite-signing", "trial",
                                    {"p12": "composite.p12", "password": "password", "kid": "kid",
                                     "algorithm": "algorithm"}, b"\x00p12", "s3cret", "kid-1")
    assert m["metadata"]["namespace"] == "trial"
    assert base64.b64decode(m["data"]["password"]) == b"s3cret"
    assert "s3cret" not in json.dumps(m)
    assert set(m["data"]) == {"composite.p12", "password", "kid", "algorithm"}


# ── CM plan ──────────────────────────────────────────────────────────────────

NEEDED = {c: kit.policy_payload(c) for c in kit.GRANTS}


def test_cm_nothing_bound_creates_both_bindings_and_policies():
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed=NEEDED, bindings=[], policies={})
    assert [(a["op"], a["controller"]) for a in p["actions"]] == [
        ("create_binding", "farmer-registry"), ("put_policy", "farmer-registry"),
        ("create_binding", "crop-sown-registry"), ("put_policy", "crop-sown-registry")]
    assert not p["conflicts"]


def _binding(controller, status="active", pm="PARTNER_E2E_BANK"):
    return {"id": f"b-{controller}", "audience": "e2e-bank", "controller_id": controller, "status": status,
            "partner_mgmt_id": pm}


def _policy(controller, **kw):
    pol = dict(kit.policy_payload(controller), status="active", version=1)
    pol.update(kw)
    return pol


def test_cm_everything_in_place_is_ok():
    bindings = [_binding(c) for c in kit.GRANTS]
    policies = {f"b-{c}": [_policy(c)] for c in kit.GRANTS}
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed=NEEDED, bindings=bindings,
                    policies=policies)
    assert p["actions"] == [] and p["conflicts"] == [] and sorted(p["ok"]) == sorted(kit.GRANTS)


def test_cm_narrow_policy_is_widened_without_dropping_existing_scopes():
    bindings = [_binding(c) for c in kit.GRANTS]
    policies = {f"b-{c}": [_policy(c)] for c in kit.GRANTS}
    policies["b-farmer-registry"] = [_policy("farmer-registry", allowed_data_scopes=["farmer-registry.personal_details", "x"])]
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed=NEEDED, bindings=bindings,
                    policies=policies)
    (act,) = p["actions"]
    assert act["op"] == "put_policy" and act["binding_id"] == "b-farmer-registry"
    assert set(act["body"]["allowed_data_scopes"]) == set(kit.GRANTS["farmer-registry"]) | {"x"}


def test_cm_wrong_purpose_needs_a_new_policy():
    bindings = [_binding("farmer-registry")]
    policies = {"b-farmer-registry": [_policy("farmer-registry", allowed_purposes=["other"])]}
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed={"farmer-registry": NEEDED["farmer-registry"]},
                    bindings=bindings, policies=policies)
    assert p["actions"][0]["op"] == "put_policy"
    assert set(p["actions"][0]["body"]["allowed_purposes"]) == {"other", "credit-assessment"}


def test_cm_suspended_binding_is_activated():
    bindings = [_binding("farmer-registry", status="suspended")]
    policies = {"b-farmer-registry": [_policy("farmer-registry")]}
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed={"farmer-registry": NEEDED["farmer-registry"]},
                    bindings=bindings, policies=policies)
    assert [a["op"] for a in p["actions"]] == ["activate_binding"]


def test_cm_pending_policy_and_foreign_pm_id_are_conflicts():
    bindings = [_binding("farmer-registry"), _binding("crop-sown-registry", pm="PARTNER_SOMEONE")]
    policies = {"b-farmer-registry": [{"status": "pending", "version": 1, "awe_request_id": "awe-1"}]}
    p = e2e.plan_cm(audience="e2e-bank", pm_ref="PARTNER_E2E_BANK", needed=NEEDED, bindings=bindings,
                    policies=policies)
    assert p["actions"] == []
    assert any("awe-1" in c for c in p["conflicts"]) and any("PARTNER_SOMEONE" in c for c in p["conflicts"])


def test_policy_validity_cap():
    assert e2e.duration_days("P90D") == 90 and e2e.duration_days("P1Y") == 365 and e2e.duration_days(None) is None
    # Not a PnYnMnWnD duration: treated as no cap (CM is the authority).
    assert e2e.policy_covers(_policy("farmer-registry", max_validity_duration="PT1H"), NEEDED["farmer-registry"],
                             "FAYDA_FAN")
    assert not e2e.policy_covers(_policy("farmer-registry", allowed_subject_id_types=["FARMER_ID"]),
                                 NEEDED["farmer-registry"], "FAYDA_FAN")
    assert e2e.policy_covers(_policy("farmer-registry", max_validity_duration="P1D"), NEEDED["farmer-registry"],
                             "FAYDA_FAN")


# ── use case, farmer, outputs ────────────────────────────────────────────────

def test_parse_allowed_partners_forms():
    assert e2e.parse_allowed_partners("use_case: x\nallowed_partners: [bank-a, 'e2e-bank']  # c\n") == ["bank-a", "e2e-bank"]
    assert e2e.parse_allowed_partners("allowed_partners:\n  - bank-a\n  # c\n  - \"*\"\ninput: {}\n") == ["bank-a", "*"]
    assert e2e.parse_allowed_partners("allowed_partners: []\n") == []
    assert e2e.parse_allowed_partners("use_case: x\n") == []
    assert e2e.parse_top_scalar("policy: p\npurpose: credit-assessment  # x\n", "purpose") == "credit-assessment"


def test_allowed_partners_change_text():
    text = e2e.allowed_partners_change("e2e-bank", ["bank-a"], "composite", "trial")
    assert "-   allowed_partners: [bank-a]" in text and "+   allowed_partners: [bank-a, e2e-bank]" in text


def test_choose_farmer_prefers_id_match_then_requested_season_then_most_seasons():
    fr = [("F1", "R1", "ACTIVE"), ("F2", "R2", "ACTIVE"), ("F3", "R3", "ACTIVE"), ("F4", "R4", "INACTIVE")]
    csr = [("F1", "OTHER", "2019", "SEASON_MEHER", "5"),          # FARMER_ID does not match FR
           ("F2", "R2", "2018", "SEASON_BELG", "1"),
           ("F3", "R3", "2019", "SEASON_MEHER", "1"), ("F3", "R3", "2018", "SEASON_MEHER", "1"),
           ("F4", "R4", "2019", "SEASON_MEHER", "9"),             # not active in FR
           ("F9", "R9", "2019", "SEASON_MEHER", "9")]             # not in FR
    assert e2e.choose_farmer(fr, csr)[0] == "F3"
    assert e2e.choose_farmer(fr, csr, crop_year=2018, season="SEASON_BELG")[0] == "F2"
    fan, seasons = e2e.choose_farmer(fr, csr)
    assert seasons[0] == (2019, "SEASON_MEHER", 1)
    assert e2e.choose_farmer(fr, []) is None


def test_curl_and_postman(partner_key):
    env = kit.build_query_envelope(partner_key, partner="p", kid="k", composite="agri-composite",
                                   subject={"type": "FAYDA_FAN", "value": "1"})
    curl = e2e.curl_command("https://agri-composite.trial.openg2p.org/", env)
    assert "https://agri-composite.trial.openg2p.org/composite/v1/use-cases/loan-profile/query" in curl
    body = curl.split("<<'JSON'\n")[1].split("\nJSON")[0]
    assert json.loads(body) == env
    col = e2e.postman_collection(env, "https://h", "http://127.0.0.1:18080", "2026-10-02T10:05:00+00:00")
    assert col["info"]["schema"].endswith("v2.1.0/collection.json")
    assert json.loads(col["item"][0]["request"]["body"]["raw"]) == env
    assert "expire" in col["item"][0]["request"]["description"]


def test_state_dry_run_writes_nothing(tmp_path):
    s = e2e.State(str(tmp_path / "st"), dry_run=True)
    _key, kid, new = s.partner_key("e2e-bank")
    assert new and kid.startswith("e2e-bank-e2e-")
    s.composite_key("agri-composite")
    assert not (tmp_path / "st").exists()


def test_state_is_private_and_reused(tmp_path):
    d = tmp_path / "st"
    s = e2e.State(str(d), dry_run=False)
    k1, kid1, new1 = s.partner_key("e2e-bank")
    c1 = s.composite_key("agri-composite")
    assert new1 and c1[4]
    assert oct(os.stat(d).st_mode & 0o777) == "0o700"
    for f in ("state.json", "e2e-bank.key.pem", "composite.p12"):
        assert oct(os.stat(d / f).st_mode & 0o777) == "0o600"
    s2 = e2e.State(str(d), dry_run=False)
    k2, kid2, new2 = s2.partner_key("e2e-bank")
    c2 = s2.composite_key("agri-composite")
    assert not new2 and kid2 == kid1 and kit.public_pem(k2) == kit.public_pem(k1)
    assert not c2[4] and c2[3] == c1[3] and kit.public_pem(c2[0]) == kit.public_pem(c1[0])


def test_confirm_requires_yes_without_a_terminal():
    run = e2e.Run.__new__(e2e.Run)
    run.a = mock.Mock(yes=False)
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        with pytest.raises(e2e.E2EError) as exc:
            run.confirm(["PM: onboard X"])
    assert exc.value.code == e2e.EXIT_DECISION
    run.a.yes = True
    run.confirm(["PM: onboard X"])  # no prompt
    run.confirm([])
