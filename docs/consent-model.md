<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Consent model: one consent for the partner, one grant per registry inside CM

> **Status: proposed, under discussion.** This replaces the earlier idea of issuing one consent object per registry, which was suggested only to fit CM's current rule of one `data_controller` per consent. The diagrams haven't been updated yet.

## The problem

A partner asks for consent to all the fields it needs. It doesn't know or care which registries hold them. The current CM model, however, allows only **one `data_controller` per consent object**, which would make partners collect a separate consent per registry.

## Proposal

**What the partner sees:** it asks once, for the fields it needs, and gets back **one consent**. It never deals with sources.

**Inside CM:** that one consent holds a **grant per registry** (data controller). Each registry validates only its own grant.

India's Account Aggregator works the same way: one consent artefact lists several data providers, and each provider checks only the accounts that belong to it.

## How it works

**1. Consent request (partner → CM).** The partner states what it needs in business terms. It doesn't name any registries.

```jsonc
POST /consent-requests
{ "subject": {"type":"fayda_token","value":"…"},
  "purpose": "credit-assessment",
  "scopes": ["farmer:profile","farmer:land","crop:season","livestock:herd"],
  "validity": "P90D", "fetch_type": "oneshot" }
```

**2. CM works out which registries are involved.** The PM policy already has **one section per department**, so it doubles as the map from scope to registry. CM also checks that the requested scopes fall within the policy.

**3. The farmer sees one screen, grouped by source.**
- Example: "Farmer Registry (Ministry A): profile, land. Crop Sown Registry: crop seasons. Livestock Registry: herd."
- The farmer authenticates with a Fayda OTP, on their own or with a DA's help.
- Listing the sources tells the farmer *who* holds the data being shared.
- If allowed, the farmer can decline individual items, so partial consent is possible.

**4. CM issues one consent artefact, signed by CM.**

```jsonc
{ "consent_id": "…", "subject": "…", "aud": "bank-a", "purpose": "credit-assessment",
  "valid_until": "…", "fetch_type": "oneshot",
  "grants": [
    { "controller": "farmer-registry",    "scopes": ["farmer:profile"] },   // land declined
    { "controller": "crop-sown-registry", "scopes": ["crop:season"] },
    { "controller": "livestock-registry", "scopes": ["livestock:herd"] }
  ] }
```

- It's stored as one consent record, with a child row per grant.
- The farmer gets **one receipt** listing all the controllers. Kantara consent receipts and ISO/IEC 27560 both allow several controllers on one receipt.
- Each department can still see every consent that touches its data.

**5. Using the consent.**
- The partner, or the composite acting for it, sends the consent ID or artefact with the request.
- Each registry calls `/validate(consent, controller = itself)`.
- CM returns **that registry's grant ∩ that registry's policy section**.
- Usage is counted **per consent and per controller**, so one-shot and periodic limits apply to each registry separately.

**6. Revocation.** The farmer can revoke the whole consent, or only one source (for example, "stop sharing my livestock data"). The partner is notified either way.

## Changes in CM

| Area | Change |
|---|---|
| Consent model | One consent with several grants, replacing the single `data_controller` field |
| `/validate` | Takes the calling registry as a parameter; returns only that registry's grant ∩ its policy section, read from PM |
| Replay guard | Today it's keyed on the consent object's ID (`jti`), which would reject the second and third registries presented with the same consent. Move replay protection to a **per-request nonce** (the DCI envelope already carries message IDs and timestamps), and count usage per consent and controller. |
| Consent screen and receipt | Grouped by source, with per-item decline if allowed |
| Intermediary presentation | Accept a consent whose audience is the partner when a registered composite presents it |

## Open questions

1. **Should consent that spans registries always be collected by CM?**
   - CM supports two ways today: the partner collects consent in its own app and signs it ("embedded"), or CM collects it through its own screen with a Fayda OTP ("originated").
   - **Recommendation:** make CM collection the default when consent spans departments. Departments are more likely to trust a consent CM witnessed than one the bank vouches for, and the farmer sees the same screen whichever partner is asking.
2. **Should the farmer be able to decline individual items?**
   - It's better for the farmer, but the partner may get less than its use case needs.
   - The composite response already reports a status per source, so a declined item shows as `denied`.
   - Alternatively, a policy could mark some scopes as mandatory, meaning the farmer must accept all of them or none.
