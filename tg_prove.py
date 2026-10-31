# tool-guardian --prove: does YOUR model actually drive the router?
# Called by: tool_guardian.main (--prove)
"""tool-guardian --prove                 three bundled prompts against your model endpoint
python tg_prove.py --backend           the bundled stub MCP server the prompts run against

--selftest proves the token saving and that backends start; it cannot prove a model will
call the router. --prove does: it starts a bundled stub server behind a real Router, sends
three prompts to an OpenAI-compatible chat endpoint with the router tools attached, runs every
tool call the model makes through the router, and judges the transcript:

  list_describe  list or search, then describe_tool for notes.add_note, answer names `title`
  nested_call    call_tool notes.find_notes with a nested filter the backend accepts
  stalled        call_tool notes.get_counter after discovery (a session that only searches
                 and never calls is the "stalled" case the call log flags)

Configure with the environment (nothing is read from mcp.json):
  TOOL_GUARDIAN_PROVE_URL    base URL of an OpenAI-compatible API, e.g. http://127.0.0.1:11434/v1
  TOOL_GUARDIAN_PROVE_MODEL  model name (default: the first id from GET <url>/models)
  TOOL_GUARDIAN_PROVE_KEY    optional bearer token

Prints PASS/FAIL per prompt with the prompt/completion tokens the endpoint reported (or an
estimate, marked `~`, when it reports none). Exit 0 all pass, 1 any FAIL, 2 no endpoint.
Standard library only. MIT licensed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL_ENV = "TOOL_GUARDIAN_PROVE_URL"
MODEL_ENV = "TOOL_GUARDIAN_PROVE_MODEL"
KEY_ENV = "TOOL_GUARDIAN_PROVE_KEY"
MAX_TURNS = 8
HTTP_TIMEOUT = 300

SYSTEM = ("You can reach MCP servers only through the router tools you were given. "
          "Use them to answer; do not guess.")

PROMPTS = [
    ("list_describe",
     "Which tools does the notes server offer, and which arguments does its add_note tool require?"),
    ("nested_call",
     "Using the notes server's find_notes tool, find the notes whose field tag equals urgent. "
     "Give me their ids."),
    ("stalled",
     "What is the current value of the counter on the notes server?"),
]

# ---------------------------------------------------------------- bundled backend --

COUNTER = 4217
URGENT_IDS = ["N-17", "N-42"]
BACKEND_TOOLS = [
    {"name": "add_note", "description": "Add a note with a title and optional body and tags.",
     "inputSchema": {"type": "object", "required": ["title"], "properties": {
         "title": {"type": "string"}, "body": {"type": "string"},
         "tags": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "find_notes", "description": "Find notes matching every filter in params.filters.",
     "inputSchema": {"type": "object", "required": ["params"], "properties": {
         "params": {"type": "object", "required": ["filters"], "properties": {
             "filters": {"type": "array", "items": {
                 "type": "object", "required": ["field", "op", "value"], "properties": {
                     "field": {"type": "string", "enum": ["tag", "title", "author"]},
                     "op": {"type": "string", "enum": ["eq", "contains"]},
                     "value": {"type": "string"}}}},
             "limit": {"type": "integer"}}}}}},
    {"name": "get_counter", "description": "Return the current value of the notes counter.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _backend_answer(name, args):
    if name == "get_counter":
        return "counter: %d" % COUNTER
    if name == "find_notes":
        filters = ((args.get("params") or {}).get("filters")) or []
        if any(f.get("field") == "tag" and f.get("value") == "urgent" for f in filters if isinstance(f, dict)):
            return json.dumps({"total": 2, "ids": URGENT_IDS})
        return json.dumps({"total": 0, "ids": []})
    if name == "add_note":
        return "added note N-99"
    return None


def run_backend() -> int:
    """The stub MCP server: initialize, tools/list, tools/call. stdio, one JSON per line."""
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:
            continue
        if method == "initialize":
            res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "notes", "version": "1"}}
        elif method == "tools/list":
            res = {"tools": BACKEND_TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            text = _backend_answer(p.get("name"), p.get("arguments") or {})
            res = ({"content": [{"type": "text", "text": text}]} if text is not None
                   else {"content": [{"type": "text", "text": "no such tool"}], "isError": True})
        else:
            res = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}) + "\n")
        sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- endpoint --------

def _post(url, body, key):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # nosec B310 -- user-set URL
        return json.loads(resp.read().decode("utf-8"))


def _first_model(base, key):
    req = urllib.request.Request(base + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
        return str((data.get("data") or [{}])[0].get("id") or "")
    except (OSError, ValueError, IndexError, AttributeError):
        return ""


def openai_tools(router_tools):
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
            for t in router_tools]


def _args(raw):
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw or "{}")
    except ValueError:
        return {}
    return val if isinstance(val, dict) else {}


def run_prompt(chat, router, tools, prompt, est):
    """One conversation. Returns {"calls": [(name, args, result)], "answer", "prompt_tokens",
    "completion_tokens", "estimated"}. `chat(messages, tools)` returns the endpoint's JSON."""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    out = {"calls": [], "answer": "", "prompt_tokens": 0, "completion_tokens": 0, "estimated": False}
    for _ in range(MAX_TURNS):
        resp = chat(messages, tools)
        usage = resp.get("usage") or {}
        msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
        if "prompt_tokens" in usage:
            out["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            out["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        else:
            out["estimated"] = True
            out["prompt_tokens"] += est(messages) + est(tools)
            out["completion_tokens"] += est(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            out["answer"] = str(msg.get("content") or "")
            return out
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name, args = str(fn.get("name") or ""), _args(fn.get("arguments"))
            try:
                result = router.handle(name, args)
            except Exception as exc:  # noqa: BLE001 -- the model sees what a client would show
                result = "ROUTER ERROR: %s: %s" % (type(exc).__name__, exc)
            out["calls"].append((name, args, result))
            messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": result})
    return out


def _called(calls, tool, target=None):
    return [c for c in calls if c[0] == tool and (target is None or str(c[1].get("tool") or "").endswith(target))]


def judge(name, run):
    """(passed, reason) for one prompt's transcript."""
    calls, answer = run["calls"], run["answer"]
    if name == "list_describe":
        if not (_called(calls, "list_capabilities") or _called(calls, "search_capabilities")):
            return False, "never listed or searched the catalogue"
        if not _called(calls, "describe_tool", "add_note"):
            return False, "never called describe_tool for notes.add_note"
        return ("title" in answer.lower()), "answer names title" if "title" in answer.lower() else "answer does not name title"
    if name == "nested_call":
        hits = [c for c in _called(calls, "call_tool", "find_notes") if URGENT_IDS[0] in c[2]]
        if not hits:
            sent = _called(calls, "call_tool", "find_notes")
            return False, ("find_notes never returned the urgent notes (%d attempt(s))" % len(sent))
        ok = all(i in answer for i in URGENT_IDS)
        return ok, "answer has both ids" if ok else "answer is missing an id"
    if name == "stalled":
        if not _called(calls, "call_tool", "get_counter"):
            return False, "stalled: %d discovery call(s), no call_tool" % len(calls)
        ok = str(COUNTER) in answer
        return ok, "answer has the counter" if ok else "answer is missing the counter"
    return False, "unknown prompt"


def prove(tg, out=sys.stdout, chat=None) -> int:
    """`tg` is the tool_guardian module. Returns the exit code."""
    base = os.environ.get(URL_ENV, "").strip().rstrip("/")
    if not base and chat is None:
        print("--prove needs a model endpoint: set %s to an OpenAI-compatible base URL, e.g.\n"
              "  %s=http://127.0.0.1:11434/v1\n"
              "optional: %s=<model name>, %s=<bearer token>" % (URL_ENV, URL_ENV, MODEL_ENV, KEY_ENV), file=out)
        return 2
    key = os.environ.get(KEY_ENV, "").strip()
    model = os.environ.get(MODEL_ENV, "").strip() or (_first_model(base, key) if base else "") or "default"
    if chat is None:
        def chat(messages, tools):
            return _post(base + "/chat/completions",
                         {"model": model, "messages": messages, "tools": tools, "temperature": 0}, key)
    tg.CALL_LOG = ""   # a proof run is not a session of yours: keep it out of the call log
    router = tg.Router("", options={}, policy=False)   # a capability test: the user's policy file must not fail it
    router.remember_catalogue = lambda: None   # the stub must not replace the hook's catalogue
    router.start({"notes": {"command": sys.executable, "args": [os.path.abspath(__file__), "--backend"],
                            "description": "notes: add, find and count notes"}})
    try:
        if router.backends["notes"].status != "ok":
            print("FAIL setup: the bundled backend did not start: %s" % router.backends["notes"].error, file=out)
            return 1
        tools = openai_tools(tg.build_router_tools(router.backends))
        print("prove: %s model %s, %d prompts" % (base or "(stub)", model, len(PROMPTS)), file=out)
        print("prove: policy off -- your TOOL_GUARDIAN_POLICY file is not applied to the bundled stub server", file=out)
        failed = 0
        for name, prompt in PROMPTS:
            if router._ceiling is not None:
                router._ceiling.count = 0   # each prompt is its own session
            try:
                run = run_prompt(chat, router, tools, prompt, tg.est_tokens)
                passed, why = judge(name, run)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                run, passed, why = {"prompt_tokens": 0, "completion_tokens": 0, "estimated": False,
                                    "calls": []}, False, "endpoint error: %s" % exc
            failed += 0 if passed else 1
            mark = "~" if run.get("estimated") else ""
            print("%s %-13s tokens: prompt %s%d, completion %s%d, total %s%d | %d tool call(s) | %s"
                  % ("PASS" if passed else "FAIL", name, mark, run["prompt_tokens"], mark,
                     run["completion_tokens"], mark, run["prompt_tokens"] + run["completion_tokens"],
                     len(run["calls"]), why), file=out)
        print("prove: %d of %d passed" % (len(PROMPTS) - failed, len(PROMPTS)), file=out)
        return 1 if failed else 0
    finally:
        for b in router.backends.values():
            b.stop()


if __name__ == "__main__":
    if "--backend" in sys.argv:
        raise SystemExit(run_backend())
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import tool_guardian
    raise SystemExit(prove(tool_guardian))
