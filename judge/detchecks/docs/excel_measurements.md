# Excel session answers (Patrick, 2026-10-03)

1. Frozen sheet whose only stored selection is A1 in the top-left pane (attempt 1408): Excel opens with the cursor on the FIRST SCROLLING CELL, not A1. DCF -> D7, Assumptions -> D7, WACC -> F10 (each sheet's frozen corner). Code assumption (A1) is WRONG: such sheets must FAIL Active cell reset to A1 (62). ~38 corpus files flip to fail.
2. Conditional-format (dxf) solid fill: Excel paints bgColor ONLY; fgColor is ignored. fgColor yellow + no bgColor -> no fill; fgColor yellow + bgColor indexed 64 -> BLACK; fgColor yellow + bgColor blue -> blue; fgColor indexed 64 + bgColor yellow -> yellow; fgColor yellow + bgColor 00000000 -> BLACK (alpha ignored). Screenshot confirmed. Agent-tool CF "yellow" fills never show yellow; some show black (matters for No white-on-white hiding (94): black text on them would be hidden).
3. CF "contains text" treats * and ? as WILDCARDS (Excel evaluates the stored SEARCH formula): PASS matched "P*S" and "P?SS"; literal "P*S" matched too. All red/bold. Evaluate containsText rules with wildcard semantics.
4. Number-format colour tags do NOT apply to error values: #N/A and #DIV/0! under [White]/[Red] show in the normal (black) font; number 5 under [White]General is invisible. Code assumption (errors shown unformatted) CONFIRMED; no change.
5. showZeros=0: a format WITH an explicit zero section still prints that section; only General and single-section formats go blank. Seen: General -> blank; #,##0;(#,##0);"-" -> "-"; 0.00;-0.00;0.00 -> "0.00"; 0;-0;"nil" -> "nil"; General;General;"zero" -> "zero"; 0.00 (one section) -> blank. Code assumption WRONG for 3-section formats: they must be rendered (a "0.00" zero section then fails Zeros as dashes (66); "nil"/"zero" text per the non-dash-text rule). 0 corpus files affected today.
6. Row heights WITHOUT customHeight are NOT honoured: Excel auto-fits. Row 3 (sheet default 80 pt, not custom) -> 15 pt; row 5 (ht 60, not custom) -> 15 pt; row 7 (ht 60, custom) -> 60 pt. Code assumption WRONG: Reasonable row heights (73) must ignore stored heights (row ht and sheetFormatPr defaultRowHeight) that lack customHeight. Attempt 1505 flips to pass.
7. outlineLevelRow=0 in the sheet header does NOT stop grouping: Excel shows the 1/2 outline buttons and a "+" at row 14 that expands the hidden rows 10-13. Rows' own outline levels decide. Code assumption CONFIRMED (hidden rows in such a collapsed group are exempt from No hidden rows/columns (93)); no change.
8. Number-format oddities (no ##### anywhere):
   - TRUE under ;;; -> blank (assumed, CONFIRMED)
   - TRUE under 0;-0;0;"txt" -> "txt" (assumed TRUE: WRONG — booleans go through the text section)
   - text abc under [Red]0 -> black (colour tag of a numeric section does not apply to text)
   - -5 under [<0]0;0 -> 5 (assumed, CONFIRMED: no automatic minus in a conditional section)
   - -5 under [>0]0;0 -> 5 (assumed -5: WRONG — no automatic minus in the fallback section either)
   - 123 under a one-space format -> blank (assumed, CONFIRMED)
   - 123 under an empty format code -> 123, no repair prompt (assumed General, CONFIRMED)
   - 5 under [Red]0 + CF font blue -> BLUE (conditional-format font colour beats the number-format colour tag)
   - 50000 under [h]:mm -> 1200000:00 (assumed hours, CONFIRMED)
   All these cases can now be marked certain in numfmt.py.

Session complete 2026-10-03. Fixes to apply: Q1 (62 active cell = frozen corner when the stored selection sits in a non-active pane), Q2 (dxf fill = bgColor only), Q3 (containsText wildcards), Q5 (showZeros=0 keeps explicit zero sections), Q6 (ignore non-custom row heights in 73), Q8 (numfmt: booleans via text section, no auto-minus in conditional/fallback sections, text ignores numeric colour tags, CF font beats format colour). Q4 and Q7 confirmed, no change.

## Follow-up measurements (Patrick, 2026-10-04, by hand)

9. Active cell reset to A1 (62), openpyxl's frozen-pane shape (attempt 1272, tab 'FCF': `<pane xSplit="5" ySplit="10" topLeftCell="F11" activePane="bottomRight"/>` + `<selection pane="bottomRight" activeCell="A1" sqref="A1"/>`): Excel's Name Box shows **A1**. Excel keeps a stored selection of the active pane even when the cell lies outside that pane. Code assumption CONFIRMED; the 89 attempts with this shape keep passing. (Contrast with item 1: when the active pane has NO selection, Excel uses the pane's top-left cell.) By the same logic Excel Online's bare `<selection pane="bottomRight"/>` (no activeCell; attempts 1285, 1375, 1505, 2208) is read as A1 — inferred, not measured.
10. Consistent zoom level (61), a sheet storing both zoomScale="100" and zoomScaleNormal="80" (attempt 1272, tab 'Instructions', normal view): Excel shows **100%**. The check's reading (zoomScale for the normal view) is CONFIRMED; the 22 attempts with this shape keep passing.

## Follow-up measurements (Patrick, 2026-10-06, by hand)

11. Active cell reset to A1 (62), several `<selection>` elements for the active pane (attempt 2379 `MeridianOutdoor`, tabs 'Income Statement' and 'Budget - Income Statement': `<pane xSplit="1" ySplit="8" topLeftCell="B9" activePane="bottomRight" state="frozen"/>` with `<selection pane="bottomRight" activeCell="A1" sqref="A1"/>` followed later by `<selection pane="bottomRight" activeCell="B9" sqref="B9"/>`): Name Box shows **B9** on both. Attempt 2402 `Telecom`, tab 'Balance Sheet' (bottomRight `A1` then `A4`): **A4**. Rule: the LAST stored selection of the active pane decides (the earlier ones are ignored). Code assumption (undecidable, raise) replaced by the rule; attempts 2379, 2402, 3390 and 3505, which judge v13 could not grade, now fail 62.
