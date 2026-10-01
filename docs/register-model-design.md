<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Register model: record kinds, trust and corrections (design note)

*Design note, 30 September 2026; phase 1 built 1 October 2026. Sections say what exists today and what is proposed; [section 7](#7-platform-changes-in-phases) says what phase 1 built.*

The registry platform grew two register kinds side by side: the conventional register and the activity register. Some of their differences are fundamental; others are just features one kind got first. This note sets out:
- **one register model, with two record kinds**;
- **one trust layer and one correction model** for both kinds;
- the **API changes** that follow;
- how the Farmer, Crop Sown and Livestock registries fit in.

## 1. One register model, two record kinds

A **register** belongs to a registry and holds records of **one kind**. A **registry** can hold registers of both kinds.

| Record kind | A record is | Examples |
|---|---|---|
| **Entity** | Something that exists and is maintained over time | Farmer, land parcel, household, animal, cluster, veterinarian, worksite |
| **Occurrence** | Something that happened, once, at a time | Crop sown, harvest, pest sighting, vaccination, birth, sale, attendance |

The kind is declared on the register definition. Today that's `register_purpose`: `REGISTER` / `TABLE` for entities, `ACTIVITY` for occurrences.

**Rule of thumb:** when a second value arrives, ask whether it *replaces* the first because it describes the same thing better, or is *another fact* at another time.
- **Replaces it:** entity. The farmer's corrected birth date replaces the wrong one.
- **Another fact:** occurrence. September's crop condition doesn't replace July's; both were true.

### What differs between the kinds

These differences are necessary; merging them would cost correctness or scale.

| Behaviour | Entity | Occurrence | Why they differ |
|---|---|---|---|
| **A second value** | New **version** of the same record | New **record**; a correction supersedes the old one | An occurrence's later value is often another fact, not a fix |
| **What "the value" is** | The latest version | All the records combined: total harvest, days present, doses given | Occurrences are summed or counted; versions never are |
| **Identity** | Functional ID, dedup, registrant authentication | Idempotency key | Two sowings on one plot are two facts, not duplicates |
| **Grouping** | Parent/child records (Farmer → Land) | Contexts (one crop on one plot in one season) | A crop season is a thread of events, not an entity |
| **Current state** | The record itself | Derived: projections and aggregates | See "What the value is" |
| **Time** | Valid from a date | Occurred at + recorded at; backdating limits; due windows; closed periods | Payroll, seasons and campaigns close |
| **Storage** | Table + history table | Append-only table, partitioned by year | Volume: people × days, plots × seasons × stages |

### What an occurrence never does

An occurrence's **facts are never modified**: what happened, when, where, to whom, and its payload and location. Only two things around it change:
- **Lifecycle status:** `CURRENT` → `SUPERSEDED` (replaced by a correcting occurrence) or `VOIDED` (withdrawn: it didn't happen). The record stays.
- **Verification:** held as **verification records** attached to it (section 2). Today it is columns on the row.

So an error is fixed by a **new** occurrence that points to the old one. A change in reality is simply **another** occurrence. Both old records stay readable, and anything already paid or certified on the original can be reversed and reissued against the correction.

### What is common to both kinds

These should work the same for entities and occurrences, and some of them are built for only one kind today:

| Capability | Entity today | Occurrence today | Proposed |
|---|---|---|---|
| **Verification** | A verification table tied to change requests (changes themselves are approved in AWE) | Verification columns on the activity | **One verification model** (section 2) |
| **Corrections** | Change request → new version + history | Supersede / void, with a reason | **One correction model** (section 3) |
| **Forms** | Sections and tabs of widgets | Generated from each type's JSON Schema | Either works for either kind. Allow JSON-Schema forms on entity sections, and section layouts on activity types (`section_ui_schema` exists) |
| **Reference rules** (code list, geography, local record, external ID; strict / lenient) | Widget-bound code lists only | Built | Available to entity registers too |
| **Location as named levels** (`geo_dimensions`) | `geo_code_hierarchy_json` on the record | Built | Named levels on entity records too, for area statistics |
| **Idempotent ingestion, submission IDs, schema versions** | No | Built | Both kinds |
| **Participants with roles** | Parent links only | One subject + payload references | **Typed participants** (section 4) |
| **Sharing** (DCI, consent, data policies) | Built | Built | Both kinds, plus trust status (section 6) |

## 2. Trust layer: verification

The registry's job is that its records are **authentic, and verified where that is needed**. Whether a programme will act on a record (approval for payment or eligibility) is decided in PBMS, not here.

### The terms

| Term | What it means | What happens to the data | Entity register | Activity register |
|---|---|---|---|---|
| **Validation** | Automatic checks at entry: format, required fields, code lists, plausibility | Bad input is rejected or warned on; nothing is stored about it | Built | Built |
| **Change** | The world changed: the farmer moved, got a new phone, sold a plot | New version; the old value stays in history as true *at that time* | Change request | Doesn't apply: an occurrence never changes; a new event is a new activity |
| **Correction** | The record was wrong: a typo, a mis-measured area | New version; the old value is marked as an error, never true | Change request marked as a correction, with a reason | Supersede, with a reason |
| **Void** | The record shouldn't exist: a duplicate, an event that never happened | Kept, but no longer counts | Record status (e.g. deactivated) | Void, with a reason |
| **Approval** (of a change) | A supervisor authorises a change or correction before it enters the register | Governs who may change data; says nothing about whether it is true | AWE on change requests, as built | Not needed: appends and corrections are governed by permissions and rules |
| **Verification** | An independent check that recorded data matches reality or an authoritative source: a field visit, a document, a Fayda lookup, a trusted device | **Never changes the data.** Adds a trust status (verified, failed or pending), with who checked, how and with what evidence | Phase 2 | Built as a single step; moves onto the common model |
| **Dispute** | The person concerned says a record is wrong | Triggers verification, then a correction if the claim holds | Phase 2 | Phase 2 |

**How they relate:**
- Verification is about **truth**; approval is about **authority to change**. Both can apply to the same data.
- A change or correction **resets the verification** of what it touched: new data hasn't been checked.
- A failed verification leads to a **correction** or a **void**; it never edits the data itself.

### The model (proposed)

**Verification policy**, set in configuration. What needs verifying depends on the registry, so the target is chosen per case:
- a **field** (e.g. the Fayda FAN, checked against Fayda);
- a **section** (e.g. land tenure, from a document);
- the **whole record**;
- an **activity type** (e.g. Sown and Harvested, as today).

For each target, the policy says:
- whether verification is **required**;
- which **methods** count: manual (a supervisor or DA), document, system check against an authoritative source, or a **trusted source** whose authenticated submissions count as verified (a vet's system, a biometric device);
- **who** may verify (roles);
- what **evidence** is expected (a photo, a document, a device capture).

**Verification records** are attached to a target. Each one holds:
- **outcome** (verified / failed) and **method**;
- **actor** (person, system or source);
- **evidence** references;
- **remarks**, and **when**.

A target can have several: a verification, a dispute, a re-verification. The log is kept.

**Verification status** is derived from the records and is what the UI shows and the APIs filter and share: `NOT_REQUIRED`, `PENDING`, `VERIFIED`, `FAILED`, and `DISPUTED` while a dispute is open. A change or correction to a verified target sets it back to `PENDING`.

**One model for both kinds.** Activity verification (today's columns on the activity row) becomes verification records on the activity, with its status derived as for entities. Behaviour doesn't change; the platform has one verification mechanism, not two.

**Mapping from today:**
- `g2p_register_verifications` (entities) and the verification columns on activities both become **verification records**;
- AWE approval of change requests stays as it is: it governs changes, not truth;
- certificates (VC issuance in the agent portal) are unchanged and outside this model.

Existing behaviour is the default policy, so the Farmer Registry and NSR behave as before.

## 3. One correction model

A **correction** fixes data that was wrong, always with a **reason** (see [the terms](#the-terms)). How it lands depends on the kind:

| Situation | Entity | Occurrence |
|---|---|---|
| **A value was wrong** | Change request → new version; history keeps the old one | Correcting occurrence **supersedes** the old one; the old one stays |
| **It shouldn't exist** | Deactivate / archive | **Void** ("it didn't happen"); it stays |
| **Who may** | Change requests go through AWE, as today | The `activity:correct` permission |
| **Disputed** (the subject says it's wrong) | A dispute recorded against the target; resolved by verification, then a correction if the claim holds | Same |
| **Verification** | The changed fields go back to pending | The correcting activity starts pending, if its type requires verification |

**The resurvey example.** An agent resurveys a plot and finds a different value. That's either:
- a **correction** (the first survey was wrong): supersede; or
- a **new observation** (things changed): a new occurrence.

The form asks which. A supersede of an occurrence carries the same information as a change request on an entity; the choice between the two depends on the record kind, and the difference is in what later readings mean.

## 4. Participants, and entities first

**Participants (proposed).** An occurrence has named roles, not just one subject:
- a vaccination has a farmer, an animal and a vet;
- a sowing has a farmer and a plot;
- attendance has a person, an event and a worksite.

Each participant has a **role** and is **always typed** (decided): `role: vet, register: Veterinarian, id: V789` for a register in the same registry, or `role: vet, system: vet-registry, id: V789` for one elsewhere. The type says where the ID lives, so it can be checked and looked up, and two registries with the same ID are never confused. Submitters send only the role and the ID; the activity type's configuration supplies the type, as reference rules do today.

One role is the **primary subject**, used for contexts and summaries. Participants are indexed, so "all vaccinations by vet V789" or "all activities on plot L1" is a direct query. Today's `subject_*` fields become the primary participant, and today's payload references (`plot_id`, `da_id`) become roles.

**Entities first (decided).** An entity register is updated **before** any occurrence about that entity is recorded. Occurrences never create or change entities:
- **Birth:** the animal is registered in the Animal register first. The *Born* occurrence then records the birth against that animal.
- **Sale:** the owner change goes through the Animal register's own change request first. The *Sold* occurrence records the sale.
- **Cluster enrolment:** the cluster and its membership are maintained in the Cluster register. Activities only refer to the cluster.

**The two kinds stay independent this way.** Each kind has its own write path and its own approvals, and an occurrence is only ever about entities that already exist.

**Consequences:**
- **Referenced entities must exist.** For a register in the same registry, a strict local-record rule enforces it. For another registry, a lookup enforces it where one is available. Where it isn't (the Crop Sown Registry checks Farmer Registry IDs for format only), the rule is an operating procedure: register the farmer and plot in the Farmer Registry first.
- **Temporary IDs are withdrawn (decided).** Temporary IDs (`TMP-…` plots created offline and resolved later) let an occurrence arrive before its entity. Offline capture must now register the new plot first, before the occurrence. The Crop Sown Registry no longer accepts them; the platform keeps the feature, unused, for a capture tool that registers the entity first.
- **The history and the current state can be traced in both directions.** The occurrence (e.g. the sale) is the history of what happened; the entity's current state (the owner) comes from its own change request. Each can point to the other, through the change request's reference and the occurrence's participants.

## 5. How the three registries fit

### Farmer Registry (entities; can hold occurrences later)

| Register | Kind | Verification | Corrections |
|---|---|---|---|
| Farmer (incl. declared main crops) | Entity | Approval of changes (AWE), as today. **Verification** of the Fayda FAN by a system check (Fayda lookup: trusted source) | Change request → new version |
| Land (child of Farmer) | Entity | Verification of size and tenure (survey, document) | Change request; verification of the changed fields goes back to pending |
| Household, members | Entity | As Farmer | Change request |
| Livestock (child of Farmer) | Entity | As Farmer | Change request. Kept until a Livestock registry exists |
| *(later)* Farm visits, trainings | Occurrence | Verification optional | Supersede / void |

Nothing changes for the Farmer Registry unless it adopts field verification. Its existing verification table becomes verification records, and AWE approval of changes stays as it is.

### Crop Sown Registry (occurrences, plus a cluster entity)

| Register | Kind | Verification | Notes |
|---|---|---|---|
| CropSown | Occurrence. Types: Planned, Land prepared, Sown, Growth observed, Infestation reported, Damage reported, Harvested. Context: plot × crop year × season × crop | **Verification:** Sown and Harvested, by a supervisor from photo evidence (as today); Infestation optionally | Participants: farmer (primary), plot, development agent. The farmer and plot are external references to the Farmer Registry, checked for format. Location = the plot's woreda |
| **Cluster** (proposed) | **Entity** | Approval of changes | Holds the cluster's attributes and membership, which the `CLUSTER_ENROLLED` activity carries today. Clusters and membership are maintained here first; activities only refer to the cluster. Cluster totals are derived from the plot activities |

**Corrections:**
- a wrong area → a sowing that supersedes the old one;
- a sowing that didn't happen → void;
- the correcting activity is verified again where its type requires it; programmes (PBMS) are told of the change.

**Resurvey:** a correction supersedes; a later observation is a new Growth observed.

### Livestock Registry (an entity and occurrences in one registry)

| Register | Kind | Verification | Notes |
|---|---|---|---|
| Animal | Entity (animal ID, species, sex, date of birth, current owner) | Approval of changes | The current owner is authoritative entity data |
| Veterinarian (or a provider registry) | Entity | — | May be in another registry |
| Livestock events | Occurrence. Types: Born, Vaccinated, Treated, Sold. Context: the animal | **Vaccinated:** verified by a **trusted source** (the vet's authenticated system). **Sold:** verified when it is an official transfer. **Born:** off | Participants: animal (primary), farmer, vet (and dam for Born) |

**Entities first:**
- a newborn animal is **registered** in the Animal register, then the Born occurrence is recorded;
- a change of owner goes through the Animal register's **change request** (with its approval), and the Sold occurrence records the sale.

The occurrences are the history. Current animal status (e.g. last vaccinated, sold) is **derived** from the events in the context's projection; the authoritative owner is the Animal register's.

**Corrections:**
- a wrong batch or dose → a vaccination that supersedes the old one, verified again;
- a sale that didn't happen → void the Sold occurrence, and correct the owner through the Animal register's own change request.

## 6. APIs: what changes

### Today

| API | Entities | Occurrences |
|---|---|---|
| **Staff** | Register data, metadata, sections and tabs; change requests; `/verifications` (get / add, tied to change requests); intake forms; ingestion and outgestion config | `/activity/*`:<br>• append, supersede, void, verify, reject;<br>• search, timeline, contexts;<br>• work list, projections, indicators, aggregates;<br>• locks, temporary references;<br>• `get_subject_activities` (profile tab), `get_latest_activity`, schema versions. |
| **Partner** | `/partner/ingest_data` (becomes change requests); `/dci/registry/sync/search` (records) | `/partner/activity/append_activities`, `correct_activities` (phase 1); DCI search of activities, context state (`…:CropSeason`) and aggregates |
| **Beneficiary** | `/beneficiary_portal`: own registers and sections | — |
| **Agent portal** | VC issuance and verification (register records) | — |

### Proposed changes

**Staff API**

| Area | Change |
|---|---|
| **Verification (new, common)** | `/verification/*`:<br>• get the verification policy for a register, section, field or activity type;<br>• add a verification record (verified, failed, dispute) on any target (record, section, field, occurrence), with evidence;<br>• list a target's verification records and status;<br>• list items awaiting verification (a work queue).<br>`/verifications/*` and `/activity/verify_activity`, `reject_activity` remain as shortcuts onto it. |
| **Corrections** | Entity corrections are change requests marked as corrections (existing flow, approved in AWE); occurrence corrections are supersedes. Both reset the verification of what they touch. `supersede_activity` and `void_activity` remain. |
| **Participants** *(built, phase 1)* | `append_activity` / `append_activities` accept `participants[]` (role and ID; the type comes from configuration). `search_activities` filters by participant and role. The profile tab covers every role, not only the subject. |
| **Entity registers** | Locations as named levels on records; reference rules on entity sections; idempotency keys on entity ingestion |

**Partner API**

| Area | Change |
|---|---|
| **Corrections by partners** *(built, phase 1)* | `/partner/activity/correct_activities`: supersede or void with a reason, same signed envelope. Today partners can only append. Entity corrections continue through `/partner/ingest_data` as change requests. |
| **Trusted-source submissions** | Partner Management marks a partner as a **trusted source** for given activity types or fields. Its signed submissions record an automatic verification, with the signature as evidence. |
| **Participants** *(built, phase 1)* | `append_activities` accepts `participants[]` |
| **DCI: verification in responses** | Records, activities, crop-season state and aggregates carry their **verification status**, clamped like any other scope. Searches can require a status, e.g. only **verified** sowings. |
| **DCI: subscribe / notify** | Subscribe to occurrence events (appended, superseded, voided, verified), published from the existing outbox, so a programme hears when a sowing is verified or corrected |

**Beneficiary API**

| Area | Change |
|---|---|
| **Own occurrences (new)** | A farmer's own activities, crop seasons (context state) and summaries |
| **Disputes (new)** | Dispute a record or occurrence about oneself, recorded against it, for staff to resolve by verification and, if the claim holds, a correction |


**Compatibility.** All of this is additive:
- existing endpoints keep working;
- the default verification policy reproduces today's behaviour (verification only where `requires_verification` is set; AWE approval of changes unchanged);
- Farmer Registry and NSR need no change until they adopt a new capability.

## 7. Platform changes, in phases

### Phase 1 (built)

No verification work: entity change requests keep their AWE approval, and activity verification stays as it was (`requires_verification` per type).

| Change | Where | What was built |
|---|---|---|
| **Participants** | Platform | Each activity type declares its roles (`participant_roles`: role → payload field, primary flag). Every activity's participants are stored typed and indexed (`g2p_activity_participants`: role, `LOCAL` register record or `EXTERNAL` system, ID). `append` accepts `participants[]` (role + ID); search filters by participant and role; the profile tab finds a record in any role. The staff UI lists participants on an activity |
| **Entities first** | Platform, CSR | A local entity is checked strictly (the cluster must exist in the Cluster register). Farmer and plot stay format-only (another registry). Temporary plot IDs withdrawn from the Crop Sown Registry |
| **Partner corrections** | Partner API | `/partner/activity/correct_activities`: supersede or void with a reason, signed like `append_activities`; a partner can only correct its own submissions |
| **Cluster as an entity** | CSR | A Cluster register (code, name, crop, woreda, zone, water source, area, smallholders…), created through an intake form and changed through change requests, both approved in AWE. `CLUSTER_ENROLLED` carries only the cluster ID |
| **Crop-change link** | Platform, CSR | A context can name the context it replaces (`replaces_crop_season_id` in the payload). The old one is closed and both point to each other, in the projection, the reporting view, the DCI crop-season record and the staff UI |
| **Verification in DCI** | CSR | Activities carry `verification_status`; the crop-season state carries `sowing_verified`, `harvest_verified` and `pending_verification_count` |
| **Sample data** | Platform, CSR, FR | See below |

**Sample data, shared by convention.** The Farmer and Crop Sown registries never read each other's database. Both derive sample IDs from Master Data's sample people (the country pack's `samples/individuals.json`):
- sample person `ETH-IND-0007` is farmer `FR-0007`;
- their plots are `LAND-0007-1`, `LAND-0007-2`… in their woreda.

The Farmer Registry's loader numbers each sample person's lands this way. The Crop Sown Registry loads its samples in two parts:
- **db-seed** loads two sample clusters;
- **a platform Celery task** (`activity_sample_data_worker`, on when `activity_load_sample_data` is set) asks the domain service for sample steps (`sample_activities`) and records them once through the normal write path. Each step is idempotent, and may be verified or corrected.

CSR's samples are past and current Meher and Belg seasons for the adult sample people, at every stage, including:
- an infestation and a drought;
- verified and pending sowings;
- one correction;
- cluster enrolments.

### Phase 2 (proposed)

1. **Verification model** (section 2), common to both kinds: verification policies per field, section, record or activity type; verification records with evidence; derived status; automatic verification from trusted sources; status in DCI. It absorbs today's verification table and activity verification columns.
2. **Corrections and disputes.** Entity corrections marked as such on change requests; disputes; a change or correction resets verification.
4. **Shared capabilities on entity registers:** named-level locations, reference rules, idempotent ingestion.
5. **Beneficiary API for occurrences** (it needs beneficiary authentication on the registry first), and **DCI subscribe/notify** for occurrence events.

## 8. Decisions

| Question | Decision |
|---|---|
| Certificates and programme approval in the registry? | **No.** The registry keeps records authentic and verified; whether a programme acts on them (approval, eligibility, payment) is decided in PBMS. VC issuance stays as it is, outside the verification model |
| Participant IDs: plain or typed? | **Always typed** (register or system + ID). The activity type supplies the type; submitters send role + ID |
| Occurrences changing entities (birth creates an animal, sale changes the owner)? | **No: entities first.** The entity register is updated before any occurrence about the entity is recorded |
| Verification vocabulary | `NOT_REQUIRED`, `PENDING`, `VERIFIED`, `FAILED`, `DISPUTED`. Occurrence lifecycle as built (`ACTIVE`, `SUPERSEDED`, `VOIDED`; FHIR `amended` / `entered-in-error`) |
| What is verified | **Configurable per case:** a field, a section, the whole record, or an activity type. Each target says whether it is required, which methods count, who verifies and what evidence |
| Temporary IDs | **Withdrawn.** Offline capture registers the entity first |
| Sample data across registries | **Shared by convention, not by reading another registry's database.** IDs derive from Master Data's sample people |
| Cluster changes | **Approved in AWE,** like other entity registers |
| Crops in the Farmer Registry? | **Declared "main crops" only**: the crops the farmer mainly grows, from the CROP_COMMODITY list, recorded at registration. FR's Crops tab (crop records per land) is removed; what was planned, sown and harvested each season is held only in the Crop Sown Registry |
| Livestock in the Farmer Registry? | **Kept as it is** (FR's Livestock tab) until a Livestock registry exists; then reduced to a declared summary or removed |
| Land: in the Farmer Registry or its own registry? | **In the Farmer Registry for now**, as a child of the farmer; the Crop Sown Registry's plot IDs refer to it. If a land administration (cadastre) registry is set up, the parcel moves there and the Farmer Registry keeps only a reference to it (and how the farmer uses it) |

## Related

- [Register vs activity register](register-vs-activity-register.md): the two kinds as built today
- [Activity register](activity-register.md)
- [Concept notes review](activity-registry-concept-review.md): where this proposal comes from
- [Crop Sown Registry](crop-sown-registry.md)
- [How each registry is built](registry-model.md)
