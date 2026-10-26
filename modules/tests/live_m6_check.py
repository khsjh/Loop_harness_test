"""Prepare a natural-failure M6 ledger task; run only with caller-owned approval.

Synthetic fixture inputs and answers stay in the isolated controller.
No approval is created and no first-round failure is prescribed.
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
from modules.harness.run_policy import DEFAULT_LIMITS, DEFAULT_TIMEOUT_POLICY
from modules.harness.run_loop import execute
from modules.harness.run_report import report
from modules.harness.run_store import commit
from modules.tests.runtime_fixture import python_runtime

OBJECTIVE = """Implement modules/ledger.py summarize(lines). Consume an iterable of JSONL strings once.
Ignore whitespace-only lines. Each nonblank line must be a JSON object with exactly id, account, amount.
id and account must be strings with nonempty strip(), but preserve their original spelling.
amount must be an integer, excluding bool; accept arbitrarily large and negative integers.
Reject malformed JSON, repeated object keys, invalid shapes/types and extra/missing keys with ValueError.
The same id with an identical account and amount is an ignored duplicate; conflicting reuse raises ValueError.
Return a list sorted lexicographically by account, each dict containing account, count, total, min, max,
computed from unique accepted IDs. Empty input returns []. Do not edit tests or rules and do not run tests.
Implement the final behavior immediately; no deliberate failure or first-round checkpoint is requested."""


def prepare(args):
    if args.prepare.exists():
        raise ContractError("prepare output already exists")
    bundle = Path(tempfile.mkdtemp(prefix="m6-live-", dir=HARNESS_ROOT / "modules/tests/fixtures"))
    root, workspace = bundle / "controller", bundle / "workspace"
    (root / "modules").mkdir(parents=True)
    shutil.copyfile(HARNESS_ROOT / "modules/save_dialog.py", root / "modules/save_dialog.py")
    (workspace / "modules/tests").mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("Isolated M6 fixture. Edit only modules/ledger.py. Do not run tests. LOG_BY_HARNESS=1.\n")
    (workspace / "modules/ledger.py").write_text("def summarize(lines):\n    raise NotImplementedError\n")
    shutil.copyfile(HARNESS_ROOT / "modules/tests/fixtures/m6_ledger_checks.py", workspace / "modules/tests/check_ledger.py")
    # This is explicitly test data, not a paraphrase of the real user's question.
    input_ref = capture("Synthetic M6 integration input: " + OBJECTIVE, root=root, source_kind="manual_import")
    assignments = {name: {"agent": name, "model": getattr(args, name + "_model"),
                         "reasoning_effort": getattr(args, name + "_effort")} for name in ("codex", "claude")}
    python, runtime = python_runtime()
    request = {"schema_version": 1, "run_id": "m6-live-" + uuid.uuid4().hex[:12], "parent_run_id": None,
        "input_ref": input_ref, "authorization_ref": "m6-integration", "workspace": str(workspace),
        "objective": OBJECTIVE, "allowed_paths": ["modules/ledger.py"],
        "instruction_files": [str(HARNESS_ROOT / "AGENTS.md"), str(workspace / "AGENTS.md")],
        "assignments": {"implementer": assignments[args.implementer],
                        "reviewer": assignments["claude" if args.implementer == "codex" else "codex"]},
        "verification_commands": [{"argv": [python, "-B", "-S", "-m", "modules.tests.check_ledger",
                                             "--stress-seconds", str(args.stress_seconds)],
            "cwd": str(workspace), "timeout_seconds": max(30, args.stress_seconds + 120),
            "protected_paths": ["modules/tests"], "runtime_paths": list(runtime)}],
        "limits": dict(DEFAULT_LIMITS), "timeout_policy": dict(DEFAULT_TIMEOUT_POLICY),
        "dialog_owner": "harness", "caller_dialog": None, "report_identity": {"ai": "harness", "model": "none"}}
    validate_run(request, root)
    plan = {"schema_version": 1, "controller_root": str(root), "request": request, "stress_seconds": args.stress_seconds}
    args.prepare.parent.mkdir(parents=True, exist_ok=True)
    commit(args.prepare.resolve(), plan)
    commit(root / "request.json", request)
    print(json.dumps({"prepared": str(args.prepare.resolve()), "controller_root": str(root),
                      "request_file": str(root / "request.json"), "run_id": request["run_id"],
                      "model_called": False, "approval_created": False}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", type=Path)
    modes.add_argument("--request", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--implementer", choices=["codex", "claude"])
    for name in ("codex-model", "codex-effort", "claude-model", "claude-effort"):
        parser.add_argument("--" + name)
    parser.add_argument("--stress-seconds", type=int)
    args = parser.parse_args(argv)
    try:
        options = ("implementer", "codex_model", "codex_effort", "claude_model", "claude_effort", "stress_seconds")
        if args.prepare:
            if args.approval or any(getattr(args, n) is None for n in options):
                parser.error("prepare requires explicit roles, model/effort and stress-seconds; no approval")
            if not 0 <= args.stress_seconds <= 7200:
                parser.error("stress seconds must be between 0 and 7200")
            prepare(args)
            return 0
        if not args.approval or any(getattr(args, n) is not None for n in options):
            parser.error("execution requires only a prepared request and approval")
        plan = loads(args.request.read_bytes())
        root = Path(plan["controller_root"]).resolve()
        if root.parent.parent != HARNESS_ROOT / "modules/tests/fixtures" or not root.parent.name.startswith("m6-live-") or root.name != "controller":
            raise ContractError("not an isolated M6 controller")
        if (root / "modules/save_dialog.py").read_bytes() != (HARNESS_ROOT / "modules/save_dialog.py").read_bytes():
            raise ContractError("copied saver differs from installed source")
        result = execute(plan["request"], args.approval, root=root)
        evidence = HARNESS_ROOT / "runs" / (plan["request"]["run_id"] + "-evidence")
        evidence.mkdir(parents=True)
        commit(evidence / "summary.json", report(root, plan["request"]["run_id"]))
        commit(evidence / "fixture-manifest.json", {"path": str(root.parent), "run_id": plan["request"]["run_id"],
            "controller_root": str(root), "retention": "keep", "reason": "M6 natural task and operating evidence"})
        print(json.dumps({"status": result["status"], "reason_code": result["reason_code"],
                          "run_path": result["run_path"], "evidence": str(evidence)}, indent=2))
        return 0 if result["status"] == "success" else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
