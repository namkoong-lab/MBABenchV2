import sys
from pathlib import Path

# Tests import the workspace member's packages by path, same as its own
# entry points do.
_MEMBER_ROOT = Path(__file__).resolve().parents[1]
if str(_MEMBER_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMBER_ROOT))
