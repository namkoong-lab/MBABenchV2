"""Saving a workbook whose column starts inside a merged range must not crash
(2026-09-19: copy_file died with "'MergedCell' object has no attribute
'column_letter'" on CoastalAggregates, so solution.xlsx was never created)."""
from openpyxl import Workbook, load_workbook

from excel_mcp_server.core.workbook_io import _auto_fit_columns, _save_workbook_sync


def test_a_merged_title_row_does_not_break_the_save(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "A long merged title across the top of the sheet"
    ws.merge_cells("A1:D2")                      # B1, C1, D1 become MergedCell - the first cell of their columns
    ws["B3"], ws["C3"] = "label", 12345.678
    _auto_fit_columns(wb)                        # raised AttributeError before the fix
    assert ws.column_dimensions["B"].width >= 10 and ws.column_dimensions["D"].width >= 10
    out = tmp_path / "merged.xlsx"
    _save_workbook_sync(wb, out)
    assert "A1:D2" in [str(r) for r in load_workbook(out).active.merged_cells.ranges]
