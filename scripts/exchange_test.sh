#!/usr/bin/env bash
#
# exchange_test.sh
# ----------------
# One shot after installing an Agri Exchange: the operator setup, then a
# partner's end-to-end test.
#
#   1. setup_exchange.py --yes   the composite's signing key, its PM entries in the
#                                exchange and each department, its CM bindings and
#                                policies in each department (idempotent: a rerun
#                                changes nothing that is already in place)
#   2. admin credentials          PM and CM admin client secrets of the exchange,
#                                read from the cluster into this process only
#   3. partner_test.py --auto-approve
#                                the partner onboarded in PM, bound in the exchange
#                                CM, a consent and the signed query
#
# Requires: kubectl (current context, or --context), python3 with
# scripts/requirements.txt installed (or PYTHON=path/to/venv/bin/python).
#
# USAGE:
#   ./exchange_test.sh [--namespace agrix] [--base-domain agrix.openg2p.org]
#                      [--registry farmer-registry=trial] [--registry crop-sown-registry=dept1]
#                      [--partner bank-a] [--consent-id <ID>] [--context <ctx>]
#                      [--setup-only] [--test-only] [--dry-run]
#
#   --registry      repeatable; default farmer-registry=trial and crop-sown-registry=dept1
#   --consent-id    query with a consent collected in the partner portal
#   --setup-only    only step 1;  --test-only  only steps 2-3
#   --dry-run       step 1 as a dry run (no writes) and stop
#
# Exit code: that of the step that failed, else partner_test.py's (0 ok).

set -euo pipefail

NAMESPACE="agrix"
BASE_DOMAIN=""
PARTNER="bank-a"
CONSENT_ID=""
CONTEXT=""
REGISTRIES=()
SETUP=true
TEST=true
DRY_RUN=false
PYTHON="${PYTHON:-python3}"
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { sed -n '2,32p' "$0"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    --base-domain)  BASE_DOMAIN="$2"; shift 2 ;;
    --registry)     REGISTRIES+=("$2"); shift 2 ;;
    --partner)      PARTNER="$2"; shift 2 ;;
    --consent-id)   CONSENT_ID="$2"; shift 2 ;;
    --context)      CONTEXT="$2"; shift 2 ;;
    --setup-only)   TEST=false; shift ;;
    --test-only)    SETUP=false; shift ;;
    --dry-run)      DRY_RUN=true; TEST=false; shift ;;
    -h|--help)      usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done
BASE_DOMAIN="${BASE_DOMAIN:-$NAMESPACE.openg2p.org}"
[[ ${#REGISTRIES[@]} -eq 0 ]] && REGISTRIES=("farmer-registry=trial" "crop-sown-registry=dept1")

KUBECTL=(kubectl -n "$NAMESPACE")
[[ -n "$CONTEXT" ]] && KUBECTL=(kubectl --context "$CONTEXT" -n "$NAMESPACE")

_blue() { printf "\033[34m%s\033[0m\n" "$*" >&2; }

if [[ "$SETUP" == true ]]; then
  _blue "==> 1. Exchange setup ($NAMESPACE; ${REGISTRIES[*]})"
  args=(--namespace "$NAMESPACE")
  for r in "${REGISTRIES[@]}"; do args+=(--registry "$r"); done
  [[ -n "$CONTEXT" ]] && args+=(--context "$CONTEXT")
  if [[ "$DRY_RUN" == true ]]; then args+=(--dry-run); else args+=(--yes); fi
  "$PYTHON" "$HERE/setup_exchange.py" "${args[@]}"
fi

if [[ "$TEST" == true ]]; then
  _blue "==> 2. Admin credentials ($NAMESPACE)"
  secret() {
    "${KUBECTL[@]}" get secret "$1" -o jsonpath='{.data.client_secret}' | base64 -d
  }
  PM_ADMIN_CLIENT_SECRET="$(secret commons-services-staff-portal)"
  CM_ADMIN_CLIENT_SECRET="$(secret consent-manager)"
  export PM_ADMIN_CLIENT_SECRET CM_ADMIN_CLIENT_SECRET
  echo "   PM and CM admin client secrets read (not shown)" >&2

  _blue "==> 3. Partner test ($PARTNER at $BASE_DOMAIN)"
  args=(--base-domain "$BASE_DOMAIN" --partner "$PARTNER" --auto-approve)
  [[ -n "$CONSENT_ID" ]] && args+=(--consent-id "$CONSENT_ID")
  "$PYTHON" "$HERE/partner_test.py" "${args[@]}"
fi
