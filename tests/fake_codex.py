"""Scripted local app-server peer for the Codex assistant's real stdio client."""

import json
import os
import sys


def _model(slug, name, default, efforts, default_effort, hidden=False):
    return {"id": slug, "model": slug, "displayName": name, "description": f"{name} for tests", "hidden": hidden,
            "isDefault": default, "defaultReasoningEffort": default_effort,
            "supportedReasoningEfforts": [{"reasoningEffort": e, "description": e} for e in efforts]}


MODEL_PAGES = [
    {"data": [_model("codex-fast", "Codex Fast", True, ["low", "medium"], "low"),
              _model("codex-internal", "Internal", False, ["low"], "low", hidden=True)], "nextCursor": "page-2"},
    {"data": [_model("codex-deep", "Codex Deep", False, ["low", "medium", "high", "xhigh", "max", "ultra"], "high")],
     "nextCursor": None},
]


def main():
    plan = json.loads(os.environ.get("CANVAS_HUB_FAKE_CODEX_PLAN", "{}"))
    calls = list(plan.get("calls") or [])
    pending, last_result = 1000, None
    thread_id, turn_id = "thread-test", "turn-test"

    def emit(message):
        print(json.dumps(message), flush=True)

    def notification(method, params):
        emit({"method": method, "params": {"threadId": thread_id, **params}})

    def advance():
        nonlocal pending
        if calls:
            call = calls.pop(0)
            emit({"id": pending, "method": "item/tool/call", "params": {
                "threadId": thread_id, "turnId": turn_id, "callId": str(pending),
                "tool": call["name"], "arguments": call.get("arguments", {}), "namespace": call.get("namespace")}})
        elif not plan.get("hang"):
            text = plan.get("reply") or "Codex reply: " + (last_result or "OK")
            for delta in (text[:5], text[5:]):
                notification("item/agentMessage/delta", {"itemId": "final", "delta": delta})
            notification("item/completed", {"item": {"type": "agentMessage", "id": "final", "text": text}})
            notification("thread/tokenUsage/updated", {"tokenUsage": {"last": {"inputTokens": 12, "outputTokens": 8}}})
            notification("turn/completed", {"turn": {"id": turn_id, "status": "completed"}})

    for line in sys.stdin:
        message = json.loads(line)
        with open(os.environ["CANVAS_HUB_FAKE_CODEX_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps(message) + "\n")
        method, rid = message.get("method"), message.get("id")
        if method == "initialize":
            if plan.get("exit"):
                return
            if plan.get("bad_json"):
                print("not-json", flush=True)
                return
            emit({"id": rid, "result": {"userAgent": "codex/0.163.0"}})
        elif method == "account/read":
            emit({"id": rid, "result": {"requiresOpenaiAuth": True, "account": None if plan.get("signed_out") else {"type": "chatgpt"}}})
        elif method == "model/list":
            if plan.get("no_models"):
                emit({"id": rid, "error": {"code": -32600, "message": "model list unavailable"}})
            else:       # two pages, a hidden model and an effort the client leaves out
                cursor = (message.get("params") or {}).get("cursor")
                emit({"id": rid, "result": MODEL_PAGES[1] if cursor == "page-2" else MODEL_PAGES[0]})
        elif method == "mcpServerStatus/list":
            emit({"id": rid, "result": {"data": [{"name": "unrelated-server"}], "nextCursor": None}})
        elif method == "thread/start":
            if plan.get("thread_error"):
                emit({"id": rid, "error": {"code": -32600, "message": "Update the Codex CLI"}})
            else:
                emit({"id": rid, "result": {"thread": {"id": thread_id}, "model": "codex-test-model"}})
        elif method == "turn/start":
            if plan.get("early"):
                notification("item/agentMessage/delta", {"itemId": "early", "delta": "Early text"})
            emit({"id": rid, "result": {"turn": {"id": turn_id}}})
            if plan.get("turn_error"):
                notification("turn/completed", {"turn": {"id": turn_id, "status": "failed", "error": {"message": "Codex capacity error"}}})
            elif plan.get("approval"):
                emit({"id": 999, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id}})
            else:
                advance()
        elif method is None and rid == 999:
            advance()
        elif method is None and rid == pending:
            result = message["result"]
            last_result = result["contentItems"][0]["text"]
            pending += 1
            advance()


if __name__ == "__main__":
    main()
