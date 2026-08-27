"""JSON stdin/stdout bridge for an isolated Claude CC Switch profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

from .claude_profiles import ClaudeMessagesClient


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-kernel --claude-bridge")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="default")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args(argv)
    request = json.load(sys.stdin)
    result = ClaudeMessagesClient(Path(args.profile), args.timeout_seconds).complete(request.get("messages", ()), model=args.model, effort=args.effort)
    json.dump({"content": result["content"], "tool_calls": [], "usage": result.get("usage", {}), "model_id": result.get("resolved_model") or args.model}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
