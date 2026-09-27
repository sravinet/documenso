#!/usr/bin/env bash
# Mint the focused provisioner credential for deploy/scaleway, from a
# high-privilege bootstrap profile (for example an owner key), then stop using
# that profile.
#
#   scripts/bootstrap-provisioner.sh <bootstrap-profile> [expiry-days]
#
# Creates, idempotently:
#
#   project     documenso-control       Pulumi state bucket; the key's default project
#   application documenso-provisioner
#     └── policy documenso-provisioner
#           rule 0 (Organization): ProjectManager, IAMApplicationManager, IAMPolicyManager
#           rule 1 (all Projects): the products the stacks declare, and nothing else
#   API key     expiring after [expiry-days] (default 7)
#   scw profile `documenso`              holding that key
#   bucket      documenso-pulumi-state-<org8>, versioned, in documenso-control
#
# The provisioner cannot be fully confined: IAMPolicyManager can grant any
# permission. What this narrows is everything else — no users, groups, billing,
# instances or Kubernetes — and how long the key lives.
#
# The generated secret key is written straight into the scw config (0600) and
# never printed.

set -euo pipefail

BOOTSTRAP_PROFILE="${1:?usage: $0 <bootstrap-profile> [expiry-days]}"
EXPIRY_DAYS="${2:-7}"
TARGET_PROFILE="documenso"

scw_b() { scw -p "$BOOTSTRAP_PROFILE" "$@"; }

command -v scw >/dev/null && command -v jq >/dev/null || {
  echo "scw and jq are required" >&2
  exit 1
}

ORG="$(scw_b config get default-organization-id)"
REGION="$(scw_b config get default-region)"
REGION="${REGION:-fr-par}"

ensure_project() {
  local id
  id="$(scw_b account project list name=documenso-control -o json | jq -r '.[0].id // empty')"
  if [ -z "$id" ]; then
    id="$(scw_b account project create name=documenso-control \
      description="Documenso deploy control plane: Pulumi state and the provisioner's default project." \
      -o json | jq -r .id)"
  fi
  echo "$id"
}

ensure_application() {
  local id
  id="$(scw_b iam application list name=documenso-provisioner -o json | jq -r '.[0].id // empty')"
  if [ -z "$id" ]; then
    id="$(scw_b iam application create name=documenso-provisioner \
      description="Provisions Documenso foundation and 48h session stacks (deploy/scaleway)." \
      tags.0=documenso -o json | jq -r .id)"
  fi
  echo "$id"
}

CONTROL_PROJECT="$(ensure_project)"
APPLICATION="$(ensure_application)"

ORG_SETS=(ProjectManager IAMApplicationManager IAMPolicyManager)
PRODUCT_SETS=(
  ContainersFullAccess
  ContainerRegistryFullAccess
  RelationalDatabasesFullAccess
  ObjectStorageFullAccess
  VPCFullAccess
  PrivateNetworksFullAccess
  TransactionalEmailFullAccess
  DomainsDNSFullAccess
)

if [ -z "$(scw_b iam policy list application-ids.0="$APPLICATION" -o json | jq -r '.[] | select(.name == "documenso-provisioner") | .id')" ]; then
  args=(name=documenso-provisioner application-id="$APPLICATION" tags.0=documenso
    description="Documenso provisioner. No users, groups, billing, compute or Kubernetes.")
  for i in "${!ORG_SETS[@]}"; do args+=("rules.0.permission-set-names.$i=${ORG_SETS[$i]}"); done
  args+=("rules.0.organization-id=$ORG")
  for i in "${!PRODUCT_SETS[@]}"; do args+=("rules.1.permission-set-names.$i=${PRODUCT_SETS[$i]}"); done
  args+=("rules.1.organization-id=$ORG")
  scw_b iam policy create "${args[@]}" -o json >/dev/null
fi

EXPIRES_AT="$(date -u -v+"${EXPIRY_DAYS}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+${EXPIRY_DAYS} days" +%Y-%m-%dT%H:%M:%SZ)"

KEY_JSON="$(scw_b iam api-key create application-id="$APPLICATION" default-project-id="$CONTROL_PROJECT" \
  expires-at="$EXPIRES_AT" description="documenso provisioner" -o json)"

scw config profile activate default >/dev/null 2>&1 || true
scw -p "$TARGET_PROFILE" config set \
  access-key="$(jq -r .access_key <<<"$KEY_JSON")" \
  secret-key="$(jq -r .secret_key <<<"$KEY_JSON")" \
  default-organization-id="$ORG" \
  default-project-id="$CONTROL_PROJECT" \
  default-region="$REGION" >/dev/null
unset KEY_JSON

STATE_BUCKET="documenso-pulumi-state-${ORG:0:8}"
if ! scw -p "$TARGET_PROFILE" object bucket get "$STATE_BUCKET" region="$REGION" >/dev/null 2>&1; then
  scw -p "$TARGET_PROFILE" object bucket create name="$STATE_BUCKET" region="$REGION" enable-versioning=true >/dev/null
fi

cat <<EOF
Provisioner ready.
  scw profile        $TARGET_PROFILE
  application        $APPLICATION
  control project    $CONTROL_PROJECT
  key expires        $EXPIRES_AT
  state bucket       $STATE_BUCKET

Next:
  export SCW_PROFILE=$TARGET_PROFILE
  pulumi login 's3://$STATE_BUCKET?endpoint=s3.$REGION.scw.cloud&region=$REGION&s3ForcePathStyle=true'

Then stop using the '$BOOTSTRAP_PROFILE' profile for this project.
EOF
