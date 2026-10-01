# Agri Stack use-case composite

One generic service that serves approved **use cases**, such as `loan-profile`, by querying several
registries over DCI and returning one signed response. Each use case is a YAML file, not code. The
design is in [docs/composite.md](../docs/composite.md).

How a query is served:

1. The partner posts a signed envelope to `POST /composite/v1/use-cases/{use_case}/query`
   (`{use_case}` or `{use_case}@{major}`).
2. The composite checks the partner's signature against its Partner Management (PM) key (no key
   means rejection), then checks the partner is in `allowed_partners`, validates the input, and
   applies the rate limit.
3. It checks the consent: the partner's signature on it, that its subject is the request's subject,
   and that it has a grant for every mandatory source. An optional source with no grant is reported
   as `denied` and not called.
4. It calls each source as a DAG (`depends_on`). Each call is a DCI `sync/search` signed with the
   composite's own key. The partner's consent goes unchanged in `authorize.consent_jws`, and the
   partner's ID in `header.meta.on_behalf_of`.
5. It maps the results (JSONPath), computes derived values, reports a status per source
   (`ok | no_record | denied | unavailable | error`), signs the response and keeps nothing.
6. Audit events (`request`, `source_call`, `response`) go to the Audit Manager without blocking the
   request.

```
composite/
  backend/                 Python package openg2p_agri_composite (openg2p-fastapi-common), tests
  use-cases/               use-case files (loan-profile.yaml is the sample)
  docker/agri-composite-api/Dockerfile
  deployment/charts/openg2p-agri-composite/   Helm chart (Rancher catalog)
  deployment/scripts/uninstall-agri-composite.sh
  scripts/partner_kit.py   test keys, onboarding payloads, consent, signed calls
```

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
`status: published` is served. See [use-cases/loan-profile.yaml](use-cases/loan-profile.yaml).

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
