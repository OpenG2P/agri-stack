<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Composite: API, configuration and testing

How to call, configure, run and test the [use-case composite](composite.md). The code, Helm chart and scripts are in the agri-stack repo under `composite/`. The design and what is built are on the [composite page](composite.md#as-built).

## API

| Method | Path | |
|---|---|---|
| `GET` | `/composite/v1/use-cases` | Published use cases: input, parameters, sources, output fields, the consent grants needed |
| `GET` | `/composite/v1/use-cases/{use_case}` | One use case (`name` or `name@major`) |
| `POST` | `/composite/v1/use-cases/{use_case}/query` | Run a query. The major version can also be sent in the `X-Use-Case-Major` header. |
| `GET` | `/ping` | Health check |

Request (a detached JWS over `{header, message}`, as in DCI; `sender_id` is the partner's ID):

```json
{"signature": "<header>..<signature>",
 "header": {"version": "1.0.0", "message_id": "…", "message_ts": "2026-10-01T10:00:00Z",
            "action": "query", "sender_id": "bank-a", "receiver_id": "agri-composite"},
 "message": {"subject": {"type": "FAYDA_FAN", "value": "123456789012"},
             "parameters": {"crop_year": 2019, "season": "SEASON_MEHER"},
             "consent_jws": "<partner-signed consent with a grant per registry>"}}
```

Response (signed by the composite, `header.status` `succ`/`rjct`). The body is
`{use_case, version, request_id, subject, sources: {id: {status, detail?}}, data}`. On failure,
`data` is replaced by `error: {code, message}`, and the HTTP status is one of:

| Status | Meaning |
|---|---|
| 400 | Invalid envelope, input or stale timestamp |
| 401 | Bad signature or unknown partner |
| 403 | Partner not allowed, or a consent problem (`consent_*`), or a mandatory source denied |
| 404 | Unknown use case |
| 429 | Rate limit reached |
| 502 / 503 / 504 | A mandatory source failed (error / unavailable / timed out) |

## Use-case files

One YAML file per use case, validated strictly when loaded (unknown keys are errors). Pods check the
directory every 30 s and reload changed files without a restart. A file that fails validation is
logged and skipped; if an earlier version of it loaded, that version keeps serving. Only
`status: published` is served. See [`composite/use-cases/loan-profile.yaml`](../composite/use-cases/loan-profile.yaml).

| Key | |
|---|---|
| `use_case`, `version` (semver), `status`, `title`, `description` | Partners call `use_case@<major>`; without a major, they get the highest published major |
| `policy`, `purpose` | Informational until PM has policies |
| `consent.required` | Requires `message.consent_jws` and checks it (see above) |
| `allowed_partners` | Partner IDs allowed to call (`"*"` = any verified partner). This **stands in for the PM policy association**, which PM does not have yet. |
| `input.subject.id_types`, `input.parameters.<name>` | Parameters have `{type: integer\|number\|string\|boolean, required, default, min, max, enum}`. `batch.max_subjects` must be 1. |
| `sources[]` | `{id, controller, requirement: mandatory\|optional, depends_on: [], dci: {reg_type, reg_record_type, query_template}, timeout_ms, retries}` |
| `response.mapping` | `out.path: <JSONPath>` over `{subject, parameters, sources: {id: {status, records: [...]}}}`. Wildcards and filters give lists; other paths give one value. |
| `response.derived` | `out.path: <expr>` using `sum`, `count`, `min`, `max`, `first`, `round` over JSONPaths and numbers (no `eval`). The mapped output is at `$.data`. |
| `response.source_status` | Include `sources` in the response (default `true`) |
| `execution.overall_timeout_ms`, `execution.partial_response` | With `allowed`, only a failing mandatory source fails the request. With `denied`, any failing source does. |
| `limits.rate_per_partner` | e.g. `60/min`, as a token bucket per pod and worker. Global limits belong to the gateway. |
| `audit.events` | Which audit events to send (default: all three) |

Also accepted from the design but not acted on yet: `owner`, `consent.collection`, `consent.mode`,
`sources[].request_scopes`, `response.schema`, `response.correlate_on`, `response.mode: merged`,
`execution.fan_out: parallel`, and `limits.daily_quota_per_partner`.

**Query templates.** A template is sandboxed Jinja, given inline or as the name of a `.j2` file next
to the use case. It renders the query part of the DCI `search_criteria`, as
`{"query_type", "query": {"type", "value"}, "pagination"?, "sort"?}`. The composite adds `reg_type`,
`reg_record_type` and the consent. The context is `subject`, `parameters`, `sources` (the results of
`depends_on`), `request_id`, `today` and `use_case`. Use `| tojson` for values. Keep a space between
consecutive braces (`} }`) so literal JSON doesn't read as `{{ … }}`. Templates are rendered with
sample input when they're loaded, and must produce valid queries.

A source whose dependency isn't `ok` is not called. It takes the dependency's status, with
`detail: "not called: …"`.

## Run locally

```bash
cd composite/backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install "git+https://github.com/openg2p/openg2p-fastapi-common@develop#subdirectory=openg2p-fastapi-common"
pip install -e ".[test]"
pytest                                     # no network needed

python ../scripts/partner_kit.py keys      # test keys in scripts/kit-out (git-ignored)
export AGRI_COMPOSITE_USE_CASES_DIR=../use-cases
export AGRI_COMPOSITE_SIGNING_P12_PATH=../scripts/kit-out/composite.p12 AGRI_COMPOSITE_SIGNING_P12_PASSWORD=openg2p-test
export AGRI_COMPOSITE_PARTNER_MGMT_API_URL=http://localhost:9001          # a PM partner-api
export AGRI_COMPOSITE_REGISTRIES='{"farmer-registry":{"url":"http://localhost:9002/dci/registry/sync/search"},
                                  "crop-sown-registry":{"url":"http://localhost:9003/dci/registry/sync/search"}}'
python -m openg2p_agri_composite.main run  # http://localhost:8000/docs
```

## Configuration (environment, prefix `AGRI_COMPOSITE_`)

| Variable | Default | |
|---|---|---|
| `USE_CASES_DIR` | `use-cases` (`/app/use-cases` in the image) | Use-case directory |
| `USE_CASES_RELOAD_SECONDS` | `30` | `0` = no reload |
| `REGISTRIES` | `{}` | JSON `{controller: {url, partner_id?, receiver_id?}}`. `url` is the registry's DCI sync search URL. If `partner_id` (its DCI ID, e.g. `farmer-registry`) is set, the registry's response signature is verified against `PARTNER_<ID>` in PM. |
| `COMPOSITE_PARTNER_ID` | `agri-composite` | The composite's ID. It is the `sender_id` towards registries, and partners use it as `receiver_id`. |
| `SIGNING_P12_PATH`, `SIGNING_P12_PASSWORD`, `SIGNING_KID`, `SIGNING_ALGORITHM` | –, –, thumbprint, `auto` | The composite's key. If the kid is set, it must match the kid registered in PM. |
| `PARTNER_MGMT_API_URL` | `http://commons-services-pm-partner-api` | Partner keys are fetched by `PARTNER_<SENDER_ID>` and cached (`PARTNER_KEY_*` TTLs) |
| `CRYPTO_ALLOWED_ALGORITHMS` | `EdDSA,ES256,RS256` | |
| `REQUEST_MAX_SKEW_SECONDS` | `300` | How far `header.message_ts` may be from now |
| `DEFAULT_SOURCE_TIMEOUT_MS`, `DEFAULT_OVERALL_TIMEOUT_MS` | `5000`, `10000` | Used when the use case doesn't set them |
| `HTTP_MAX_CONNECTIONS`, `HTTP_MAX_KEEPALIVE_CONNECTIONS` | `200`, `50` | One pooled client per worker |
| `AUDIT_MANAGER_URL` | empty (off) | e.g. `http://commons-services-auditmanager:80` |
| `NO_OF_WORKERS` | `2` | gunicorn workers per pod |

## Try it on a cluster as a partner

**1. Keys and onboarding.** Run `python scripts/partner_kit.py keys` (defaults: partner `bank-a`,
composite `agri-composite`). It writes a partner key and the composite's `.p12` to
`scripts/kit-out/`, and prints what to set up:
- **PM:** onboarding requests for `PARTNER_BANK_A` and `PARTNER_AGRI_COMPOSITE`, each with its public
  key and kid. Approve both.
- **Kubernetes:** the `kubectl create secret generic agri-composite-signing …` command
  (`composite.p12`, `password`, `kid`, `algorithm`).
- **CM:** a binding and policy for audience `bank-a` with each registry: `farmer-registry` with
  `farmer_personal_details, family_details, farm_details, main_crops`, and `crop-sown-registry` with
  `farmer_reference, crop_season, measures, location`. The farmer ID that the Crop Sown Registry
  query needs is read from `farmer_personal_details`.

**2. Registries.** Each registry's partner API needs signature validation and consent enforcement
on, `consent_data_controller` set to its controller ID (`farmer-registry` / `crop-sown-registry`),
and the CM support for one consent with a grant per registry, presented by the composite on the
partner's behalf (contract §2–3).

**3. Install the chart** `openg2p-agri-composite` (Rancher catalog: "Agri Stack Composite"). Check
`composite.registries` against the registries' release names. The defaults are
`fr-partner-api` and `csr-partner-api` (registry releases named `fr` and `csr`). Use cases are edited in Rancher's **Edit YAML**
view (`composite.useCases`). The sanity hook pings the API and lists the use cases.

**4. Call it:**

```bash
python scripts/partner_kit.py describe --url https://agri-composite.<ns>.openg2p.org
python scripts/partner_kit.py call --url https://agri-composite.<ns>.openg2p.org \
    --subject FAYDA_FAN:<FAN of a registered farmer> --param crop_year=2019 --param season=SEASON_MEHER
```

`call` builds a consent with both grants (`--controllers` narrows it), signs the envelope, verifies
the composite's signature on the answer and prints it. To remove the release:
`deployment/scripts/uninstall-agri-composite.sh --namespace <ns>`.

## End-to-end test from a laptop

`scripts/e2e.py` does the steps above against a namespace with your kubectl context, through
`kubectl port-forward`, and prints the composite's JSON answer:

```bash
pip install -r composite/scripts/requirements.txt
python composite/scripts/e2e.py --namespace <ns> --dry-run   # GETs and SELECTs only; prints the plan
python composite/scripts/e2e.py --namespace <ns>             # set up (lists the writes, asks), then call
python composite/scripts/e2e.py --namespace <ns> --call-only --crop-year 2018 --season SEASON_MEHER \
    --emit-curl --emit-postman loan-profile.postman.json
```

- **Setup** (idempotent): a test partner (`--partner`, default `e2e-bank`) and the composite are
  onboarded and approved in PM, or get their key added by a key-update request; the partner's
  audience gets a CM binding and policy for each registry; Secret `agri-composite-signing` is
  created and the composite restarted. A signing Secret whose key PM already serves is left alone.
  Admin tokens come from Keycloak with the PM and CM admin clients' secrets (read with kubectl, held in
  memory). Keys are kept in `~/.agri-composite-e2e/<ns>/` (mode 0700) and reused.
- **Stops** with exit code 2 when a decision is needed: the partner is not in the use case's
  `allowed_partners` (it prints the Helm value change), a CM policy is waiting for AWE approval, or a
  PM key conflicts.
- **Farmer:** `--fan`, or a FAN that is in the Farmer Registry and has crop seasons in the Crop Sown
  Registry (read-only `psql` in the Postgres pod).
- **Call:** consent with both grants, signed envelope, response signature checked against the
  composite's PM key. Exit code 3 if the call fails or a source does not answer `ok`/`no_record`.
- **Output:** each run writes two files to `composite/scripts/out/` (git-ignored, owner-only; they hold a farmer's personal data), with the same timestamp: the response JSON (`<use case>-<namespace>-<time>.json`) and everything the script printed (`e2e-<namespace>-<time>.log`). The screen shows the log and a summary with each source's status; `--print` also prints the JSON; `--out-dir` saves the files elsewhere.
- `--emit-curl` / `--emit-postman` give a pre-signed request for the ingress and a port-forward. It
  expires about 5 minutes after signing (`message_ts` and the consent's `issued_at`).

Unit tests for its pure parts: `pytest composite/scripts/tests`.
