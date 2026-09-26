"""Bundle the per-task rubric-suitability annotations into the repo.

Lists s3://<bucket>/<s3_root>/rubric_suitability/task_id=N/ (top level only —
history/ saves never participate), keeps the complete annotations by the
chosen annotator, picks the latest by created_at, and writes each one to
judge/rubric_suitability/task_id=N.json with the annotator field removed.

The judge reads these bundled files first (utils.rubric_suitability) so a
grading run needs no S3 access for suitability gating.

Usage:
    uv run python operation_scripts/bundle_rubric_suitability.py --annotator NAME
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "utils"))

from utils import repo_config, rubric_suitability  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--annotator", required=True,
                    help="annotator whose complete annotations are bundled")
    ap.add_argument("--s3-root", default="SpreadsheetSmith")
    ap.add_argument("--out", type=Path, default=rubric_suitability.BUNDLED_DIR)
    args = ap.parse_args()

    client = repo_config.s3_client()
    bucket = repo_config.require_s3_bucket()
    root = f"{args.s3_root}/rubric_suitability/"
    resp = client.list_objects_v2(Bucket=bucket, Prefix=root, Delimiter="/")
    folders = sorted(p["Prefix"] for p in resp.get("CommonPrefixes", []))
    args.out.mkdir(parents=True, exist_ok=True)

    written, missing = 0, []
    for folder in folders:
        task_id = int(folder.rstrip("/").rsplit("=", 1)[1])
        listing = client.list_objects_v2(Bucket=bucket, Prefix=folder, Delimiter="/")
        docs = []
        for obj in listing.get("Contents", []):
            if not obj["Key"].endswith(".json"):
                continue
            doc = json.loads(client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
            if doc.get("annotator") == args.annotator and doc.get("complete") is True:
                docs.append((doc.get("created_at") or "", obj["Key"], doc))
        if not docs:
            missing.append(task_id)
            continue
        _, key, doc = max(docs, key=lambda t: (t[0], t[1]))
        doc.pop("annotator", None)
        (args.out / f"task_id={task_id}.json").write_text(json.dumps(doc, indent=2) + "\n")
        written += 1
        print(f"task {task_id}: {Path(key).name.split('_', 1)[-1]}")

    print(f"\nwrote {written} annotation(s) to {args.out}")
    if missing:
        print(f"no complete annotation for tasks: {missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
