"""Scripted stand-ins for an Anthropic Messages endpoint (SSE streaming) and an
OpenAI-compatible Chat Completions endpoint. The real SDKs talk to them, so
the assistant's request building, tool loop and response parsing are tested
without a real key. The script: on a user question, call the tool named in
``FakeLLM.plan`` (in order); once every planned tool has a result, answer
with text quoting the last tool result."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLM:
    def __init__(self, plan: list[tuple[str, dict]]):
        self.plan = plan
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                srv.requests.append(body)
                srv.headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.path.endswith("/messages"):
                    srv._anthropic(self, body)
                else:
                    srv._openai(self, body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()

    def _next(self, n_results: int):
        return self.plan[n_results] if n_results < len(self.plan) else None

    # ---- Anthropic SSE ------------------------------------------------------------
    def _anthropic(self, h, body):
        msgs = body["messages"]
        n_results = sum(1 for m in msgs if m["role"] == "user" and isinstance(m["content"], list)
                        and any(b.get("type") == "tool_result" for b in m["content"]))
        last_result = None
        for m in msgs:
            if m["role"] == "user" and isinstance(m["content"], list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        last_result = b["content"]
        step = self._next(n_results) if body.get("tools") else None
        events = [("message_start", {"type": "message_start", "message": {
            "id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"], "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}})]
        if step:
            name, args = step
            events += [
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": "Let me check."}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": f"toolu_{n_results}", "name": name, "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                         "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 1}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                                   "usage": {"output_tokens": 20}}),
            ]
        else:
            text = "Answer based on: " + (last_result or "OK")[:300]
            events += [
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 20}}),
            ]
        events.append(("message_stop", {"type": "message_stop"}))
        payload = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    # ---- OpenAI chat completions ------------------------------------------------------
    def _openai(self, h, body):
        msgs = body["messages"]
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        step = self._next(len(tool_msgs)) if body.get("tools") else None
        if step:
            name, args = step
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{len(tool_msgs)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
            finish = "tool_calls"
        else:
            last = tool_msgs[-1]["content"] if tool_msgs else "OK"
            message = {"role": "assistant", "content": "Answer based on: " + last[:300]}
            finish = "stop"
        resp = {"id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        data = json.dumps(resp).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
