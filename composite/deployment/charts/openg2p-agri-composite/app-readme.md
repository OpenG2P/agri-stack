# Agri Stack Composite

The **use-case composite** serves approved Agri Stack use cases, such as `loan-profile`. A partner
sends one signed request with a farmer's identifier and consent; the composite queries each
registry (Farmer Registry, Crop Sown Registry, …) over DCI, signed with its own key and carrying the
partner's consent unchanged, and returns one signed response with a status per registry. Each
registry still checks the consent with the Consent Manager; nothing is stored by the composite.

Before installing, create the composite's signing key Secret and onboard the composite (and each
partner) in Partner Management — see the composite README in the agri-stack repository.

**Use cases** are YAML configuration, not code. They are not in this form: edit `composite.useCases`
in the **Edit YAML** view of the values (one entry per use case), or point
`composite.existingUseCasesConfigMap` at your own ConfigMap. Running pods pick up changes within about
a minute, without a restart.

Full documentation: [Open Agri Stack](https://docs.openg2p.org/open-agri-stack) on the OpenG2P documentation site.
