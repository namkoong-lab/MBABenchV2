"""Offline check: rubric_9 + rubric_9_weights satisfy the judge's own
consistency contract, and calculate_scores handles all 12 categories.

Run from judge/:  python tests_offline/test_rubric9_consistency.py
No DB, S3, or LLM access.
"""
import json
import sys
from pathlib import Path

JUDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JUDGE))
sys.path.insert(0, str(JUDGE / "main_scripts"))

from judge import (  # noqa: E402
    _resolve_category_score,
    calculate_scores,
    validate_rubric_weights_consistency,
)
from utils.prompt_utils import (  # noqa: E402
    build_check_name_mapping,
    check_letter,
    render_rubric_checks,
)

RUBRIC = JUDGE / "prompts/rubrics/rubric_9.json"
WEIGHTS = JUDGE / "prompts/rubrics/rubric_9_weights.json"


def main() -> int:
    # 1. The judge's own validator must accept the pair.
    validate_rubric_weights_consistency(str(RUBRIC), str(WEIGHTS))
    print("OK  validate_rubric_weights_consistency(rubric_9, rubric_9_weights)")

    # Also confirm the v1 pair still validates after de-hardcoding.
    validate_rubric_weights_consistency(
        str(JUDGE / "prompts/rubrics/rubric_8.json"),
        str(JUDGE / "prompts/rubrics/rubric_6_weights.json"),
    )
    print("OK  validate_rubric_weights_consistency(rubric_8, rubric_6_weights)")

    rubric = json.loads(RUBRIC.read_text())
    weights = json.loads(WEIGHTS.read_text())

    # 2. Synthetic all-pass judgement -> total score 100.
    all_pass = {
        cat: [
            {"name": c["name"], "meets_criteria": True, "mistakes": []}
            for c in checks
        ]
        for cat, checks in rubric.items()
    }
    res = calculate_scores(all_pass, weights, max_mistakes=1)
    assert abs(res["total_score"] - 100.0) < 0.5, res["total_score"]
    print(f"OK  all-pass total_score = {res['total_score']:.2f}")
    assert len(res["criteria_scores"]) == 12, len(res["criteria_scores"])

    # 3. Fail one whole category -> total drops by that category's weight.
    one_fail = {
        cat: [
            {
                "name": c["name"],
                "meets_criteria": cat != "Rounding",
                "mistakes": ["x"] if cat == "Rounding" else [],
            }
            for c in checks
        ]
        for cat, checks in rubric.items()
    }
    res2 = calculate_scores(one_fail, weights, max_mistakes=1)
    drop = res["total_score"] - res2["total_score"]
    assert abs(drop - 1.0) < 0.5, f"Rounding (1%) fail dropped {drop}"
    print(f"OK  failing Rounding drops score by {drop:.2f} (weight 1%)")

    # 4. Check letters continue Excel-style past Z, so >26-check categories
    #    (Formatting has 36) render, map, and validate without truncation.
    for idx, expected in [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB"),
                          (35, "AJ"), (51, "AZ"), (52, "BA")]:
        got = check_letter(idx)
        assert got == expected, f"check_letter({idx}) = {got}, want {expected}"
    print("OK  check_letter A..Z, AA.. sequence")

    rendered = render_rubric_checks(str(RUBRIC), "Formatting")
    assert "Check AJ:" in rendered, "36th Formatting check not rendered"
    print("OK  render_rubric_checks(Formatting) renders all 36 checks")

    mapping = build_check_name_mapping(str(RUBRIC))
    assert len(mapping) == 132, f"mapping covers {len(mapping)} checks, want 132"
    assert ("Formatting", "AJ") in mapping, "letter AJ missing from name mapping"
    assert ("Structure", "Z") in mapping, "letter Z missing from name mapping"
    print("OK  build_check_name_mapping covers all 132 checks")

    # 5. Legacy score keys resolve through category aliases: rubric_9 names
    #    the category "Formulas" while the v1 rubric says "Formula".
    criteria = res["criteria_scores"]
    formula = _resolve_category_score(criteria, "Formula", "Formulas")
    assert formula is not None and abs(formula - 100.0) < 0.5, formula
    assert _resolve_category_score(criteria, "Accuracy") is not None
    assert _resolve_category_score(criteria, "NoSuchCategory") is None
    print("OK  _resolve_category_score Formula/Formulas alias")

    # 6. Completeness against the weights contract: all-pass leaves no
    #    weighted category unscored.
    missing = [
        cat
        for cat in weights["CategoryWeights"][0]
        if criteria.get(cat, {}).get("normalized_score") is None
    ]
    assert missing == [], f"unexpected missing categories: {missing}"
    print("OK  no missing categories for a complete rubric_9 judgement")

    print("ALL RUBRIC_9 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
