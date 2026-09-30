<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Register model: record kinds, trust and corrections (design note)

*Design note, 30 September 2026. A proposal: sections say what exists today and what is proposed.*

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
- **Trust:** verification, approval and attestation, held as **trust records** attached to it (section 2). Today these are columns on the row.

So an error is fixed by a **new** occurrence that points to the old one. A change in reality is simply **another** occurrence. Both old records stay readable, and anything already paid or certified on the original can be reversed and reissued against the correction.

### What is common to both kinds

These should work the same for entities and occurrences, and some of them are built for only one kind today:

| Capability | Entity today | Occurrence today | Proposed |
|---|---|---|---|
| **Trust** (approval, verification, attestation) | Change request approval (AWE); a verification table tied to change requests | Verification columns on the activity | **One trust layer** (section 2) |
| **Corrections** | Change request → new version + history | Supersede / void, with a reason | **One correction model** (section 3) |
| **Forms** | Sections and tabs of widgets | Generated from each type's JSON Schema | Either works for either kind. Allow JSON-Schema forms on entity sections, and section layouts on activity types (`section_ui_schema` exists) |
| **Reference rules** (code list, geography, local record, external ID; strict / lenient) | Widget-bound code lists only | Built | Available to entity registers too |
| **Location as named levels** (`geo_dimensions`) | `geo_code_hierarchy_json` on the record | Built | Named levels on entity records too, for area statistics |
| **Idempotent ingestion, submission IDs, schema versions** | No | Built | Both kinds |
| **Participants with roles** | Parent links only | One subject + payload references | **Typed participants** (section 4) |
| **Sharing** (DCI, consent, data policies) | Built | Built | Both kinds, plus trust status (section 6) |

## 2. Trust layer

### Three trust steps

Three questions, often confused, each switched on **per register, per activity type and, where needed, per field**:

| Step | Question | Who or what performs it | Typical use |
|---|---|---|---|
| **Verification** | Is there enough evidence that it's true, or happened as reported? | A person (supervisor, DA), a system check (a Fayda lookup), or a **trusted source** by itself (an authenticated vet system, a biometric device) | Sowing seen on a geo-tagged photo; a farmer's FAN matched in Fayda |
| **Approval** | Will the programme stand behind it and act on it? | An authorised person, through a workflow (AWE) | A sowing that triggers a subsidy; a harvest used as an official figure; a change to a farmer's record |
| **Attestation** | Does an authority vouch for it, in a form others can check? | An issuer, as a **verifiable credential** (a certificate) | Crop certificate from a verified sowing; vaccination certificate; land certificate from a land record |

These apply to **both kinds**:
- verifying a land parcel's size is verification on an entity field;
- approving a change request is approval on an entity change;
- a vaccination certificate is attestation on an occurrence.

### The model (proposed)

**Trust policy**, set in configuration per register, per activity type, and **per section** for entity registers (decided; sections match today's forms and verification table):
- which steps are **required**, and **for what**:
  - `record`: the record needs it to be accepted;
  - `use:<purpose>`: needed only before a use such as `use:payment` or `use:certificate`;
- **who may perform** each step: roles, and **trusted sources**, i.e. channels or partners whose authenticated submissions verify automatically;
- **what evidence** is expected (a photo, a document, a device capture, a signature).

Approval is **off unless a policy turns it on**, at the point where the programme will act on the data. Certificates are issued only from occurrences that happened, never from plans: a plan is an intention (decided).

**Trust records** are attached to a target: an entity record, a section of it, a field, a change request, or an occurrence. Each one holds:
- **step** (verification, approval, attestation) and **outcome** (confirmed / rejected / revoked);
- **actor** (person, system or source) and **method** (manual, trusted-source, system check);
- **evidence** references (documents, photos, device records);
- **remarks**, and **when**.

There can be several per target: a verification, then a dispute, then a supervisor's confirmation.

**Trust status** is a record's current trust, *derived* from its trust records (e.g. verified, not approved). It's what the UI shows, and what APIs filter and share. The vocabulary follows existing standards where there is one:

| Step | Statuses | Source |
|---|---|---|
| **Verification** | `NOT_REQUIRED`, `PENDING`, `VERIFIED`, `REJECTED`; `DISPUTED` while a dispute is open | Today's activity verification statuses, plus a dispute state |
| **Approval** | `NOT_REQUIRED`, `PENDING`, `APPROVED`, `REJECTED`, `CANCELLED` | The platform's existing approval statuses (change requests, AWE) |
| **Attestation** (certificate) | `ACTIVE`, `SUSPENDED`, `REVOKED` | W3C Verifiable Credentials status lists (revocation, suspension) |
| **Occurrence lifecycle** | `ACTIVE`, `SUPERSEDED`, `VOIDED` | As built. HL7 FHIR equivalents: `amended` / `corrected` (superseded), `entered-in-error` (voided) |

**One summary label** is shown to users: the highest step reached, i.e. **Recorded → Verified → Approved → Attested**, with a flag for *rejected*, *disputed* or *revoked*.

**Certificates:** an attestation creates a certificate ID and issues a verifiable credential. The credential names the subject, the facts attested and the record it points to. It is:
- **revoked** when that record is superseded or voided (occurrence), or changed (entity);
- **reissued** only after the correcting record meets the same trust policy.

**Mapping from today:**
- `g2p_register_verifications` (entities) and the verification columns on activities (occurrences) both become **trust records**;
- AWE approval of change requests becomes the **approval** step for entity changes;
- the agent portal's VC issuance becomes the **attestation** step for both kinds.

Existing behaviour is the default policy, so Farmer Registry and NSR behave as before.

## 3. One correction model

A **correction** is a request to fix data, always with a **reason**. It is applied **directly** (with the right permission) or **after approval**, as the policy says. How it lands depends on the kind:

| Situation | Entity | Occurrence |
|---|---|---|
| **A value was wrong** | Change request → new version; history keeps the old one | Correcting occurrence **supersedes** the old one; the old one stays |
| **It shouldn't exist** | Deactivate / archive | **Void** ("it didn't happen"); it stays |
| **Needs approval?** | Change requests go through AWE, as today | Per activity type (**new**): the correction waits for approval before it supersedes |
| **Disputed** (the subject says it's wrong) | A dispute recorded as a trust record; the outcome is a change request or a rejection | Same: a dispute trust record, resolved by a correction or by confirming the original |
| **What happens downstream** | Certificates on changed fields are revoked and reissued | Certificates on the superseded occurrence are revoked and reissued from the correction |

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
- **Temporary IDs conflict with it.** Temporary IDs (`TMP-…` plots created offline and resolved later) let an occurrence arrive before its entity. Under this rule, offline capture must register the new plot first, in the same sync, before the occurrence. Temporary IDs should be withdrawn, or kept only for a capture tool that submits the entity first.
- **The history and the current state can be traced in both directions.** The occurrence (e.g. the sale) is the history of what happened; the entity's current state (the owner) comes from its own change request. Each can point to the other, through the change request's reference and the occurrence's participants.

## 5. How the three registries fit

### Farmer Registry (entities; can hold occurrences later)

| Register | Kind | Trust policy | Corrections |
|---|---|---|---|
| Farmer | Entity | Approval of changes (AWE), as today. **Verification** of the Fayda FAN by a system check (Fayda lookup: trusted source) | Change request → new version |
| Land (child of Farmer) | Entity | Verification of size and tenure (survey, document). **Attestation**: land certificate | Change request; the land certificate is revoked and reissued on change |
| Household, members | Entity | As Farmer | Change request |
| *(later)* Farm visits, trainings | Occurrence | Verification optional | Supersede / void |

Nothing changes for the Farmer Registry unless it adopts field verification or certificates. Its existing verification table and AWE approvals become the default trust policy.

### Crop Sown Registry (occurrences, plus a cluster entity)

| Register | Kind | Trust policy | Notes |
|---|---|---|---|
| CropSown | Occurrence. Types: Planned, Land prepared, Sown, Growth observed, Infestation reported, Damage reported, Harvested. Context: plot × crop year × season × crop | **Verification:** Sown and Harvested, by a supervisor from photo evidence (as today); Infestation optionally. **Approval** (new): Sown `use:subsidy`, Harvested `use:official-figure`; off otherwise. **Attestation** (new): crop certificate from a verified (and, where required, approved) sowing | Participants: farmer (primary), plot, development agent. The farmer and plot are external references to the Farmer Registry, checked for format. Location = the plot's woreda |
| **Cluster** (proposed) | **Entity** | Approval of changes | Holds the cluster's attributes and membership, which the `CLUSTER_ENROLLED` activity carries today. Clusters and membership are maintained here first; activities only refer to the cluster. Cluster totals are derived from the plot activities |

**Corrections:**
- a wrong area → a sowing that supersedes the old one;
- a sowing that didn't happen → void;
- if a certificate or subsidy was issued, it is revoked and reissued from the correction.

**Resurvey:** a correction supersedes; a later observation is a new Growth observed.

### Livestock Registry (an entity and occurrences in one registry)

| Register | Kind | Trust policy | Notes |
|---|---|---|---|
| Animal | Entity (animal ID, species, sex, date of birth, current owner) | Approval of changes | The current owner is authoritative entity data |
| Veterinarian (or a provider registry) | Entity | — | May be in another registry |
| Livestock events | Occurrence. Types: Born, Vaccinated, Treated, Sold. Context: the animal | **Vaccinated:** verified by a **trusted source** (the vet's authenticated system); approval only `use:campaign-payment` or `use:coverage-figure`; attestation: vaccination certificate. **Sold:** approval when it's an official transfer. **Born:** off unless a birth certificate is issued | Participants: animal (primary), farmer, vet (and dam for Born) |

**Entities first:**
- a newborn animal is **registered** in the Animal register, then the Born occurrence is recorded;
- a change of owner goes through the Animal register's **change request** (with its approval), and the Sold occurrence records the sale.

The occurrences are the history. Current animal status (e.g. last vaccinated, sold) is **derived** from the events in the context's projection; the authoritative owner is the Animal register's.

**Corrections:**
- a wrong batch or dose → a vaccination that supersedes the old one (approval if the type requires it); the certificate is revoked and reissued;
- a sale that didn't happen → void the Sold occurrence, and correct the owner through the Animal register's own change request.

## 6. APIs: what changes

### Today

| API | Entities | Occurrences |
|---|---|---|
| **Staff** | Register data, metadata, sections and tabs; change requests; `/verifications` (get / add, tied to change requests); intake forms; ingestion and outgestion config | `/activity/*`:<br>• append, supersede, void, verify, reject;<br>• search, timeline, contexts;<br>• work list, projections, indicators, aggregates;<br>• locks, temporary references;<br>• `get_subject_activities` (profile tab), `get_latest_activity`, schema versions. |
| **Partner** | `/partner/ingest_data` (becomes change requests); `/dci/registry/sync/search` (records) | `/partner/activity/append_activities`; DCI search of activities, context state (`…:CropSeason`) and aggregates |
| **Beneficiary** | `/beneficiary_portal`: own registers and sections | — |
| **Agent portal** | VC issuance and verification (register records) | — |

### Proposed changes

**Staff API**

| Area | Change |
|---|---|
| **Trust (new, common)** | `/trust/*`:<br>• get the trust policy for a register, type or field;<br>• add a trust record (verify, approve, reject, attest, dispute) on any target (record, section, field, change request, occurrence), with evidence;<br>• list a target's trust records and trust status;<br>• list items awaiting a trust step (a work queue per step).<br>`/verifications/*` and `/activity/verify_activity`, `reject_activity` remain as shortcuts onto it. |
| **Corrections (common)** | `/corrections/*`:<br>• submit a correction on any target, with a reason and the corrected data;<br>• list pending corrections;<br>• decide one.<br>Entity corrections become change requests (existing flow); occurrence corrections become supersedes, held for approval when policy requires. `supersede_activity` and `void_activity` remain. |
| **Participants** | `append_activity` / `append_activities` accept `participants[]` (role and ID; the type comes from configuration). `search_activities` filters by participant and role. The profile tab covers every role, not only the subject. |
| **Certificates** | Issue, revoke and list certificates for a record or occurrence, reusing the agent portal's VC issuance, which the staff API calls |
| **Entity registers** | Locations as named levels on records; reference rules on entity sections; idempotency keys on entity ingestion |

**Partner API**

| Area | Change |
|---|---|
| **Corrections by partners (new)** | `/partner/activity/correct_activities`: supersede or void with a reason, same signed envelope. Today partners can only append. Entity corrections continue through `/partner/ingest_data` as change requests. |
| **Trusted-source submissions** | Partner Management marks a partner as a **trusted source** for given activity types. Its signed submissions record an automatic verification. A payload may carry a **signed attestation** (e.g. the vet's signature), stored as a trust record. |
| **Participants** | `append_activities` accepts `participants[]` |
| **DCI: trust in responses** | Records, activities, crop-season state and aggregates carry their **trust status** under a `trust` key, clamped like any other scope. Searches can require a status, e.g. only **approved** sowings, the filter a subsidy scheme needs. |
| **DCI: subscribe / notify** | Subscribe to occurrence events (appended, superseded, voided, verified, approved), published from the existing outbox, so a programme hears when a sowing is approved or corrected |
| **Certificates** | No registry API needed: partners check certificates with the VC verifier (agent portal / Inji Verify) |

**Beneficiary API**

| Area | Change |
|---|---|
| **Own occurrences (new)** | A farmer's own activities, crop seasons (context state) and summaries |
| **Own certificates (new)** | List and fetch; hand over to a wallet (existing VC flow) |
| **Disputes (new)** | Dispute a record or occurrence about oneself, recorded as a trust record, for staff to resolve through a correction or a confirmation |

**Agent portal API:** extend VC issuance from register records to occurrences (crop, vaccination and training certificates).

**Compatibility.** All of this is additive:
- existing endpoints keep working;
- the default trust policy reproduces today's behaviour (AWE approvals on change requests; verification only where `requires_verification` is set);
- Farmer Registry and NSR need no change until they adopt a new capability.

## 7. Platform changes, in order

1. **Trust layer.** Trust policies, trust records and trust status for both kinds, absorbing today's verification table and activity verification columns. This includes automatic verification from trusted sources, and trust status in DCI.
2. **Correction model.** A common correction request with approval by policy, and disputes. Plus partner corrections for occurrences.
3. **Certificates.** Attestation through VC issuance for occurrences as well as entities, with revoke and reissue on correction.
4. **Participants.** Named, indexed roles on occurrences; the subject becomes the primary role.
5. **Entities first.** Existence checks for referenced entities; withdraw temporary IDs, or limit them to capture tools that register the entity first.
6. **Cluster as an entity in the Crop Sown Registry,** and the crop-change link.
7. **Shared capabilities on entity registers:** named-level locations, reference rules, idempotent ingestion.
8. **Beneficiary API for occurrences,** and DCI subscribe/notify for occurrence events.

## 8. Decisions

| Question | Decision |
|---|---|
| Trust policy on entity registers: per field or per section? | **Per section** |
| Certificates for plans? | **No.** Certificates only for occurrences that happened |
| Participant IDs: plain or typed? | **Always typed** (register or system + ID). The activity type supplies the type; submitters send role + ID |
| Occurrences changing entities (birth creates an animal, sale changes the owner)? | **No: entities first.** The entity register is updated before any occurrence about the entity is recorded |
| Trust status vocabulary | From standards where they exist: the platform's approval statuses, W3C VC status lists for certificates, FHIR equivalents for the occurrence lifecycle. One summary label: Recorded → Verified → Approved → Attested (section 2) |

**Still open:** withdrawing temporary IDs (section 4) versus keeping them for capture tools that register the entity first.

## Related

- [Register vs activity register](register-vs-activity-register.md): the two kinds as built today
- [Activity register](activity-register.md)
- [Concept notes review](activity-registry-concept-review.md): where this proposal comes from
- [Crop Sown Registry](crop-sown-registry.md)
- [How each registry is built](registry-model.md)
