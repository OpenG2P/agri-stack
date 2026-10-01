<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Open items

- **Common farmer identifier.** Confirm that every department's registry stores the same Fayda-based identifier. If each registry is a separate MOSIP relying party with its own token, records can't be joined.
- **Consent across registries.** Decide on the [consent model](consent-model.md): one consent for the partner with a grant per registry, collected by CM or by the partner, and whether the farmer may decline individual items.
- **Replay guard.** CM's replay check is per consent ID. Under the single-consent model it moves to a per-request nonce, with usage counted per consent and controller.
- **Presentation via the composite.** CM must accept a consent whose audience is the partner when a registered composite presents it.
- **Moving policies to PM.** Move `PartnerPolicy` from CM to PM, with a section per department and partner associations; `/validate` then reads from PM. CM's design docs still describe policies held in CM.
- **Crop sown fork.** The Crop Sown Registry overwrites the platform core with a modified copy and patches the ingestion worker. The genuine gaps should be fixed upstream as part of the [activity register](activity-register.md).
- **Activity register design.** Settle the verification, sequence, period-locking and calendar rules (see [activity register](activity-register.md#new-features-not-in-the-registry-platform-today)).
- **Crop seasons in two registries.** Crop seasons can be kept by the Crop Sown Registry, or by a register in the Farmer Registry's own extension (not built). If a country runs both, decide which one is authoritative for crop seasons, or how the two are merged when data is shared, so that sown area isn't counted twice.
- **Generic, code-free activity storage.** The Observations design keeps every type in one platform table, and adds types through an API with no code. That would let "Activity" appear under Add Register. Deferred: crop sown needs an extension either way. Revisit for simple registers such as attendance.
- **Geography of a plot in two places.** The Crop Sown Registry records the plot's woreda on the crop season; the Farmer Registry has its own location for the Land record. If they disagree, the crop season's woreda is what CSR's figures use. Decide whether CSR should check it against the Farmer Registry when it looks plots up.
- **Dashboards on the reporting views.** The Crop Sown Registry now has reporting views by region, zone and woreda; the Superset/Insights dashboards on them are not built. Views bypass data policies, so dashboard access must be limited to roles allowed to see all locations.
- **Register model design, phase 2.** Phase 1 of the [design note](register-model-design.md#7-platform-changes-in-phases) is built:
  - typed participants;
  - entities first, with temporary plot IDs withdrawn;
  - partner corrections;
  - the Cluster register;
  - the crop-change link;
  - verification status in DCI;
  - shared sample data.

  Phase 2 remains, from the [concept notes review](activity-registry-concept-review.md):
  1. a common verification model for both kinds: what to verify configurable per field, section, record or activity type; verification records with evidence; automatic verification from trusted sources; status in DCI;
  2. corrections marked as such on entity change requests, disputes, and verification reset on change;
  3. a beneficiary API for occurrences (it needs beneficiary authentication on the registry first);
  4. DCI subscribe/notify for occurrence events;
  5. a season-window date check.

  Programme approval and certificates are not registry work: PBMS decides whether a programme acts on verified records.
- **Who may search across subjects.** Activity-register aggregates can be searched without a subject (e.g. for a benefit run). Today the registry operator allow-lists those partners and their scopes in the partner API's configuration (`dci_bulk_aggregate_partners`), because there is no per-person consent for such a search. This belongs in a Partner Management policy (a programme's legal basis for bulk access, approved by the department), checked like any other policy.
- **Sample data at the registries' edges.** The CSR sample farmer and plot IDs match the Farmer Registry's only by convention (`FR-<n>`, `LAND-<n>-<k>` from Master Data's sample people). If either registry changes its sample ID rule, the other must follow.
- **Aligning with the Observations design.** Built: the profile tab, `schema_version`, `submission_id`, the asynchronous enrichment and aggregate layer, geography as named levels with indicators and reporting views by level, season windows, and aggregates over DCI. Still to do: per-type switches for enrichment and aggregation with per-stage status, a beneficiary API, and agent-app capture (offline drafts, sync badge). The Crop Sown Registry uses the aggregate layer for a farmer's season summary and has no enrichment yet; weather or satellite data would be the first. See [activity register](activity-register.md#relation-to-the-observations-design).
- **Activity register gaps.** Not yet designed:
  - bulk export API
  - file import into activity registers
  - archiving old partitions
  - agent-portal entry
  - looking up Farmer Registry farmer and plot IDs (today only their format is checked)
- **Registries read Master Data's database directly.** Registries no longer copy code lists at install; they query Master Data's code-list tables live over a database connection. That couples every registry to Master Data's schema. A public read API for code lists (see [Layer 2](registry-model.md#layer-2-split)) would replace the direct connection.
- **Ethiopia country pack.**
  - Master Data's pack loader upserts but never deletes. An existing Master Data therefore keeps retired codes, such as the old `CROP_SEASON` values `SEASON_SUMMER`, `SEASON_MONSOON` and `SEASON_WINTER`, after a reload. Retiring a code needs an `is_active` flag or a delete step.
  - `CROP_COMMODITY` still lacks enset, pulses beyond faba bean, haricot bean and chickpea, and horticulture beyond a handful of crops. The list needs review with MoA.
  - `SEED_VARIETY` is flat. Tying a variety to its crop needs typed attributes per list (below).
- **Registry platform build pins.** Fresh builds resolve SQLAlchemy 2.1, which no longer installs `greenlet`, and async database access then fails. Core now pins `sqlalchemy[asyncio] >=2.0,<2.1`.
- **Concurrent migrations.** Every API migrates on start; concurrent `CREATE TABLE`s collided and left tables missing. Core migration now takes a Postgres advisory lock. The extension's own migration (e.g. Farmer tables) still runs unlocked and can still collide; one API then logs the error while another completes the tables, as before. A lasting fix is to run migrations once, as a Helm hook Job, instead of in every API on start.
- **MDS as the single reference-data service.** Design typed attributes per list (for seed varieties, input products, breeds), AWE approvals, history, a public read API and a change feed. See [registry model](registry-model.md#layer-2-split).
- **MDS partner endpoints.** They currently have no authentication decorator.
