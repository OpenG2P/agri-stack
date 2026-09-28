<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Activity register

> This page uses the Crop Sown Registry as the worked example. The design as built is under [Design in the registry platform](#design-in-the-registry-platform); the Crop Sown Registry itself is described in [Crop Sown Registry](crop-sown-registry.md).

## What it is

An **activity register** records things that happened, such as attendance, sowing, a harvest or a pest sighting. Records are **appended and never edited**. A correction is recorded as a new record that supersedes the old one.

A registry instance can hold:

| Configuration | Example |
|---|---|
| **Register only** | Farmer Registry, DA Registry |
| **Activity register only** | Attendance register, a standalone Crop Sown Registry |
| **Both** | Farmer Registry with a "farm visits" activity register attached to the farmer register |

An activity can refer to a subject in the **same instance** (a farmer register next to it) or **held elsewhere** (a Fayda token, a Farmer Registry ID, a plot in another registry). Crop sown is supported both ways; see [Where an activity register lives](#where-an-activity-register-lives).

An activity register does **not** use:
- functional ID issuance (optional per activity type, e.g. a receipt number)
- deduplication
- change requests or maker-checker
- history tables
- intake-form mirror tables
- completion scores
- registrant authentication

## Crop sown, remodelled

**How it's built today:** crop sown invents a header register, `CropSown` (one record per farmer, crop year and season). It has **8 child `TABLE` registers**, and every one of them runs through change requests, history and intake forms. It also:
- ships a modified copy of the platform core
- patches the ingestion worker so that stage forms attach to the right header
- duplicates approval state in its own fields (`status`, `rejection_reason`, `edit_count`)
- stores every date twice, with an extra `*_date_ec` string holding the Ethiopian-calendar date

**As an activity register:**

| Today | As an activity register |
|---|---|
| `CropSown` header register (functional ID, dedup at 70) | **Activity context** "plot × season": a grouping key (farmer ref + plot ref + crop year + season) with an open/closed status. Crop sown doesn't need to be a register. |
| Planning, Cultivation, Sowing, Production, Harvest, Infestation tables | **Activity types**: `PLANNED`, `LAND_PREPARED`, `SOWN`, `GROWTH_OBSERVED`, `HARVESTED`, `INFESTATION_REPORTED`. Each has a JSON-Schema payload and a few promoted columns. |
| Cluster / CultivationCluster tables | A **cluster register** in the same instance, referenced by activities |
| `lifecycle_stage` on the header | A **projection**: current stage, area sown, yield and last activity per plot × season |
| `farmer_name`, `region_name`… copied onto records | **Typed references** to the Farmer Registry / Fayda and to MDS geography, with names shown through a cached lookup |
| `da_name`, `da_mobile_number` on each line | A reference to the **DA Registry** |
| `status`, `rejection_reason`, `edit_count` | A **lightweight verification** state: submitted → verified / rejected (with reason) |
| `sowing_date_ec`, `harvest_date_ec`… as strings | **Ethiopian calendar support**: store the Gregorian date, enter and display in the Ethiopian calendar |
| `sync_id`, `temporary_land_id` | **Idempotency key** plus **resolution of temporary IDs** created offline |
| Patched ingestion worker (find header, attach child) | Ingestion writes activities directly with context keys, so no parent lookup is needed |
| Custom `dashboard-ui` | **Configured indicators** over projections |

## Changes to the registry platform

### Core platform

1. **Metamodel.** Add an `ACTIVITY` kind to the register definitions, plus **activity type definitions**. An activity type needs no `master_register_id`. Allow instances that have only registers, only activities, or both.
2. **Split the base model.** Break `G2PRegister` into mixins (record base, approvable, identifiable, searchable, linked) and add a `G2PActivity` base with the standard fields:
   - `activity_id`, `activity_type`
   - `occurred_at`, `recorded_at`, `recorded_by`
   - `channel`, `idempotency_key`
   - `supersedes_id`, `status`
   - subject and context references
3. **Write path.**
   - An append API, including batch submission, that bypasses change requests and intake forms.
   - Updates and deletes are blocked at both the API and the database level.
   - Correcting or voiding a record needs permission and a reason.
4. **Ingestion.** ODK, file import and partner ingestion write activities directly.
5. **Search and query.** By subject, context, activity type and time range, across time-partitioned tables.
6. **Access control and data policy.** Filter on the activity's location or owning org unit rather than on a register record's address.
7. **Outgest and DCI.** Publish on insert; support DCI search and subscribe/notify for activities, with consent enforced.
8. **Staff UI.**
   - The activity list with filters is the home screen.
   - A timeline per subject or context.
   - Entry forms reuse the existing `ui-widgets`.
   - Batch entry, e.g. marking attendance for a whole session.
9. **Schema migrations (Alembic).** `create_all` can't change existing tables. This gap affects the whole platform, not just activity registers.

### New features not in the registry platform today

| # | Feature | Why (crop sown / attendance) |
|---|---|---|
| 1 | **Projections** (current-state records) | Current stage per plot × season; attendance rate per person |
| 2 | **Activity context**: a grouping key with open/closed status | Plot × season for crop sown; session for attendance. Replaces the invented header. |
| 3 | **Typed references**: internal register, external registry, Fayda, MDS codes and geography; validation per type (strict / lenient / none); names shown without copying | Farmer and plot held elsewhere; walk-ins at a training session |
| 4 | **Sequence rules**: allowed order, repeatable vs once per context, expected dates | Can't harvest before sowing; infestation is repeatable; harvest due about N days after sowing |
| 5 | **Work lists** from sequence rules | "Plots in my kebele overdue for a harvest visit" |
| 6 | **Lightweight verification** (submitted → verified / rejected) | Supervisor checks a DA's entries without full change management |
| 7 | **Corrections and period locking**: supersede or void with a reason; limits on backdating; closing a period with controlled reopen | No changes to a season after it's closed; no attendance changes after month-end |
| 8 | **Business uniqueness rules**, as well as idempotency | One `SOWN` per plot × season × crop; warn about likely duplicates |
| 9 | **Ethiopian calendar** entry and display | Replaces the duplicate `*_date_ec` string fields |
| 10 | **Support for offline capture**: client-generated IDs, resolving temporary IDs, sync conflicts | ODK in areas with no network; `temporary_land_id` |
| 11 | **Geography on activities**: point or polygon, geo-tagged photos | Plot location at sowing; infestation photo |
| 12 | **Time partitioning, retention and archiving** | Attendance and seasonal data grow quickly |
| 13 | **Configured indicators** over projections | Sown area per woreda, yield per hectare, attendance rate |
| 14 | **Bulk export API** | Analytics, audits, FAO reporting |

## Design in the registry platform

This is how the activity register is built in `registry-platform` (branch `feature/activity-register`).

### Data model

The activity model is **its own base class**, `G2PActivity`, next to `G2PRegister`. `G2PRegister` itself is unchanged, so existing registries behave exactly as before.

| Table | Purpose |
|---|---|
| `g2p_register_definitions` (existing) | An activity register is a row with `register_purpose = ACTIVITY` |
| `g2p_activity_<name>` (per register) | The activities. Standard fields plus a JSONB `payload`, and payload fields promoted to typed columns for filtering, indexes, data policies and indicators. **Partitioned by year** of `occurred_at`, plus a default partition. |
| `g2p_activity_projection_<name>` (per register) | The current state, one row per context |
| `g2p_activity_types` | Per type: JSON Schema, form layout, rules (repeatable, uniqueness, prior types with warn/block, due window, backdating limit, verification), reference rules, Ethiopian-calendar fields |
| `g2p_activity_contexts` | Grouping key (e.g. plot × year × season × crop) with subject, attributes and open/closed status |
| `g2p_activity_period_locks` | Closed periods, with who reopened them and why |
| `g2p_activity_idempotency_keys` | One activity per key, across all partitions |
| `g2p_activity_outbox` | Events written in the same transaction as the activity |
| `g2p_activity_temporary_references` | Offline temporary IDs and what they resolved to |
| `g2p_activity_indicators` | Indicator definitions: aggregate, column, group-by, filters. No SQL is stored in configuration. |
| `g2p_activity_odk_forms`, `g2p_activity_odk_failures` | ODK Central form mappings and the submissions that failed |

- **Append-only is enforced in the database.** A trigger rejects `DELETE`, and rejects any `UPDATE` that touches columns other than status and verification.
- **Extensions need no migration code.** The platform migration finds an extension's `G2PActivity…` and `G2PActivityProjection…` models and creates them: partitions, indexes and trigger.

### Writing an activity (one transaction)

1. **Idempotency.** If the idempotency key has been seen before, return the existing activity.
2. **Prepare the payload.**
   - Convert Ethiopian-calendar dates.
   - Let the domain service add derived values (e.g. yield per hectare).
   - Validate against the type's JSON Schema.
3. **Check references.**
   - Code lists, including nested rows such as fertiliser types: strict. The registry holds no copy; it reads them from Master Data.
   - Master Data geography.
   - Records in the same registry.
   - External IDs: pattern only, or a lookup through the domain service; strict, lenient or none.
   - Temporary IDs: recorded now and resolved later.
4. **Find or open the context** and lock it, so concurrent writes to one context are serialised.
5. **Check rules:** dates, closed periods, repeatability, uniqueness, sequence. Warnings are stored on the activity; blocking rules reject it.
6. **Save.** Insert the activity and its idempotency key, recompute the context's projection in the same transaction, and write an outbox event.

**Corrections:**
- **Supersede:** a new activity replaces the old one; the old one is kept as `SUPERSEDED`.
- **Void:** the activity no longer counts, but stays in the history.
- **Verify or reject:** changes only the verification columns.
- All of these need a reason and are blocked inside closed periods.

### Interfaces

| Where | What |
|---|---|
| Staff API `/activity/*` | Registers and types (types include code-list options for forms); append (single and batch, atomic or per item); supersede, void, verify, reject; get, search, timeline; contexts (open, close, reopen); work list; projections; indicators; period locks; temporary references; rebuild projections |
| Partner API `/partner/activity/append_activities` | DCI-style signed envelope (PM keys); per-item outcomes |
| Partner API `/dci/registry/sync/search` | `reg_type` can be an activity register. Returns current activities only, rendered by the register's DCI template, with the consent clamp applied as for records. |
| Celery | `activity_outbox_worker` (outgest and aggregate hook), `activity_reconcile_worker` (repairs projection drift), `activity_partition_worker` (next year's partitions), `activity_odk_pull_worker` (ODK Central) |
| Staff UI | Activity registers on the home page and at `/activity`. Per register: activities (filters, detail panel with verify/reject/correct/void), current state, work list, indicators, record (single or batch, form generated from the JSON Schema, Ethiopian-calendar date picker), settings. Per context: current state, timeline, record the next activity. |

**Permissions:** `activity:view`, `activity:create`, `activity:correct`, `activity:verify`, `activity:configure`. They are mapped onto the existing IAM roles by the registry's IAM registration.

**Data policies fail closed for activity registers.** A policy on a column the activity table doesn't have denies access, instead of being skipped as it is for record registers.

**An activity register's identity is fixed.** Its mnemonic names the extension's activity classes, so the generic register configuration can't rename it or switch it to or from a record register. That configuration also counts activities and contexts when deciding whether the register holds data, which is what stops a register in use from being deleted.

## Relation to the Observations design

The registry platform's [Observations design](https://docs.openg2p.org/products/registry/registry/design/observations-design) models the same idea under another name. Both use append-only records, a JSON Schema per type, corrections as new records, lifecycle order, and roll-ups. **The platform term stays "activity" for now.**

**Where the two agree**

| Observations | Activity register |
|---|---|
| Observation type, scoped per register | Activity type, scoped per register |
| `VOIDS`: a new record replaces the old one, which is archived | Supersede |
| `FOLLOWS`: harvest links to its sowing | Prior types, with warn or block |
| Payload validated against the type's JSON Schema | Same |

**Where they differ, and what we chose**

| Topic | Observations | Activity register | Decision |
|---|---|---|---|
| Where the data lives | Inside the subject's registry; recorded from the record's profile | A register of its own. The subject can be in the same registry or in another one | **Support both.** See [Where an activity register lives](#where-an-activity-register-lives). |
| Storage | One generic platform table for every type in every register; types added through an API, with no code | One table per register from the extension, with typed columns, yearly partitions and an append-only trigger | **Keep per-register tables.** Both deployments of crop sown ship an extension, and crop sown needs typed columns, partitions and a domain service. A generic, code-free table is deferred ([open items](open-items.md)). |
| Grouping a lifecycle | An explicit `FOLLOWS` link, chosen by the agent, one step at a time | A context derived from the payload (plot × year × season × crop) | **Keep contexts.** They handle an 8-step lifecycle and intercropping. An explicit link is an option for types that can't derive a key. |
| Roll-ups | Asynchronous: optional enrichment, then adapter-computed aggregates per subject and period, with history | Synchronous: a projection per context, recomputed in the same transaction, plus declarative indicators | **Keep projections for current state; add aggregates and enrichment as an asynchronous layer on top.** |
| Code lists | The registry's local `G2PAttribute` tables | Read live from Master Data | **Ours.** The local tables no longer exist in the platform. |
| Offline sync | Batch endpoint with no idempotency | Idempotency key, temporary IDs | Ours |
| Governance | Not covered | Verification, period locks, reference rules, DCI with consent, data policies, permissions | Ours |

**Taken from the Observations design**
- **A tab on the subject's profile.** A record in a record register (e.g. a Farmer Registry Land record) shows its activities and can record a new one, with the subject filled in.
- **`schema_version`**, incremented on the activity type and stamped on each activity.
- **Provenance on the envelope:** `source` (`AGENT_APP`, `STAFF_WEB_UI`, `PARTNER_<mnemonic>` checked against Partner Management) and `submission_id` for a batch.
- **An asynchronous layer on top of projections:**
  - **enrichment**, such as weather or satellite data, stored beside the payload and never replacing it;
  - **aggregates** per subject and period (`period_key` with start and end dates, plus geography dimensions), with history. These are for roll-ups across contexts, e.g. a farmer's season summary across plots.
- **Form behaviour:**
  - defaults from the subject's last activity of the same type;
  - offline drafts with a sync badge;
  - choice chips for code lists.

## Where an activity register lives

A crop season can be recorded in either of two places. The platform supports both.

| Deployment | Example | Plot and farmer | Where staff record |
|---|---|---|---|
| **Its own registry** | The Crop Sown Registry, run by a separate department | References to the Farmer Registry, **checked for format only** (`EXTERNAL`, lenient); temporary plot IDs resolved later | The registry's activity pages |
| **Inside a record registry** | A `CropSown` activity register inside the Farmer Registry | The Farmer Registry's own Land and farmer records (`LOCAL_RECORD`, strict); the subject is the Land record | The activity pages, **and** an Activities tab on the Land or farmer record |

- **One implementation.** The crop-sown models, domain service and activity definitions are one package. The Crop Sown Registry wraps it as a registry of its own; the Farmer Registry installs it as one more register.
- **Only the reference rules and the seed differ.** Context key, activity types, projection, indicators and DCI rendering are the same, so data can move between the two deployments.
- **If a country runs both,** one of them has to be authoritative for crop seasons, or the two are merged when data is shared. See [open items](open-items.md).
