# Judge environment. Source this before any judge run:
#
#   cd judge && source project_configs.sh
#
# Secrets live in judge/.env (gitignored — copy judge/.env.example).
# This file holds NO secret values and is safe to commit.
#
# judge/.env is grep-parsed rather than sourced: the Neon URL contains a
# raw & that breaks `source` under zsh.

_judge_dir="${0:a:h}"
[ -d "$_judge_dir" ] || _judge_dir="$(pwd)"
_envfile="$_judge_dir/.env"

if [ ! -f "$_envfile" ]; then
    echo "❌ $_envfile missing — copy judge/.env.example to judge/.env and fill it in" >&2
    return 1 2>/dev/null || exit 1
fi

_getvar() { grep "^$1=" "$_envfile" | head -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//'; }

export DATABASE_URL="$(_getvar DATABASE_URL)"
export OPENROUTER_API_KEY="$(_getvar OPENROUTER_API_KEY)"

for _v in DATABASE_URL OPENROUTER_API_KEY; do
    eval "_val=\${$_v}"
    if [ -z "$_val" ]; then
        echo "❌ $_v is empty in $_envfile" >&2
        return 1 2>/dev/null || exit 1
    fi
done

# The judge always runs its Google-slug models through OpenRouter, never
# direct-to-Gemini (no Gemini key on this setup). Without this override,
# llm_utils routes google/* to Gemini's endpoint and auth fails.
# Exact-match set (no wildcards): every google/* slug reachable via
# judge.openrouter_model or --model must be listed here, or that run dies
# with "Missing or invalid Authorization header" from Google.
export JUDGE_OPENROUTER_MODELS="google/gemini-2.5-pro,google/gemini-2.5-flash,google/gemini-2.5-flash-lite"

# Repo-relative scratch; LibreOffice for recalc-before-extraction.
export SCRATCH_PATH="$_judge_dir/scratch"
export LIBREOFFICE_PATH="/Applications/LibreOffice.app/Contents/MacOS/soffice"

# Optional repo-scoped AWS credentials (only used when uploading grading
# artifacts to S3, i.e. without --no-s3-upload). Empty lines in .env leave
# these unset and boto3 falls back to the machine default chain (~/.aws).
_aws_id="$(_getvar AWS_ACCESS_KEY_ID)"
_aws_secret="$(_getvar AWS_SECRET_ACCESS_KEY)"
if [ -n "$_aws_id" ] && [ -n "$_aws_secret" ]; then
    export AWS_ACCESS_KEY_ID="$_aws_id"
    export AWS_SECRET_ACCESS_KEY="$_aws_secret"
fi

unset _judge_dir _envfile _v _val _aws_id _aws_secret
unset -f _getvar

echo "✅ judge env loaded (DATABASE_URL, OPENROUTER_API_KEY set; model routing pinned to OpenRouter)"
