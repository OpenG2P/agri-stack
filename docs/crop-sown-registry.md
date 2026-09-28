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
- **The plot** is a Farmer Registry land record. The plot, farmer and DA are held by other registries, so they are checked for **format only** and never block an entry. A plot created offline (`TMP-…`) is recorded and resolved later.

## Activity types

| Type | Records | Rules |
|---|---|---|
| `PLANNED` | Variety, planned area, cropping system, planned sowing date (Ethiopian calendar), planned seed and fertilisers, expected yield | Once per crop season |
| `LAND_PREPARED` | Method (oxen / tractor / manual / zero tillage), area, irrigation source and method, soil fertility | Once; expected after `PLANNED` (warning) |
| `SOWN` | Area sown, variety, seed type and source, seed kg, sowing method, fertilisers (type + kg), compost/manure, machinery, geo-tagged photo | Once; expected after `PLANNED` (warning); due 0–60 days after planning; **verified by a supervisor** |
| `CLUSTER_ENROLLED` | Cluster ID and name, agro-ecological zone, cluster area, smallholders, water source | Once |
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

## Farmer's season summary

For each farmer, crop year and season, the Crop Sown Registry keeps a summary across all their plots and crops. It is an aggregate, `FARMER_SEASON_SUMMARY`, with period key `<crop year>|<season>`. It holds:
- crop seasons and plots;
- planned, sown and harvested area;
- quantity harvested and yield;
- infestations and damage reports;
- a breakdown by crop.

The period runs over the Ethiopian crop year, Meskerem 1 to the last day of Pagume.

The summary is recomputed from the crop-season projections after every change, including corrections and voids, and each value is kept in its history.

## Channels

- **Staff portal:** single entry or batch.
- **Partner systems:** e.g. a cooperative, via the signed partner API.
- **ODK Central:** a `crop_sowing` form whose submissions are pulled every few minutes. The ODK instance ID is the idempotency key; photos are stored as documents; failed submissions are kept for review.

## DCI

`reg_type = CropSown` returns current activities. The consent scopes are the record's top-level keys:
- `activity`
- `crop_season`
- `measures`
- `farmer_reference`
- `location`
