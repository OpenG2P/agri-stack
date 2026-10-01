<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Crop Sown Registry

The Crop Sown Registry (CSR) records what each farmer plans, prepares, sows, observes and harvests, plot by plot and season by season. It is a registry instance with **only an activity register** (see [Activity register](activity-register.md)). There are no record registers, no functional IDs and no change requests.

Repository: `OpenG2P/crop-sown-registry`. It is a thin extension of the registry platform, packaged like the Farmer Registry.

The Crop Sown Registry is **independent**. It shares data with the Farmer Registry (farmer and plot IDs), not code. If the Farmer Registry itself is to keep crop seasons against its Land records, that is a register in the Farmer Registry's own extension; see [Where an activity register lives](activity-register.md#where-an-activity-register-lives).

## Crop season: the activity context

**One context is one crop on one plot in one season:**

```
<plot_id>|<crop_year>|<season>|<crop>        e.g.  LND-7781|2019|SEASON_MEHER|CROP_TEFF
```

- **Intercropping** is two contexts on the same plot.
- **The subject** is the farmer (Farmer Registry ID). The Fayda FAN is carried alongside.
- **The plot** is a Farmer Registry land record. The plot, farmer and DA are held by other registries, so they are checked for **format only** and never block an entry.
- **Entities first** ([register model design](register-model-design.md#4-participants-and-entities-first)): the farmer and plot are registered in the Farmer Registry before crop activities are recorded. Temporary plot IDs (`TMP-…`) are no longer accepted; a plot found in the field is registered first.
- **Participants:** every activity records its participants in typed roles: farmer (primary), plot and development agent (Farmer Registry and other systems), and cluster (the Cluster register here). Activities can be searched by participant.
- **A changed crop** is a new crop season that names the one it replaces (`replaces_crop_season_id`). The old season is closed, and each points to the other.
- **The location** is the plot's **woreda**, chosen from Master Data's geography. It is required when a crop season is planned or sown; later activities take it from their crop season. Every activity stores it with its zone, region and country as named levels, so every figure can be rolled up by level. The registry can't read the plot's location from the Farmer Registry, which may be on another instance, so the woreda is entered.

## Activity types

| Type | Records | Rules |
|---|---|---|
| `PLANNED` | Variety, planned area, cropping system, planned sowing date (Ethiopian calendar), planned seed and fertilisers, expected yield | Once per crop season |
| `LAND_PREPARED` | Method (oxen / tractor / manual / zero tillage), area, irrigation source and method, soil fertility | Once; expected after `PLANNED` (warning) |
| `SOWN` | Area sown, variety, seed type and source, seed kg, sowing method, fertilisers (type + kg), compost/manure, machinery, geo-tagged photo | Once; expected after `PLANNED` (warning); due 0–60 days after planning; **verified by a supervisor** |
| `CLUSTER_ENROLLED` | Cluster ID only; the cluster must exist in the Cluster register | Once |
| `GROWTH_OBSERVED` | Growth stage, crop condition, area under crop, estimated yield | Repeatable; **requires `SOWN`** |
| `INFESTATION_REPORTED` | Pest / disease / weed, agent (e.g. fall armyworm, wheat rust, striga), severity, area, % damage, action, pesticide | Repeatable; **requires `SOWN`** |
| `DAMAGE_REPORTED` | Cause (drought, flood, hail, frost, wind, wildlife), area, % loss | Repeatable; **requires `SOWN`** |
| `HARVESTED` | Area harvested, quantity (quintals), yield (computed), post-harvest loss, stored / sold / consumed / kept as seed, sale price | Once; **requires `SOWN`**; due 90–180 days after sowing; **verified by a supervisor** |

**Where the fields come from:** the crop-sown sample registry, extended from:
- FAO's World Programme for the Census of Agriculture 2020 (crop module and crop-loss causes)
- Ethiopia's CSA Agricultural Sample Survey (Meher/Belg seasons, UREA/DAP/NPS fertilisers, quintals)

**Plausibility warnings** (never blocking):
- sown area more than 1.5 × the planned area
- observed or harvested area larger than the sown area
- yield above 150 qt/ha
- stored + sold + consumed + seed quantities adding up to more than the harvest

## Code lists

**All code lists live in Master Data.** The Crop Sown Registry keeps none of its own. The registry platform checks every coded field against Master Data at the time of writing, the same way the Farmer Registry's dropdowns and validation read their lists.

The lists are the **agriculture domain of the Ethiopia country pack** (`openg2p-data`, `packs/ETH/domains/agriculture`). Master Data loads that domain when installed with `geoSeed.domains: [agriculture]`.

| Used for | Lists |
|---|---|
| The crop season | `CROP_COMMODITY`, `CROP_SEASON` (Meher, Belg, Irrigation), `SEED_VARIETY` |
| Planning and land preparation | `CROPPING_SYSTEM`, `LAND_PREPARATION_METHOD`, `IRRIGATION_SOURCE`, `IRRIGATION_METHOD`, `SOIL_FERTILITY` |
| Sowing | `SEED_TYPE`, `SEED_SOURCE`, `SOWING_METHOD`, `FERTILIZER_TYPE`, `FARM_MACHINERY` |
| Clusters | `AGRO_ECOLOGICAL_ZONE`, `WATER_SOURCE` |
| Observation, infestation and damage | `CROP_GROWTH_STAGE`, `CROP_CONDITION`, `INFESTATION_TYPE`, `INFESTATION_AGENT`, `INFESTATION_SEVERITY`, `PEST_CONTROL_ACTION`, `CROP_DAMAGE_CAUSE` |

- **Codes carry their list's prefix,** such as `CROP_TEFF`, `SEASON_MEHER` and `SEV_HIGH`.
- **Adding or changing a code** is a change to the country pack, not to the registry.
- **Without the agriculture domain in Master Data,** every activity is rejected on its crop code.

The activity types, indicators and ODK mapping are **defined in one file**, `scripts/activity_definitions.py`. The seed SQL is generated from it.

CI checks two things:
- the committed SQL matches the definitions;
- every list and code the definitions use exists in the Ethiopia pack.

## Crop season status (projection)

One row per crop season:
- furthest **stage** reached (planned → land prepared → sown → growing → harvested)
- planned area and date, expected yield
- area sown, sowing date, seed type, whether sowing was verified
- whether the harvest was verified
- the crop season it replaced, or was replaced by
- latest growth stage and condition
- infestations (count, worst severity), damage reports (count, worst % loss)
- area harvested, quantity, yield, harvest date
- activities awaiting verification

## Indicators

- area sown by crop
- quantity harvested by crop
- average yield by crop
- crops by stage
- farmers reporting
- crops with infestations

Each is grouped by crop year and season.

## Reporting by geography

- **Indicators by level:**
  - area sown by region, by zone and by woreda (with crop);
  - quantity harvested by region;
  - average yield by region;
  - farmers reporting by woreda.

  They sit alongside the existing indicators by crop year, season and crop.
- **Reporting views** (in the registry's database, for Superset and Insights):

  | View | One row per |
  |---|---|
  | `csr_rpt_crop_season` | crop season, with region, zone and woreda codes and names |
  | `csr_rpt_crop_performance_region` | crop year, season, region, crop |
  | `csr_rpt_crop_performance_zone` | crop year, season, zone, crop |
  | `csr_rpt_crop_performance_woreda` | crop year, season, woreda, crop |

  The performance views count crop seasons, farmers and plots; sum planned, sown and harvested area and production; compute yield as production ÷ harvested area; and count infested and damaged crop seasons.
- **They are plain views over the crop-season projection,** which the platform keeps current, so they need no refresh.

## Farmer's season summary

For each farmer, crop year and season, the Crop Sown Registry keeps a summary across all their plots and crops. It is an aggregate, `FARMER_SEASON_SUMMARY`, with period key `<crop year>|<season>`. It holds:
- crop seasons and plots;
- planned, sown and harvested area;
- quantity harvested and yield;
- infestations and damage reports;
- a breakdown by crop.

The period is the season's window in the Ethiopian crop year, following the CSA Agricultural Sample Survey:

| Season | Window | Why |
|---|---|---|
| Meher | Meskerem – Yekatit (Sep – Feb) | Meher crops are harvested September to February |
| Belg | Megabit – Pagume (Mar – Aug) | Belg crops are harvested March to August |
| Irrigation | Hidar – Ginbot (Nov – May) | The dry season |

The summary's location is the geographic levels all of the farmer's plots share, e.g. one woreda, or only a zone when the plots span woredas. Programmes read it through DCI with record type `spdci-extensions-agri:ActivityAggregate`, keyed by farmer ID.

The summary is recomputed from the crop-season projections after every change, including corrections and voids, and each value is kept in its history.

## Cluster register

Clusters are **entities** in their own register in this registry, not activity data:
- **fields:** code (e.g. `CL-ET0406-001`), name, crop, woreda, agro-ecological zone, water source, area, smallholders, year established, coordinator;
- **changes:** created through an intake form and changed through change requests, both approved in AWE, like the Farmer Registry's registers.

A plot joins a cluster with a `CLUSTER_ENROLLED` activity. Cluster totals are derived from the plots' activities.

## Sample data

Sample data is **off by default**. For a demo, turn on two Rancher questions:
- **Load Sample Data** (`dbSeed.loadSampleData`) loads the sample clusters;
- **Load sample crop seasons** (`REGISTRY_CELERY_WORKERS_ACTIVITY_LOAD_SAMPLE_DATA`) loads the crop seasons, and is shown only when the first is on.

The crop seasons need the sample clusters, so the second question alone loads nothing.

- **Clusters:** db-seed loads two sample clusters.
- **Crop seasons:** the platform's sample task records them once, through the normal write path, after the activity types and clusters are loaded.
  - Farmers are the adults among Master Data's sample people, with the Farmer Registry's IDs: `ETH-IND-0007` → `FR-0007`.
  - Plots are `LAND-0007-1` (every farmer) and `LAND-0007-2` (every third), in the person's woreda.
  - The Farmer Registry numbers sample lands the same way, so a demo of both shows the same farmers and plots. Neither reads the other.
- **What they cover:**
  - Meher 2018 (complete, with an infestation for some);
  - Belg 2018 (complete for some farmers, with a drought);
  - Meher 2019 (growing, so harvests are due on the work list).

  Most sowings and harvests are verified; every fifth farmer's are pending. One sowing is corrected, and the farmers in a sample cluster's woreda are enrolled in it.

## Channels

- **Staff portal:** single entry or batch.
- **Partner systems:** e.g. a cooperative, via the signed partner API. A partner can correct (supersede or void) only what it submitted.
- **ODK Central:** a `crop_sowing` form whose submissions are pulled every few minutes. The ODK instance ID is the idempotency key; photos are stored as documents; failed submissions are kept for review.

## DCI

A DCI search with `reg_type = CropSown`, by farmer ID, returns one of three record types:

| `reg_record_type` | Returns | Used for |
|---|---|---|
| `spdci-extensions-agri:CropActivity` | The farmer's current activities (plans, sowings, observations, harvests), each with its verification status | Evidence, audit |
| `spdci-extensions-agri:CropSeason` | Each crop season's current state: stage, planned and sown area, seed type, whether sowing and harvest were verified, activities awaiting verification, growth and infestation status, harvest, yield, location, and the season it replaced or was replaced by | **Decisions** such as a fertiliser subsidy or a loan |
| `spdci-extensions-agri:ActivityAggregate` | The farmer's season summaries across plots and crops | Decisions on the farmer as a whole |

All three share one set of consent scopes, the record's top-level keys:
- `activity` (activities only)
- `crop_season`
- `measures`
- `farmer_reference`
- `location`

A partner's policy therefore covers all three record types the same way.

**Querying.** Every search is synchronous (`/dci/registry/sync/search`).
- **By farmer ID** (`idtype-value`): every crop season or summary of the farmer.
- **Filtered** (`expression`): `subject_id` (the farmer ID) plus filters.
  - Activities can be filtered by any plain field: `activity_type`, `occurred_at`, `verification_status`, `crop`, `plot_id`…
  - Crop seasons can be filtered by any plain projection column, e.g. `crop_year`, `season`, `crop`, `stage`.
  - Summaries can be filtered by `aggregate_type`, `period_key`, `crop_year` and `season`.
  - The operators are those of entity searches (`$eq`, `$in`, `$gte`…).

Results come newest first, so "the farmer's last 10 activities" needs no filter: `page_size: 10`. For example, "wheat sown by FR-0007 in Meher 2019" is the summary for `crop_year: 2019`, `season: SEASON_MEHER`, read at `measures.by_crop.CROP_WHEAT.area_sown_ha`. See the [composite worked example](composite.md#worked-example-wheat-sown-by-a-farmer-this-season).
