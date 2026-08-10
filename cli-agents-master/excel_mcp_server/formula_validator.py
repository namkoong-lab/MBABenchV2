"""
Formula validation utilities for Excel agent

Validates Excel formulas for:
- Invalid function names
- Potential undefined named ranges
- Basic syntax errors
"""

import re
from typing import Dict, List, Set

# Comprehensive list of valid Excel functions
VALID_EXCEL_FUNCTIONS: Set[str] = {
    # Math & Stats
    "SUM", "AVERAGE", "COUNT", "COUNTA", "COUNTBLANK", "MAX", "MIN",
    "MEDIAN", "MODE", "STDEV", "STDEVP", "VAR", "VARP",
    "ROUND", "ROUNDUP", "ROUNDDOWN", "CEILING", "FLOOR", "TRUNC", "INT",
    "ABS", "SQRT", "POWER", "EXP", "LN", "LOG", "LOG10",
    "MOD", "QUOTIENT", "PRODUCT", "FACT", "GCD", "LCM",
    "SIGN", "RAND", "RANDBETWEEN",

    # Conditional & Logic
    "IF", "IFS", "AND", "OR", "NOT", "XOR", "TRUE", "FALSE",
    "IFERROR", "IFNA", "SWITCH",
    "SUMIF", "SUMIFS", "AVERAGEIF", "AVERAGEIFS",
    "COUNTIF", "COUNTIFS", "MAXIFS", "MINIFS",

    # Financial
    "PMT", "IPMT", "PPMT", "FV", "PV", "RATE", "NPER",
    "NPV", "IRR", "XIRR", "XNPV", "MIRR",
    "CUMIPMT", "CUMPRINC", "EFFECT", "NOMINAL",
    "SLN", "SYD", "DB", "DDB", "VDB",
    "PRICE", "YIELD", "DURATION", "MDURATION",

    # Lookup & Reference
    "VLOOKUP", "HLOOKUP", "XLOOKUP", "LOOKUP",
    "INDEX", "MATCH", "INDIRECT", "OFFSET", "CHOOSE",
    "ROW", "ROWS", "COLUMN", "COLUMNS",
    "ADDRESS", "AREAS", "TRANSPOSE",
    "GETPIVOTDATA", "HYPERLINK",

    # Text
    "CONCATENATE", "CONCAT", "TEXTJOIN",
    "LEFT", "RIGHT", "MID", "LEN", "TRIM",
    "UPPER", "LOWER", "PROPER",
    "SUBSTITUTE", "REPLACE", "REPT",
    "FIND", "FINDB", "SEARCH", "SEARCHB",
    "TEXT", "VALUE", "NUMBERVALUE",
    "CHAR", "CODE", "CLEAN", "EXACT",

    # Date & Time
    "DATE", "YEAR", "MONTH", "DAY", "TODAY", "NOW",
    "TIME", "HOUR", "MINUTE", "SECOND",
    "WEEKDAY", "WEEKNUM", "ISOWEEKNUM",
    "EOMONTH", "EDATE", "WORKDAY", "NETWORKDAYS",
    "DATEDIF", "DAYS", "DAYS360",
    "DATEVALUE", "TIMEVALUE",

    # Database & Array
    "SUMPRODUCT", "MMULT", "MDETERM", "MINVERSE",
    "TRANSPOSE", "UNIQUE", "SORT", "SORTBY", "FILTER",
    "SEQUENCE", "RANDARRAY", "ARRAY",

    # Information & Error Checking
    "ISBLANK", "ISERROR", "ISERR", "ISNA",
    "ISNUMBER", "ISTEXT", "ISLOGICAL", "ISREF",
    "ISFORMULA", "ISNONTEXT", "ISODD", "ISEVEN",
    "TYPE", "N", "NA", "ERROR.TYPE",
    "CELL", "INFO", "SHEET", "SHEETS",

    # Statistical (Extended)
    "AVERAGEA", "COUNTA", "MAXA", "MINA",
    "CORREL", "COVARIANCE.P", "COVARIANCE.S",
    "FORECAST", "FORECAST.LINEAR", "INTERCEPT", "SLOPE",
    "PEARSON", "RSQ", "STEYX", "TREND", "GROWTH",
    "PERCENTILE", "PERCENTILE.INC", "PERCENTILE.EXC",
    "QUARTILE", "QUARTILE.INC", "QUARTILE.EXC",
    "RANK", "RANK.AVG", "RANK.EQ",
    "STANDARDIZE", "Z.TEST",

    # Engineering (Common ones)
    "CONVERT", "BIN2DEC", "BIN2HEX", "BIN2OCT",
    "DEC2BIN", "DEC2HEX", "DEC2OCT",
    "HEX2BIN", "HEX2DEC", "HEX2OCT",
    "OCT2BIN", "OCT2DEC", "OCT2HEX",

    # Web & External Data
    "WEBSERVICE", "FILTERXML", "ENCODEURL",
}

# Common function name mistakes and their corrections
FUNCTION_SUGGESTIONS: Dict[str, str] = {
    "SUMPMT": "Use CUMIPMT for cumulative interest, or PMT for payment amount",
    "AVGIF": "Use AVERAGEIF",
    "AVGIFS": "Use AVERAGEIFS",
    "LOOKUP2": "Use VLOOKUP or XLOOKUP",
    "SUMAVERAGE": "Use SUM and AVERAGE separately",
    "COUNTUNIQUE": "Use SUMPRODUCT(1/COUNTIF(range,range))",
    "CONCATENATEIF": "Use TEXTJOIN with IF",
    "VLOOK": "Use VLOOKUP",
    "HLOOK": "Use HLOOKUP",
    "AVGERAGE": "Use AVERAGE (check spelling)",
    "SUMM": "Use SUM (check spelling)",
}


def extract_function_names(formula: str) -> List[str]:
    """
    Extract all function names from an Excel formula.

    Args:
        formula: Excel formula string (with or without leading '=')

    Returns:
        List of unique function names found in the formula
    """
    # Remove string literals to avoid false positives
    # Match both single and double quotes
    formula_cleaned = re.sub(r'"[^"]*"', '', formula)
    formula_cleaned = re.sub(r"'[^']*'", '', formula_cleaned)

    # Find function names (word characters followed by opening paren)
    # Pattern: word boundary, uppercase letter start, word chars, optional whitespace, opening paren
    functions = re.findall(r'\b([A-Z][A-Z0-9_.]*)\s*\(', formula_cleaned)

    return list(set(functions))  # Return unique names


def extract_potential_names(formula: str) -> List[str]:
    """
    Extract potential named ranges from formula (heuristic).

    Named ranges are typically:
    - All uppercase or mixed case
    - Not cell references (A1, B2, etc.)
    - Not worksheet references
    - Not function names

    Args:
        formula: Excel formula string

    Returns:
        List of potential named range references
    """
    # Remove string literals
    formula_cleaned = re.sub(r'"[^"]*"', '', formula)
    formula_cleaned = re.sub(r"'[^']*'", '', formula_cleaned)

    # Remove worksheet references (Sheet1!A1)
    formula_cleaned = re.sub(r"'[^']*'!", '', formula_cleaned)
    formula_cleaned = re.sub(r'\w+!', '', formula_cleaned)

    # Find words that could be named ranges
    # Pattern: uppercase letter or underscore, followed by word chars
    # Must be 3+ chars to avoid false positives on cell refs
    potential_names = re.findall(r'\b([A-Z_][A-Z0-9_]{2,})\b', formula_cleaned)

    # Filter out function names
    functions = set(extract_function_names(formula))

    # Filter out obvious cell references (A1, B2, etc.) and ranges (A1:B10)
    cell_pattern = re.compile(r'^[A-Z]{1,3}\d+$')

    names = [
        name for name in potential_names
        if name not in functions
        and name not in VALID_EXCEL_FUNCTIONS
        and not cell_pattern.match(name)
    ]

    return list(set(names))


def extract_cell_references(formula: str) -> List[str]:
    """
    Extract all cell references from an Excel formula.

    Examples:
        "=A1 + B2" → ["A1", "B2"]
        "=SUM(A1:A10)" → ["A1:A10"]
        "='Sheet1'!B1" → ["Sheet1!B1"]
        "=B1 * (B2 / 100)" → ["B1", "B2"]

    Args:
        formula: Excel formula string

    Returns:
        List of cell references found in the formula
    """
    # Remove ONLY double-quoted string literals to avoid false positives
    # DO NOT remove single quotes - they're part of worksheet references like 'Sheet Name'!A1
    formula_cleaned = re.sub(r'"[^"]*"', '', formula)

    # Pattern for cell references with optional worksheet qualifier
    # Matches: A1, B2, AA10, Sheet1!A1, 'Sheet Name'!A1, A1:B10

    # First, extract quoted worksheet references: 'Sheet Name'!A1
    quoted_refs = re.findall(r"'([^']+)'!([A-Z]+\d+(?::[A-Z]+\d+)?)", formula)
    refs_with_sheets = [f"{sheet}!{ref}" for sheet, ref in quoted_refs]

    # Extract unquoted worksheet references: Sheet1!A1
    unquoted_refs = re.findall(r'(\w+)!([A-Z]+\d+(?::[A-Z]+\d+)?)', formula_cleaned)
    refs_with_sheets.extend([f"{sheet}!{ref}" for sheet, ref in unquoted_refs])

    # Remove worksheet references from formula for plain cell extraction
    # Must match the same pattern as extraction to avoid orphaned cell refs
    formula_no_sheets = re.sub(r"'[^']+'![A-Z]+\d+(?::[A-Z]+\d+)?", '', formula_cleaned)
    formula_no_sheets = re.sub(r'\w+![A-Z]+\d+(?::[A-Z]+\d+)?', '', formula_no_sheets)

    # Extract plain cell references: A1, B2, A1:B10
    plain_refs = re.findall(r'\b([A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?)\b', formula_no_sheets)

    # Combine all references
    all_refs = refs_with_sheets + plain_refs

    return list(set(all_refs))


def _cell_in_range(cell: str, start_cell: str, end_cell: str) -> bool:
    """
    Check if a cell falls within a given range.

    Args:
        cell: Cell address (e.g., "B2")
        start_cell: Range start (e.g., "B1")
        end_cell: Range end (e.g., "B5")

    Returns:
        bool: True if cell is within the range
    """
    import re
    from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

    try:
        # Parse cell coordinates
        cell_col, cell_row = coordinate_from_string(cell.upper())
        start_col, start_row = coordinate_from_string(start_cell.upper())
        end_col, end_row = coordinate_from_string(end_cell.upper())

        # Convert column letters to numbers for comparison
        cell_col_num = column_index_from_string(cell_col)
        start_col_num = column_index_from_string(start_col)
        end_col_num = column_index_from_string(end_col)

        # Ensure start <= end for both row and column
        min_row, max_row = min(start_row, end_row), max(start_row, end_row)
        min_col, max_col = min(start_col_num, end_col_num), max(start_col_num, end_col_num)

        # Check if cell is within range
        return (min_row <= cell_row <= max_row and
                min_col <= cell_col_num <= max_col)

    except Exception:
        # If parsing fails, assume not in range
        return False


def detect_circular_reference(cell: str, formula: str, worksheet: str = None) -> Dict[str, any]:
    """
    Detect if a formula contains a circular reference to its own cell.

    Enhanced detection for complex circular references including:
    - Direct self-reference: B1 = B1 + 1
    - Implicit same-sheet reference: B1 = B1 - B2
    - Cross-sheet same cell: Workings!B1 = Workings!B1 + 1
    - Range-based circular: B1 = SUM(A1:C1) where B1 is in A1:C1

    Args:
        cell: Target cell address (e.g., "B1", "A10")
        formula: Excel formula string
        worksheet: Optional worksheet name for context

    Returns:
        {
            "is_circular": bool,
            "error": str or None,
            "suggestion": str or None
        }
    """
    # Extract all cell references from the formula
    cell_refs = extract_cell_references(formula)

    # Normalize cell to uppercase
    cell = cell.upper()

    # ENHANCED: More aggressive detection with detailed error messages

    # 1. Check for direct self-reference (B1 in formula when setting B1)
    for ref in cell_refs:
        # Handle both plain cell refs and sheet-qualified refs
        if '!' in ref:
            sheet_part, cell_part = ref.split('!', 1)
            # If no worksheet provided, assume same sheet for comparison
            if worksheet is None or sheet_part == worksheet:
                if cell_part.upper() == cell:
                    return {
                        "is_circular": True,
                        "error": f"CIRCULAR REFERENCE DETECTED: Cell {worksheet+'!' if worksheet else ''}{cell} references itself via {ref}",
                        "suggestion": f"Remove {ref} from the formula or use a cell from a different worksheet (e.g., Assumptions!{cell})"
                    }
        else:
            # Plain cell reference - check against target cell
            if ref.upper() == cell:
                return {
                    "is_circular": True,
                    "error": f"CIRCULAR REFERENCE DETECTED: Cell {cell} references itself directly in formula",
                    "suggestion": f"Remove {cell} from the formula or reference a different cell"
                }

    # 2. ENHANCED: Check for range-based circular references with better detection
    import re
    # Match both plain ranges (A1:B5) and function ranges (SUM(A1:B5))
    range_patterns = [
        r'([A-Z]+\d+):([A-Z]+\d+)',  # Plain ranges: A1:B5
        r'\w+\s*\(\s*([A-Z]+\d+):([A-Z]+\d+)',  # Function ranges: SUM(A1:B5)
    ]

    for pattern in range_patterns:
        range_matches = re.findall(pattern, formula.upper())
        for start_cell, end_cell in range_matches:
            if _cell_in_range(cell, start_cell, end_cell):
                return {
                    "is_circular": True,
                    "error": f"CIRCULAR REFERENCE DETECTED: Cell {cell} is within range {start_cell}:{end_cell} referenced in its own formula",
                    "suggestion": f"Either exclude {cell} from the range {start_cell}:{end_cell} or place the formula in a different cell"
                }

    # 3. ENHANCED: Check for indirect circular references through same-sheet qualified names
    if worksheet:
        # Check if formula references current worksheet explicitly
        same_sheet_pattern = f"{worksheet}!"
        if same_sheet_pattern in formula:
            # Extract all references to current sheet
            import re
            current_sheet_refs = re.findall(rf'{re.escape(worksheet)}!([A-Z]+\d+)', formula, re.IGNORECASE)
            if cell in [ref.upper() for ref in current_sheet_refs]:
                return {
                    "is_circular": True,
                    "error": f"CIRCULAR REFERENCE DETECTED: Cell {worksheet}!{cell} explicitly references itself in formula",
                    "suggestion": f"Remove {worksheet}!{cell} reference or use a cell from a different worksheet"
                }

    # No circular reference detected
    return {
        "is_circular": False,
        "error": None,
        "suggestion": None
    }


def validate_formula(formula: str, cell: str = None, worksheet: str = None) -> Dict[str, any]:
    """
    Validate an Excel formula for syntax and function name errors.

    Args:
        formula: Excel formula string (should start with '=')

    Returns:
        Dictionary with validation results:
        {
            "valid": bool,              # True if no errors
            "errors": List[str],        # List of error messages
            "warnings": List[str],      # List of warning messages
            "functions_used": List[str], # Functions found in formula
            "potential_names": List[str] # Potential named ranges
        }
    """
    errors: List[str] = []
    warnings: List[str] = []

    formula_stripped = formula.strip()

    # Check basic syntax
    if not formula_stripped.startswith('='):
        errors.append("Formula must start with '='")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "functions_used": [],
            "potential_names": []
        }

    # Check for empty formula
    if formula_stripped == '=':
        errors.append("Formula is empty (only '=')")
        return {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "functions_used": [],
            "potential_names": []
        }

    # Check for circular references if cell and/or worksheet provided
    if cell:
        circ_check = detect_circular_reference(cell, formula, worksheet)
        if circ_check["is_circular"]:
            errors.append(circ_check["error"])
            if circ_check["suggestion"]:
                warnings.append(f"Suggestion: {circ_check['suggestion']}")

    # Extract function names
    functions = extract_function_names(formula)

    # Check for invalid functions
    invalid_funcs = [f for f in functions if f not in VALID_EXCEL_FUNCTIONS]

    for func in invalid_funcs:
        suggestion = FUNCTION_SUGGESTIONS.get(
            func,
            "Verify this is a valid Excel function"
        )
        errors.append(f"Invalid function '{func}': {suggestion}")

    # Check for potential undefined named ranges
    potential_names = extract_potential_names(formula)

    for name in potential_names:
        warnings.append(
            f"Potential undefined name '{name}' - ensure it's defined or use cell reference instead"
        )

    # Check for common syntax issues
    paren_balance = formula.count('(') - formula.count(')')
    if paren_balance != 0:
        errors.append(
            f"Unbalanced parentheses: {abs(paren_balance)} {'extra opening' if paren_balance > 0 else 'extra closing'}"
        )

    bracket_balance = formula.count('[') - formula.count(']')
    if bracket_balance != 0:
        errors.append(
            f"Unbalanced brackets: {abs(bracket_balance)} {'extra opening' if bracket_balance > 0 else 'extra closing'}"
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "functions_used": functions,
        "potential_names": potential_names
    }


def validate_formulas_batch(formulas: List[str]) -> Dict[str, any]:
    """
    Validate multiple formulas and return aggregated results.

    Args:
        formulas: List of Excel formula strings

    Returns:
        {
            "total": int,
            "valid": int,
            "invalid": int,
            "results": List[Dict]  # Individual validation results
        }
    """
    results = [validate_formula(f) for f in formulas]

    return {
        "total": len(formulas),
        "valid": sum(1 for r in results if r["valid"]),
        "invalid": sum(1 for r in results if not r["valid"]),
        "results": results
    }
