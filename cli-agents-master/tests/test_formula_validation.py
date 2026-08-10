#!/usr/bin/env python3
"""
Test formula validation functionality

Tests the formula validator against known error cases from baseline solutions.
"""

import sys
from pathlib import Path

# Add excel_mcp_server to path
sys.path.insert(0, str(Path(__file__).parent / "excel_mcp_server"))

from formula_validator import validate_formula, VALID_EXCEL_FUNCTIONS

def test_valid_formulas():
    """Test that valid formulas pass validation"""
    print("\n" + "="*80)
    print("TEST 1: Valid Formulas")
    print("="*80)

    valid_formulas = [
        "=SUM(A1:A10)",
        "=PMT(0.08/12, 60, -10000)",
        "=AVERAGE(B1:B20)",
        "=IF(A1>0, SUM(B1:B10), 0)",
        "=VLOOKUP(A1, B1:C10, 2, FALSE)",
        "=SUMIF(A:A, '>100', B:B)",
        "=PMT('Assumptions'!B3/100/12, 'Assumptions'!B4*12, -('Assumptions'!B1*'Assumptions'!B2/100))",
    ]

    passed = 0
    failed = 0

    for formula in valid_formulas:
        result = validate_formula(formula)
        if result["valid"]:
            print(f"✅ {formula[:60]}")
            passed += 1
        else:
            print(f"❌ {formula[:60]}")
            print(f"   Errors: {result['errors']}")
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_invalid_functions():
    """Test that invalid function names are caught"""
    print("\n" + "="*80)
    print("TEST 2: Invalid Function Names")
    print("="*80)

    invalid_formulas = [
        ("=SUMPMT(0.08/12, 60, -10000)", "SUMPMT"),
        ("=AVGIF(A:A, '>100', B:B)", "AVGIF"),
        ("=LOOKUP2(A1, B1:C10)", "LOOKUP2"),
    ]

    passed = 0
    failed = 0

    for formula, expected_error in invalid_formulas:
        result = validate_formula(formula)
        if not result["valid"] and any(expected_error in err for err in result["errors"]):
            print(f"✅ Caught invalid function: {formula[:50]}")
            print(f"   Error: {result['errors'][0]}")
            passed += 1
        else:
            print(f"❌ Failed to catch: {formula[:50]}")
            print(f"   Result: {result}")
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_undefined_names():
    """Test that potential undefined names trigger warnings"""
    print("\n" + "="*80)
    print("TEST 3: Undefined Named Ranges (Warnings)")
    print("="*80)

    formulas_with_names = [
        ("=CFADS * 1.2", "CFADS"),
        ("=EBITDA + Revenue", "EBITDA"),
        ("=TOTAL_DEBT_SERVICE / 12", "TOTAL_DEBT_SERVICE"),
        ("=Total_Interest / Assumptions!B4", "Total_Interest"),
    ]

    passed = 0
    failed = 0

    for formula, expected_name in formulas_with_names:
        result = validate_formula(formula)
        if any(expected_name in warn for warn in result["warnings"]):
            print(f"✅ Warning for undefined name: {formula}")
            print(f"   Warning: {result['warnings'][0]}")
            passed += 1
        else:
            print(f"❌ Failed to warn about: {formula}")
            print(f"   Result: {result}")
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_syntax_errors():
    """Test that syntax errors are caught"""
    print("\n" + "="*80)
    print("TEST 4: Syntax Errors")
    print("="*80)

    syntax_errors = [
        ("=SUM(A1:A10", "parentheses"),          # Missing closing paren
        ("=SUM((A1:A10))", "parentheses"),        # Extra opening paren
        ("=", "empty"),                          # Empty formula
        ("SUM(A1:A10)", "must start"),          # Missing =
    ]

    passed = 0
    failed = 0

    for formula, expected_error_type in syntax_errors:
        result = validate_formula(formula)
        if not result["valid"]:
            print(f"✅ Caught syntax error: {formula}")
            print(f"   Error: {result['errors'][0]}")
            passed += 1
        else:
            print(f"❌ Failed to catch: {formula}")
            print(f"   Result: {result}")
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_known_baseline_errors():
    """Test against actual errors found in baseline solutions"""
    print("\n" + "="*80)
    print("TEST 5: Known Baseline Errors")
    print("="*80)

    baseline_errors = [
        # From FORMULA_ERROR_ANALYSIS.md
        ("=CFADS", "CFADS", "Race-To-The-Bottom workspace"),
        ("=TOTAL_DEBT_SERVICE", "TOTAL_DEBT_SERVICE", "Race-To-The-Bottom workspace"),
        ("=EBITDA Projection", "EBITDA", "1st Year Analyst workspace"),
        ("=SUMPMT('Assumptions'!B3/100/12, 'Assumptions'!B4*12, -B1*B2/100)", "SUMPMT", "Speed-It-Up workspace"),
        ("=SUM(B1*Assumptions!B4*12-Total_Interest)", "Total_Interest", "Speed-It-Up workspace"),
    ]

    passed = 0
    failed = 0

    for formula, expected_issue, source in baseline_errors:
        result = validate_formula(formula)

        has_error = not result["valid"]
        has_warning = len(result["warnings"]) > 0
        found_issue = has_error or has_warning

        if found_issue and (expected_issue in str(result["errors"]) or expected_issue in str(result["warnings"])):
            print(f"✅ Detected issue in: {formula[:50]}")
            print(f"   Source: {source}")
            if has_error:
                print(f"   Error: {result['errors'][0]}")
            else:
                print(f"   Warning: {result['warnings'][0]}")
            passed += 1
        else:
            print(f"❌ Failed to detect issue: {formula[:50]}")
            print(f"   Source: {source}")
            print(f"   Result: {result}")
            failed += 1

    print(f"\n📊 Results: {passed} passed, {failed} failed")
    return failed == 0


def test_excel_function_coverage():
    """Test that we have good coverage of Excel functions"""
    print("\n" + "="*80)
    print("TEST 6: Excel Function Coverage")
    print("="*80)

    print(f"Total Excel functions in validator: {len(VALID_EXCEL_FUNCTIONS)}")

    # Check for common financial functions
    financial_funcs = ["PMT", "IPMT", "PPMT", "FV", "PV", "NPV", "IRR", "CUMIPMT"]
    missing_financial = [f for f in financial_funcs if f not in VALID_EXCEL_FUNCTIONS]

    if missing_financial:
        print(f"❌ Missing financial functions: {missing_financial}")
        return False
    else:
        print(f"✅ All common financial functions present")

    # Check for common statistical functions
    stat_funcs = ["SUM", "AVERAGE", "COUNT", "MAX", "MIN", "STDEV", "VAR"]
    missing_stat = [f for f in stat_funcs if f not in VALID_EXCEL_FUNCTIONS]

    if missing_stat:
        print(f"❌ Missing statistical functions: {missing_stat}")
        return False
    else:
        print(f"✅ All common statistical functions present")

    print(f"\n✅ Function coverage looks good!")
    return True


def main():
    """Run all tests"""
    print("\n" + "="*80)
    print("FORMULA VALIDATION TEST SUITE")
    print("="*80)

    tests = [
        ("Valid Formulas", test_valid_formulas),
        ("Invalid Functions", test_invalid_functions),
        ("Undefined Names", test_undefined_names),
        ("Syntax Errors", test_syntax_errors),
        ("Known Baseline Errors", test_known_baseline_errors),
        ("Function Coverage", test_excel_function_coverage),
    ]

    results = []

    for test_name, test_func in tests:
        try:
            passed = test_func()
            results.append((test_name, passed))
        except Exception as e:
            print(f"\n❌ Test '{test_name}' crashed: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_name, False))

    # Print summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)

    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")

    total_passed = sum(1 for _, passed in results if passed)
    total_tests = len(results)

    print(f"\n📊 Overall: {total_passed}/{total_tests} tests passed")

    if total_passed == total_tests:
        print("\n✅ All tests passed! Formula validation is working correctly.")
        return 0
    else:
        print(f"\n❌ {total_tests - total_passed} tests failed. Please fix before deployment.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
