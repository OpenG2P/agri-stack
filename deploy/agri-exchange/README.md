# Agri Exchange (Agri X) install bundle

Installs the Agri Stack exchange into one namespace (default `agrix`), in order:

| # | Release | Chart | Exchange override |
|---|---|---|---|
| 1 | `commons` | `openg2p/openg2p-commons-base` | `commons-base.yaml.gotmpl` — Novu and Kafka UI off |
| 2 | `commons-services` | `openg2p/openg2p-commons-services` | `commons-services.yaml.gotmpl` — registry-only services, WebSub and AWE off; Consent Manager with its partner portal (toggle `partnerPortal`), in the exchange role (receipt issuer `agri-stack-exchange-cm`, presenter `agri-composite`) with your signing key |
| 3 | `agri-composite` | `openg2p/openg2p-agri-composite` | `agri-composite.yaml.gotmpl` — consent mode `exchange`, department registry URLs |

The commons charts are used unchanged; everything exchange-specific is in the
override files. The release names `commons` and `commons-services` are required
(the commons charts refer to `commons-postgresql`, `commons-keycloak`,
`commons-services-…`). Chart versions are pinned in `versions.yaml` (one tested
set per bundle version); operator settings are in `values.yaml`.

Full guide: [docs.openg2p.org → Open Agri Stack → Deployment](https://docs.openg2p.org).

## Prerequisites

- A cluster with the OpenG2P base setup (Istio with the `internal` gateway),
  DNS and TLS for `*.<namespace>.openg2p.org` (or your `baseDomain`).
- `helm`, [`helmfile`](https://github.com/helmfile/helmfile) v1 and the
  `helm-diff` plugin (`helm plugin install https://github.com/databus23/helm-diff`).
- The Consent Manager signing key Secret in the namespace (no demo key):

  ```sh
  kubectl create namespace agrix
  kubectl -n agrix create secret generic agrix-cm-signing \
    --from-file=cm_signing.p12=./cm_signing.p12 --from-literal=password='<p12 password>'
  ```

## Install / upgrade

Edit `values.yaml` (at least `cmSigningKey.secretName` and `registries`), then:

```sh
helmfile -e agrix apply
```

Each release waits for the previous one (`needs`, `wait: true`). To upgrade,
pull the new bundle (new `versions.yaml`) and run the same command;
`helmfile -e agrix diff` shows the changes first.

## Rancher (no helmfile on the cluster side)

Render the three override files with your `values.yaml`:

```sh
helmfile -e agrix write-values --output-file-template 'out/{{ .Release.Name }}.yaml'
# or the full manifests: helmfile -e agrix template
```

Then in Rancher → Apps → Charts (OpenG2P repo), in the namespace, install in
this order, each with the version from `versions.yaml`, pasting the rendered
file into "Edit YAML", and wait until each is fully up before the next:

1. `openg2p-commons-base` as release **`commons`** ← `out/commons.yaml`
2. `openg2p-commons-services` as release **`commons-services`** ← `out/commons-services.yaml`
3. `openg2p-agri-composite` as release **`agri-composite`** ← `out/agri-composite.yaml`

## After install

- Register the composite (`agri-composite`) as a partner in Partner Management
  and create its signing Secret (see the composite chart values).
- Each department Consent Manager (Farmer Registry, Crop Sown Registry installs)
  must trust this one as a receipt issuer: issuer `agri-stack-exchange-cm`,
  JWKS `https://consent-manager-partner.<baseDomain>/api/consent-manager-partner/.well-known/jwks.json`,
  presenter `agri-composite`.
