"""Translate ACEval's model bridge envelope to a Codex App Server turn."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Optional, Sequence

from .codex_profiles import CodexAppServer, CodexDirectUnavailable, CodexProfileError, CodexResponsesClient


def _request() -> Mapping[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodexProfileError("FORGE model request is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise CodexProfileError("FORGE model request must be an object")
    messages = value.get("messages")
    tools = value.get("tools", ())
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise CodexProfileError("FORGE model messages must be an array")
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise CodexProfileError("FORGE model tools must be an array")
    if tools:
        raise CodexProfileError("The built-in Codex analysis bridge does not allow tools")
    if any(not isinstance(item, Mapping) for item in messages):
        raise CodexProfileError("FORGE model messages contain an invalid item")
    return value


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="forge-kernel --codex-bridge")
    parser.add_argument("--codex-executable", required=True)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args(argv)
    try:
        request = _request()
        try:
            client = CodexResponsesClient(Path(args.codex_home), args.timeout_seconds)
            result = client.complete(request["messages"], model=args.model, effort=args.effort)
        except CodexDirectUnavailable:
            with CodexAppServer(Path(args.codex_executable), Path(args.codex_home), args.timeout_seconds) as server:
                result = server.complete(request["messages"], model=args.model, effort=args.effort)
        json.dump(
            {"content": result["content"], "tool_calls": [], "usage": result.get("usage", {})},
            sys.stdout,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        sys.stdout.write("\n")
        return 0
    except (CodexProfileError, OSError, ValueError) as exc:
        sys.stderr.write("Codex local analysis failed: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
