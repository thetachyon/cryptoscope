#!/usr/bin/env bash
# Deploy all 16 Cryptoscope scopes to Fly.io — DRAFT, NOT RUN.
#
# Reviewed by the parent before first use. Requires a working flyctl auth
# (fly auth login) with access to the account/team that will own the apps.
# Nothing here is executed by the deploy-prep work; run it by hand.
#
# What it does, per scope (ethscope, sepoliascope, optimismscope, gnosisscope,
# solscope, bitscope, walletscope, usdtscope, usdcscope, daiscope, usdescope,
# fdusdsscope, pyusdsscope, tusdscope, usddscope, overview):
#   1. Creates the Fly app named exactly after the scope, if it does not
#      already exist under this account/team.
#   2. Deploys it from the app directory with a remote builder
#      (fly deploy --remote-only; no local Docker needed).
#
# App names are globally unique. If a name is already taken by someone else,
# the script FAILS LOUDLY naming the collision and stops — it never renames
# an app silently, because every sibling/cross-link URL in the repo is
# https://<scope>.fly.dev and a rename would break them all.
#
# No secrets are required: the apps are keyless and degrade gracefully when
# an upstream is unreachable. Do not add secrets here without review.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SCOPES=(
  ethscope sepoliascope optimismscope gnosisscope
  solscope bitscope walletscope
  usdtscope usdcscope daiscope usdescope
  fdusdsscope pyusdsscope tusdscope usddscope
  overview
)

command -v flyctl >/dev/null 2>&1 || {
  echo "ERROR: flyctl not found. Install it first (https://fly.io/install.sh)." >&2
  exit 1
}

# Refuse to run without auth; 'fly auth whoami' fails when logged out.
if ! flyctl auth whoami >/dev/null 2>&1; then
  echo "ERROR: flyctl is not authenticated. Run 'flyctl auth login' first." >&2
  exit 1
fi

fail() { echo "ERROR: $*" >&2; exit 1; }

for scope in "${SCOPES[@]}"; do
  echo "=== $scope ==="

  # Does this account/team already own an app of this name?
  if flyctl apps list --json 2>/dev/null | grep -q "\"name\": *\"$scope\""; then
    echo "App '$scope' already exists; deploying into it."
  else
    echo "Creating app '$scope'..."
    # If the name is taken by someone else (or creation fails for any other
    # reason), stop right here and say so — never rename, never continue.
    if ! flyctl apps create "$scope" 2>&1 | tee "/tmp/fly-create-$scope.log"; then
      fail "could not create Fly app '$scope' (see /tmp/fly-create-$scope.log). If the name is taken by another account, pick a resolution by hand — do not rename the scope."
    fi
    # Double-check the app we now own really is the one just created.
    flyctl apps list --json 2>/dev/null | grep -q "\"name\": *\"$scope\"" \
      || fail "app '$scope' was not found after creation; refusing to deploy."
  fi

  # Deploy from the app directory so fly.toml + Dockerfile resolve locally.
  (cd "$scope" && flyctl deploy --remote-only) \
    || fail "deploy of '$scope' failed; fix it before continuing to the next scope."
done

echo "All ${#SCOPES[@]} scopes deployed."
echo "Verify each: https://<scope>.fly.dev/healthz should return {\"status\":\"ok\"}."
