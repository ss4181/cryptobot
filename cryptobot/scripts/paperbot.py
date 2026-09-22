"""Bootstrap launcher so the CLI also works with this environment's embedded Python.

On a normal interpreter ``python -m cryptobot <cmd>`` works directly from the
workspace root.  The AutoClaw-embedded Python here resolves its ``._pth`` entries
relative to the interpreter directory (and ignores ``PYTHONPATH``), so the
workspace root never lands on ``sys.path``.  This wrapper inserts it and then
delegates to the exact same entry point::

    python cryptobot/scripts/paperbot.py backtest --offline
    python cryptobot/scripts/paperbot.py run --cycles 5 --replay --replay-bars 400
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from cryptobot.cli import main  # noqa: E402  (path bootstrap must come first)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
