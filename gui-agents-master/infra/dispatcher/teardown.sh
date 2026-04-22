#!/usr/bin/env bash
# Terminate a gui-agents EC2 box (or all of them), identified by tag.
#
# Usage:
#   ./teardown.sh --alias claude-1           # terminate one box by alias tag
#   ./teardown.sh --all                      # terminate every gui-agents box
#
# Optional:
#   --region us-east-1   (default: $AWS_REGION, else $AWS_DEFAULT_REGION, else us-east-1)
#   --yes                skip the confirmation prompt
#
# Terminating destroys the EBS root volume (Chrome cookies and all). The
# security group and key pair are not touched — they're reused on respin.
set -euo pipefail

# Load saved defaults from aws_bootstrap.sh if present.
DEFAULTS_FILE="$(cd "$(dirname "$0")" && pwd)/.aws_defaults"
# shellcheck disable=SC1090
[[ -f "$DEFAULTS_FILE" ]] && source "$DEFAULTS_FILE"

ALIAS=""
ALL="false"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-${GUI_AGENTS_REGION:-us-east-1}}}"
YES="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --alias)  ALIAS="$2"; shift 2 ;;
    --all)    ALL="true"; shift ;;
    --region) REGION="$2"; shift 2 ;;
    --yes|-y) YES="true"; shift ;;
    -h|--help) awk '/^set -euo/{exit} /^#[^!]/{sub(/^# ?/,""); print}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

if [[ "$ALL" == "false" && -z "$ALIAS" ]]; then
  echo "pass --alias NAME or --all" >&2
  exit 2
fi

FILTERS=(
  "Name=tag:Project,Values=gui-agents"
  "Name=instance-state-name,Values=pending,running,stopping,stopped"
)
if [[ -n "$ALIAS" ]]; then
  FILTERS+=("Name=tag:alias,Values=$ALIAS")
fi

IDS=()
while IFS= read -r _line; do
  [[ -n "$_line" ]] && IDS+=("$_line")
done < <(aws ec2 describe-instances \
  --region "$REGION" \
  --filters "${FILTERS[@]}" \
  --query 'Reservations[].Instances[].InstanceId' \
  --output text | tr '\t' '\n')

if [[ ${#IDS[@]} -eq 0 ]]; then
  echo "no matching instances found in region=$REGION"
  exit 0
fi

echo "Found ${#IDS[@]} instance(s):"
aws ec2 describe-instances \
  --region "$REGION" \
  --instance-ids "${IDS[@]}" \
  --query 'Reservations[].Instances[].[InstanceId,Tags[?Key==`alias`]|[0].Value,State.Name,PublicDnsName]' \
  --output table

if [[ "$YES" != "true" ]]; then
  read -rp "Terminate these? [y/N] " ans
  case "$ans" in
    [Yy]|[Yy][Ee][Ss]) ;;
    *) echo "aborted"; exit 0 ;;
  esac
fi

aws ec2 terminate-instances --region "$REGION" --instance-ids "${IDS[@]}" >/dev/null
echo "Termination requested for ${#IDS[@]} instance(s). Waiting…"
aws ec2 wait instance-terminated --region "$REGION" --instance-ids "${IDS[@]}"
echo "Done."
