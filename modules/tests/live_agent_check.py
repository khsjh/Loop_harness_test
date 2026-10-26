"""Explicit, bounded live M3 check. Never invoked by unittest discovery.

Prepare a request, then execute it with a caller-owned approval registry.
Fixtures remain under modules/tests/fixtures;
call evidence is preserved under the installed harness root's runs directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

from modules.harness.authorization import load_authorizations
from modules.harness.contracts import ContractError, loads, validate_request
from modules.harness.process_runner import DEFAULT_TIMEOUT_SECONDS, run_process
from modules.harness.recording import HARNESS_ROOT, json_bytes, write_once
from modules.harness.run_agent_once import run_once


def snapshot(workspace):
    """Include additions, deletions, link targets and mode changes without following links."""
    result = {}
    for path in sorted(workspace.rglob("*")):
        name = str(path.relative_to(workspace))
        mode = path.lstat().st_mode
        if path.is_symlink():
            result[name] = {"mode": mode, "link": str(path.readlink())}
        elif path.is_file():
            result[name] = {"mode": mode, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            result[name] = {"mode": mode}
    return result


def assess(result, before, after, check, role):
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    allowed = {"modules/calculator.py"} if role == "implementer" else set()
    violations = sorted(set(changed) - allowed)
    response = result.get("response") or {}
    expected = response.get("status") == "complete" if role == "implementer" else response.get("verdict") == "approved"
    test_ok = (check is not None and check.returncode == 0 and not check.timed_out
               and not check.output_limit_exceeded and check.cleanup_completed
               and not check.descendants_terminated and not check.post_exit_drain_expired
               and not check.spawn_error and not check.io_error)
    return {"passed": result["call_status"] == "ok" and expected and test_ok and not violations,
            "changed_paths": changed, "scope_violations": violations,
            "response_satisfies_task": expected, "tests_passed": test_ok}


def prepare_request(args, parser):
    if args.authorizations:
        parser.error("--authorizations is only used with --request")
    if args.timeout_seconds is None:
        args.timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    for field in ("agent", "role", "model", "effort", "authorization_ref"):
        if not getattr(args, field):
            parser.error(f"--prepare requires --{field.replace('_', '-')}")
    if args.prepare.exists() or args.prepare.is_symlink():
        parser.error("--prepare output already exists; use a new file")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    run_id = "m3-live-" + uuid.uuid4().hex[:12]
    fixtures = HARNESS_ROOT / "modules/tests/fixtures"
    if args.workspace:
        workspace = args.workspace.resolve()
        check_workspace(workspace)
    else:
        workspace = Path(tempfile.mkdtemp(prefix="live-", dir=fixtures))
        (workspace / "modules/tests").mkdir(parents=True)
        (workspace / "AGENTS.md").write_text(
            "This is an isolated M3 calculator fixture. All scripts belong under modules/.\n"
            "Only modules/calculator.py is an implementation target; tests and rules are fixed.\n"
            "The caller records the interaction when LOG_BY_HARNESS=1.\n", encoding="utf-8")
        (workspace / "modules/calculator.py").write_text("def add(a, b):\n    return a - b\n")
        (workspace / "modules/tests/test_calculator.py").write_text(
            "import unittest\nfrom modules.calculator import add\n\n"
            "class CalculatorTests(unittest.TestCase):\n"
            "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
            "    def test_negative(self): self.assertEqual(add(-4, -7), -11)\n"
            "    def test_zero(self): self.assertEqual(add(0, 0), 0)\n")
    verification = verification_command()
    task = ("Fix modules/calculator.py so add returns the sum of two integers. "
            "Change only that file. Do not run tests; the caller will run the fixed tests separately."
            if args.role == "implementer" else
            "Review modules/calculator.py and the fixed tests in modules/tests/test_calculator.py. "
            "Check positive, negative and zero input correctness. Do not edit any file.")
    if args.role == "reviewer":
        check = run_process(verification, cwd=workspace, timeout_seconds=10)
        task += "\nIndependent test result: " + json.dumps({"exit_code": check.returncode,
                  "stdout": check.stdout.decode(), "stderr": check.stderr.decode()})
    request = {"schema_version": 1, "run_id": run_id, "call_id": args.agent + "-" + args.role,
               "round": None, "agent": args.agent, "role": args.role,
               "model": args.model, "reasoning_effort": args.effort,
               "authorization_ref": args.authorization_ref,
               "task": task, "workspace": str(workspace),
               "allowed_paths": ["modules/calculator.py"] if args.role == "implementer" else [],
               "instruction_files": [str(HARNESS_ROOT / "AGENTS.md"), str(workspace / "AGENTS.md")],
               "verification_commands": [verification], "timeout_seconds": args.timeout_seconds}
    validate_request(request)
    write_once(args.prepare.resolve(), json_bytes(request))
    print(json.dumps({"request_file": str(args.prepare.resolve()), "workspace": str(workspace),
                      "authorization_ref": args.authorization_ref, "model_called": False}, indent=2))
    return 0


def verification_command():
    return [sys.executable, "-B", "-m", "unittest", "discover", "-s", "modules/tests", "-v"]


def check_workspace(workspace):
    fixtures = HARNESS_ROOT / "modules/tests/fixtures"
    if workspace.parent != fixtures or not workspace.name.startswith("live-"):
        raise ContractError("workspace must name a live-* fixture created by this script")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path, help="write a request for caller approval; no model call")
    mode.add_argument("--request", type=Path, help="execute a previously prepared request")
    parser.add_argument("--authorizations", type=Path, help="caller-owned approval registry")
    parser.add_argument("--authorization-ref")
    parser.add_argument("--agent", choices=["codex", "claude"])
    parser.add_argument("--role", choices=["implementer", "reviewer"])
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--timeout-seconds", type=int,
                        help="model call limit in seconds (default: 3600)")
    args = parser.parse_args(argv)
    if args.prepare:
        return prepare_request(args, parser)
    if not args.authorizations:
        parser.error("--request requires --authorizations")
    if any(value is not None for value in (args.agent, args.role, args.model, args.effort,
                                          args.workspace, args.authorization_ref, args.timeout_seconds)):
        parser.error("execution settings must come from the prepared request")
    request = loads(args.request.read_bytes())
    workspace = validate_request(request)
    check_workspace(workspace)
    registry = load_authorizations(args.authorizations, workspace)
    run_id, role = request["run_id"], request["role"]
    verification = verification_command()
    before = snapshot(workspace)
    outcome = run_once(request, registry)
    passed = False
    if outcome.call_dir and outcome.result["call_status"] != "recording_error":
        after = snapshot(workspace)
        preliminary = assess(outcome.result, before, after, None, role)
        # Never execute a test file that the model changed outside the allowed scope.
        check = None if preliminary["scope_violations"] else run_process(verification, cwd=workspace, timeout_seconds=10)
        assessment = assess(outcome.result, before, snapshot(workspace), check, role)
        passed = assessment["passed"]
        verification_result = {
            "workspace": str(workspace), "before": before, "after": after,
            "test_process": check.metadata() if check else None,
            "stdout": check.stdout.decode() if check else "",
            "stderr": check.stderr.decode() if check else "Tests skipped: scope violation",
            **assessment,
        }
        write_once(outcome.call_dir / "live-verification.json", json_bytes(verification_result))
    output = {"run_id": run_id, "workspace": str(workspace), "passed": passed,
              "call_status": outcome.result["call_status"], "error": outcome.result["error"],
              "response": outcome.result["response"],
              "result_path": str(outcome.call_dir / "result.json") if outcome.call_dir else None}
    if outcome.result["call_status"] == "recording_error":
        output["recovery"] = outcome.as_dict()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ContractError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "passed": False}), file=sys.stderr)
        sys.exit(1)
