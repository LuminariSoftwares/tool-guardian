"""A scripted model for testing MCP client configs without a real model.

    python tests/clients/mock_model.py --port 18080 --log requests.jsonl

Speaks two wire formats on one port:
  - OpenAI-compatible: GET /v1/models, POST /v1/chat/completions (plain or streamed)
    -- OpenCode, Cline, Continue;
  - Anthropic Messages: POST /v1/messages (plain or streamed) -- Claude Code with
    ANTHROPIC_BASE_URL pointed here.

Every chat request is appended to --log as {"tools": [...names...], "tool_results": [...],
"answer": [...]} so a test can check what the client actually sent. The script it follows:

  1. no tool result in the conversation yet -> call the tool whose name contains
     "list_capabilities" with {} (a client that did not load tool-guardian has none: the
     mock then answers "NO_ROUTER" as text);
  2. one tool result is present -> answer "ROUTER_OK: <first 60 chars of that result>": as
     text, or through the client's own finishing tool when it has one (Cline ends a task
     only through `submit_and_exit` / `attempt_completion`, never on plain text);
  3. more tool results (the finishing tool's own) -> the same answer as text.

Standard library only. Dev harness for examples/*, not part of the CI matrix.
"""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mock-router-model"
FINISH_TOOLS = ("submit_and_exit", "attempt_completion")


def _tools(body):
    """[(name, json schema)] from either format."""
    out = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):                     # OpenAI
            out.append((fn["name"], fn.get("parameters") or {}))
        elif tool.get("name"):                                           # Anthropic
            out.append((tool["name"], tool.get("input_schema") or {}))
    return out


def _content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_content_text(part.get("content")) if part.get("type") == "tool_result"
                        else part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _tool_results(body):
    results = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":                                # OpenAI
            results.append(_content_text(message.get("content")))
        elif isinstance(message.get("content"), list):                   # Anthropic
            results.extend(_content_text(block.get("content")) for block in message["content"]
                           if isinstance(block, dict) and block.get("type") == "tool_result")
    return results


def _finish_args(schema, reply):
    """Fill the finishing tool's arguments: strings get the reply, booleans get true
    (Cline's `submit_and_exit` needs `verified`)."""
    props = schema.get("properties") or {}
    args = {}
    for key in set(schema.get("required") or []) | set(props):
        spec = json.dumps(props.get(key) or {})
        if '"boolean"' in spec:          # also catches anyOf / type lists
            args[key] = True
        elif '"string"' in spec or spec == "{}":
            args[key] = reply
    return args


def decide(body):
    """Return ("tool", name, args) or ("text", reply, None) for one request."""
    tools = _tools(body)
    results = _tool_results(body)
    if results:
        reply = "ROUTER_OK: " + " ".join(results[0].split())[:60]
        finish = [(n, s) for n, s in tools if n in FINISH_TOOLS]
        if len(results) == 1 and finish:
            return "tool", finish[0][0], _finish_args(finish[0][1], reply)
        return "text", reply, None
    for name, _ in tools:
        if "list_capabilities" in name:
            return "tool", name, {}
    return "text", "NO_ROUTER", None


class Handler(BaseHTTPRequestHandler):
    log_path = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the client's terminal clean
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse(self, data, event=None):
        head = ("event: %s\n" % event) if event else ""
        self.wfile.write((head + "data: " + json.dumps(data) + "\n\n").encode("utf-8"))

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/").endswith("/models"):
            self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}]})
        else:
            self._json(404, {"error": "not found"})

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json(400, {"error": "bad json"})
            return
        path = self.path.split("?")[0].rstrip("/")
        if path.endswith("/messages/count_tokens"):
            self._json(200, {"input_tokens": 10})
            return
        if not (path.endswith("/chat/completions") or path.endswith("/messages")):
            self._json(404, {"error": "unknown path " + path})
            return
        kind, value, args = decide(body)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"path": path, "tools": [n for n, _ in _tools(body)],
                                         "tool_results": _tool_results(body),
                                         "answer": [kind, value], "args": args}) + "\n")
        if path.endswith("/messages"):
            self._anthropic(body, kind, value, args)
        else:
            self._openai(body, kind, value, args)

    def _openai(self, body, kind, value, args):
        created = int(time.time())
        if kind == "tool":
            call = {"index": 0, "id": "call_mock_1", "type": "function",
                    "function": {"name": value, "arguments": json.dumps(args)}}
            message = {"role": "assistant", "content": None,
                       "tool_calls": [{k: v for k, v in call.items() if k != "index"}]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": value}
            finish = "stop"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            self._json(200, {"id": "chatcmpl-mock", "object": "chat.completion", "created": created, "model": MODEL,
                             "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
            return

        def chunk(delta, finish_reason=None, with_usage=False):
            obj = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": created, "model": MODEL,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if with_usage:
                obj["usage"] = usage
            self._sse(obj)

        self._sse_start()
        chunk({"role": "assistant", "content": ""})
        chunk({"tool_calls": [call]} if kind == "tool" else {"content": value})
        chunk({}, finish, with_usage=True)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _anthropic(self, body, kind, value, args):
        if kind == "tool":
            block = {"type": "tool_use", "id": "toolu_mock_1", "name": value, "input": args}
            stop = "tool_use"
        else:
            block = {"type": "text", "text": value}
            stop = "end_turn"
        usage = {"input_tokens": 10, "output_tokens": 5}
        message = {"id": "msg_mock", "type": "message", "role": "assistant", "model": body.get("model") or MODEL,
                   "content": [block], "stop_reason": stop, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
            self._json(200, message)
            return
        self._sse_start()
        self._sse({"type": "message_start", "message": dict(message, content=[], stop_reason=None)}, "message_start")
        if kind == "tool":
            self._sse({"type": "content_block_start", "index": 0, "content_block": dict(block, input={})},
                      "content_block_start")
            self._sse({"type": "content_block_delta", "index": 0,
                       "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}}, "content_block_delta")
        else:
            self._sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      "content_block_start")
            self._sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": value}},
                      "content_block_delta")
        self._sse({"type": "content_block_stop", "index": 0}, "content_block_stop")
        self._sse({"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                   "usage": {"output_tokens": 5}}, "message_delta")
        self._sse({"type": "message_stop"}, "message_stop")
        self.wfile.flush()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--log", default="")
    a = ap.parse_args(argv)
    Handler.log_path = a.log or None
    server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("mock model on http://127.0.0.1:%d" % a.port, file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
