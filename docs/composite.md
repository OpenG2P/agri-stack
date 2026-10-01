<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Use-case composite

> **Status: first version built (October 2026)**, in the agri-stack repo under `composite/` (service, Helm chart, partner test kit). See [As built](#as-built) for what is in it and what is not yet.

A **composite** serves one approved use case, such as `credit-profile`, by querying several departmental registries and returning one response. There is **one generic composite service**; each use case is a **configuration file** that it loads, not new code.

The configuration describes **how** a request is served. It **never grants access**. What a partner may receive is decided only by the PM policy and the farmer's consent, and each registry enforces that itself (see [policy and consent](policy-and-consent.md)).

## Example configuration: `credit-profile`

```yaml
# ── Identity ──────────────────────────────────────────────────────────────
use_case: credit-profile
version: 1.2.0                        # semver; breaking response changes → new major
status: published                     # draft | validated | published | deprecated | retired
title: Farmer credit profile
description: Identity, landholding, recent crops and herd, for credit assessment.
owner: agri-stack-platform

# ── Governance (links, does not grant) ────────────────────────────────────
policy: credit-assessment             # PM policy; caller must be associated with it
purpose: credit-assessment            # must be one of the policy's allowed purposes
consent:
  required: true
  collection: cm-originated           # cm-originated | partner-embedded
  mode: single                        # one consent, one grant per registry (see consent-model.md)

# ── Input: what the SP sends ──────────────────────────────────────────────
input:
  subject:
    id_types: [fayda_token, farmer_id]   # must be within the policy's subject_id_types
  parameters:
    seasons: { type: integer, min: 1, max: 4, default: 2 }
  batch: { max_subjects: 1 }             # 1 = one farmer per request

# ── Sources: one entry per registry ───────────────────────────────────────
sources:
  - id: farmer
    controller: farmer-registry          # PM participant; endpoint and keys resolved from PM
    requirement: mandatory               # mandatory | optional
    request_scopes: [farmer:profile, farmer:land]
    dci:
      reg_type: Farmer
      reg_record_type: spdci-extensions-dci:Farmer
      query_template: templates/farmer-by-id.json.j2
    timeout_ms: 2000
    retries: 1

  - id: crops
    controller: crop-sown-registry
    requirement: optional
    request_scopes: [crop:season]
    dci:
      reg_type: CropSown
      reg_record_type: spdci-extensions-agri:ActivityAggregate   # the farmer's season summaries
      query_template: templates/crop-seasons.json.j2   # uses parameters.seasons
    timeout_ms: 2500
    retries: 1

  - id: herd
    controller: livestock-registry
    requirement: optional
    request_scopes: [livestock:herd]
    dci:
      reg_type: Livestock
      query_template: templates/herd-by-owner.json.j2
    timeout_ms: 2000
    retries: 0

# ── Assembly: what the SP gets back ───────────────────────────────────────
response:
  mode: merged                           # merged | data-blind
  schema: schemas/credit-profile-1.json  # published JSON Schema / JSON-LD context for SPs
  correlate_on: subject                  # results joined on the resolved subject identifier
  mapping:                               # source fields → response fields
    farmer.name:          "$.farmer.name"
    farmer.sex:           "$.farmer.sex"
    farmer.kebele:        "$.farmer.address.kebele"
    land.parcels:         "$.farmer.land[*]"
    crops.seasons:        "$.crops.records[*]"
    herd.animals:         "$.herd.records[*]"
  derived:
    land.total_ha:        "sum($.farmer.land[*].area_ha)"
  source_status: true                    # per-source ok | no_record | denied | unavailable

# ── Behaviour ─────────────────────────────────────────────────────────────
execution:
  fan_out: parallel
  overall_timeout_ms: 3000
  partial_response: allowed              # a mandatory source failing → whole request fails
limits:
  rate_per_partner: 60/min
  daily_quota_per_partner: 20000
audit:
  events: [request, source_call, response]   # sent to audit-manager, linked by request ID
```

## What each section does

| Section | Purpose | Notes |
|---|---|---|
| **Identity** | Name, version and lifecycle status | Partners call `use_case@major`. A breaking change to the response means a new major version, and the old one is deprecated with a sunset date. |
| **Governance** | Links the use case to a PM policy, a purpose and a consent mode | The gateway rejects callers not associated with `policy`. The consent settings follow the [consent model](consent-model.md). |
| **Input** | What the partner sends: subject identifier types, parameters, batch size | Checked before any registry is called |
| **Sources** | One entry per registry: controller, whether it's mandatory, requested scopes, DCI query template, timeout, retries | Endpoints and signing keys come from **PM**, never from this file. The query templates use the Jinja style the registries already use for DCI mapping. |
| **Response** | Merged or data-blind, published schema, field mapping, derived fields, per-source status | In data-blind mode `mapping` and `derived` aren't allowed. Each registry encrypts its part to the partner's PM key and the composite only bundles the parts. |
| **Execution / limits / audit** | Timeouts, partial-response rule, rate limits, audit events | Limits are enforced at the gateway |

## Checks when a use case is published

A use case moves from `draft` → `validated` → `published` only if all of these pass:

1. The `policy` exists in PM and is active, and `purpose` is one of its allowed purposes.
2. Each source's `request_scopes` ⊆ that controller's **approved** section of the policy. A source whose section isn't yet approved can stay in the configuration, but it will always return `denied` until the section is approved.
3. `input.subject.id_types` ⊆ the policy's allowed subject identifier types.
4. Every `mapping` path refers only to fields covered by the requested scopes, using each registry's published scope-to-field mapping. A use case can't map a field it didn't ask for.
5. The query templates render valid DCI requests against sample data. The sandbox runs each use case end to end with test fixtures.
6. The response schema validates against the mapped output.

Publishing is done by the Agri Stack platform team. Because the configuration can't widen access, it doesn't need department approval; the departments have already approved the policy.

## What happens at runtime

1. **Gateway:** authenticates the partner, checks its association with `policy` in PM, applies rate limits, and routes to `use_case@version`.
2. **Composite:**
   - verifies the partner's signature and validates the input
   - renders one DCI request per source from its template
   - signs each request with its own PM key, carrying the partner's consent and the request ID
   - sends the requests in parallel
3. **Each registry:** calls CM `/validate` and releases only the effective scopes.
4. **Composite:**
   - waits up to the timeout
   - applies the partial-response rule
   - maps the results, computes derived fields, adds the per-source status, and validates against the schema
   - responds, then discards everything
5. **Audit:** every step sends an audit event, linked by the request ID.

## Worked example: wheat sown by a farmer this season

The question is "total area of wheat sown by farmer X in this season". Both registries answer with one **synchronous** DCI call each, `POST /dci/registry/sync/search`. Neither registry has an asynchronous search, so the composite never waits for a callback.

**Farmer Registry:** who the farmer is. Search the Farmer register by Fayda FAN (`foundational_id`) or farmer ID (`functional_record_id`):

```json
"search_criteria": {
  "reg_type": "Farmer",
  "query_type": "expression",
  "query": {"type": "expression", "value": {"expression": {"query": {"foundational_id": {"$eq": "<FAN>"}}}}}
}
```

The farmer record carries both identifiers, `UIN` (the FAN) and `FARMER_ID` (e.g. `FR-0007`). It comes with the farmer's linked records (land, household, livestock) and declared main crops, whether the search is by exact field or by ID.

Any question about one farmer follows the same pattern: the farmer's record from the Farmer Registry, and from the Crop Sown Registry the farmer's activities, crop seasons or season summaries, each filtered on its own fields. Nothing in the registries is specific to a particular question.

**Crop Sown Registry:** what they sowed. Search the farmer's season summary for the crop year and season:

```json
"search_criteria": {
  "reg_type": "CropSown",
  "reg_record_type": "spdci-extensions-agri:ActivityAggregate",
  "query_type": "expression",
  "query": {"type": "expression", "value": {"expression": {"query": {
    "subject_id": "FR-0007", "aggregate_type": "FARMER_SEASON_SUMMARY",
    "crop_year": 2019, "season": "SEASON_MEHER"}}}}
}
```

One record comes back. The answer is at `measures.by_crop.CROP_WHEAT.area_sown_ha`, beside the season's totals and the other crops. Alternatively, `reg_record_type: spdci-extensions-agri:CropSeason` with `"crop": "CROP_WHEAT"` returns the wheat crop seasons, one per plot, each with its `area_sown_ha`, stage and whether sowing was verified. The mapping then sums them.

| | Season summary (`…:ActivityAggregate`) | Crop seasons (`…:CropSeason`) |
|---|---|---|
| Records | One per farmer and season | One per plot and crop |
| Answer | Read one field | Sum over plots |
| Freshness | Updated by the outbox worker, seconds after each activity | Updated in the same transaction as each activity |
| Filters | `aggregate_type`, `period_key`, `crop_year`, `season` | Any plain projection column: `crop_year`, `season`, `crop`, `stage`, `plot_id`… |

**Sequencing.** The Crop Sown Registry knows the farmer by farmer ID, not by FAN.
- **The partner sends a farmer ID:** both sources are called in parallel.
- **The partner sends a FAN:** the Farmer Registry is called first, and the Crop Sown query uses the `FARMER_ID` it returns. The source then declares `depends_on: farmer`, and its query template reads the farmer ID from the farmer result.

**"This season"** is resolved by the composite (the crop year and season from today's date, using the season windows in the [Crop Sown Registry](crop-sown-registry.md#farmers-season-summary)) and passed as parameters. Without them, the summaries come back newest first.

## Response to the partner (example)

```jsonc
{
  "use_case": "credit-profile@1",
  "request_id": "…",
  "subject": { "type": "fayda_token", "value": "…" },
  "sources": {
    "farmer": { "status": "ok" },
    "crops":  { "status": "ok" },
    "herd":   { "status": "unavailable" }
  },
  "data": {
    "farmer": { "name": "…", "sex": "F", "kebele": "…" },
    "land":   { "parcels": [ … ], "total_ha": 1.4 },
    "crops":  { "seasons": [ … ] }
  }
}
```

## Where configurations live

- **Storage:** configuration files, query templates and schemas are kept in a Git repository, reviewed like code, and loaded by the composite service. An admin UI is optional.
- **Sandbox:** each published use case appears in the developer sandbox with its schema, sample request and response, and a test harness.
- **Adding a use case** (e.g. `input-subsidy-eligibility`) means a new configuration file plus the matching PM policy. No code changes are needed unless the use case requires a new kind of derived field.

## As built

**Service.** One stateless FastAPI service (`openg2p-agri-composite`), no database. Use cases are YAML files in a Helm-managed ConfigMap (edited in Rancher's YAML values editor), validated at load and reloaded within 30 seconds of a change; only `published` ones are served. Registries are configured as `controller → DCI search URL`.

**Partner API.** `POST /composite/v1/use-cases/{use_case}[@major]/query` with a signed envelope (detached JWS over `{header, message}`, the partner's PM key, as for registry DCI searches):
- `message.subject` (type and value), `message.parameters`, and `message.consent_jws`: **one consent with a grant per registry** (see [consent model](consent-model.md)).
- `GET /composite/v1/use-cases` and `/{use_case}` describe the published use cases (input, parameters, sources, output fields).

**What it does per request:**
1. Verifies the partner's signature against PM; checks the message is fresh.
2. Checks the partner is in the use case's `allowed_partners` (a stand-in for the PM policy association, which PM doesn't have yet).
3. Validates the input and applies a per-partner rate limit.
4. Verifies the consent (the partner's signature, the subject is the one asked about, a grant for every mandatory source). An optional source without a grant is reported `denied` and not called.
5. Calls the sources in dependency order (`depends_on`), in parallel within a level, each a DCI sync search signed with the **composite's own PM key**, carrying the partner's consent unchanged and the partner's ID in `header.meta.on_behalf_of`. Per-source timeout and retries; overall timeout.
6. Maps the results (JSONPath), computes derived fields (sum, count, min, max, first, round), and returns a response signed by the composite, with a status per source (`ok`, `no_record`, `denied`, `unavailable`, `error`).
7. Sends audit events (request, each source call, response; no data) to the audit manager, without ever blocking the request.

**Each registry still decides.** It verifies the composite's signature, validates its own grant in the consent with CM, checks that the consent's subject is the person searched (directly, or through its own data, e.g. a farmer ID recorded with the farmer's FAN), and clamps the record to the effective scopes.

**Sample use case `loan-profile`:** by Fayda FAN or farmer ID; optional crop year and season. Sources: the farmer (Farmer Registry), then the farmer's season summaries and crop seasons (Crop Sown Registry). Returns the farmer's identity and location, land parcels and total land, declared main crops, crop seasons, season summaries and total area sown.

**Scaling.** Stateless pods behind a CPU-based autoscaler; a pooled HTTP client per worker; partner keys cached; no database; startup never calls a registry. Rate limits are per pod: global limits and daily quotas belong to the API gateway.

**Not built yet:**
- the checks when a use case is published (against PM policies, which PM doesn't hold yet);
- data-blind mode;
- daily quotas and global rate limits (API gateway);
- registry endpoints and policies held in PM rather than in configuration;
- calling the crop sources in parallel with the farmer when the partner already sends a farmer ID;
- a configuration UI.
