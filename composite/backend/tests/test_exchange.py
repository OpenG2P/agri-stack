"""Consent exchange mode: the exchange Consent Manager (stubbed) turns the
partner's consent into one consent receipt per registry."""

import json

import httpx
import pytest
from conftest import CSR_URL, FR_URL, SCOPES
from test_engine import Harness

CM_URL = "http://exchange-cm.test"
VALIDATE_URL = f"{CM_URL}/consent/v1/validate"
RECEIPTS = {"farmer-registry": "fr.receipt.jws", "crop-sown-registry": "csr.receipt.jws"}


class ExchangeCm:
    def __init__(self):
        self.calls = []
        self.response = lambda body: httpx.Response(
            200, json={"decision": "permit", "reason_code": "ok", "receipts": dict(RECEIPTS)}
        )

    def __call__(self, request: httpx.Request):
        body = json.loads(request.content)
        self.calls.append((str(request.url), body))
        return self.response(body)


@pytest.fixture
def x(use_cases_dir, composite_p12, pm_transport, registries, partner_key):
    h = Harness(use_cases_dir, composite_p12, pm_transport, registries, partner_key)
    h.cm = ExchangeCm()
    inner = h.client._transport

    async def mux(request: httpx.Request):
        if str(request.url).startswith(CM_URL):
            return h.cm(request)
        return await inner.handle_async_request(request)

    h.client = httpx.AsyncClient(transport=httpx.MockTransport(mux))
    h.engine.settings.consent_mode = "exchange"
    h.engine.settings.exchange_cm_url = CM_URL + "/"
    return h


def authorize(call):
    return call["message"]["search_request"][0]["search_criteria"].get("authorize")


async def test_passthrough_is_default_and_never_calls_cm(x):
    from openg2p_agri_composite.core.engine import EngineSettings

    assert EngineSettings().consent_mode == "passthrough"
    x.engine.settings.consent_mode = "passthrough"  # CM URL still set: ignored
    env = x.envelope()
    status, body = await x.query(env)
    assert status == 200, body
    assert x.cm.calls == []
    assert len(x.registries.calls) == 3
    assert all(authorize(c)["consent_jws"] == env["message"]["consent_jws"] for _u, c in x.registries.calls)


async def test_exchange_sends_each_registry_its_receipt(x):
    env = x.envelope(parameters={"crop_year": 2019})
    status, body = await x.query(env)
    assert status == 200, body
    assert body["message"]["sources"] == {
        "farmer": {"status": "ok"}, "season_summaries": {"status": "ok"}, "crop_seasons": {"status": "ok"}}
    assert len(x.cm.calls) == 1
    url, req = x.cm.calls[0]
    assert url == VALIDATE_URL
    assert req["consent_jws"] == env["message"]["consent_jws"]
    assert req["issue_receipts"] is True
    assert req["partner_id"] == "agri-composite"
    assert req["request_context"]["subject_id"] == env["message"]["subject"]
    # receipts carry only the use case's scopes (required and optional), nothing else the consent grants
    assert req["request_context"]["requested_scopes"] == sorted(s for scopes in SCOPES.values() for s in scopes)
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(FR_URL)] == ["fr.receipt.jws"]
    assert [authorize(c)["consent_jws"] for c in x.registries.calls_to(CSR_URL)] == ["csr.receipt.jws"] * 2


async def test_exchange_never_asks_for_scopes_beyond_the_use_case(x):
    grants = [{"data_controller": c, "data_scopes": list(s) + [f"{c}.extra"]} for c, s in SCOPES.items()]
    status, body = await x.query(x.envelope(consent=x.consent(grants=grants)))
    assert status == 200, body
    requested = x.cm.calls[0][1]["request_context"]["requested_scopes"]
    assert not [s for s in requested if s.endswith(".extra")]


async def test_exchange_partner_checks_still_run_first(x, partner_key):
    from conftest import Key

    status, body = await x.query(x.envelope(consent=x.consent(key=Key("other"))))
    assert status == 403 and body["message"]["error"]["code"] == "consent_signature_invalid"
    assert x.cm.calls == [] and x.registries.calls == []


async def test_exchange_cm_deny(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "deny", "reason_code": "unknown_partner", "detail": "not onboarded"})
    status, body = await x.query(x.envelope())
    assert status == 403
    err = body["message"]["error"]
    assert err["code"] == "consent_denied" and "unknown_partner" in err["message"]
    assert x.registries.calls == []


async def test_exchange_permit_without_receipt_for_mandatory_source(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "permit", "receipts": {"crop-sown-registry": "csr.receipt.jws"}})
    status, body = await x.query(x.envelope())
    assert status == 403
    err = body["message"]["error"]
    assert err["code"] == "consent_receipt_missing" and "farmer-registry" in err["message"]
    assert x.registries.calls == []


async def test_exchange_no_receipts_field_fails_mandatory(x):
    x.cm.response = lambda body: httpx.Response(200, json={"decision": "permit"})
    status, body = await x.query(x.envelope())
    assert status == 403 and body["message"]["error"]["code"] == "consent_receipt_missing"


async def test_exchange_missing_receipt_for_optional_source(x):
    x.cm.response = lambda body: httpx.Response(
        200, json={"decision": "permit", "receipts": {"farmer-registry": "fr.receipt.jws"}})
    status, body = await x.query(x.envelope())
    assert status == 200, body
    srcs = body["message"]["sources"]
    assert srcs["farmer"]["status"] == "ok"
    for sid in ("season_summaries", "crop_seasons"):
        assert srcs[sid]["status"] == "unavailable"
        assert "no consent receipt for crop-sown-registry" in srcs[sid]["detail"]
    assert x.registries.calls_to(CSR_URL) == []


async def test_exchange_ungranted_optional_source_stays_denied(x):
    status, body = await x.query(x.envelope(consent=x.consent(controllers=("farmer-registry",))))
    assert status == 200, body
    assert body["message"]["sources"]["crop_seasons"]["status"] == "denied"


@pytest.mark.parametrize(
    "fail, http, code",
    [
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), 503, "consent_exchange_unavailable"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), 503, "consent_exchange_unavailable"),
        (lambda r: httpx.Response(502), 503, "consent_exchange_unavailable"),
        (lambda r: httpx.Response(404), 502, "consent_exchange_error"),
        (lambda r: httpx.Response(200, content=b"nope"), 502, "consent_exchange_error"),
    ],
)
async def test_exchange_cm_unreachable_or_broken(x, fail, http, code):
    x.cm.response = fail
    status, body = await x.query(x.envelope())
    assert status == http and body["message"]["error"]["code"] == code
    assert x.registries.calls == []


async def test_exchange_without_cm_url_fails_closed(x):
    x.engine.settings.exchange_cm_url = ""
    status, body = await x.query(x.envelope())
    assert status == 503 and body["message"]["error"]["code"] == "consent_exchange_unavailable"
    assert x.registries.calls == []


def test_settings_default_passthrough_and_reject_unknown_mode(monkeypatch):
    from openg2p_agri_composite.config import Settings
    from pydantic import ValidationError

    assert Settings().consent_mode == "passthrough" and Settings().consent_exchange_cm_url == ""
    monkeypatch.setenv("AGRI_COMPOSITE_CONSENT_MODE", "exchange")
    monkeypatch.setenv("AGRI_COMPOSITE_CONSENT_EXCHANGE_CM_URL", CM_URL)
    s = Settings()
    assert s.consent_mode == "exchange" and s.consent_exchange_cm_url == CM_URL
    with pytest.raises(ValidationError):
        Settings(consent_mode="bogus")
