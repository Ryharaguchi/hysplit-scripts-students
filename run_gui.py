"""Windows-native GUI entry point for Python and a frozen portable EXE."""

from __future__ import annotations

import sys
from pathlib import Path

from hysplit_tool.platform_guard import require_native_windows


def main() -> int:
    require_native_windows()
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        from hysplit_tool.portable import application_dir, standalone_diagnostics
        output = (
            Path(sys.argv[2]) if len(sys.argv) >= 3
            else application_dir() / "HYSPLIT_Batch_diagnostics.json"
        )
        return 0 if standalone_diagnostics(output) else 2
    if len(sys.argv) != 1:
        raise SystemExit("Usage: HYSPLIT_Batch.exe [--self-test [diagnostic.json]]")
    from hysplit_tool.gui import main as launch
    launch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
