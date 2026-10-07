"""JSON stdin/stdout bridge for an isolated Claude CC Switch profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import List, Optional

from .claude_profiles import ClaudeMessagesClient, ClaudeProfileError, ClaudeProfileManager


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-kernel --claude-bridge")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="default")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    args = parser.parse_args(argv)
    request = json.load(sys.stdin)
    profile_path = Path(args.profile)
    # Resolve ids again at bridge execution time: a long-lived task may have
    # been created before CC Switch switched from one model alias to another.
    manager = ClaudeProfileManager(profile_path.parent.parent)
    selected = manager.resolve_model(args.model, args.effort)
    try:
        result = ClaudeMessagesClient(profile_path, args.timeout_seconds).complete(
            request.get("messages", ()),
            model=selected["model"],
            effort=selected["effort"],
            max_output_tokens=args.max_output_tokens,
        )
    except ClaudeProfileError as exc:
        # Keep the subprocess protocol failure concise.  Emitting a Python
        # traceback makes the desktop event unreadable and obscures the
        # actionable proxy status (for example HTTP 524).
        print("Claude bridge error: %s" % str(exc)[:1000], file=sys.stderr)
        return 2
    json.dump({"content": result["content"], "tool_calls": [], "usage": result.get("usage", {}), "model_id": result.get("resolved_model") or selected["model"]}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
