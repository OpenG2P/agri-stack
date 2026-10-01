#!/usr/bin/env bash
#
# uninstall-agri-composite.sh
# ---------------------------
# Cleanly uninstall an Agri Stack Composite Helm release and what it created.
# The composite is stateless: no database, no PVCs, no Keycloak client. What
# remains after `helm uninstall` is at most the sanity hook Job (kept for its
# logs) and, if you ask, the signing-key Secret you created yourself.
#
# Steps:
#   0. Stop in-flight Jobs (the sanity hook) so `helm uninstall --wait` does not block
#   1. helm uninstall <release>        (Deployment, Service, HPA, VirtualService, use-case ConfigMap)
#   2. Delete leftover Jobs and their Pods (hook Jobs survive helm uninstall)
#   3. Sweep leftover ConfigMaps / Secrets labelled with the release
#   4. Optionally delete the signing-key Secret (--drop-signing-secret)
#
# Not touched: Partner Management (the composite's partner and key) and Consent
# Manager bindings — they live in those services; remove them there if needed.
#
# Requires: kubectl, helm, bash 4+.
#
# USAGE:
#   ./uninstall-agri-composite.sh \
#       --namespace <ns> \
#       [--release <name>]          (default: agri-composite)
#       [--drop-signing-secret]     (also delete the signing Secret; kept by default since you created it)
#       [--signing-secret <name>]   (default: agri-composite-signing)
#       [--dry-run]                 (print actions, change nothing)
#       [--yes]                     (skip interactive confirmation)
#
# EXAMPLES:
#   ./uninstall-agri-composite.sh --namespace trial --dry-run
#   ./uninstall-agri-composite.sh --namespace trial
#   ./uninstall-agri-composite.sh --namespace trial --yes --drop-signing-secret

set -euo pipefail

RELEASE="agri-composite"
NAMESPACE=""
SIGNING_SECRET="agri-composite-signing"
DROP_SIGNING_SECRET=false
DRY_RUN=false
ASSUME_YES=false

usage() { sed -n '2,33p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)             RELEASE="$2";          shift 2 ;;
    --namespace|-n)        NAMESPACE="$2";        shift 2 ;;
    --signing-secret)      SIGNING_SECRET="$2";   shift 2 ;;
    --drop-signing-secret) DROP_SIGNING_SECRET=true; shift ;;
    --dry-run)             DRY_RUN=true;          shift ;;
    --yes|-y)              ASSUME_YES=true;       shift ;;
    -h|--help)             usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

[[ -z "$NAMESPACE" ]] && { echo "ERROR: --namespace is required"; exit 1; }

_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
_green() { printf "\033[32m%s\033[0m\n" "$*"; }
_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
_blue()  { printf "\033[34m%s\033[0m\n" "$*"; }

run() {
  # Print + execute, or only print with --dry-run. Never aborts: cleanup is idempotent.
  echo "  \$ $*"
  if [[ "$DRY_RUN" == false ]]; then
    eval "$@" || _yellow "  (command returned non-zero — continuing)"
  fi
}

_blue "==> Pre-flight checks"
command -v kubectl >/dev/null || { _red "kubectl not found"; exit 1; }
command -v helm    >/dev/null || { _red "helm not found";    exit 1; }

if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  _yellow "  Namespace '$NAMESPACE' does not exist — nothing to do"
  exit 0
fi
if helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; then
  HELM_RELEASE_EXISTS=true
  _green "  Helm release '$RELEASE' found in namespace '$NAMESPACE'"
else
  HELM_RELEASE_EXISTS=false
  _yellow "  Helm release '$RELEASE' not found — will skip helm uninstall"
fi

_blue "==> Plan"
echo "Will DELETE:"
echo "  - Helm release:        $RELEASE (namespace: $NAMESPACE)"
echo "  - Jobs/ConfigMaps/Secrets labelled app.kubernetes.io/instance=$RELEASE"
[[ "$DROP_SIGNING_SECRET" == true ]] && echo "  - Signing Secret:      $SIGNING_SECRET (--drop-signing-secret)"
echo "Will PRESERVE:"
echo "  - Partner Management partner/key and Consent Manager bindings (managed in those services)"
[[ "$DROP_SIGNING_SECRET" == false ]] && echo "  - Signing Secret '$SIGNING_SECRET' (use --drop-signing-secret to remove)"
echo
for kind in job configmap secret; do
  echo "${kind^}s (label app.kubernetes.io/instance=$RELEASE):"
  kubectl -n "$NAMESPACE" get "$kind" -l "app.kubernetes.io/instance=$RELEASE" --no-headers 2>/dev/null \
    | awk '{print "  - " $1}' || true
done
echo

[[ "$DRY_RUN" == true ]] && _yellow "DRY-RUN: no changes will be made."
if [[ "$ASSUME_YES" == false && "$DRY_RUN" == false ]]; then
  _red "Type the release name ('$RELEASE') to confirm:"
  read -r CONFIRM
  [[ "$CONFIRM" != "$RELEASE" ]] && { _red "Confirmation did not match. Aborting."; exit 1; }
fi

_blue "==> [0/4] Stop in-flight Jobs"
run "kubectl -n '$NAMESPACE' delete job -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --wait=false"

_blue "==> [1/4] Helm uninstall"
if [[ "$HELM_RELEASE_EXISTS" == true ]]; then
  run "helm uninstall '$RELEASE' -n '$NAMESPACE' --wait --timeout 5m || true"
else
  echo "  (skipped — release not present)"
fi

_blue "==> [2/4] Delete leftover Jobs and their Pods"
run "kubectl -n '$NAMESPACE' delete job -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --wait=true --timeout=2m"
run "kubectl -n '$NAMESPACE' delete pod -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found --field-selector=status.phase!=Running"

_blue "==> [3/4] Sweep leftover ConfigMaps / Secrets"
run "kubectl -n '$NAMESPACE' delete configmap -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"
run "kubectl -n '$NAMESPACE' delete secret    -l 'app.kubernetes.io/instance=$RELEASE' --ignore-not-found"

_blue "==> [4/4] Signing Secret"
if [[ "$DROP_SIGNING_SECRET" == true ]]; then
  run "kubectl -n '$NAMESPACE' delete secret '$SIGNING_SECRET' --ignore-not-found"
else
  echo "  (kept — '$SIGNING_SECRET')"
fi

echo
_green "==> Done."
[[ "$DRY_RUN" == true ]] && _yellow "    (dry-run — nothing was actually changed)"
exit 0
