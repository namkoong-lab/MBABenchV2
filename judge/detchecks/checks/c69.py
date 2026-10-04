"""69 Formatting/Sufficient column widths.

Rubric: "All values display fully; no truncation or '###'" / bad: "Some columns truncate values or
show '###'".  Rule as implemented (report.md approach C, "sure overflow"):

A visible numeric or date cell - constant or formula result, spill / array members included -
FAILS when the text Excel displays for it (its full number format, including a conditional-format
number format that applies) is wider than the column's text area under EVERY platform model by more
than SURE_TOL_PX (2 px at 96 dpi).  Excel then shows '####' whatever the viewer's platform.

  * Platform models (MODELS): Windows 96 dpi and 120 dpi (Excel for Mac behaves like the 120 dpi
    grid: a 9 px digit for Calibri 11), each with per-glyph rounded advances (GDI) and fractional
    advances.  A cell that overflows under some models only is a "band" case: reported in stats,
    never a mistake.
  * Column text area = column pixels - CELL_MARGIN_PX (5), merged spans summed for the anchor cell
    (covered cells are not displayed).  Column pixels follow ECMA-376 col/@width:
    px = trunc(((256 * w + trunc(128 / MDW)) / 256) * MDW), where MDW is the Normal font's maximum
    digit width in whole pixels at the model's dpi (7 px for Calibri 11, Aptos Narrow 11 and Arial
    10 at 96 dpi; 9 px at 120 dpi).  A column without <col> is baseColWidth (8) digits + 5 px rounded
    up to a multiple of 8 px, or sheetFormatPr/@defaultColWidth through the formula; for a
    LibreOffice-style defaultColWidth the lenient reading trunc(w * MDW + 5) is also allowed in the
    non-reference models (Excel's reading of it is unverified).
  * Text width = sum of the glyph advances of the cell's own font face, size and weight (GLYPHS,
    1/1000 em per character, read once from the real font files with fontTools - see
    detchecks/scratch/colwidths/glyph_table.py).  Rounded model: each advance rounded to whole
    pixels at ppem = size * dpi / 72 (plain rounding; the fonts' hdmx tables agree with it for
    Arial, and Calibri / Aptos have none).  Padding tokens `_x` are measured as the glyph x;
    `?` blanks as a space (Excel: a digit width; lenient).  Unknown faces are measured as Calibri,
    the narrowest common face (a miss, never a false '###'), and listed in stats.
  * Alignment indent (left / right / distributed) takes INDENT_SPACES_PER_LEVEL (3) Normal-font
    spaces per level off the text area, as in Reasonable row heights (73).
  * General format never shows '####' while a shorter rendering exists: Excel rounds decimals away
    and falls back to d E+nn scientific notation.  The need of a General cell is therefore the
    narrowest of its full text, its 0-decimal rounding and its 1-digit scientific form.
  * Dates count (they are numbers; toy T3).  Negative / out-of-range dates show '#####' at any width
    and are not a width defect (NEGATIVE_DATE_FAILS = False; counted in stats).
  * Shrink-to-fit cells never show '####' (exempt).  Cells in hidden rows or columns (hidden flag,
    zero width / height) are not displayed and are skipped (No hidden rows/columns (93) owns them),
    except the anchor of a merge whose span still has a visible column and a visible row: Excel draws
    the merged value across the visible part, so it is measured against the span's visible columns in
    a second pass (review fix 69-F4; HIDDEN_ANCHOR_SPAN).
    Hidden sheets ARE graded (whole-workbook rule, as 73 and 66 do).  Fill, centre-across-selection
    and rotated numbers are skipped (how Excel clips them is unverified; counted in stats).
    Booleans and errors are not values this check measures.
  * Text labels overflowing into empty neighbours, or cut off by a filled neighbour, are ordinary
    formatting and pass (toy T4; rubric guidance), with two exceptions from report.md, each behind a
    constant: TEXT_NUMBER_CLIP - a number stored as text (constant, <= TEXT_NUMBER_MAX_CHARS
    characters, left/general aligned, unwrapped) that is cut off by a filled cell to its right by at
    least one Normal digit under every model; TEXT_HIDDEN_COL_MAX_PX - any text of >= 3 characters
    in a column of at most 12 px (reference model) whose overflow is blocked by a filled cell.

Values.  Constants are read from the file and routed by their stored type.  A formula result is
routed by its TRUSTED value, never by the delivered `t` attribute (review fix 69-F1: `t` is a cache
attribute - agent tools write t="str" placeholders with empty caches, LibreOffice a #NAME? placeholder -
so a number behind such a placeholder was never measured, even when a value copy held it).  A trusted
value (Excel cache, LibreOffice copy, unknown-writer cache) that is a number is measured; a trusted
text / boolean / error result is not a value this check measures (the recalc pipeline vets a LibreOffice
copy's #NAME? / #VALUE! values, so with it those cells are trusted errors and skipped, as ruled
2026-10-04).  An untrusted value (openpyxl / XlsxWriter cache, no cache, a LibreOffice-written #NAME? /
#VALUE! cache read without the pipeline, missing from the copy) leaves the cell
undecided whatever its placeholder type - unless its display cannot depend on the value: a covered
(non-anchor) merged cell is never displayed, and a format whose numeric sections are all empty (';;;')
prints nothing for every number (review fix 69-F2; a conditional-format number format over the cell
keeps it undecided).  If the workbook fails on measured cells anyway, undecided cells are listed in
stats and nothing is raised (UNTRUSTED_ONLY_IF_VERDICT_NEEDS); otherwise the first one raises
GradingError (no fallback).  The same holds for a conditional-format number format whose rule cannot
be evaluated when the possible displays disagree, and for a rendering numfmt marks uncertain.

Conditional formats.  Sheets whose rules carry a number format (dxf numFmt) are streamed a second
time: every numeric cell inside such a range is rendered under each possible outcome (c66.fires
decides which rules fire; unknown rules branch), and the cell fails only if every outcome overflows.
First-pass candidates inside those ranges are handed to the second pass.
"""
from __future__ import annotations

import itertools
import math
import re
from bisect import bisect_left, bisect_right

from ..core import numfmt as N
from ..core.refs import group_cells, index_to_col, location, make_ref, range_to_str
from ..errors import GradingError
from .base import Check
from .c66 import UNKNOWN, fires

# ---------------------------------------------------------------- rule constants
SURE_TOL_PX = 2.0                 # a cell fails when its overflow (96-dpi px) exceeds this under EVERY model
CELL_MARGIN_PX = 5                # column pixels not available to text (ECMA-376 padding)
MODELS = (("w96r", 96, True), ("w96f", 96, False), ("w120r", 120, True), ("w120f", 120, False))
REFERENCE_MODEL = "w96r"          # strict default-column reading; also the model quoted in messages
DEFAULT_FONT_PT = 11.0
DEFAULT_BASE_COL_WIDTH = 8.0
INDENT_SPACES_PER_LEVEL = 3       # ECMA-376 alignment/@indent: one level = 3 spaces of the Normal font
INDENT_ALIGNMENTS = ("left", "right", "distributed")
NEGATIVE_DATE_FAILS = False       # '#####' for a negative date/time is not a width defect
GENERAL_SHORTENS = True           # General format falls back to fewer decimals / scientific before '####'
UNTRUSTED_ONLY_IF_VERDICT_NEEDS = True   # undecided cells raise only while the verdict is still open
HIDDEN_ANCHOR_SPAN = True         # a merge anchor in a hidden row / column is measured against the visible span (69-F4)
TEXT_NUMBER_CLIP = True           # a number stored as text cut off by a filled neighbour fails
TEXT_NUMBER_MAX_CHARS = 20
TEXT_NUMBER_MIN_CLIP_DIGITS = 1.0  # ... when at least this many Normal digits are cut off (every model)
TEXT_HIDDEN_COL_MAX_PX = 12       # any text (>= 3 chars) in a column this narrow, blocked by a filled cell, fails
TEXT_HIDDEN_MIN_CHARS = 3
TEXT_WALK_MAX_COLS = 60           # how far right an overflowing label is followed for a blocker
MAX_UNDECIDED_LISTED = 12
MAX_BAND_LISTED = 8
MAX_CANDIDATES = 2_000_000        # buffered overflow candidates per sheet; more raises
RENDER_CACHE_MAX = 400_000

_NUMLIKE = re.compile(r"^\(?[-+]?[$€£¥]?\s?\d[\d,]*(\.\d+)?\s?%?\)?$")

# ---------------------------------------------------------------- glyph metrics (stdlib only)
# Advance widths in 1/1000 em of every character a rendered number or date can contain, per face and
# weight, read once from the real font files (detchecks/scratch/colwidths/glyph_table.py, fontTools).
# 0 = the face lacks the glyph (measured as its digit).  Order = GLYPH_CHARS.
GLYPH_CHARS = '0123456789.,\'()-+%/:$€£¥ ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!"#&*;<=>?@[\\]^_`{|}~\xa0−–—’°×…'
GLYPHS = {
    ("calibri", False): (507,507,507,507,507,507,507,507,507,507,252,250,221,303,303,306,498,715,386,268,507,507,507,507,226,579,544,533,615,488,459,631,623,252,319,520,420,855,646,662,517,673,543,459,487,642,567,890,519,487,468,479,525,423,525,498,305,471,525,229,239,455,229,799,525,527,525,525,349,391,335,525,452,715,433,453,395,326,401,498,682,498,268,498,498,498,463,894,307,386,307,498,498,291,314,460,314,498,226,498,498,905,250,339,498,690),
    ("calibri", True): (507,507,507,507,507,507,507,507,507,507,267,258,233,312,312,306,498,729,430,276,507,507,507,507,226,606,561,529,630,488,459,637,631,267,331,547,423,874,659,676,532,686,563,473,495,653,591,906,551,520,478,494,537,418,537,503,316,474,537,246,255,480,246,813,537,538,537,537,355,399,347,537,473,745,459,474,397,326,438,498,705,498,276,498,498,498,463,898,325,430,325,498,498,300,344,475,344,498,226,498,498,905,258,342,498,711),
    ("calibri light", False): (507,507,507,507,507,507,507,507,507,507,245,245,214,299,299,306,498,707,362,263,507,507,507,507,226,563,535,535,607,489,460,627,619,244,312,504,419,845,638,654,508,666,532,453,483,636,554,881,501,469,463,471,520,425,520,494,299,469,520,221,230,441,221,791,520,521,520,520,345,387,329,520,440,699,418,441,394,326,380,498,670,498,263,498,498,498,463,892,297,362,297,498,498,286,299,453,299,498,226,498,498,905,245,337,498,679),
    ("calibri light", True): (507,507,507,507,507,507,507,507,507,507,267,258,233,312,312,306,498,729,430,276,507,507,507,507,226,606,561,529,630,488,459,637,631,267,331,547,423,874,659,676,532,686,563,473,495,653,591,906,551,520,478,494,537,418,537,503,316,474,537,246,255,480,246,813,537,538,537,537,355,399,347,537,473,745,459,474,397,326,438,498,705,498,276,498,498,498,463,898,325,430,325,498,498,300,344,475,344,498,226,498,498,905,258,342,498,711),
    ("arial", False): (556,556,556,556,556,556,556,556,556,556,278,278,191,333,333,333,584,889,278,278,556,556,556,556,278,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,278,355,556,667,389,278,584,584,584,556,1015,278,278,278,469,556,333,334,260,334,584,278,584,556,1000,222,400,584,1000),
    ("arial", True): (556,556,556,556,556,556,556,556,556,556,278,278,238,333,333,333,584,889,278,333,556,556,556,556,278,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,333,474,556,722,389,333,584,584,584,611,975,333,278,333,584,556,333,389,280,389,584,278,584,556,1000,278,400,584,1000),
    ("arial narrow", False): (456,456,456,456,456,456,456,456,456,456,228,228,157,273,273,273,479,729,228,228,456,456,456,456,228,547,547,592,592,547,501,638,592,228,410,547,456,683,592,638,547,638,592,547,501,592,547,774,547,547,501,456,456,410,456,456,228,456,456,182,182,410,182,683,456,456,456,456,273,410,228,456,410,592,410,410,410,228,291,456,547,319,228,479,479,479,456,832,228,228,228,385,456,273,274,213,274,479,228,479,456,820,182,400,479,820),
    ("arial narrow", True): (456,456,456,456,456,456,456,456,456,456,228,228,195,273,273,273,479,729,228,273,456,456,456,456,228,592,592,592,592,547,501,638,592,228,456,592,501,683,592,638,547,638,592,547,501,592,547,774,547,547,501,456,501,456,501,456,273,501,501,228,228,456,228,729,501,501,501,501,319,456,273,501,456,638,456,456,410,273,389,456,592,319,273,479,479,479,501,800,273,228,273,479,456,273,319,230,319,479,456,479,456,820,228,400,479,820),
    ("aptos narrow", False): (507,507,507,507,507,507,507,507,507,507,260,260,192,304,304,306,507,761,313,260,507,507,507,507,187,534,553,633,627,509,480,646,646,243,305,522,457,722,645,667,529,667,555,519,436,621,531,813,505,493,470,487,514,479,514,485,278,450,507,224,224,448,243,784,507,505,514,514,309,443,296,510,414,656,406,414,399,326,339,498,588,498,260,507,507,507,455,806,271,313,271,507,416,499,271,462,271,507,187,507,416,832,247,365,507,792),
    ("aptos narrow", True): (507,507,507,507,507,507,507,507,507,507,267,267,207,307,307,306,507,752,354,267,507,507,507,507,181,553,558,634,633,515,481,641,653,272,333,562,459,734,646,658,544,658,569,538,450,618,549,840,537,526,489,499,530,496,530,506,304,461,525,251,252,482,271,806,525,517,530,530,336,459,319,525,445,677,444,445,417,326,377,498,610,498,267,507,507,507,459,766,304,354,304,507,405,485,304,466,304,507,181,507,405,809,267,377,507,815),
    ("aptos", False): (534,534,534,534,534,534,534,534,534,534,286,286,210,293,293,340,534,826,339,286,534,534,534,534,203,589,604,692,686,556,524,708,707,260,331,568,500,790,706,732,577,732,606,566,479,681,585,892,553,540,516,531,561,525,561,527,301,484,551,239,239,487,260,853,551,552,561,561,334,486,323,559,452,721,442,452,438,293,370,537,643,457,286,534,534,534,501,895,294,339,294,534,460,552,294,270,294,534,203,534,460,920,270,365,534,818),
    ("aptos", True): (534,534,534,534,534,534,534,534,534,534,300,300,232,293,293,340,534,841,387,300,534,534,534,534,203,620,619,707,707,573,537,718,729,294,367,623,512,821,723,737,605,737,634,598,505,696,617,940,599,586,547,552,586,549,586,556,335,511,579,268,268,534,296,889,579,573,586,586,369,516,355,582,494,761,495,494,466,293,420,537,677,457,300,534,534,534,518,900,337,387,337,534,460,552,337,270,337,534,203,534,460,920,298,378,534,850),
    ("times new roman", False): (500,500,500,500,500,500,500,500,500,500,250,250,180,333,333,333,564,833,278,278,500,500,500,500,250,722,667,667,722,611,556,722,722,333,389,722,611,889,722,722,556,722,667,556,611,722,722,944,722,722,611,444,500,444,500,444,333,500,500,278,278,500,278,778,500,500,500,500,333,389,278,500,500,722,500,500,444,333,408,500,778,500,278,564,564,564,444,921,333,278,333,469,500,333,480,200,480,541,250,564,500,1000,333,400,564,1000),
    ("times new roman", True): (500,500,500,500,500,500,500,500,500,500,250,250,278,333,333,333,570,1000,278,333,500,500,500,500,250,722,667,722,722,667,611,778,778,389,500,778,667,944,722,778,611,778,722,556,667,722,722,1000,722,722,667,500,556,444,556,444,333,500,556,278,333,556,278,833,556,500,556,556,444,389,333,556,500,722,500,500,444,333,555,500,833,500,333,570,570,570,500,930,333,278,333,581,500,333,394,220,394,520,250,570,500,1000,333,400,570,1000),
    ("verdana", False): (636,636,636,636,636,636,636,636,636,636,364,364,269,454,454,454,818,1076,454,454,636,636,636,636,352,684,686,698,771,632,575,775,751,421,455,693,557,843,748,787,603,787,695,684,616,732,684,989,685,615,685,601,623,521,623,596,352,623,633,274,344,592,274,973,633,607,623,623,427,521,394,633,592,818,592,592,525,394,459,818,727,636,454,818,818,818,545,1000,454,454,454,818,636,636,635,454,635,818,352,818,636,1000,269,542,818,818),
    ("verdana", True): (711,711,711,711,711,711,711,711,711,711,361,361,332,543,543,480,867,1272,689,402,711,711,711,711,342,776,762,724,830,683,650,811,837,546,555,771,637,948,847,850,733,850,782,710,682,812,764,1128,764,737,692,668,699,588,699,664,422,699,712,342,403,671,342,1058,712,687,699,699,497,593,456,712,650,979,669,651,597,402,587,867,862,711,402,867,867,867,617,964,543,689,543,867,711,711,711,543,711,867,342,867,711,1000,332,587,867,1049),
    ("tahoma", False): (546,546,546,546,546,546,546,546,546,546,303,303,211,383,383,363,728,977,382,354,546,546,546,546,312,600,589,601,678,561,521,667,675,373,417,588,498,771,667,708,551,708,621,557,584,656,597,902,581,576,559,525,553,461,553,526,318,553,558,229,282,498,229,840,558,543,553,553,360,446,334,558,498,742,495,498,444,332,401,728,674,546,354,728,728,728,474,909,383,382,383,728,546,546,480,382,480,728,312,728,546,909,211,471,728,817),
    ("tahoma", True): (637,637,637,637,637,637,637,637,637,637,312,312,275,454,454,431,818,1199,577,363,637,637,637,637,293,685,686,667,757,615,581,745,764,483,500,696,572,893,771,770,657,770,726,633,612,739,675,1028,685,670,623,599,632,527,629,594,382,629,640,302,363,603,302,954,640,617,629,629,434,515,416,640,579,890,604,576,526,343,489,818,781,637,363,818,818,818,566,920,454,577,454,818,637,546,623,637,623,818,293,818,637,909,275,520,818,1000),
    ("georgia", False): (614,430,559,552,565,528,566,502,596,566,270,270,215,375,375,374,643,817,469,312,610,642,622,615,241,671,654,642,749,653,599,725,815,390,518,694,604,927,767,744,610,744,702,561,619,756,667,976,710,615,602,504,560,454,574,483,325,509,582,293,292,536,286,881,591,539,571,560,410,432,345,575,497,737,505,492,444,331,412,643,710,472,312,643,643,643,479,929,375,469,375,643,643,500,430,375,430,643,241,643,643,857,227,419,643,807),
    ("georgia", True): (701,490,626,625,649,599,648,554,676,648,328,328,269,447,447,379,703,879,472,367,641,715,690,732,254,758,757,715,834,721,671,807,913,446,595,817,686,1023,839,820,701,820,797,649,684,833,762,1126,809,732,689,596,646,531,663,572,393,577,680,354,346,632,344,1016,690,636,658,648,520,513,397,677,567,863,588,562,525,376,510,703,799,482,367,703,703,703,548,967,447,472,447,703,703,500,500,388,500,703,254,703,703,928,269,420,703,942),
    ("courier new", False): (600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600),
    ("courier new", True): (600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600,600),
    ("consolas", False): (550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550),
    ("consolas", True): (550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550,550),
    ("cambria", False): (554,554,554,554,554,554,554,554,554,554,205,205,237,382,382,332,554,890,490,264,506,628,528,609,220,623,611,562,662,575,537,611,687,324,307,629,537,815,681,653,568,653,621,496,593,648,604,921,571,570,538,488,547,441,555,488,303,494,552,278,266,524,271,832,558,531,556,547,414,430,338,552,504,774,483,504,455,286,393,619,688,427,264,554,554,554,422,885,350,490,350,554,371,285,387,316,387,712,220,554,500,1000,221,375,554,752),
    ("cambria", True): (592,592,592,592,592,592,592,592,592,592,232,232,251,408,408,337,592,976,505,280,543,640,556,641,220,652,651,573,705,578,551,646,722,350,341,682,551,846,679,695,614,695,662,513,639,676,634,961,619,604,566,535,591,469,597,531,326,520,597,314,302,592,308,890,604,569,597,591,461,459,365,597,531,798,525,531,479,335,422,618,740,453,280,592,592,592,452,921,368,505,368,592,371,285,393,320,393,592,220,592,500,1000,235,378,592,772),
    ("century gothic", False): (554,554,554,554,554,554,554,554,554,554,277,277,198,369,369,332,606,775,437,277,554,554,554,554,277,740,574,813,744,536,485,872,683,226,482,591,462,919,740,869,592,871,607,498,426,655,702,960,609,592,480,683,682,647,685,650,314,673,610,200,203,502,200,938,610,655,682,682,301,388,339,608,554,831,480,536,425,295,309,720,757,425,277,606,606,606,591,867,351,605,351,672,500,378,351,672,351,606,280,606,500,1000,351,400,606,1000),
    ("century gothic", True): (560,560,560,560,560,560,560,560,560,560,280,280,220,380,380,420,600,860,460,280,560,560,560,560,280,740,580,780,700,520,480,840,680,280,480,620,440,900,740,840,560,840,580,520,420,640,700,900,680,620,500,660,660,640,660,640,280,660,600,240,260,580,240,940,600,640,660,660,320,440,300,600,560,800,560,580,460,280,360,600,680,440,280,600,600,600,560,740,320,640,320,600,500,420,340,600,340,600,280,600,500,1000,280,400,600,1000),
    ("candara", False): (549,350,462,486,533,491,552,473,550,548,252,252,254,353,353,252,504,504,267,252,444,444,444,444,217,616,586,549,643,521,489,621,652,276,416,592,490,854,674,694,553,694,605,511,509,683,554,886,558,550,519,491,556,452,550,513,339,540,542,218,218,488,229,819,542,559,556,556,354,419,359,536,480,766,504,465,456,252,452,504,682,504,252,504,504,504,353,1009,353,267,353,504,504,274,353,252,353,504,217,504,500,1000,252,267,500,1000),
    ("candara", True): (534,334,479,471,537,473,529,465,536,528,252,252,252,353,353,252,504,504,267,252,444,444,444,444,217,630,598,552,645,527,491,622,653,279,399,589,485,864,665,697,556,697,596,523,526,658,574,917,587,552,515,491,555,445,550,512,362,551,550,249,249,510,258,832,550,559,560,555,385,419,363,541,489,788,515,489,435,252,457,504,682,504,252,504,504,504,353,1009,353,267,353,504,504,274,353,252,353,504,217,504,500,1000,252,267,500,1000),
    ("corbel", False): (514,448,511,453,517,480,524,428,515,524,264,264,196,300,300,333,513,805,275,264,513,513,513,513,200,635,593,589,671,551,504,668,667,246,375,605,521,822,696,730,570,739,592,552,555,671,607,882,584,594,585,490,536,438,536,498,318,531,532,232,241,487,232,829,527,533,531,523,335,404,347,518,462,712,455,479,444,264,362,655,669,538,267,513,513,513,426,1011,315,275,315,513,488,342,300,227,300,513,200,513,488,830,219,408,513,792),
    ("corbel", True): (515,495,499,488,528,489,550,488,556,551,292,317,210,306,306,333,527,827,294,292,527,527,527,527,206,657,623,584,689,568,521,672,698,276,402,632,538,826,728,734,601,760,628,577,570,700,638,910,636,634,601,515,557,441,558,513,328,550,560,249,263,516,252,852,555,554,550,550,350,424,379,545,491,749,521,514,468,292,395,665,692,528,317,527,527,527,449,1024,315,294,315,527,488,391,305,237,305,527,206,527,488,830,243,408,527,877),
    ("dejavu sans", False): (636,636,636,636,636,636,636,636,636,636,318,318,275,390,390,361,838,950,337,337,636,636,636,636,318,684,686,698,770,632,575,775,752,295,295,656,557,863,748,787,603,787,695,635,611,732,684,989,685,611,685,613,635,550,635,615,352,635,634,278,278,579,278,974,634,612,635,635,411,521,392,634,592,818,592,592,525,401,460,838,780,500,337,838,838,838,531,1000,390,337,390,838,500,500,636,337,636,838,318,838,500,1000,318,500,838,1000),
    ("dejavu sans", True): (696,696,696,696,696,696,696,696,696,696,380,380,306,457,457,415,838,1002,365,400,696,696,696,696,348,774,762,734,830,683,683,821,837,372,372,775,637,995,837,850,733,850,770,720,682,812,774,1103,771,724,725,675,716,593,716,678,435,716,712,343,343,665,343,1042,712,687,716,716,493,595,478,712,652,924,645,652,582,456,521,838,872,523,400,838,838,838,580,1000,457,365,457,838,500,500,712,365,712,838,348,838,500,1000,380,500,838,1000),
}
FACE_ALIASES = {
    "helvetica": "arial", "helvetica neue": "arial", "liberation sans": "arial", "arimo": "arial",
    "arial unicode ms": "arial", "roboto": "arial",       # Roboto digits 0.562 em vs Arial 0.556 (judge table)
    "carlito": "calibri", "aptos display": "aptos", "aptos serif": "cambria",
    "liberation serif": "times new roman", "tinos": "times new roman",
    "liberation mono": "courier new", "cousine": "courier new",
}
DEFAULT_FACE = "calibri"          # unknown faces: the narrowest common face (a miss, never a false ###)
_CHAR_INDEX = {ch: i for i, ch in enumerate(GLYPH_CHARS)}
_DIGIT_IDX = _CHAR_INDEX["0"]
_SPACE_IDX = _CHAR_INDEX[" "]


def face_key(name) -> tuple[str, bool]:
    """(table key, known?) for a font name."""
    k = (name or "").strip().lower()
    k = FACE_ALIASES.get(k, k)
    if (k, False) in GLYPHS:
        return k, True
    return DEFAULT_FACE, False


def glyph_row(face_k: str, bold: bool) -> tuple:
    row = GLYPHS.get((face_k, bool(bold)))
    if row is None:
        row = GLYPHS[(face_k, False)]
    return row


def adv_em1000(row: tuple, ch: str) -> int:
    """Advance of one character in 1/1000 em (a missing glyph counts as a digit)."""
    i = _CHAR_INDEX.get(ch)
    if i is None:
        return row[_DIGIT_IDX]
    return row[i] or row[_DIGIT_IDX]


def text_em1000(row: tuple, text: str) -> int:
    return sum(adv_em1000(row, ch) for ch in text)


def text_px(row: tuple, text: str, ppem: float, rounded: bool) -> float:
    """Width of text in pixels at ppem: per-glyph rounded advances (GDI) or fractional."""
    if rounded:
        return float(sum(round(adv_em1000(row, ch) * ppem / 1000.0) for ch in text))
    return text_em1000(row, text) * ppem / 1000.0


def ppem_at(size_pt: float, dpi: float) -> float:
    return (size_pt if size_pt and size_pt > 0 else DEFAULT_FONT_PT) * dpi / 72.0


def mdw_px(face_k: str, size_pt: float, dpi: float) -> int:
    """Excel's maximum digit width of the Normal font: whole pixels at the model's dpi."""
    row = glyph_row(face_k, False)
    p = ppem_at(size_pt, dpi)
    return max(1, int(max(round(row[_CHAR_INDEX[d]] * p / 1000.0) for d in "0123456789")))


def space_px(face_k: str, size_pt: float, dpi: float) -> int:
    row = glyph_row(face_k, False)
    return max(1, int(round(row[_SPACE_IDX] * ppem_at(size_pt, dpi) / 1000.0)))


def col_px(width_ch: float, mdw: int) -> int:
    """ECMA-376 col/@width (characters incl. padding) -> pixels."""
    if width_ch is None or width_ch <= 0:
        return 0
    return int(math.trunc(((256.0 * width_ch + math.trunc(128.0 / mdw)) / 256.0) * mdw))


def default_col_px(fmt, mdw: int) -> tuple[int, int]:
    """(strict, lenient) pixel width of a column without <col>: baseColWidth digits + 5 px rounded up
    to 8 px, or defaultColWidth through the ECMA formula (strict) / trunc(w * MDW + 5) (lenient:
    Excel's reading of a LibreOffice-written 8.5390625 is unverified)."""
    base = fmt.base_col_width if fmt.base_col_width is not None else DEFAULT_BASE_COL_WIDTH
    base_px = int(math.ceil((base * mdw + 5) / 8.0) * 8)
    dcw = fmt.default_col_width
    if dcw is not None and dcw > 0:
        strict = col_px(dcw, mdw)
        return strict, max(strict, int(math.trunc(dcw * mdw + 5)), base_px)
    return base_px, base_px


def general_alternatives(v: float, full: str) -> list:
    """Texts Excel may fall back to for a General-format number in a narrow column: the full text,
    the 0-decimal rounding, and 1-significant-digit scientific notation."""
    out = [full]
    if not GENERAL_SHORTENS or not isinstance(v, (int, float)) or isinstance(v, bool):
        return out
    if math.isfinite(v):
        if abs(v) < 1e15:
            r = int(math.floor(abs(v) + 0.5))
            out.append(("-" if v < 0 and r else "") + str(r))
        if v != 0:
            out.append(f"{v:.0E}")
    return out


def blank_for_any_number(code: str) -> bool:
    """True when the format prints nothing for EVERY number (';;;', ';;'): all its numeric sections are
    empty.  A numeric cell's display then cannot depend on its value (text results are never measured,
    booleans and errors neither), so an untrusted formula result under it is not undecided (69-F2).
    Conservative: a tag-only section ('[Red]', '[>100]') renders General and is not empty."""
    try:
        f = N.parse_format(code)
    except GradingError:
        return False
    if not f.numeric:
        return False
    return all(f.sections[i].is_empty for i in f.numeric)


def _normal_font(st):
    """Font of the Normal cell style (builtinId 0), else fonts[0]; None without fonts."""
    fid = None
    for cs in st.cell_styles:
        if cs.builtin_id == 0 and cs.xf_id is not None and 0 <= cs.xf_id < len(st.cell_style_xfs):
            fid = st.cell_style_xfs[cs.xf_id].font_id
            break
    if fid is None or not (0 <= fid < len(st.fonts)):
        fid = 0
    return st.fonts[fid] if st.fonts else None


class _Style:
    __slots__ = ("shrink", "skip", "code", "fmt_err", "row", "face", "known", "size", "bold", "indent_levels",
                 "indent_sides", "horizontal", "wrap", "rot", "text_overflow_ok", "always_blank")

    def __init__(self):
        self.skip = None


class _Model:
    __slots__ = ("name", "dpi", "rounded", "mdw", "space", "scale")

    def __init__(self, name, dpi, rounded, mdw, space):
        self.name, self.dpi, self.rounded, self.mdw, self.space = name, dpi, rounded, mdw, space
        self.scale = 96.0 / dpi          # model px -> 96-dpi px


class _SheetGeo:
    """Column text areas per model for one sheet (lazy, cached per column)."""
    __slots__ = ("head", "models", "dflt", "cache")

    def __init__(self, head, models):
        self.head, self.models = head, models
        self.dflt = {}
        for m in models:
            strict, lenient = default_col_px(head.format, m.mdw)
            self.dflt[m.name] = strict if m.name == REFERENCE_MODEL else lenient
        self.cache = {}

    def col_pixels(self, c: int):
        """{model: px} for column c, or None when the column is hidden / zero width."""
        v = self.cache.get(c, 0)
        if v != 0:
            return v
        ci = self.head.col_info(c)
        if ci is not None and ci.hidden:
            v = None
        elif ci is not None and ci.width is not None:
            v = None if ci.width <= 0 else {m.name: col_px(ci.width, m.mdw) for m in self.models}
        else:
            v = dict(self.dflt)
        self.cache[c] = v
        return v

    def span_pixels(self, c1: int, c2: int):
        """{model: px} summed over columns c1..c2 (hidden columns count 0); None if all hidden."""
        tot = {m.name: 0 for m in self.models}
        any_vis = False
        for c in range(c1, min(c2, c1 + 16383) + 1):
            p = self.col_pixels(c)
            if p is None:
                continue
            any_vis = True
            for k, px in p.items():
                tot[k] += px
        return tot if any_vis else None


class _CfRule:
    __slots__ = ("prio", "order", "rule", "boxes", "anchor", "fmt", "stop")


class C69(Check):
    number = 69
    key = "Formatting/Sufficient column widths"
    needs_rows = True
    needs_cells = True
    needs_values = True
    sheet_kinds = ("worksheet", "dialogsheet", "macrosheet")

    # ------------------------------------------------------------------ workbook
    def start(self, wb):
        super().start(wb)
        st = wb.styles
        self.st = st
        self.date1904 = bool(wb.date1904)
        nf = _normal_font(st)
        self.normal_face = nf.name if nf is not None else None
        self.normal_size = float(nf.sz) if nf is not None and nf.sz and nf.sz > 0 else DEFAULT_FONT_PT
        nk, _known = face_key(self.normal_face)
        self.normal_key = nk
        self.models = [_Model(n, dpi, rnd, mdw_px(nk, self.normal_size, dpi), space_px(nk, self.normal_size, dpi))
                       for n, dpi, rnd in MODELS]
        self.ref_model = next(m for m in self.models if m.name == REFERENCE_MODEL)
        self._styles = {}
        self._render_cache = {}
        self._width_cache = {}
        self._sheets = []            # per-sheet records
        self._pass2 = {}
        self.unknown_faces = {}
        self.n_values_read = 0
        self.n_undecided = 0
        self.undecided = []          # [{sheet, ref, why}]
        self.band = []               # cells overflowing under some models only (stats)
        self.n_band = 0
        self.counts = {"numeric_cells": 0, "measured": 0, "sure_overflows": 0, "hidden_row_cells": 0,
                       "hidden_col_cells": 0, "hidden_anchor_cells": 0, "shrink_to_fit": 0, "skipped_alignment": 0,
                       "general_cells": 0, "negative_dates": 0, "blank_display": 0, "cf_cells": 0, "text_cells": 0,
                       "text_number_clips": 0, "text_hidden_col": 0, "text_overflow_into_empty": 0,
                       "formula_nonnumeric": 0, "undecided_not_displayed": 0}

    # ------------------------------------------------------------------ styles
    def _style(self, s) -> _Style:
        v = self._styles.get(s)
        if v is not None:
            return v
        v = _Style()
        st = self.st
        xf = st.xf(s)
        font = st.font(s)
        al = xf.alignment
        v.shrink = bool(al.shrink_to_fit)
        v.horizontal = al.horizontal
        v.wrap = bool(al.wrap_text)
        v.rot = int(al.text_rotation or 0)
        v.skip = None
        if al.horizontal in ("fill", "centerContinuous"):
            v.skip = al.horizontal
        elif v.rot:
            v.skip = "rotated"
        v.fmt_err = None
        try:
            v.code = N.resolve_format(xf.num_fmt_id, st.num_fmts)
        except GradingError as e:
            v.code, v.fmt_err = "General", str(e)
        v.always_blank = v.fmt_err is None and blank_for_any_number(v.code)
        k, known = face_key(font.name)
        v.face, v.known = (font.name or "").strip() or None, known
        v.row = glyph_row(k, font.b)
        v.size = float(font.sz) if font.sz and font.sz > 0 else DEFAULT_FONT_PT
        v.bold = bool(font.b)
        v.indent_levels = int(al.indent or 0) if al.indent and al.horizontal in INDENT_ALIGNMENTS else 0
        v.indent_sides = 2 if al.horizontal == "distributed" else 1
        v.text_overflow_ok = (not v.wrap and not v.shrink and not v.rot and al.horizontal in (None, "general", "left"))
        self._styles[s] = v
        return v

    # ------------------------------------------------------------------ measuring
    def _pads(self, code: str, section_index):
        """Padding characters (`_x` tokens) of the section used, measured as the glyph x instead of
        the space the renderer printed."""
        if section_index is None or "_" not in code:
            return ""
        try:
            secs = N.parse_format(code).sections
            toks = secs[section_index].tokens
        except (IndexError, AttributeError, GradingError):
            return ""
        return "".join(v for k, v in toks if k == "pad")

    def _need_px(self, sty: _Style, text: str, pads: str, m: _Model) -> float:
        """Text width under model m in model pixels, indent included."""
        key = (text, pads, id(sty.row), sty.size, m.name)
        w = self._width_cache.get(key)
        if w is None:
            p = ppem_at(sty.size, m.dpi)
            w = text_px(sty.row, text, p, m.rounded)
            if pads:
                w += text_px(sty.row, pads, p, m.rounded) - len(pads) * text_px(sty.row, " ", p, m.rounded)
            if len(self._width_cache) > RENDER_CACHE_MAX:
                self._width_cache.clear()
            self._width_cache[key] = w
        if sty.indent_levels:
            w += sty.indent_levels * INDENT_SPACES_PER_LEVEL * m.space * sty.indent_sides
        return w

    def _overflows(self, sty: _Style, texts: list, pads: str, pixels: dict) -> dict:
        """{model: overflow in 96-dpi px} using the narrowest of `texts` per model."""
        out = {}
        for m in self.models:
            avail = pixels[m.name] - CELL_MARGIN_PX
            need = min(self._need_px(sty, t, pads, m) for t in texts)
            out[m.name] = (need - avail) * m.scale
        return out

    def _render(self, v, t, code: str):
        key = (v, t, code)
        r = self._render_cache.get(key)
        if r is None:
            r = N.render(v, code, value_type=("d" if t == "d" else None), date1904=self.date1904)
            if len(self._render_cache) > RENDER_CACHE_MAX:
                self._render_cache.clear()
            self._render_cache[key] = r
        return r

    def _measure(self, sty: _Style, v, t, code: str, pixels: dict):
        """(status, overflows, text): status 'fit' | 'over' | 'band' | 'hash' | 'blank' | 'uncertain'."""
        r = self._render(v, t, code)
        if r.is_hash:
            return "hash", None, r.text
        if r.is_blank:
            return "blank", None, r.text
        texts = [r.text]
        if r.kind == "general":
            self.counts["general_cells"] += 1
            vn = v
            if t == "d":
                # an ISO-typed date under General shows its serial (the rendered text); the
                # shortenings start from that number, not from the ISO string (review fix 69-F5)
                try:
                    vn = float(r.text)
                except ValueError:
                    vn = None
            texts = general_alternatives(vn, r.text)
        pads = self._pads(code, r.section_index)
        # exact pre-screen: the fractional 96-dpi model is one of the models, so if it does not
        # overflow beyond the tolerance the cell is no sure overflow
        m96f = self.models[1]
        avail = pixels[m96f.name] - CELL_MARGIN_PX
        if min(self._need_px(sty, x, pads, m96f) for x in texts) - avail <= SURE_TOL_PX:
            return "fit", None, r.text
        ov = self._overflows(sty, texts, pads, pixels)
        if min(ov.values()) > SURE_TOL_PX:
            if not r.certain:
                return "uncertain", ov, r.text
            return "over", ov, r.text
        return "band", ov, r.text

    # ------------------------------------------------------------------ per sheet
    def sheet_start(self, head):
        f = head.format
        self.geo = _SheetGeo(head, self.models)
        self._sheet_name = head.name
        self._zero_default = bool(f.zero_height)
        self._row_hidden = False
        self._hidden_rows = set()
        self._cur_row = None
        self._shown_rows = set()     # zeroHeight sheets only: rows with a visible <row> (absent rows are hidden)
        self._cands = []             # [(r, c, s, v, t, text, ov_min_96r, note)] sure overflows (base format)
        self._uncertain = []         # [(r, c, ref, why)]
        self._undec_sheet = []       # untrusted formula results [(r, c, ref, s)], settled at sheet_end
        self._hidden_valued = 0      # value / formula cells in hidden rows or columns (merge anchors among them: pass 2)
        self._text_pending = []      # labels overflowing their column, waiting for a blocker (this row)
        self._text_clips = []        # [(r, c, kind, text, clip_chars, blocker_col)]
        self._sheet_counts = {"numeric_cells": 0, "sure_overflows": 0, "values_read": 0}

    def row(self, row):
        self._text_pending = []
        self._cur_row = row.r
        ht = row.ht
        self._row_hidden = bool(row.hidden) or (ht is not None and ht <= 0) or (ht is None and self._zero_default)
        if self._row_hidden:
            self._hidden_rows.add(row.r)
        elif self._zero_default:
            self._shown_rows.add(row.r)

    def _span_has_visible_row(self, r1: int, r2: int) -> bool:
        """Is any row r1..r2 displayed?  Rows without <row> are visible unless the sheet is zeroHeight."""
        n = r2 - r1 + 1
        if self._zero_default:
            shown = self._shown_rows
            if n <= len(shown):
                return any(r in shown for r in range(r1, r2 + 1))
            return any(r1 <= r <= r2 for r in shown)
        hidden = self._hidden_rows
        if n > len(hidden):
            return True
        return any(r not in hidden for r in range(r1, r2 + 1))

    def cell(self, cell):
        if self._row_hidden:
            if cell.raw is not None or cell.is_formula_result:
                self.counts["hidden_row_cells"] += 1
                self._hidden_valued += 1
            return
        if cell.is_formula_result:
            # a formula result blocks overflowing labels whatever it shows (even ""); its delivered
            # type is a cache attribute and says nothing reliable (69-F1): the trusted value decides
            self._block_check(cell, filled=True)
            self._numeric(cell, cell.t)
            return
        t = cell.t
        if t == "n" or t == "d":
            if cell.raw is None:
                self._block_check(cell, filled=False)
                return
            self._block_check(cell, filled=True)
            self._numeric(cell, t)
            return
        if t in ("s", "inlineStr", "str"):
            self._block_check(cell, filled=cell.raw is not None)
            if cell.raw is not None:
                self._text(cell)
            return
        # booleans, errors: not values this check measures, but they block overflow
        self._block_check(cell, filled=cell.raw is not None)

    # ------------------------------------------------------------------ numeric cells
    def _route(self, cell):
        """(kind, value, t) for any cell: constants by their stored type (a number / ISO date with a
        value element is 'number', anything else 'other'), formula results by _formula_value."""
        if cell.is_formula_result:
            return self._formula_value(cell)
        t = cell.t
        if (t == "n" or t == "d") and cell.raw is not None:
            return "number", cell.value, t
        return "other", None, t

    def _formula_value(self, cell):
        """What a formula result holds, decided by its TRUSTED value and never by the delivered `t`
        (a cache attribute: agent tools write t="str" placeholders with empty caches, LibreOffice a
        #NAME? placeholder).  -> (kind, value, t): kind 'number' (t 'n', or 'd' for a cached ISO
        date), 'other' (text, boolean, error, empty: not measured) or 'untrusted' (undecided)."""
        if not cell.value_trusted:
            return "untrusted", None, "n"
        v = cell.value
        if v is None or isinstance(v, bool):
            return "other", v, cell.t
        if isinstance(v, (int, float)):
            return "number", v, "n"
        if cell.t == "d" and isinstance(v, str) and cell.value_source == "cached":
            return "number", v, "d"
        return "other", v, cell.t                   # text (str), error (ExcelError)

    def _numeric(self, cell, t):
        formula = cell.is_formula_result
        kind = "number"
        if formula:
            kind, v, t = self._formula_value(cell)
            if kind == "other":
                self.counts["formula_nonnumeric"] += 1
                return
        else:
            v = cell.value
        self.counts["numeric_cells"] += 1
        self._sheet_counts["numeric_cells"] += 1
        sty = self._style(cell.s)
        if sty.shrink:
            self.counts["shrink_to_fit"] += 1
            return
        pixels = self.geo.col_pixels(cell.col)
        if pixels is None:
            self.counts["hidden_col_cells"] += 1
            self._hidden_valued += 1
            return
        if sty.skip:
            self.counts["skipped_alignment"] += 1
            return
        if not sty.known:
            self.unknown_faces[sty.face or "(none)"] = self.unknown_faces.get(sty.face or "(none)", 0) + 1
        if sty.fmt_err:
            raise GradingError(f"{self.key}: {cell.sheet}!{cell.ref}: number format cannot be read: {sty.fmt_err}")
        if kind == "untrusted":
            # settled at sheet_end: dropped when never displayed (covered merged cell) or blank
            # whatever the value (';;;' outside conditional-format ranges), else undecided
            self._undec_sheet.append((cell.row, cell.col, cell.ref, cell.s))
            return
        if formula:
            self.n_values_read += 1
            self._sheet_counts["values_read"] += 1
        if v is None or isinstance(v, bool) or (t != "d" and not isinstance(v, (int, float))):
            return
        if t != "d" and not math.isfinite(v):
            return
        self.counts["measured"] += 1
        status, ov, text = self._measure(sty, v, t, sty.code, pixels)
        self._settle(status, ov, text, cell.row, cell.col, cell.s, v, t, cell.ref, sty, self._cands, self._uncertain)

    def _settle(self, status, ov, text, r, c, s, v, t, ref, sty, cands, uncertain):
        """File one measured cell: a sure overflow into `cands`, an unverified rendering into
        `uncertain` (as (r, c, ref, why)), everything else into the counts."""
        if status == "fit":
            return
        if status == "blank":
            self.counts["blank_display"] += 1
            return
        if status == "hash":
            self.counts["negative_dates"] += 1
            if NEGATIVE_DATE_FAILS:
                cands.append((r, c, s, v, t, text, 999.0, "negative date"))
            return
        if status == "uncertain":
            uncertain.append((r, c, ref, f"display of {v!r} under number format {sty.code!r} is not verified"))
            return
        if status == "band":
            self.n_band += 1
            if len(self.band) < MAX_BAND_LISTED:
                self.band.append({"cell": f"{self._sheet_name}!{ref}", "text": text,
                                  "overflow_px_by_model": {k: round(x, 1) for k, x in ov.items()}})
            return
        cands.append((r, c, s, v, t, text, ov[REFERENCE_MODEL], None))
        if len(cands) > MAX_CANDIDATES:
            raise GradingError(f"{self.key}: more than {MAX_CANDIDATES:,} overflowing cells on sheet "
                               f"{self._sheet_name!r}; too many to grade")

    # ------------------------------------------------------------------ text cells (two narrow rules)
    def _text(self, cell):
        self.counts["text_cells"] += 1
        if not (TEXT_NUMBER_CLIP or TEXT_HIDDEN_COL_MAX_PX):
            return
        sty = self._style(cell.s)
        if not sty.text_overflow_ok:
            return
        pixels = self.geo.col_pixels(cell.col)
        if pixels is None:
            return
        v = cell.value
        if not isinstance(v, str):
            return
        s = v.strip()
        if not s or "\n" in s:
            return
        kind = None
        if TEXT_HIDDEN_COL_MAX_PX and pixels[REFERENCE_MODEL] <= TEXT_HIDDEN_COL_MAX_PX and len(s) >= TEXT_HIDDEN_MIN_CHARS:
            kind = "hidden_col"
        elif TEXT_NUMBER_CLIP and len(s) <= TEXT_NUMBER_MAX_CHARS and _NUMLIKE.match(s):
            kind = "number_text"
        if kind is None:
            return
        need = {m.name: self._need_px(sty, s, "", m) for m in self.models}
        span = {k: px - CELL_MARGIN_PX for k, px in pixels.items()}
        if all(need[k] <= span[k] for k in need):
            return
        self._text_pending.append([cell.col, cell.col, need, span, kind, s])

    def _block_check(self, cell, filled: bool):
        """Follow pending overflowing labels of this row: empty columns extend their room, a filled
        cell blocks them (clipped)."""
        pend = self._text_pending
        if not pend:
            return
        c = cell.col
        keep = []
        for p in pend:
            start, last, need, span, kind, s = p
            if c <= start:
                keep.append(p)
                continue
            for cc in range(last + 1, c):             # columns with no <c>: empty, more room
                px = self.geo.col_pixels(cc)
                if px:
                    for k in span:
                        span[k] += px[k]
            if all(need[k] <= span[k] for k in need):
                self.counts["text_overflow_into_empty"] += 1
                continue
            if c - start > TEXT_WALK_MAX_COLS:
                continue
            if filled:
                clip = min((need[m.name] - span[m.name]) / m.mdw for m in self.models)
                self._text_clips.append((cell.row, start, kind, s, clip, c))
                continue
            px = self.geo.col_pixels(c)
            if px:
                for k in span:
                    span[k] += px[k]
            p[1] = c
            keep.append(p)
        self._text_pending = keep

    # ------------------------------------------------------------------ conditional formats
    def _cf_rules(self, tail) -> list:
        """Rules that can change a number's display width (set a number format) or stop lower ones."""
        out, order = [], 0
        for cf in tail.conditional_formats:
            if not cf.ranges:
                continue
            anchor = (cf.ranges[0][0], cf.ranges[0][1])
            for rule in cf.rules:
                order += 1
                if rule.type in ("dataBar", "iconSet", "colorScale"):
                    continue
                fmt = None
                d = rule.dxf if rule.dxf is not None else self.st.dxf(rule.dxf_id)
                if d is not None:
                    if d.num_fmt_code is not None:
                        fmt = d.num_fmt_code
                    elif d.num_fmt_id is not None:
                        fmt = N.resolve_format(d.num_fmt_id, self.st.num_fmts)
                if fmt is None and not rule.stop_if_true:
                    continue
                x = _CfRule()
                x.prio = rule.priority if rule.priority is not None else 10 ** 9
                x.order, x.rule, x.boxes, x.anchor, x.fmt, x.stop = order, rule, list(cf.ranges), anchor, fmt, bool(rule.stop_if_true)
                out.append(x)
        out.sort(key=lambda x: (x.prio, x.order))
        if not any(x.fmt is not None for x in out):
            return []
        return out

    @staticmethod
    def _in_boxes(rules, r, c) -> list:
        return [i for i, x in enumerate(rules) if any(b[0] <= r <= b[2] and b[1] <= c <= b[3] for b in x.boxes)]

    def _cf_formats(self, rules, ids, v, r, c) -> set:
        """Set of number formats (None = the cell's own) the covering rules can produce."""
        fired = [fires(rules[i].rule, v, (r, c) + rules[i].anchor) for i in ids]
        unknown = [k for k, f in enumerate(fired) if f is UNKNOWN]
        if len(unknown) > 8:
            raise GradingError(f"{self.key}: a cell is covered by more than 8 conditional-format rules this check "
                               f"cannot evaluate")
        out = set()
        for combo in itertools.product((True, False), repeat=len(unknown)):
            fl = list(fired)
            for k, i in enumerate(unknown):
                fl[i] = combo[k]
            fmt = None
            for i, f in zip(ids, fl):
                if not f:
                    continue
                x = rules[i]
                if x.fmt is not None and fmt is None:
                    fmt = x.fmt
                if x.stop:
                    break
            out.add(fmt)
        return out

    # ------------------------------------------------------------------ sheet end
    @staticmethod
    def _merge_lookup(merges, cells):
        """{(r, c): Merge} for the given cells that lie in a multi-cell merge."""
        if not merges or not cells:
            return {}
        rows = sorted({rc[0] for rc in cells})
        by_row = {}
        for rc in cells:
            by_row.setdefault(rc[0], []).append(rc[1])
        out = {}
        for m in merges:
            if m.is_single_cell:
                continue
            i, j = bisect_left(rows, m.r1), bisect_right(rows, m.r2)
            for k in range(i, j):
                r = rows[k]
                for c in by_row[r]:
                    if m.c1 <= c <= m.c2:
                        out[(r, c)] = m
        return out

    def sheet_end(self, head, tail):
        name = head.name
        rules = self._cf_rules(tail)
        cands = self._cands
        merges = tail.merges
        # merged spans: covered cells are not displayed; an anchor has the whole span
        ml = self._merge_lookup(merges, [(r, c) for r, c, *_ in cands] + [(r, c) for r, c, *_ in self._text_clips])
        final = []
        for cand in cands:
            r, c, s, v, t, text, ov96, note = cand
            m = ml.get((r, c))
            if m is not None:
                if (m.r1, m.c1) != (r, c):
                    continue
                pixels = self.geo.span_pixels(m.c1, m.c2)
                if pixels is None:
                    continue
                status, ov, text = self._measure(self._style(s), v, t, self._style(s).code, pixels)
                if status != "over":
                    if status == "band":
                        self.n_band += 1
                    continue
                cand = (r, c, s, v, t, text, ov[REFERENCE_MODEL], note)
            final.append(cand)
        clips = [x for x in self._text_clips if (x[0], x[1]) not in ml]
        self._text_pending = []
        if self._uncertain:
            # an unverified rendering in a covered (non-anchor) merged cell is never displayed (as 69-F2)
            mu = self._merge_lookup(merges, [(r, c) for r, c, *_ in self._uncertain])
            self._uncertain = [x for x in self._uncertain
                               if mu.get((x[0], x[1])) is None or (mu[(x[0], x[1])].r1, mu[(x[0], x[1])].c1) == (x[0], x[1])]
        undecided = self._settle_undecided(merges, rules)
        hidden_anchors = self._hidden_anchors(merges)
        rec = {"sheet": name, "state": head.state, "cands": final, "clips": clips, "uncertain": self._uncertain,
               "undecided": undecided, "counts": self._sheet_counts, "cf_second_pass": False,
               "cf_undecided": []}
        self._sheets.append(rec)
        if rules or hidden_anchors:
            rec["rules"] = rules
            rec["hidden_anchors"] = hidden_anchors
            rec["merges"] = [m for m in merges if not m.is_single_cell]
            rec["hidden_rows"] = self._hidden_rows
            rec["geo"] = self.geo
            self._pass2[name] = rec
        if rules:
            # cells inside a format-setting rule's ranges are judged in the second pass under every
            # possible outcome; first-pass candidates there are dropped here
            rec["cands"] = [x for x in final if not self._in_boxes(rules, x[0], x[1])]
            rec["uncertain"] = [x for x in self._uncertain if not self._in_boxes(rules, x[0], x[1])]
            rec["cf_second_pass"] = True
            self.request_second_pass(name, None)
        elif hidden_anchors:
            self.request_second_pass(name, list(hidden_anchors))
        self._cands = []
        self._text_clips = []
        self._hidden_rows = set()
        self._shown_rows = set()
        self._undec_sheet = []

    def _settle_undecided(self, merges, rules) -> list:
        """Untrusted formula results of this sheet -> [(r, c, ref)] still undecided.  Dropped (69-F2): a
        covered (non-anchor) cell of a merge, which is never displayed, and a cell whose own format
        prints nothing for every number (';;;') unless a format-setting conditional rule covers it."""
        und = self._undec_sheet
        if not und:
            return []
        ml = self._merge_lookup(merges, [(r, c) for r, c, _ref, _s in und])
        fmt_rules = [x for x in rules if x.fmt is not None]
        out = []
        for r, c, ref, s in und:
            m = ml.get((r, c))
            if m is not None and (m.r1, m.c1) != (r, c):
                self.counts["undecided_not_displayed"] += 1
                continue
            if self._style(s).always_blank and not self._in_boxes(fmt_rules, r, c):
                self.counts["undecided_not_displayed"] += 1
                continue
            out.append((r, c, ref))
        return out

    def _hidden_anchors(self, merges) -> dict:
        """{(r1, c1): Merge} for merges whose anchor sits in a hidden row or column while the span still
        shows (a visible column and a visible row): Excel draws the merged value across the visible part
        (69-F4).  Pass 1 skipped those anchors as hidden; pass 2 measures them against the visible span."""
        if not HIDDEN_ANCHOR_SPAN or not self._hidden_valued or not merges:
            return {}
        out = {}
        for m in merges:
            if m.is_single_cell:
                continue
            row_hidden = not self._span_has_visible_row(m.r1, m.r1)
            col_hidden = self.geo.col_pixels(m.c1) is None
            if not (row_hidden or col_hidden):
                continue
            if self.geo.span_pixels(m.c1, m.c2) is None or not self._span_has_visible_row(m.r1, m.r2):
                continue
            out[(m.r1, m.c1)] = m
        return out

    # ------------------------------------------------------------------ second pass (CF formats, hidden anchors)
    def second_pass_start(self, head):
        self._p2 = self._pass2.get(head.name)
        self._sheet_name = head.name
        if self._p2 is not None:
            self.geo = self._p2["geo"]
            self._p2_anchor = {(m.r1, m.c1): m for m in self._p2["merges"]}
            self._p2_merges = self._p2["merges"]

    def second_pass_cell(self, cell):
        p = self._p2
        if p is None:
            return
        key = (cell.row, cell.col)
        hidden_anchor = p["hidden_anchors"].get(key)
        rules = p["rules"]
        if hidden_anchor is None and (not rules or cell.row in p["hidden_rows"]):
            return
        ids = self._in_boxes(rules, cell.row, cell.col) if rules else []
        if hidden_anchor is None and not ids:
            return
        # routed exactly as in pass 1: constants by stored type, formula results by trusted value (69-F1)
        kind, v, t = self._route(cell)
        if kind == "other":
            return
        sty = self._style(cell.s)
        if sty.shrink or sty.skip:
            return
        if hidden_anchor is not None:
            # 69-F4: pass 1 skipped this anchor as hidden; Excel shows it across the visible span
            pixels = self.geo.span_pixels(hidden_anchor.c1, hidden_anchor.c2)
            if pixels is None:
                return
            self.counts["hidden_anchor_cells"] += 1
            if sty.fmt_err:
                raise GradingError(f"{self.key}: {cell.sheet}!{cell.ref}: number format cannot be read: {sty.fmt_err}")
            if kind == "untrusted":
                if sty.always_blank and not self._in_boxes([x for x in rules if x.fmt is not None], *key):
                    self.counts["undecided_not_displayed"] += 1
                else:
                    p["undecided"].append((cell.row, cell.col, cell.ref))
                return
            if cell.is_formula_result:
                self.n_values_read += 1
        else:
            pixels = self.geo.col_pixels(cell.col)
            if pixels is None:
                return
            m = self._p2_anchor.get(key)
            if m is None:
                # a covered (non-anchor) cell of a merge is not displayed
                for mm in self._p2_merges:
                    if mm.r1 <= cell.row <= mm.r2 and mm.c1 <= cell.col <= mm.c2:
                        return
            else:
                pixels = self.geo.span_pixels(m.c1, m.c2)
                if pixels is None:
                    return
            if kind == "untrusted":
                return                                   # settled in pass 1 (sheet_end)
        if v is None or isinstance(v, bool) or (t != "d" and not isinstance(v, (int, float))):
            return
        if t != "d" and not math.isfinite(v):
            return
        if not ids:
            # a hidden anchor outside every format-setting rule: its own format decides
            self.counts["measured"] += 1
            status, ov, text = self._measure(sty, v, t, sty.code, pixels)
            self._settle(status, ov, text, cell.row, cell.col, cell.s, v, t, cell.ref, sty, p["cands"], p["uncertain"])
            return
        self.counts["cf_cells"] += 1
        if t == "d":
            fmts = {None} | {rules[i].fmt for i in ids if rules[i].fmt is not None}
        else:
            fmts = self._cf_formats(rules, ids, v, cell.row, cell.col)
        results = {}
        for fmt in fmts:
            code = fmt if fmt is not None else sty.code
            status, ov, text = self._measure(sty, v, t, code, pixels)
            results[fmt] = (status, ov, text, code)
        over = [x for x in results.values() if x[0] == "over"]
        if len(over) == len(results):
            best = max(over, key=lambda x: x[1][REFERENCE_MODEL])
            note = f'conditional-format number format "{best[3]}"' if best[3] != sty.code else None
            p["cands"].append((cell.row, cell.col, cell.s, v, t, best[2], best[1][REFERENCE_MODEL], note))
        elif over or any(x[0] == "uncertain" for x in results.values()):
            shows = ", ".join(sorted({repr(x[2]) for x in results.values()}))
            p["cf_undecided"].append((cell.row, cell.col, cell.ref,
                                      f"depends on conditional formatting this check cannot evaluate "
                                      f"(possible displays {shows})"))

    def second_pass_end(self, head, tail):
        p = self._p2
        if p is not None:
            p["geo"] = None
        self._p2 = None

    # ------------------------------------------------------------------ verdict
    def finish(self) -> dict:
        n_sure = 0
        per_sheet = []
        undecided = []
        for rec in self._sheets:
            name = rec["sheet"]
            hid = " (hidden sheet)" if rec["state"] != "visible" else ""
            cands = rec["cands"]
            n_sure += len(cands)
            rec["counts"]["sure_overflows"] = len(cands)
            for r, c, ref in rec["undecided"]:
                undecided.append({"sheet": name, "cell": ref, "why": "formula value untrusted"})
            for r, c, ref, why in rec["uncertain"] + rec["cf_undecided"]:
                undecided.append({"sheet": name, "cell": ref, "why": why})
            if cands:
                by_cell = {(x[0], x[1]): x for x in cands}
                for box in group_cells(by_cell):
                    cells = [by_cell[(rr, cc)] for rr in range(box[0], box[2] + 1) for cc in range(box[1], box[3] + 1)
                             if (rr, cc) in by_cell]
                    worst = max(cells, key=lambda x: x[6])
                    r, c, s, v, t, text, ov96, note = worst
                    sty = self._style(s)
                    rng = range_to_str(*box)
                    n = len(cells)
                    cols = index_to_col(box[1]) if box[1] == box[3] else f"{index_to_col(box[1])}:{index_to_col(box[3])}"
                    font = f"{sty.face or 'default font'} {sty.size:g}{' bold' if sty.bold else ''}"
                    desc = (f"{n} numeric cell{'s' if n != 1 else ''} {rng} on sheet '{name}'{hid} "
                            f"display{'' if n != 1 else 's'} '####' in Excel: column {cols} is too narrow for the "
                            f"value{'s' if n != 1 else ''}; e.g. {make_ref(r, c)} shows '{text.strip()}' ({font}, "
                            f"number format \"{sty.code}\"{', ' + note if note else ''}), about {ov96:.0f} px wider "
                            f"than the column's text area at 96 dpi and overflowing under every platform model "
                            f"(96/120 dpi, rounded or fractional glyph widths).")
                    self.add_mistake(location(name, rng), desc)
            for r, c, kind, s, clip, bc in rec["clips"]:
                if kind == "number_text":
                    if clip < TEXT_NUMBER_MIN_CLIP_DIGITS:
                        continue
                    self.counts["text_number_clips"] += 1
                    desc = (f"{make_ref(r, c)} on sheet '{name}'{hid} holds the number '{s}' stored as text; it is "
                            f"about {clip:.1f} digit(s) wider than its column and cut off by the filled cell "
                            f"{make_ref(r, bc)}, so the value cannot be read.")
                else:
                    self.counts["text_hidden_col"] += 1
                    desc = (f"{make_ref(r, c)} on sheet '{name}'{hid} holds '{s[:40]}' in a column of at most "
                            f"{TEXT_HIDDEN_COL_MAX_PX} px; the filled cell {make_ref(r, bc)} cuts it off, so the "
                            f"text is hidden by its column.")
                self.add_mistake(location(name, make_ref(r, c)), desc)
            per_sheet.append({"sheet": name, **rec["counts"], "undecided": len(rec["undecided"]) + len(rec["uncertain"])
                              + len(rec["cf_undecided"]), "cf_second_pass": rec["cf_second_pass"],
                              "text_clips_flagged": len(rec["clips"])})
        self.counts["sure_overflows"] = n_sure
        failed = self.mistakes.total > 0
        if undecided and not (failed and UNTRUSTED_ONLY_IF_VERDICT_NEEDS):
            u = undecided[0]
            prov = getattr(self.wb, "provenance", None)
            why = (f"writer={prov.writer}, value_path={'given' if prov and prov.value_path else 'none'}"
                   if prov else "no provenance")
            raise GradingError(f"{self.key}: cannot decide {u['sheet']}!{u['cell']}: {u['why']} ({why}); "
                               f"{len(undecided)} cell(s) undecided in all, no measured cell overflows, so the "
                               f"verdict depends on them (no fallback)")
        stats = dict(self.counts)
        stats.update({
            "n_sheets_checked": len(self._sheets),
            "normal_font": f"{self.normal_face} {self.normal_size:g}",
            "normal_font_known": face_key(self.normal_face)[1],
            "column_unit_px": {m.name: m.mdw for m in self.models},
            "sure_tolerance_px": SURE_TOL_PX,
            "band_cells": self.n_band, "band_examples": self.band,
            "formula_values_read": self.n_values_read,
            "undecided_cells": len(undecided), "undecided_examples": undecided[:MAX_UNDECIDED_LISTED],
            "unknown_faces": self.unknown_faces,
            "second_pass_sheets": sorted(self._pass2),
            "per_sheet": per_sheet,
            "options": {"negative_date_fails": NEGATIVE_DATE_FAILS, "general_shortens": GENERAL_SHORTENS,
                        "text_number_clip": TEXT_NUMBER_CLIP, "text_hidden_col_max_px": TEXT_HIDDEN_COL_MAX_PX,
                        "untrusted_only_if_verdict_needs": UNTRUSTED_ONLY_IF_VERDICT_NEEDS,
                        "hidden_anchor_span": HIDDEN_ANCHOR_SPAN,
                        "models": [m[0] for m in MODELS]},
        })
        und_note = ""
        if undecided:
            und_note = (f" {len(undecided)} cell(s) could not be measured (untrusted formula values or conditional "
                        f"formats); the verdict does not depend on them (see stats).")
        flagged = ", ".join(f"'{p['sheet']}'" for p in per_sheet if p["sure_overflows"] or p["text_clips_flagged"])
        return self.verdict(
            f"No value is wider than its column: {self.counts['numeric_cells']} numeric cell(s) on "
            f"{len(self._sheets)} sheet(s) fit under every platform model; nothing displays '####'.",
            f"{{n}} block(s) of values too wide for their columns ('####' or cut off) on: {flagged}.{und_note}",
            stats)
