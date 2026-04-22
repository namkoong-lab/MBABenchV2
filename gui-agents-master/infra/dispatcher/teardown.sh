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

# AWS CLI v2 pipes --output table through less by default when stdout is a
# TTY. That hides our interactive confirm prompt below the pager, making
# the script appear stuck after the table prints. Disable the pager.
export AWS_PAGER=""

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

# ─── prune boxes.yaml ───────────────────────────────────────────────────────
# Drop any block whose instance_id matches one we just terminated. Match on
# instance_id (not alias) because the AWS ID is the authoritative key — the
# alias tag can be missing or re-used. Preserves everything else in the file
# verbatim so manually-added comments / order aren't disturbed.
BOXES_FILE="$(cd "$(dirname "$0")" && pwd)/boxes.yaml"
if [[ -s "$BOXES_FILE" ]]; then
  # Comma-separated for awk to split; subshell scopes the IFS change.
  ids_csv="$(IFS=,; echo "${IDS[*]}")"
  tmp_boxes="$(mktemp -t gui-agents-boxes.XXXXXX)"
  awk -v ids="$ids_csv" '
    BEGIN {
      n = split(ids, arr, ",")
      for (i=1; i<=n; i++) want[arr[i]] = 1
      block = ""; in_block = 0; block_iid = ""
    }
    function flush() {
      if (!in_block) return
      if (!(block_iid in want)) printf "%s", block
      block = ""; in_block = 0; block_iid = ""
    }
    /^[[:space:]]*-[[:space:]]*alias:/ {
      flush()
      block = $0 "\n"; in_block = 1
      next
    }
    in_block && /^[[:space:]]*instance_id:/ {
      v = $0
      sub(/^[[:space:]]*instance_id:[[:space:]]*/, "", v)
      sub(/[[:space:]]*$/, "", v)
      block_iid = v
      block = block $0 "\n"
      next
    }
    in_block { block = block $0 "\n"; next }
    { print }
    END { flush() }
  ' "$BOXES_FILE" > "$tmp_boxes"
  # Compare line counts to report how many blocks were actually dropped.
  before="$(wc -l < "$BOXES_FILE" | tr -d ' ')"
  after="$(wc -l < "$tmp_boxes" | tr -d ' ')"
  mv "$tmp_boxes" "$BOXES_FILE"
  echo "Pruned $BOXES_FILE (lines: $before → $after)"
fi
