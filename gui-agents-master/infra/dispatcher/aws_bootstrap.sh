#!/usr/bin/env bash
# One-shot AWS prerequisites for gui-agents boxes. Idempotent — run it any
# time and it'll only create what's missing.
#
# Does three things:
#   1. Verifies the AWS CLI is installed and your creds work.
#   2. Creates an EC2 key pair (if one with --key-name doesn't exist) and
#      saves the private key to ~/.ssh/<key-name>.pem (0400).
#   3. Creates a security group (if --sg-name doesn't exist) and authorizes
#      SSH from your current public IP. Re-running adds your new IP if it
#      changed (old CIDRs are left alone — use --prune-ips to remove them).
#
# Prints the final --key-name / --sg-id values you hand to spinup.sh.
#
# Usage:
#   ./aws_bootstrap.sh                              # uses defaults
#   ./aws_bootstrap.sh --region us-east-1
#   ./aws_bootstrap.sh --key-name myteam --sg-name myteam-sg
#
# Flags:
#   --key-name NAME       default: mbabenchv2-gui-agents
#   --sg-name NAME        default: mbabenchv2-gui-agents-sg
#   --region REGION       default: $AWS_REGION, else $AWS_DEFAULT_REGION, else us-east-1
#   --prune-ips           remove all existing SSH CIDRs before re-adding
#                         the current IP (useful if you've added many)
#   -y, --yes             skip confirmation prompts
set -euo pipefail

KEY_NAME="mbabenchv2-gui-agents"
SG_NAME="mbabenchv2-gui-agents-sg"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PRUNE_IPS="false"
YES="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-name)   KEY_NAME="$2"; shift 2 ;;
    --sg-name)    SG_NAME="$2"; shift 2 ;;
    --region)     REGION="$2"; shift 2 ;;
    --prune-ips)  PRUNE_IPS="true"; shift ;;
    -y|--yes)     YES="true"; shift ;;
    -h|--help)    awk '/^set -euo/{exit} /^#[^!]/{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if ! command -v aws >/dev/null 2>&1; then
  echo "aws CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html" >&2
  exit 2
fi

echo "── verifying AWS credentials ──"
if ! IDENT="$(aws sts get-caller-identity --region "$REGION" --output json 2>&1)"; then
  echo "aws sts get-caller-identity failed:" >&2
  echo "$IDENT" >&2
  echo "run 'aws configure' (or export AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)." >&2
  exit 2
fi
ACCOUNT="$(echo "$IDENT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Account"])')"
ARN="$(echo "$IDENT" | python3 -c 'import sys,json; print(json.load(sys.stdin)["Arn"])')"
echo "  account: $ACCOUNT"
echo "  arn:     $ARN"
echo "  region:  $REGION"

echo
echo "── detecting your public IP ──"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
if [[ -z "$MY_IP" ]]; then
  echo "could not detect public IP" >&2
  exit 2
fi
MY_CIDR="${MY_IP}/32"
echo "  public IP: $MY_IP"

if [[ "$YES" != "true" ]]; then
  read -rp "Proceed with key '$KEY_NAME' and sg '$SG_NAME' in $REGION? [y/N] " ans
  case "$ans" in
    [Yy]|[Yy][Ee][Ss]) ;;
    *) echo "aborted"; exit 0 ;;
  esac
fi

# ─── key pair ──────────────────────────────────────────────────────────────
echo
echo "── key pair '$KEY_NAME' ──"
PEM_PATH="$HOME/.ssh/$KEY_NAME.pem"

if aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEY_NAME" >/dev/null 2>&1; then
  echo "  exists in AWS (region=$REGION)"
  if [[ ! -f "$PEM_PATH" ]]; then
    echo "  WARNING: $PEM_PATH is not on disk locally."
    echo "  AWS cannot re-send the private key. Either:"
    echo "    - locate your existing $KEY_NAME.pem and copy it to $PEM_PATH (chmod 400), or"
    echo "    - delete the key in AWS and re-run this script:"
    echo "        aws ec2 delete-key-pair --region $REGION --key-name $KEY_NAME"
  else
    echo "  private key already at $PEM_PATH"
  fi
else
  mkdir -p "$HOME/.ssh"
  if [[ -e "$PEM_PATH" ]]; then
    echo "  AWS has no key named '$KEY_NAME', but $PEM_PATH already exists." >&2
    echo "  Refusing to overwrite. Move it aside and re-run." >&2
    exit 2
  fi
  echo "  creating key pair in AWS and saving private key to $PEM_PATH"
  aws ec2 create-key-pair \
    --region "$REGION" \
    --key-name "$KEY_NAME" \
    --query KeyMaterial \
    --output text > "$PEM_PATH"
  chmod 400 "$PEM_PATH"
  echo "  created."
fi

# ─── security group ────────────────────────────────────────────────────────
echo
echo "── security group '$SG_NAME' ──"

SG_ID="$(aws ec2 describe-security-groups \
  --region "$REGION" \
  --filters "Name=group-name,Values=$SG_NAME" \
  --query 'SecurityGroups[0].GroupId' \
  --output text 2>/dev/null || true)"

if [[ -z "$SG_ID" || "$SG_ID" == "None" ]]; then
  echo "  creating"
  SG_ID="$(aws ec2 create-security-group \
    --region "$REGION" \
    --group-name "$SG_NAME" \
    --description "gui-agents boxes: SSH from operator IPs" \
    --query GroupId --output text)"
  aws ec2 create-tags \
    --region "$REGION" \
    --resources "$SG_ID" \
    --tags "Key=Project,Value=gui-agents" "Key=Name,Value=$SG_NAME"
  echo "  created: $SG_ID"
else
  echo "  exists: $SG_ID"
fi

if [[ "$PRUNE_IPS" == "true" ]]; then
  echo "  pruning existing SSH ingress rules…"
  EXISTING_CIDRS="$(aws ec2 describe-security-groups \
    --region "$REGION" --group-ids "$SG_ID" \
    --query "SecurityGroups[0].IpPermissions[?ToPort==\`22\` && FromPort==\`22\`].IpRanges[].CidrIp" \
    --output text)"
  for cidr in $EXISTING_CIDRS; do
    echo "    revoking $cidr"
    aws ec2 revoke-security-group-ingress \
      --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port 22 --cidr "$cidr" >/dev/null || true
  done
fi

echo "  authorizing SSH from $MY_CIDR (no-op if already present)"
aws ec2 authorize-security-group-ingress \
  --region "$REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_CIDR" >/dev/null 2>&1 || {
    # InvalidPermission.Duplicate is fine; surface anything else.
    err="$(aws ec2 authorize-security-group-ingress \
      --region "$REGION" --group-id "$SG_ID" \
      --protocol tcp --port 22 --cidr "$MY_CIDR" 2>&1 || true)"
    if [[ "$err" != *InvalidPermission.Duplicate* ]]; then
      echo "    authorize-security-group-ingress failed: $err" >&2
    fi
  }

# ─── persist defaults ──────────────────────────────────────────────────────
# spinup.sh / teardown.sh source this file for their defaults, so you
# don't have to re-type --key-name / --sg-id / --region every time.
# Gitignored (see repo .gitignore).
DEFAULTS_FILE="$(cd "$(dirname "$0")" && pwd)/.aws_defaults"
cat > "$DEFAULTS_FILE" <<EOF
# Written by aws_bootstrap.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Safe to edit.
# spinup.sh and teardown.sh source this file.
GUI_AGENTS_REGION="$REGION"
GUI_AGENTS_KEY_NAME="$KEY_NAME"
GUI_AGENTS_SG_NAME="$SG_NAME"
GUI_AGENTS_SG_ID="$SG_ID"
EOF
echo "  saved defaults to $DEFAULTS_FILE"

# ─── summary ───────────────────────────────────────────────────────────────
cat <<EOF

========================================================
AWS bootstrap complete.
  region:   $REGION
  key-name: $KEY_NAME   (private key: $PEM_PATH)
  sg-name:  $SG_NAME
  sg-id:    $SG_ID
  your IP:  $MY_CIDR (allowed in $SG_NAME)

Next — spinup.sh / teardown.sh now read these from .aws_defaults, so:
  ./infra/dispatcher/spinup.sh --alias <name> --provider <claude|chatgpt>
========================================================
EOF
