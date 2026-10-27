"""Offline M5 CLI double, controlled only by the test runner's environment."""
import json
import os
from pathlib import Path
import sys
import time

if "--version" in sys.argv:
    print("fake-m5 1")
    sys.exit(0)
if "--help" in sys.argv:
    print("--ignore-user-config --output-schema --output-last-message --ephemeral --sandbox --json --model --cd "
          "--ask-for-approval --no-daemon --restricted --safe-mode --tools --allowedTools --permission-mode "
          "--permission-prompts --json-schema --output-format --strict-mcp-config --no-session-persistence "
          "--disable-slash-commands --effort")
    sys.exit(0)
prompt = sys.stdin.read()
request = json.loads(prompt[prompt.index('{'):])["request"]
feedback = json.loads(request["task"].split("new authorization:\n", 1)[1])
mode = os.environ.get("M5_FAKE_MODE", "normal")
if mode == "nonzero":
    print("offline CLI failed", file=sys.stderr)
    sys.exit(7)
if mode == "malformed":
    print("not JSON")
    sys.exit(0)
role = request["role"]
if mode == "hang":
    if os.environ.get("M5_FAKE_READY"):
        # SIGKILL tests wait until stdin has been consumed. Killing the
        # controller earlier can deliver partial JSON/EOF instead of a hang.
        Path(os.environ["M5_FAKE_READY"]).write_text(str(os.getpid()))
    time.sleep(30)
if role == "implementer":
    if mode not in {"needs_user", "fail_tests"} and not (mode == "fix_second" and request["round"] == 1):
        Path("modules/calculator.py").write_text("def add(a, b):\n    return a + b\n")
    response = {"schema_version": 1, "status": "complete", "summary": "fixture implementation",
                "changed_files": ["modules/calculator.py"], "remaining_work": [], "questions": []}
    if mode == "needs_user":
        response.update(status="needs_user", questions=["Choose a behavior."], changed_files=[])
    if mode == "incomplete":
        response.update(status="incomplete", remaining_work=["More work required."])
    if mode == "scope_violation":
        Path("AGENTS.md").write_text("changed rules")
    if mode == "test_tamper":
        Path("modules/tests/test_calculator.py").write_text("# no tests\n")
    if mode == "review_then_test_failure" and request["round"] == 2:
        Path("modules/calculator.py").write_text("def add(a,b): return a-b\n")
    if mode == "timeout_progress":
        Path("modules/calculator.py").write_text(f"def add(a,b): return a+b\n# round {request['round']}\n")
else:
    response = {"schema_version": 1, "verdict": "approved", "summary": "fixture review", "findings": [], "questions": []}
    if mode == "review_needs_user":
        response.update(verdict="needs_user", questions=["Choose a behavior."])
    if mode == "review_reject" or (mode in {"review_second", "review_then_test_failure"} and request["round"] == 1):
        response.update(verdict="changes_requested", findings=[{"severity": "medium", "path": "modules/calculator.py",
                                                                "line": 2, "message": "Please inspect again."}])
    if mode == "review_write":
        Path("modules/calculator.py").write_text("def add(a,b): return 999\n")
response["schema_version"] = request["schema_version"]
if mode == "invalid_response":
    response["summary"] = ""
if role == "implementer" and request["schema_version"] == 2:
    response["timeout_recovery"] = None
    if "timeout_context" in feedback and mode != "timeout_missing_decision":
        context = feedback["timeout_context"]
        response["timeout_recovery"] = {"action": "retry", "reason": "The fixture can be repaired within budget.",
                                        "plan": "Use the repaired addition and reverify.",
                                        "test_timeout_seconds": 6, "estimated_seconds": 8, "required_rounds": 1}
        if mode == "timeout_stop":
            response["timeout_recovery"].update(action="stop", reason="Repeated hang has no feasible repair.",
                                                 plan="Stop without another test.", test_timeout_seconds=0,
                                                 estimated_seconds=0, required_rounds=0)
        if mode == "timeout_no_change":
            response["timeout_recovery"].update(test_timeout_seconds=context["timed_out_test"]["timeout_seconds"])
        if mode == "timeout_increase":
            requested = context["timed_out_test"]["timeout_seconds"] + 1
            response["timeout_recovery"].update(test_timeout_seconds=requested, estimated_seconds=requested + 1)
        if mode == "timeout_over_cap":
            response["timeout_recovery"].update(test_timeout_seconds=21601, estimated_seconds=21601)
        if mode == "timeout_over_budget":
            response["timeout_recovery"].update(estimated_seconds=86400, required_rounds=6)
        if mode == "timeout_needs_user":
            response.update(status="needs_user", questions=["Select an external dependency."], timeout_recovery=None)
if request["agent"] == "codex":
    Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(response))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(response)}}))
    print(json.dumps({"type": "turn.completed"}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": response, "modelUsage": {"fake-actual-model": {}}}))
