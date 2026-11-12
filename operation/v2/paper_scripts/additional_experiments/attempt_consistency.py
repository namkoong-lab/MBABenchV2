#!/usr/bin/env python
"""Attempt consistency: how far does the score move when the same agent redoes the same task?

GPT-6 Astra, the 30 Stage 2 tasks, two harnesses (Codex, in-house), three attempts per task and
harness: the original the leaderboard scores and two reruns (r2, r3). One grading per attempt
(judge v14, GPT-5.6 Sol). Reads the frozen snapshot written by attempt_consistency_data.py.

The report, per harness:
  1. Headline score   the three scores of each task: gap between two attempts, best-to-worst of
                      three, and the 30-task mean of each run. Beside it, the judge-only
                      reference: the Codex originals were graded three times, so the same
                      workbook has three scores that differ by judge noise alone.
  2. Deterministic    the checks a program decides (the answer check and the automatic
     checks           formatting / safety checks; grader == "deterministic"). No judge noise:
                      the same file always gets the same verdict (verified on the three gradings
                      of each Codex original). So a verdict that differs between attempts is a
                      real difference between the workbooks.
  3. Score split      the headline score is an exact sum of points from deterministic checks and
                      points from LLM-judged checks; the spread of each part across attempts.
  4. LLM-judged       how often an LLM-judged check's verdict differs between attempts, against
     checks           how often it differs between three gradings of the same file.

A check is "flagged" when its mistake count reaches pass_rule.FLAG_MISTAKES, as the leaderboard
counts it. A check "splits" when the three verdicts are not all the same.

Not like for like, original against rerun (reruns against each other are): the originals ran in
September, most through OpenAI directly and on another machine; the reruns ran on 2026-11-11
through TensorBlock. The automatic checks' code is the same for both except "No bright-yellow
highlighting" (changed 2026-10-06, after most originals were graded). Section 2 therefore also
reports r2 against r3 alone.

Writes under operation/results/v2/attempt_consistency/ (gitignored):
  scores_per_task.csv         one row per attempt
  deterministic_changes.csv   every deterministic check whose mistake count differs between attempts
  score_spread.png            the three scores of every task, both harnesses (needs matplotlib)

Run from the repo root (numpy; matplotlib for the chart):
    uv run --project judge python operation/v2/paper_scripts/additional_experiments/attempt_consistency.py
"""

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pass_rule import FLAG_MISTAKES, load_tiers, task_verdict  # noqa: E402

RESULTS = REPO_ROOT / "operation" / "results" / "v2" / "attempt_consistency"
SNAPSHOT = RESULTS / "gradings.json"
RUNS = ("orig", "r2", "r3")
HARNESSES = {"codex": "Codex", "inhouse": "In-house"}
ANSWER_CHECK = ("Accuracy", "Final calculation accuracy")
RANGE_BINS = ((0, 1), (1, 2), (2, 5), (5, 10), (10, 101))  # best-to-worst of three, in score points
BOOT_DRAWS, BOOT_SEED = 10_000, 0
BIG_SWING = 10  # score points; tasks whose three scores span at least this much are listed and labelled in the chart


# --- one grading -----------------------------------------------------------------


def points(g: dict) -> tuple[float, float, float]:
    """(deterministic points, LLM points, deterministic points available) of one grading, on the 0-100 scale.

    total_score = sum over categories of category_weight * 100 * sum(weight * score) / total_weight,
    so every check contributes category_weight * 100 * weight * score / total_weight points."""
    det = llm = det_max = 0.0
    for cat, checks in g["check_scores"].items():
        c = g["criteria_scores"][cat]
        k = 100.0 * c["category_weight"] / c["total_weight"]
        for v in checks.values():
            if v["unscored"]:
                continue
            if v["grader"] == "deterministic":
                det += k * v["weight"] * v["score"]
                det_max += k * v["weight"]
            else:
                llm += k * v["weight"] * v["score"]
    assert abs(det + llm - g["total_score"]) < 1e-6, f"grading {g['grading_id']}: points do not add up to the total"
    return det, llm, det_max


def cells(gradings: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """(category, check) -> that check's entry in each grading, for checks scored in every one."""
    keys = [{(c, n) for c, ch in g["check_scores"].items() for n, v in ch.items() if not v["unscored"]} for g in gradings]
    return {k: [g["check_scores"][k[0]][k[1]] for g in gradings] for k in sorted(set.intersection(*keys))}


def kind(entries: list[dict]) -> str:
    graders = {e["grader"] for e in entries}
    return graders.pop() if len(graders) == 1 else "mixed"


def flagged(e: dict) -> bool:
    return e["mistakes"] >= FLAG_MISTAKES


# --- statistics ------------------------------------------------------------------


def pair_gaps(trios: np.ndarray) -> np.ndarray:
    """|a - b| for every pair of the three values of every task."""
    return np.array([abs(t[i] - t[j]) for t in trios for i, j in combinations(range(len(t)), 2)])


def within_sd(trios: np.ndarray) -> float:
    """Pooled within-task standard deviation (root of the mean within-task variance)."""
    return float(np.sqrt(np.mean(np.var(trios, axis=1, ddof=1))))


def boot_mean_ci(x: np.ndarray, rng: np.random.Generator) -> tuple[float, float]:
    """95% bootstrap interval for the mean of x, resampling tasks."""
    draws = rng.choice(x, size=(BOOT_DRAWS, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def spread_line(label: str, trios: np.ndarray, rng: np.random.Generator) -> str:
    rng_ = trios.max(axis=1) - trios.min(axis=1)
    gaps = pair_gaps(trios)
    lo, hi = boot_mean_ci(rng_, rng)
    return (
        f"  {label:34s} gap between two: median {np.median(gaps):4.1f}, mean {gaps.mean():4.1f} | "
        f"best-to-worst of three: median {np.median(rng_):4.1f}, mean {rng_.mean():4.1f} [{lo:.1f}, {hi:.1f}], max {rng_.max():4.1f} | "
        f"within-task SD {within_sd(trios):4.1f}"
    )


def header(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


# --- report ----------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--snapshot", type=Path, default=SNAPSHOT)
    ap.add_argument("--no-chart", action="store_true")
    args = ap.parse_args()

    snap = json.loads(args.snapshot.read_text())
    tasks = snap["meta"]["tasks"]
    A: dict[str, dict[int, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    for a in snap["attempts"]:
        A[a["harness"]][a["task_id"]][a["run"]] = a
    assert all(sorted(A[h]) == tasks and all(set(A[h][t]) == set(RUNS) for t in tasks) for h in HARNESSES)
    tiers = load_tiers()
    rng = np.random.default_rng(BOOT_SEED)
    print(f"snapshot {snap['meta']['created']}: {len(tasks)} tasks x {len(HARNESSES)} harnesses x {len(RUNS)} attempts, judge v{snap['meta']['judge_version']}")

    # per attempt: score, its two parts, answers, flags, pass
    rows = []
    for h in HARNESSES:
        for t in tasks:
            for run in RUNS:
                a = A[h][t][run]
                g = a["grading"]
                det, llm, det_max = points(g)
                ents = [e for ch in g["check_scores"].values() for e in ch.values() if not e["unscored"]]
                v = task_verdict(g["check_scores"], tiers)
                a["calc"] = {
                    "score": g["total_score"], "det": det, "llm": llm, "det_max": det_max,
                    "n_det": sum(e["grader"] == "deterministic" for e in ents),
                    "n_llm": sum(e["grader"] != "deterministic" for e in ents),
                    "det_flags": sum(e["grader"] == "deterministic" and flagged(e) for e in ents),
                    "llm_flags": sum(e["grader"] != "deterministic" and flagged(e) for e in ents),
                    "answers_right": g["answer_check"]["n_match"], "questions": g["answer_check"]["n_questions"],
                    "answer_flagged": flagged(g["check_scores"][ANSWER_CHECK[0]][ANSWER_CHECK[1]]),
                    "passed": None if v is None else v.passed,
                }
                rows.append({"harness": h, "task_id": t, "run": run, "attempt_id": a["attempt_id"], "grading_id": g["grading_id"],
                             **{k: (round(x, 3) if isinstance(x, float) else x) for k, x in a["calc"].items()}})

    def trios(h: str, key: str) -> np.ndarray:
        return np.array([[A[h][t][run]["calc"][key] for run in RUNS] for t in tasks], dtype=float)

    # the judge-only reference: three gradings of the same Codex original
    same_file = {t: [A["codex"][t]["orig"]["grading"]] + A["codex"][t]["orig"]["repeat_gradings"] for t in tasks}
    assert all(len(gs) == 3 for gs in same_file.values()), "expected three gradings of every Codex original"
    judge_total = np.array([[g["total_score"] for g in same_file[t]] for t in tasks])
    judge_llm = np.array([[points(g)[1] for g in same_file[t]] for t in tasks])
    judge_det = np.array([[points(g)[0] for g in same_file[t]] for t in tasks])

    # ---- 1. headline score
    header("1. HEADLINE SCORE (0-100): three attempts per task")
    for h, name in HARNESSES.items():
        s = trios(h, "score")
        print(f"\n{name}")
        print("  30-task mean by run:   " + "   ".join(f"{run} {s[:, i].mean():5.1f}" for i, run in enumerate(RUNS))
              + f"   (spread of the three means {s.mean(axis=0).max() - s.mean(axis=0).min():.1f})")
        print("  passes by run:         " + "   ".join(f"{run} {sum(bool(A[h][t][run]['calc']['passed']) for t in tasks):2d}/30" for run in RUNS))
        k = Counter(sum(bool(A[h][t][run]["calc"]["passed"]) for run in RUNS) for t in tasks)
        print("  tasks passing in k of 3:   " + "   ".join(f"{i} of 3: {k[i]:2d}" for i in (3, 2, 1, 0)))
        print(spread_line("three attempts, all", s, rng))
        r = s[:, 1:]
        print(f"  {'r2 vs r3 (like for like)':34s} gap: median {np.median(abs(r[:, 0] - r[:, 1])):4.1f}, mean {abs(r[:, 0] - r[:, 1]).mean():4.1f}")
        shift = s[:, 0] - r.mean(axis=1)
        lo, hi = boot_mean_ci(shift, rng)
        print(f"  {'original minus rerun mean':34s} mean {shift.mean():+5.1f} [{lo:+.1f}, {hi:+.1f}]  (an interval clear of 0 = the originals differ systematically)")
        rr = s.max(axis=1) - s.min(axis=1)
        print("  best-to-worst of three, tasks by size:  " + "   ".join(
            f"{'<' + str(b) if a == 0 else ('>=' + str(a) if b > 100 else f'{a}-{b}')} pts: {int(((rr >= a) & (rr < b)).sum())}" for a, b in RANGE_BINS))
        big = [t for t, x in zip(tasks, rr) if x >= BIG_SWING]
        flip = [t for t in big if len({A[h][t][run]["calc"]["answer_flagged"] for run in RUNS}) > 1]
        print(f"  tasks whose three scores span {BIG_SWING}+ points: {len(big)} {big}; the answer check changed verdict in {len(flip)} of them {flip}")
    print("\nJudge-only reference (the same Codex workbook graded three times, 30 workbooks)")
    print(spread_line("three gradings, one file", judge_total, rng))
    jp = [[bool(v and v.passed) for v in (task_verdict(g["check_scores"], tiers) for g in same_file[t])] for t in tasks]
    k = Counter(sum(x) for x in jp)
    print("  passes by grading:     " + "   ".join(f"{sum(x[i] for x in jp):2d}/30" for i in range(3))
          + "   | workbooks passing in k of 3 gradings:   " + "   ".join(f"{i} of 3: {k[i]:2d}" for i in (3, 2, 1, 0)))
    for h, name in HARNESSES.items():
        va, vj = within_sd(trios(h, "score")) ** 2, within_sd(judge_total) ** 2
        print(f"  {name}: attempt-to-attempt SD {va ** 0.5:.1f} = judge {vj ** 0.5:.1f} (+) agent {max(va - vj, 0) ** 0.5:.1f}  "
              f"-> judge share of the variance {min(vj / va, 1):.0%}" + ("" if h == "codex" else "  (judge noise borrowed from the Codex files)"))

    # ---- 2. deterministic checks
    header("2. DETERMINISTIC CHECKS (decided by a program, no judge noise)")
    n_det = Counter(A[h][t][run]["calc"]["n_det"] for h in HARNESSES for t in tasks for run in RUNS)
    n_all = [A[h][t][run]["calc"]["n_det"] + A[h][t][run]["calc"]["n_llm"] for h in HARNESSES for t in tasks for run in RUNS]
    det_max = np.array([A[h][t][run]["calc"]["det_max"] for h in HARNESSES for t in tasks for run in RUNS])
    print(f"deterministic checks scored per workbook: {dict(sorted(n_det.items()))} of {min(n_all)}-{max(n_all)} checks in all")
    print(f"points they carry, of the 100: mean {det_max.mean():.1f} (range {det_max.min():.1f}-{det_max.max():.1f})")

    stable = Counter()
    for t in tasks:
        for k, ents in cells(same_file[t]).items():
            if kind(ents) == "deterministic":
                stable[len({e["mistakes"] for e in ents}) == 1] += 1
    print(f"same file graded three times: {stable[True]} of {sum(stable.values())} deterministic verdicts identical"
          + ("" if not stable[False] else f"  ** {stable[False]} DIFFER **"))

    changes, per_check = [], defaultdict(lambda: defaultdict(Counter))
    llm_split, mixed, pairs = defaultdict(Counter), Counter(), defaultdict(Counter)
    for h in HARNESSES:
        for t in tasks:
            for k, ents in cells([A[h][t][run]["grading"] for run in RUNS]).items():
                kd, f = kind(ents), [flagged(e) for e in ents]
                if kd == "mixed":
                    mixed[k] += 1
                    continue
                state = "split" if len(set(f)) > 1 else ("all flagged" if f[0] else "all clean")
                if kd == "llm":
                    llm_split[h][state] += 1
                    continue
                per_check[k][h][state] += 1
                per_check[k][h]["r2r3 split"] += f[1] != f[2]
                if k != ANSWER_CHECK:
                    for i, j in combinations(range(len(RUNS)), 2):
                        pairs[h][f"{RUNS[i]}-{RUNS[j]}"] += f[i] != f[j]
                per_check[k][h]["count differs"] += len({e["mistakes"] for e in ents}) > 1
                if len({e["mistakes"] for e in ents}) > 1:
                    changes.append({"harness": h, "task_id": t, "category": k[0], "check": k[1],
                                    **{f"mistakes_{run}": e["mistakes"] for run, e in zip(RUNS, ents)},
                                    "flag_split": state == "split"})
    print("\nverdicts across the three attempts, tasks out of 30 (clean = no mistake in any attempt, flagged = a mistake in every")
    print("attempt, split = the attempts disagree; r2r3 = the two reruns disagree):")
    print(f"  {'':42s}" + "".join(f"| {name:^34s}" for name in HARNESSES.values()))
    print(f"  {'check':42s}" + "".join(f"| {'clean':>6s}{'flagged':>9s}{'split':>7s}{'r2r3':>6s}  " for _ in HARNESSES))
    order = sorted(per_check, key=lambda k: (k != ANSWER_CHECK, -sum(per_check[k][h]["split"] for h in HARNESSES), k))
    tot = defaultdict(Counter)
    for k in order:
        line = f"  {k[1][:42]:42s}"
        for h in HARNESSES:
            c = per_check[k][h]
            line += f"| {c['all clean']:6d}{c['all flagged']:9d}{c['split']:7d}{c['r2r3 split']:6d}  "
            if k != ANSWER_CHECK:
                tot[h].update(c)
        print(line)
    line = f"  {'all automatic checks (answer check aside)':42s}"
    for h in HARNESSES:
        c = tot[h]
        line += f"| {c['all clean']:6d}{c['all flagged']:9d}{c['split']:7d}{c['r2r3 split']:6d}  "
    print(line)
    for h, name in HARNESSES.items():
        c = tot[h]
        n = c["all clean"] + c["all flagged"] + c["split"]
        tasks_any = len({r["task_id"] for r in changes if r["harness"] == h and r["flag_split"] and (r["category"], r["check"]) != ANSWER_CHECK})
        print(f"  {name}: {c['split']} of {n} automatic-check verdicts split across the three attempts ({c['split'] / n:.1%}); "
              f"r2 vs r3 alone {c['r2r3 split']} ({c['r2r3 split'] / n:.1%}); tasks with at least one split: {tasks_any}/30")
        print(f"  {name}: automatic-check verdicts that differ, by pair of attempts: " + ", ".join(f"{p} {n}" for p, n in pairs[h].items())
              + "  (similar counts = the originals behave like the reruns)")
    if mixed:
        print(f"  left out, decided by a program in some attempts and the LLM in others: {dict(mixed)}")

    print("\nanswer check (every question compared with the golden answer):")
    for h, name in HARNESSES.items():
        ok = np.array([[not A[h][t][run]["calc"]["answer_flagged"] for run in RUNS] for t in tasks])
        frac = np.array([[A[h][t][run]["calc"]["answers_right"] / A[h][t][run]["calc"]["questions"] for run in RUNS] for t in tasks])
        n_ok = ok.sum(axis=1)
        same_n = sum(len({A[h][t][run]["calc"]["answers_right"] for run in RUNS}) == 1 for t in tasks)
        print(f"  {name}: all answers right in 3 of 3 attempts: {int((n_ok == 3).sum())} tasks, in 2: {int((n_ok == 2).sum())}, in 1: {int((n_ok == 1).sum())}, "
              f"in 0: {int((n_ok == 0).sum())} | by run " + " ".join(f"{run} {int(ok[:, i].sum())}/30" for i, run in enumerate(RUNS))
              + f" | same number of right answers in all three: {same_n}/30 | share of answers right, mean by run "
              + " ".join(f"{frac[:, i].mean():.1%}" for i in range(3)))

    # ---- 3. the score as deterministic points + LLM points
    header("3. SCORE SPLIT: points from deterministic checks + points from LLM-judged checks = headline score")
    for h, name in HARNESSES.items():
        print(f"\n{name}")
        for key, label in (("score", "headline score"), ("det", "deterministic points"), ("llm", "LLM-judged points")):
            x = trios(h, key)
            print(spread_line(label, x, rng) + f" | mean by run " + " ".join(f"{x[:, i].mean():5.1f}" for i in range(3)))
        d, l = trios(h, "det"), trios(h, "llm")
        vd, vl = np.var(d, axis=1, ddof=1).mean(), np.var(l, axis=1, ddof=1).mean()
        vt = np.var(trios(h, "score"), axis=1, ddof=1).mean()
        print(f"  within-task variance: deterministic {vd:.1f} + LLM-judged {vl:.1f} + twice their covariance {vt - vd - vl:+.1f} = headline {vt:.1f}")
    print("\nJudge-only reference (the same Codex workbook graded three times)")
    print(spread_line("deterministic points", judge_det, rng))
    print(spread_line("LLM-judged points", judge_llm, rng))

    # ---- 4. LLM-judged checks
    header("4. LLM-JUDGED CHECKS: verdicts that differ")
    ref = Counter()
    for t in tasks:
        for k, ents in cells(same_file[t]).items():
            if kind(ents) == "llm":
                f = [flagged(e) for e in ents]
                ref["split" if len(set(f)) > 1 else ("all flagged" if f[0] else "all clean")] += 1
    for label, c in [(f"{name}: three attempts", llm_split[h]) for h, name in HARNESSES.items()] + [("Codex originals: one file, three gradings", ref)]:
        n = sum(c.values())
        print(f"  {label:44s} {n:5d} verdict trios: clean {c['all clean'] / n:6.1%}  flagged {c['all flagged'] / n:6.1%}  split {c['split'] / n:6.1%}")

    # ---- files
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "scores_per_task.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with (RESULTS / "deterministic_changes.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["harness", "task_id", "category", "check"] + [f"mistakes_{r}" for r in RUNS] + ["flag_split"])
        w.writeheader()
        w.writerows(changes)
    print(f"\nwrote scores_per_task.csv ({len(rows)} rows) and deterministic_changes.csv ({len(changes)} rows) under {RESULTS.relative_to(REPO_ROOT)}")
    if not args.no_chart:
        chart(A, tasks, judge_total)


def chart(A, tasks, judge_total) -> None:
    """Each task's three scores as a low-to-high line with three dots, tasks sorted by their mean: one panel per
    harness and a third for the judge-only reference (one workbook, three gradings). One hue for the attempts,
    grey for the reference; text in ink colours (the dataviz reference palette, light surface)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    surface, ink, ink2, muted, grid, axis = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
    blue, blue_light, grey, grey_light = "#2a78d6", "#9ec5f4", "#898781", "#c3c2b7"
    panels = [
        (f"{name}: three attempts per task", "a task's best and worst attempt",
         np.array([[A[h][t][run]["calc"]["score"] for run in RUNS] for t in tasks]), blue, blue_light)
        for h, name in HARNESSES.items()
    ]
    panels.append(("For scale: one Codex workbook graded three times (judge noise only)", "a workbook's highest and lowest grade",
                   judge_total, grey, grey_light))
    low = 10 * np.floor(min(p[2].min() for p in panels) / 10)
    fig, axes = plt.subplots(len(panels), 1, figsize=(7, 8.8), sharex=True, sharey=True, facecolor=surface)
    x = np.arange(len(tasks))
    for ax, (title, what, s, dot, line) in zip(axes, panels):
        order = np.argsort(s.mean(axis=1), kind="stable")
        s = s[order]
        span = s.max(axis=1) - s.min(axis=1)
        ax.set_facecolor(surface)
        ax.vlines(x, s.min(axis=1), s.max(axis=1), color=line, lw=2, zorder=2)
        ax.scatter(np.repeat(x, s.shape[1]), s.ravel(), s=34, color=dot, edgecolors=surface, linewidths=1.2, zorder=3)
        for i in np.flatnonzero(span >= BIG_SWING):
            ax.annotate(str(tasks[order[i]]), (x[i], s[i].max()), xytext=(0, 5), textcoords="offset points",
                        ha="center", va="bottom", fontsize=7, color=ink2)
        ax.text(0, 1.14, title, transform=ax.transAxes, fontsize=9.5, color=ink, fontweight="semibold", va="bottom")
        ax.text(0, 1.04, f"Gap between {what}: typically {np.median(span):.1f} points, at most {span.max():.1f}",
                transform=ax.transAxes, fontsize=8.5, color=ink2, va="bottom")
        ax.set_ylim(low - 3, 104)  # room for a dot on the lowest gridline and for a label above 100
        ax.set_yticks(np.arange(low, 101, 10))
        ax.set_xticks([])
        ax.set_xlim(-0.8, len(tasks) - 0.2)
        ax.grid(axis="y", color=grid, lw=0.6)
        ax.set_axisbelow(True)
        ax.tick_params(axis="y", length=0, labelsize=8, labelcolor=muted)
        ax.set_ylabel("score", fontsize=8, color=muted)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(axis)
    axes[-1].set_xlabel("30 tasks per panel, each panel sorted by its own average score", fontsize=8, color=muted, labelpad=6)
    fig.text(0.02, 0.975, "GPT-6 Astra: how far a task's score moves from one attempt to the next", fontsize=11.5, color=ink,
             fontweight="semibold", va="top")
    fig.text(0.02, 0.948, f"Each line is one task: its three scores, lowest to highest. Numbers name the tasks that span {BIG_SWING}+ points.",
             fontsize=8.5, color=ink2, va="top")
    fig.tight_layout(rect=(0, 0, 1, 0.925), h_pad=2.4)
    out = RESULTS / "score_spread.png"
    fig.savefig(out, dpi=200, facecolor=surface)
    print(f"wrote {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
