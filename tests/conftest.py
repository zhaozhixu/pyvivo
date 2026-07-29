"""Make ``src/pyvivo`` importable without installing the package.

The project uses a src layout, so both pytest (via this conftest) and
``python -m unittest`` (via the same insert at the top of each test module) put
``src`` on ``sys.path`` to run against the source tree without an install step.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
