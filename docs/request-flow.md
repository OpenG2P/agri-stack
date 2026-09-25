<p align="center"><img src="images/agri-stack-logo.jpg" alt="Agri Stack — DPI for Agriculture" width="120"></p>

# One request, end to end

The worked example is **Access to Credit**: a bank wants a farmer's credit profile, and the data comes from three registries.

![Sequence: bank → gateway → composite → three registries; each registry validates with CM, which reads its policy section from PM; the livestock registry times out](images/02-request-flow.svg)

In this example the livestock registry is down. The bank still gets the farmer and crop data, and the response says `livestock: unavailable` rather than failing the whole request.

## Steps

1. **Bank → gateway.** The bank calls the `credit-profile` use case with the farmer's identifier and the consent, and signs the request with its PM key.
2. **Gateway → PM.** The gateway checks in PM that the bank is associated with the policy this use case refers to (`credit-assessment`).
3. **Gateway → composite.** The request is routed to the composite, which verifies the bank's signature.
4. **Composite → registries.** The composite sends a DCI `sync/search` to each source in parallel. Each call is signed with the composite's own PM key and carries the bank's consent and original request ID unchanged.
5. **Registry → CM.** Each registry calls CM `/validate`.
6. **CM → PM.** CM checks the consent, fetches that registry's policy section from PM (cached), and returns the effective scopes.
7. **Registries → composite.** Each registry releases only the effective fields and returns a status: `ok`, `no_record`, `denied` or `unavailable`.
8. **Composite → bank.** The composite assembles the response, returns it and discards everything. Every hop emits audit events linked by the request ID, and CM records the usage so the farmer can see it.

> The diagram shows the current CM model, with one consent object per registry. See the [consent model](consent-model.md) for the proposed single consent with a grant per registry. Under that proposal, steps 4–6 pass the same consent to every registry, and each registry validates only its own grant.

## Use-case (composite) configuration

The `credit-profile` use case is defined by a configuration file loaded by the generic composite service. The file covers sources, query templates, response mapping, timeouts and the checks run before publishing. See [Use-case composite](composite.md).
