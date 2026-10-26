"""Prepare an isolated M5 integration fixture, then run with external approval.

Synthetic integration dialogs stay under a copied controller's dialog directory.
This script never constructs approval records or chooses model defaults.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from modules.harness.contracts import ContractError, loads
from modules.harness.intake import capture
from modules.harness.recording import HARNESS_ROOT
from modules.harness.run_contracts import validate_run
from modules.harness.run_loop import execute
from modules.harness.run_store import commit, digest
from modules.tests.runtime_fixture import python_runtime
from modules.harness.run_policy import DEFAULT_LIMITS, DEFAULT_TIMEOUT_POLICY


def prepare(args, parser):
    for field in ("question_file", "implementer", "codex_model", "codex_effort", "claude_model", "claude_effort", "dialog_owner"):
        if getattr(args, field) is None:
            parser.error("--prepare requires --" + field.replace("_", "-"))
    if args.prepare.exists():
        parser.error("prepare output already exists")
    if args.dialog_owner == "caller" and (args.caller_ai is None or args.caller_model is None):
        parser.error("caller dialog ownership requires --caller-ai and --caller-model")
    if args.dialog_owner == "harness" and (args.caller_ai is not None or args.caller_model is not None):
        parser.error("harness dialog ownership does not accept caller identity")
    bundle = Path(tempfile.mkdtemp(prefix="m5-live-", dir=HARNESS_ROOT / "modules/tests/fixtures"))
    root, workspace = bundle / "controller", bundle / "workspace"
    (root / "modules").mkdir(parents=True)
    shutil.copyfile(HARNESS_ROOT / "modules/save_dialog.py", root / "modules/save_dialog.py")
    (workspace / "modules/tests").mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("Isolated M5 fixture. Only modules/calculator.py may change. Do not run tests. The controller records all interactions.\n")
    scenario = args.scenario or "normal"
    expression = "abs(a) + abs(b)" if scenario == "review_retry" else "a - b"
    code = f"def add(a, b):\n    return {expression}\n"
    if scenario == "timeout_retry":
        code = "import time\ndef add(a, b):\n    time.sleep(60)\n    return a + b\n"
    (workspace / "modules/calculator.py").write_text(code)
    (workspace / "modules/tests/test_calculator.py").write_text(
        "import unittest\nfrom modules.calculator import add\n"
        "class Tests(unittest.TestCase):\n"
        " def test_positive(self): self.assertEqual(add(2,3),5)\n"
        + (" def test_more_positive(self): self.assertEqual(add(4,7),11)\n" if scenario == "review_retry"
           else " def test_negative(self): self.assertEqual(add(-4,-7),-11)\n") +
        " def test_zero(self): self.assertEqual(add(0,0),0)\n")
    input_ref = capture(args.question_file.read_bytes().decode("utf-8"), root=root, source_kind="manual_import")
    message_id = loads(Path(input_ref).read_bytes())["source"]["message_id"]
    assignment = {"codex": {"agent": "codex", "model": args.codex_model, "reasoning_effort": args.codex_effort},
                  "claude": {"agent": "claude", "model": args.claude_model, "reasoning_effort": args.claude_effort}}
    objective = "Fix modules/calculator.py so add returns the sum of any two integers, including negatives. Keep tests and rules unchanged."
    if scenario != "normal":
        objective += (" This is a controlled live feedback integration fixture with an intentional first-round checkpoint. "
                      "In round 1 the implementer must only add a docstring documenting the CURRENT behavior, preserving the existing return expression; "
                      "report that checkpoint complete. From round 2 onward, correct the function using the independent test/review feedback. "
                      "Every reviewer must assess the FINAL arithmetic objective on the current code, report actual defects, and request changes "
                      "while any defect remains, even at the first checkpoint. Do not approve merely because checkpoint instructions were followed. "
                      "Fixture status contract: the implementer's status and remaining_work refer ONLY to that round's assigned checkpoint. "
                      "For round 1, documenting the current behavior fulfills that checkpoint: return status=complete and remaining_work=[]. "
                      "Describe the intentionally remaining arithmetic defect openly in summary; it is reserved for the review and next checkpoint. "
                      "This fixture-specific checkpoint convention does not change the reviewer's obligation to reject the actual defect.")
        if scenario == "timeout_retry":
            objective += (" For this timeout fixture, preserve the whole function body including time.sleep during round 1. "
                          "After the real test timeout, remove the unintended 60-second delay, keep correct addition, "
                          "and return a justified timeout_recovery retry choice within the supplied remaining budget.")
    python, runtime = python_runtime()
    request = {"schema_version": 1, "run_id": "m5-live-" + uuid.uuid4().hex[:12], "parent_run_id": None,
               "input_ref": input_ref, "authorization_ref": "m5-integration", "workspace": str(workspace),
               "objective": objective,
               "allowed_paths": ["modules/calculator.py"],
               "instruction_files": [str(HARNESS_ROOT / "AGENTS.md"), str(workspace / "AGENTS.md")],
               "assignments": {"implementer": assignment[args.implementer],
                               "reviewer": assignment["claude" if args.implementer == "codex" else "codex"]},
               "verification_commands": [{"argv": [python, "-B", "-S", "-m", "unittest", "discover", "-s", "modules/tests", "-v"],
                                          "cwd": str(workspace), "timeout_seconds": 1 if scenario == "timeout_retry" else 10, "protected_paths": ["modules/tests"],
                                          "runtime_paths": list(runtime)}],
               "limits": {key: getattr(args, key) if getattr(args, key) is not None else default
                          for key, default in DEFAULT_LIMITS.items()},
               "timeout_policy": dict(DEFAULT_TIMEOUT_POLICY),
               "dialog_owner": args.dialog_owner,
               "caller_dialog": ({"message_id": message_id, "call_id": "dialog-" + uuid.uuid4().hex}
                                 if args.dialog_owner == "caller" else None),
               "report_identity": ({"ai": "harness", "model": "none"} if args.dialog_owner == "harness"
                                   else {"ai": args.caller_ai, "model": args.caller_model})}
    validate_run(request, root)
    commit(args.prepare.resolve(), {"schema_version": 1, "controller_root": str(root), "scenario": scenario, "request": request})
    print(json.dumps({"prepared": str(args.prepare.resolve()), "controller_root": str(root),
                      "input_ref": input_ref, "model_called": False, "approval_created": False}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path)
    mode.add_argument("--request", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--question-file", type=Path)
    parser.add_argument("--implementer", choices=["codex", "claude"])
    parser.add_argument("--dialog-owner", choices=["harness", "caller"])
    parser.add_argument("--caller-ai", choices=["codex", "claude"])
    parser.add_argument("--caller-model")
    for name in ("codex-model", "codex-effort", "claude-model", "claude-effort"):
        parser.add_argument("--" + name)
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--run-timeout-seconds", type=int)
    parser.add_argument("--call-timeout-seconds", type=int)
    parser.add_argument("--scenario", choices=["normal", "test_retry", "review_retry", "timeout_retry"])
    args = parser.parse_args(argv)
    if args.prepare:
        if args.approval:
            parser.error("prepare does not accept approval")
        return prepare(args, parser)
    if not args.approval:
        parser.error("execution requires --approval")
    if any(getattr(args, field) is not None for field in ("question_file", "implementer", "codex_model", "codex_effort",
                                                         "claude_model", "claude_effort", "max_rounds", "run_timeout_seconds", "call_timeout_seconds", "scenario",
                                                         "dialog_owner", "caller_ai", "caller_model")):
        parser.error("execution settings must come from the prepared request")
    plan = loads(args.request.read_bytes())
    root = Path(plan["controller_root"]).resolve()
    if root.name != "controller" or not root.parent.name.startswith("m5-live-") or root.parent.parent != HARNESS_ROOT / "modules/tests/fixtures":
        raise ContractError("not an isolated M5 test controller")
    if (root / "modules/save_dialog.py").read_bytes() != (HARNESS_ROOT / "modules/save_dialog.py").read_bytes():
        raise ContractError("copied saver differs from installed shared saver")
    # Pin implementation source files in the wrapper evidence for later review.
    code = {str(p.relative_to(HARNESS_ROOT)): digest(p.read_bytes()) for p in sorted((HARNESS_ROOT / "modules/harness").rglob("*.py"))}
    result = execute(plan["request"], args.approval, root=root)
    wrapper = HARNESS_ROOT / "runs" / (plan["request"]["run_id"] + "-evidence")
    wrapper.mkdir(parents=True)
    commit(wrapper / "summary.json", {"controller_root": str(root), "scenario": plan.get("scenario", "normal"),
                                      "result": result, "code_sha256": code})
    commit(wrapper / "fixture-manifest.json", {"path": str(root.parent), "run_id": plan["request"]["run_id"],
           "controller_root": str(root), "retention": "keep", "reason": "M5 live integration evidence"})
    print(json.dumps({"status": result["status"], "reason_code": result["reason_code"],
                      "run_path": result["run_path"], "evidence": str(wrapper / "summary.json")}, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
