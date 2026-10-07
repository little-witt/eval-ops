#!/usr/bin/env python3
"""Protocol fixture for the isolated Codex App Server adapter tests."""

import json
import sys


if "--version" in sys.argv:
    print("codex-cli 99.0.0-test")
    raise SystemExit(0)


def send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": request_id, "result": {"userAgent": "fake"}})
    elif method == "model/list":
        send({
            "id": request_id,
            "result": {
                "data": [
                    {
                        "id": "gpt-5-test",
                        "model": "gpt-5-test",
                        "displayName": "GPT-5 Test",
                        "description": "Deterministic fixture model",
                        "isDefault": True,
                        "hidden": False,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": "low", "description": "Fast"},
                            {"reasoningEffort": "medium", "description": "Balanced"},
                        ],
                    }
                ],
                "nextCursor": None,
            },
        })
    elif method == "thread/start":
        send({"id": request_id, "result": {"thread": {"id": "thread-test"}}})
    elif method == "turn/start":
        send({"id": request_id, "result": {"turn": {"id": "turn-test", "items": [], "status": "inProgress"}}})
        send({
            "method": "item/completed",
            "params": {
                "threadId": "thread-test",
                "turnId": "turn-test",
                "completedAtMs": 1,
                "item": {"id": "message-test", "type": "agentMessage", "phase": "final_answer", "text": "FORGE_READY"},
            },
        })
        send({
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": "thread-test",
                "turnId": "turn-test",
                "tokenUsage": {
                    "last": {"inputTokens": 12, "outputTokens": 2, "totalTokens": 14},
                    "total": {"inputTokens": 12, "outputTokens": 2, "totalTokens": 14},
                },
            },
        })
        send({
            "method": "turn/completed",
            "params": {"threadId": "thread-test", "turn": {"id": "turn-test", "items": [], "status": "completed"}},
        })

