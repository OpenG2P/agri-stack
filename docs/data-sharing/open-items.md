<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# Open items

- **Common farmer identifier.** Confirm that every department's registry stores the same Fayda-based identifier. If each registry is a separate MOSIP relying party with its own token, records can't be joined.
- **Consent across registries.** Decide on the [consent model](consent-model.md): one consent for the partner with a grant per registry, collected by CM or by the partner, and whether the farmer may decline individual items.
- **Replay guard.** CM's replay check is per consent ID. Under the single-consent model it moves to a per-request nonce, with usage counted per consent and controller.
- **Presentation via the composite.** CM must accept a consent whose audience is the partner when a registered composite presents it.
- **Moving policies to PM.** Move `PartnerPolicy` from CM to PM, with a section per department and partner associations; `/validate` then reads from PM. CM's design docs still describe policies held in CM.
- **Crop sown fork.** The Crop Sown Registry overwrites the platform core with a modified copy and patches the ingestion worker. The genuine gaps should be fixed upstream as part of the event kind.
- **MDS partner endpoints.** They currently have no authentication decorator.
