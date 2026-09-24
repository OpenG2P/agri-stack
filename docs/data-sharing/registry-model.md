<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# How each registry is built

Registries run on the OpenG2P registry platform. The platform today supports only a register and its child tables; the plan extends it to **two kinds plus child tables**. Something that earlier looked like a third "reference" kind is simply a register with the public-read switch turned on.

| Kind | Behaviour | Platform features | Examples |
|---|---|---|---|
| **Register** | Changeable, governed entities, person or not | Change requests + AWE, history, functional ID, dedup (can be switched off), DCI, optional public read | Farmer, household, DA, seed variety, training session |
| **Table** | Child rows owned by a register record | Inherits from its parent | Land parcels, household members |
| **Event** | Append-only facts. A correction supersedes the earlier event; nothing is edited in place. | Idempotency key, occurred/recorded time, external references, batch entry, time partitioning. No change requests or history tables. | Sown, harvested, attended, paid |

## Projections

A projection is current state computed from events: for example, the current crop stage per plot per season, or a beneficiary's 360 view.

- **Per-subject projections** (current crop stage, 360 view) are updated in the same database transaction as the event, so they are always exact.
- **Aggregates for dashboards** are updated asynchronously. Each change is recorded in an outbox and the projection is recomputed idempotently from it; lag alerts and a nightly reconciliation job catch anything that falls behind. Any projection can be rebuilt from the events.

## Layer 2 split

- **Master Data Service** keeps simple code lists and geography (admin areas, and boundaries stored in MinIO). It gains approvals through AWE, audit, history, a public read API and a change feed.
- **Agri Reference Registry** holds entities rich enough to need their own form, such as seed varieties, input products and breeds. It is a registry instance with public read turned on.
- Codes are defined by the country; international classifications are optional mappings. Publish code lists in SKOS-shaped JSON-LD, and boundaries through OGC API – Features.

## Pilot order

1. **Attendance:** proves standalone events.
2. **Crop sown:** proves stages and seasons.
3. **Integrated Beneficiary Registry:** proves ingestion from several sources, plus projections.
