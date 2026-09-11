#!/usr/bin/env bash
# 10-task rubric-coverage subset, Claude Code / Fable 5.1 max, benchmark v2,
# template v13 (pv 113). Sequential lane; pass a task-id list to split lanes:
#
#   ./wave_fable51_rubric10.sh                    # all 10, in the chosen order
#   ./wave_fable51_rubric10.sh 36 3 19 32 38      # lane A
#   ./wave_fable51_rubric10.sh 1 48 28 41 43      # lane B
#
# Launch shape (survives this shell/session ending, keeps the Mac awake):
#   nohup caffeinate -is ./wave_fable51_rubric10.sh > wave_fable51_rubric10.log 2>&1 & disown
#
# Resumable: before each task it checks the DB (plain SELECT, no session
# state on the pooler) for a valid row under this label + prompt_version and
# skips tasks already banked. Relaunching the same command continues.
set -u
cd "$(dirname "$0")"
CONFIG=run_configs/prod_v2_fable51_rubric10.yaml
LABEL=claudecode_anthropic/claude-fable-5-1-max
PV=113
TASKS=("$@"); [ ${#TASKS[@]} -eq 0 ] && TASKS=(36 3 19 32 38 1 48 28 41 43)

banked() {  # exit 0 if task $1 already has a valid row under LABEL/PV
  uv run --no-sync python - "$1" <<'PY'
import sys; sys.path.insert(0, "../scripts")
from config import Config
import psycopg2
cfg = Config.load(); url = cfg.get("database.v2_url") if hasattr(cfg, "get") else cfg["database"]["v2_url"]
con = psycopg2.connect(url); cur = con.cursor()
cur.execute("select 1 from task_attempts where task_id=%s and agent_model_name=%s and prompt_version=%s "
            "and not deprecated and not agent_failed limit 1", (int(sys.argv[1]), "claudecode_anthropic/claude-fable-5-1-max", 113))
sys.exit(0 if cur.fetchone() else 1)
PY
}

echo "[$(date '+%F %T')] wave start: ${TASKS[*]}  label=$LABEL pv=$PV"
for t in "${TASKS[@]}"; do
  if banked "$t" 2>/dev/null; then echo "[$(date '+%F %T')] task $t already banked — skip"; continue; fi
  echo "[$(date '+%F %T')] task $t START"
  uv run --no-sync python -m coding_agent.run_task --config "$CONFIG" --task-id "$t"
  rc=$?
  echo "[$(date '+%F %T')] task $t END rc=$rc (0 success, 2 agent_failure, 3 timeout, 97 infra)"
done
echo "[$(date '+%F %T')] wave done"
