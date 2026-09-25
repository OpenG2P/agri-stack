<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Activity register

> **Status: proposed.** This page uses the Crop Sown Registry as the worked example. Nothing described here is built in the registry platform (RP) yet.

## What it is

An **activity register** records things that happened, such as attendance, sowing, a harvest or a pest sighting. Records are **appended and never edited**. A correction is recorded as a new record that supersedes the old one.

A registry instance can hold:

| Configuration | Example |
|---|---|
| **Register only** | Farmer Registry, DA Registry |
| **Activity register only** | Attendance register, a standalone Crop Sown Registry |
| **Both** | Farmer Registry with a "farm visits" activity register attached to the farmer register |

An activity can refer to a subject in the **same instance** (a farmer register next to it) or **held elsewhere** (a Fayda token, a Farmer Registry ID, a plot in another registry).

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
| Cluster / CultivationCluster tables | A **cluster register** (reference entity, with the switch for public read available), referenced by activities |
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

## Suggested phasing

1. **Attendance pilot:**
   - core items 1–5 and 8
   - features 1 (per-subject projections only), 2, 3, 7 and 8
2. **Crop sown on activities:**
   - features 4, 5, 6, 9, 10 and 11
   - retire the `core_pkg` fork and the patch to the ingestion worker
3. **Scale and analytics:** features 12–14, plus aggregate projections and indicators.
