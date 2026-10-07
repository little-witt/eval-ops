"""PyInstaller entrypoint for the desktop service and built-in model bridge."""

import sys

from aceval.codex_bridge import main as codex_bridge_main
from aceval.claude_bridge import main as claude_bridge_main
from aceval.desktop_service import main as desktop_main


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--codex-bridge":
        raise SystemExit(codex_bridge_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--claude-bridge":
        raise SystemExit(claude_bridge_main(sys.argv[2:]))
    raise SystemExit(desktop_main(sys.argv[1:]))
