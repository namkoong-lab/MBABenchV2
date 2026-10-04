"""The one exception type of the package.

Patrick's ruling (2026-10-02): NO FALLBACK. When a check cannot grade a file
(unreadable or unsupported format, a value it needs is missing or untrusted, an
internal error) it raises GradingError with a clear message. It never quietly
passes, fails, or defers to the LLM.
"""
from __future__ import annotations


class GradingError(Exception):
    """A check could not grade a file.

    Attributes (all optional, filled in by the engine when it knows them):
      check     -- rubric key of the check that failed ("Potential Dangers/No hidden sheets")
      path      -- workbook path
      failures  -- {check key: message} for every check that failed in one grade() call
      verdicts  -- {check key: verdict} for the checks that did finish in that call
                   (for inspection only; the call as a whole failed)
    """

    def __init__(self, message: str, *, check: str | None = None, path: str | None = None,
                 failures: dict | None = None, verdicts: dict | None = None):
        super().__init__(message)
        self.check = check
        self.path = path
        self.failures = failures or {}
        self.verdicts = verdicts or {}
