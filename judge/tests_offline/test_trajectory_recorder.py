"""Offline check: TrajectoryRecorder round-trips through the validator.

Run from judge/:  python tests_offline/test_trajectory_recorder.py
No DB, S3, or LLM access.
"""
import gzip
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))

from utils.trajectory import BLOB_THRESHOLD, TrajectoryRecorder  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_vjt", str(JUDGE / "operation_scripts" / "validate_judge_trajectory.py")
)
_vjt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vjt)
validate = _vjt.validate

BIG_CSV = "cell," * (BLOB_THRESHOLD // 5 + 10)  # comfortably over the threshold


def fake_response(text="ok", prompt_tokens=100, completion_tokens=10):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def build_run(out_dir, shrink_without_evict=False):
    """Record a plausible one-category agentic run; return the gz path."""
    rec = TrajectoryRecorder(out_dir / "trajectory.jsonl")
    rec.record_header(
        mode="agentic", model="test-model", check_order=["Accuracy"],
        rubric={"path": "rubric_9.json", "md5": "x"},
    )
    seed = [
        {"role": "system", "content": "judge instructions"},
        {"role": "user", "content": BIG_CSV},
    ]
    rec.record_call(
        mode="agentic", category="Accuracy", round=1, purpose="tool_round",
        model="test-model", request_messages=seed, tools=["read_file"],
        response=fake_response("reading"),
    )
    rec.record_event(
        "tool_exec", category="Accuracy", round=1, phase="main",
        tool="read_file", args={"filename": "a.csv"}, result=BIG_CSV,
    )
    wire2 = seed + [
        {"role": "assistant", "content": "reading"},
        {"role": "tool", "content": BIG_CSV},
    ]
    rec.record_call(
        mode="agentic", category="Accuracy", round=2, purpose="tool_round",
        model="test-model", request_messages=wire2, tools=["read_file"],
        response=fake_response("recording", prompt_tokens=300),
    )
    if shrink_without_evict:
        rec.record_call(
            mode="agentic", category="Accuracy", round=3, purpose="tool_round",
            model="test-model", request_messages=seed, tools=["read_file"],
            response=fake_response("shrunk"),
        )
    else:
        rec.record_event(
            "tool_exec", category="Accuracy", round=3, phase="main",
            tool="evict_tool_results", args={"before_round": 3},
            result="evicted rounds 1-2",
        )
        rec.record_call(
            mode="agentic", category="Accuracy", round=3, purpose="tool_round",
            model="test-model",
            request_messages=seed + [{"role": "user", "content": "[evicted]"}],
            tools=["read_file"], response=fake_response("done"),
        )
    rec.record_outcome(
        category="Accuracy",
        judgement=[{"check": "A", "decision": "pass", "mistakes": []}],
        coverage="A✓", pending=[], rounds_used=3,
    )
    gz = rec.close()
    # Telemetry sibling for the token cross-check (sums match exactly)
    total = 110 + 310 + 110
    (out_dir / "token_tracking.json").write_text(json.dumps({"total_tokens": total}))
    return Path(gz)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)

        # 1. Happy path: dedup works, plain file replaced by gz, validator PASSes.
        gz = build_run(out)
        assert gz.exists() and not (out / "trajectory.jsonl").exists()
        with gzip.open(gz, "rt") as f:
            records = [json.loads(l) for l in f]
        blob_records = [r for r in records if r["type"] == "blob"]
        assert len(blob_records) == 1, (
            f"BIG_CSV should dedup to one blob, got {len(blob_records)}"
        )
        assert records[-1]["type"] == "end"
        r = validate(out)
        assert r["problems"] == [], r["problems"]
        assert any("eviction" in n for n in r["notes"]), r["notes"]
        print("OK  happy path: 1 blob, end record, validator PASS w/ eviction note")

        # 2. Wire shrink without eviction -> problem.
        out2 = Path(td) / "shrink"
        out2.mkdir()
        build_run(out2, shrink_without_evict=True)
        r2 = validate(out2)
        assert any("no eviction" in p for p in r2["problems"]), r2
        print("OK  unexplained wire shrink flagged as problem")

        # 3. Dangling blob ref -> problem.
        out3 = Path(td) / "dangling"
        out3.mkdir()
        (out3 / "trajectory.jsonl").write_text(
            "\n".join([
                json.dumps({"type": "header", "step": 1, "check_order": []}),
                json.dumps({
                    "type": "call", "step": 2, "category": "Accuracy",
                    "request": {"messages": [{"content": {"$blob": "deadbeef"}}]},
                    "usage": {"total_tokens": 1},
                }),
            ])
        )
        r3 = validate(out3)
        assert any("dangling blob" in p for p in r3["problems"]), r3
        print("OK  dangling blob ref flagged as problem")

        # 4. Token mismatch -> problem.
        out4 = Path(td) / "tokens"
        out4.mkdir()
        build_run(out4)
        (out4 / "token_tracking.json").write_text(json.dumps({"total_tokens": 5000}))
        r4 = validate(out4)
        assert any("token mismatch" in p for p in r4["problems"]), r4
        print("OK  token mismatch flagged as problem")

        # 5. Disabled recorder writes nothing and close() is a no-op.
        out5 = Path(td) / "disabled"
        out5.mkdir()
        rec = TrajectoryRecorder(out5 / "trajectory.jsonl", enabled=False)
        rec.record_call(
            mode="standard", category="Accuracy", round=1, purpose="stage",
            model="m", request_messages=[],
        )
        assert rec.close() is None
        assert not any(out5.iterdir())
        print("OK  disabled recorder is a no-op")

    print("ALL TRAJECTORY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
