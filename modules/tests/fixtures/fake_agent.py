"""Offline CLI fixture. It is run by the tests through the real process runner."""
import json
import os
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("fake-cli 1.0")
    sys.exit(0)
if "--help" in sys.argv:
    print("--ignore-user-config --output-schema --output-last-message --ephemeral --sandbox "
          "--json --model --cd --ask-for-approval --no-daemon --restricted --safe-mode --tools "
          "--allowedTools --permission-mode --permission-prompts --json-schema --output-format "
          "--strict-mcp-config --no-session-persistence --disable-slash-commands --effort")
    sys.exit(0)

prompt = sys.stdin.read()
context = json.loads(prompt[prompt.index('{'):])
request = context["request"]
assert os.environ.get("LOG_BY_HARNESS") == "1"
assert Path.cwd() == Path(request["workspace"])
if request["task"] == "timeout":
    print("partial", flush=True)
    time.sleep(30)
role = request["role"]
response = ({"schema_version": 1, "status": "complete", "summary": "모의 구현",
             "changed_files": [], "remaining_work": [], "questions": []}
            if role == "implementer" else
            {"schema_version": 1, "verdict": "approved", "summary": "모의 리뷰",
             "findings": [], "questions": []})
case = request["task"]
response["schema_version"] = request["schema_version"]
if role == "implementer" and request["schema_version"] == 2:
    response["timeout_recovery"] = None
if case == "invalid_response":
    response["summary"] = ""
if request["agent"] == "codex":
    final = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    if case != "missing_final":
        final.write_text(json.dumps(response), encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "fake", "model": "actual-test-model"}))
    printed = dict(response)
    if case == "mismatch":
        printed["summary"] = "different"
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(printed)}}))
    print(json.dumps({"type": "turn.failed" if case == "cli_error" else "turn.completed"}))
else:
    outer = {"type": "result", "subtype": "success", "is_error": False,
             "structured_output": response, "modelUsage": {"actual-test-model": {}}}
    if case == "cli_error":
        outer.update(subtype="error_during_execution", is_error=True)
    if case == "missing_final":
        del outer["structured_output"]
    print(json.dumps(outer))
if case == "orphan_after_response":
    sys.stdout.flush()
    if os.fork() == 0:
        time.sleep(30)
        os._exit(0)
sys.exit(7 if case == "nonzero" else 0)
