"""A scripted ACP agent for tests. FAKE selects the behaviour; the MCP tool is called for real."""
import json
import os
import signal
import subprocess
import sys
import time

BEHAVIOUR = os.environ.get("FAKE", "submit")
options = {"mode": os.environ.get("FAKE_MODE", "read-only"), "model": "m1"}
if os.environ.get("FAKE_MUTATE"):  # touches the snapshot while it is being shut down
    def on_term(signum, frame):
        os.chmod(os.getcwd(), 0o755)
        open(os.path.join(os.getcwd(), "late.txt"), "w").write("x")
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)

GOOD = {"verdict": "CHANGES_REQUESTED", "summary": "Achei um erro no denominador.",
        "findings": [{"id": "F1", "severity": "high", "location": "a.py:2", "impact": "wrong mean", "scenario": "avg([2,4])", "fix": "use len"}]}


def send(message):
    print(json.dumps(message), flush=True)


def config():
    return [{"id": key, "currentValue": value} for key, value in options.items()]


def read_until(predicate):
    for line in sys.stdin:
        message = json.loads(line)
        if predicate(message):
            return message


def ask_permission(call_id, kind):
    send({"jsonrpc": "2.0", "id": "perm", "method": "session/request_permission", "params": {
        "sessionId": "sid-1", "toolCall": {"toolCallId": call_id, "kind": kind, "status": "pending"},
        "options": [{"optionId": "a", "kind": "allow_once"}, {"optionId": "aa", "kind": "allow_always"}, {"optionId": "r", "kind": "reject_once"}]}})
    answer = read_until(lambda m: m.get("id") == "perm")
    return answer["result"]["outcome"].get("optionId")


def call_tool(servers, arguments_list):
    spec = servers[0]
    process = subprocess.Popen([spec["command"], *spec["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    replies = []

    def rpc(number, method, params):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())
    rpc(1, "initialize", {})
    for number, arguments in enumerate(arguments_list, 2):
        replies.append(rpc(number, "tools/call", {"name": "submit_review", "arguments": arguments}))
    process.terminate()
    return replies


servers = []
for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif method in ("session/new", "session/load"):
        servers = params["mcpServers"]
        if method == "session/load":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sid-1", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD HISTORY"}}}})
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": "sid-1", "configOptions": config()}})
    elif method == "session/set_config_option":
        if not os.environ.get("FAKE_REFUSE_MODE") or params["configId"] != "mode":
            options[params["configId"]] = params["value"]
        send({"jsonrpc": "2.0", "id": request_id, "result": {"configOptions": config()}})
    elif method == "session/delete":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif method == "session/prompt":
        stop = "end_turn"
        if BEHAVIOUR in ("submit", "bad_then_good", "submit_error", "submit_cancelled"):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sid-1", "update": {
                "sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "other", "title": "tstack-review: submit_review"}}})
            if ask_permission("t1", "other") == "a":
                bad = {"verdict": "APPROVE", "summary": "x", "findings": [{"id": "F1", "severity": "high"}]}
                call_tool(servers, [bad, GOOD] if BEHAVIOUR == "bad_then_good" else [GOOD])
        elif BEHAVIOUR == "write":
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sid-1", "update": {
                "sessionUpdate": "tool_call", "toolCallId": "t2", "kind": "edit", "title": "Edit pwned.txt"}}})
            ask_permission("t2", "edit")
        elif BEHAVIOUR == "flood":  # a steady stream of updates, then a late answer
            end = time.time() + 3
            while time.time() < end:
                send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sid-1", "update": {
                    "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}}}})
                time.sleep(0.005)
        elif BEHAVIOUR == "hang":
            read_until(lambda m: m.get("method") == "session/cancel")
            stop = "cancelled"
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "sid-1", "update": {
            "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "DONE"}}}})
        if BEHAVIOUR == "submit_cancelled":
            stop = "cancelled"
        if BEHAVIOUR == "submit_error":
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": "agent failed"}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": stop}})
