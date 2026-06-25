#!/usr/bin/env bash
# DESTRUCTIVE. Full-nuclear reset of all gui-agents AWS + local state.
# Use this when you want to test the setup scripts from truly scratch.
#
# Does, in order:
#   1. Terminates every EC2 instance tagged Project=gui-agents.
#   2. Waits for termination so the security group can be freed.
#   3. Deletes the AWS key pair  (default: mbabenchv2-gui-agents).
#   4. Deletes the security group (default: mbabenchv2-gui-agents-sg).
#   5. Deletes the local private key file (~/.ssh/<key-name>.pem).
#   6. Deletes infra/dispatcher/.aws_defaults.
#   7. Deletes infra/dispatcher/boxes.yaml.
#
# After this, `aws_bootstrap.sh` will CREATE new key/SG from scratch.
#
# Flags:
#   --key-name NAME  default: value from .aws_defaults, else "mbabenchv2-gui-agents"
#   --sg-name  NAME  default: value from .aws_defaults, else "mbabenchv2-gui-agents-sg"
#   --region   NAME  default: $AWS_REGION, else $AWS_DEFAULT_REGION, else us-east-1
#   -y, --yes        skip the confirmation prompt
set -euo pipefail

# Pull saved values as defaults if present.
DISPATCHER_DIR="$(cd "$(dirname "$0")" && pwd)"
AWS_DEFAULTS="$DISPATCHER_DIR/.aws_defaults"
BOXES_FILE="$DISPATCHER_DIR/boxes.yaml"
# shellcheck disable=SC1090
[[ -f "$AWS_DEFAULTS" ]] && source "$AWS_DEFAULTS"

KEY_NAME="${GUI_AGENTS_KEY_NAME:-mbabenchv2-gui-agents}"
SG_NAME="${GUI_AGENTS_SG_NAME:-mbabenchv2-gui-agents-sg}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${GUI_AGENTS_REGION:-us-east-1}}}"
YES="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-name) KEY_NAME="$2"; shift 2 ;;
    --sg-name)  SG_NAME="$2"; shift 2 ;;
    --region)   REGION="$2"; shift 2 ;;
    -y|--yes)   YES="true"; shift ;;
    -h|--help)  awk '/^set -euo/{exit} /^#[^!]/{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

PEM_PATH="$HOME/.ssh/$KEY_NAME.pem"

# ── plan ───────────────────────────────────────────────────────────────────
echo "FULL RESET (region=$REGION)"
echo "  1. Terminate all instances tagged Project=gui-agents"
echo "  2. Delete AWS key pair:  $KEY_NAME"
echo "  3. Delete security group: $SG_NAME"
echo "  4. Delete local private key: $PEM_PATH"
echo "  5. Delete $AWS_DEFAULTS"
echo "  6. Delete $BOXES_FILE"
echo

if [[ "$YES" != "true" ]]; then
  read -rp "This is destructive. Proceed? [y/N] " ans
  case "$ans" in
    [Yy]|[Yy][Ee][Ss]) ;;
    *) echo "aborted"; exit 0 ;;
  esac
fi

# ── 1. terminate instances ────────────────────────────────────────────────
echo
echo "── terminating gui-agents instances ──"
IDS=()
while IFS= read -r _line; do
  [[ -n "$_line" ]] && IDS+=("$_line")
done < <(aws ec2 describe-instances \
  --region "$REGION" \
  --filters "Name=tag:Project,Values=gui-agents" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' \
  --output text | tr '\t' '\n')
if [[ ${#IDS[@]} -gt 0 ]]; then
  echo "  terminating: ${IDS[*]}"
  aws ec2 terminate-instances --region "$REGION" --instance-ids "${IDS[@]}" >/dev/null
  echo "  waiting for 'terminated'…"
  aws ec2 wait instance-terminated --region "$REGION" --instance-ids "${IDS[@]}"
  echo "  done"
else
  echo "  no matching instances"
fi

# ── 2. delete key pair ────────────────────────────────────────────────────
echo
echo "── deleting key pair '$KEY_NAME' ──"
if aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  aws ec2 delete-key-pair --region "$REGION" --key-name "$KEY_NAME"
  echo "  deleted"
else
  echo "  not present"
fi

# ── 3. delete security group ──────────────────────────────────────────────
echo
echo "── deleting security group '$SG_NAME' ──"
SG_ID="$(aws ec2 describe-security-groups \
  --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' \
  --output text 2>/dev/null || true)"
if [[ -n "$SG_ID" && "$SG_ID" != "None" ]]; then
  if aws ec2 delete-security-group --region "$REGION" --group-id "$SG_ID" 2>err.$$; then
    echo "  deleted: $SG_ID"
    rm -f err.$$
  else
    echo "  FAILED to delete $SG_ID:"
    sed 's/^/    /' err.$$
    rm -f err.$$
    echo "  (a stray ENI or non-gui-agents instance may still reference it.)"
  fi
else
  echo "  not present"
fi

# ── 4. delete local private key ───────────────────────────────────────────
echo
echo "── deleting local .pem ──"
if [[ -e "$PEM_PATH" ]]; then
  rm -f "$PEM_PATH"
  echo "  removed $PEM_PATH"
else
  echo "  not present: $PEM_PATH"
fi

# ── 5 + 6. delete local state files ───────────────────────────────────────
echo
echo "── deleting local state ──"
for f in "$AWS_DEFAULTS" "$BOXES_FILE"; do
  if [[ -e "$f" ]]; then
    rm -f "$f"
    echo "  removed $f"
  else
    echo "  not present: $f"
  fi
done

cat <<EOF

Full reset complete. To re-run the setup from scratch:
  ./infra/dispatcher/aws_bootstrap.sh
  ./infra/dispatcher/spinup.sh --alias <name> --provider <claude|chatgpt>
EOF
