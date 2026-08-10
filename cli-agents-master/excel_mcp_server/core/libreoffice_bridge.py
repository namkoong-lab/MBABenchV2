"""LibreOffice recalculation bridge."""
from .shared_state import _lo_engine
from .workbook_io import _get_file_path


def _recalculate_with_libreoffice(filename: str) -> dict:
    """Trigger LibreOffice recalculation for the given file.

    Returns dict with keys: success, duration_ms, error
    """
    from .shared_state import _lo_engine
    if _lo_engine is None or not _lo_engine.is_running:
        return {"success": False, "duration_ms": 0, "error": "LibreOffice engine not available"}

    file_path = str(_get_file_path(filename))
    return _lo_engine.recalculate(file_path)
