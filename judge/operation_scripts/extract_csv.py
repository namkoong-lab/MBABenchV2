"""Extract CSVs from an Excel (.xlsx) file using the project's enhanced extraction pipeline.

Usage:
    python -m operation_scripts.extract_csv path/to/file.xlsx
    python -m operation_scripts.extract_csv path/to/file.xlsx -o path/to/output
"""

import argparse
import sys
from pathlib import Path

from utils.excel_utils import _attempt_sheet_name_filter, process_all_worksheets
from utils.logger import logger
from utils.misc_utils import load_project_configs


def main():
    parser = argparse.ArgumentParser(
        description="Extract enhanced CSVs from an Excel (.xlsx) file."
    )
    parser.add_argument(
        "file",
        help="Path to the Excel file to extract CSVs from.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Output directory for extracted CSVs. "
        "Defaults to judge/scratch/extracted_csvs_output/<file_stem>/",
    )
    parser.add_argument(
        "--run-calculation",
        action="store_true",
        help="Run Excel formula calculations via LibreOffice before extracting CSVs.",
    )
    parser.add_argument(
        "--attempt-sheet-name-filter", "--filter-attempts", "-fa",
        action="store_true",
        help="Only keep attempt sheets starting with 'answers_' or 'model_', "
        "stripping the prefix from the output name. Other sheets are kept "
        "with a '_nograde_' prefix.",
    )
    args = parser.parse_args()

    load_project_configs()

    # Resolve file path relative to cwd
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        sys.exit(1)
    if file_path.suffix.lower() not in (".xlsx", ".xls"):
        logger.error(f"Expected an Excel file (.xlsx/.xls), got: {file_path.suffix}")
        sys.exit(1)

    # Resolve output directory relative to cwd
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    else:
        project_root = Path(__file__).parent.parent.resolve()
        output_dir = project_root / "scratch" / "extracted_csvs_output" / file_path.stem

    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Extracting CSVs from: {file_path}")
    logger.info(f"Output directory:     {output_dir}")

    sheet_filter = _attempt_sheet_name_filter if args.attempt_sheet_name_filter else None

    process_all_worksheets(
        excel_file_path=str(file_path),
        output_dir=output_dir,
        run_calculation=args.run_calculation,
        sheet_name_filter=sheet_filter,
    )

    logger.info(f"Done. CSVs saved to: {output_dir}")


if __name__ == "__main__":
    main()
