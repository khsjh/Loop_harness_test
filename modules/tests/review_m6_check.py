"""Snapshot M6 source for a read-only cross-tool review, preserving its evidence."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from modules.harness.contracts import ContractError, loads
from modules.harness.authorization import resolve_source
from modules.harness.recording import HARNESS_ROOT
from modules.harness.run_agent_once import run_once
from modules.harness.run_store import commit, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", type=Path)
    mode.add_argument("--request", type=Path)
    parser.add_argument("--authorizations", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--input-ref", type=Path,
                        help="existing intake of the verbatim user instruction; reused across reviews")
    args = parser.parse_args()
    if args.prepare:
        if not args.model or not args.effort or not args.input_ref or args.authorizations or args.prepare.exists():
            parser.error("prepare requires explicit model/effort, input-ref and a new destination, without authorizations")
        ref = str(args.input_ref.resolve())
        intake = loads(Path(ref).read_bytes())
        message = loads(Path(intake["source"]["path"]).read_bytes())
        resolve_source(intake["source"], message["content"], HARNESS_ROOT / "modules/tests/fixtures")
        bundle = Path(tempfile.mkdtemp(prefix="m6-review-", dir=HARNESS_ROOT / "modules/tests/fixtures"))
        root, workspace = bundle / "controller", bundle / "workspace"
        root.mkdir()
        workspace.mkdir()
        files = list((HARNESS_ROOT / "modules/harness").rglob("*.py")) + list((HARNESS_ROOT / "modules/tests").glob("*.py"))
        files += [HARNESS_ROOT / name for name in ("AGENTS.md", "rules/default_rules.md", "modules/save_dialog.py",
                  "docs/architecture.md", "docs/usage.md", "docs/validation.md", "docs/sharing.md",
                  "modules/tests/fixtures/fake_loop_agent.py",
                  "modules/tests/fixtures/fake_background_worker.py", "modules/tests/fixtures/m6_ledger_checks.py")]
        hashes = {}
        for source in files:
            target = workspace / source.relative_to(HARNESS_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[str(source.relative_to(HARNESS_ROOT))] = digest(source.read_bytes())
        request = {"schema_version": 1, "run_id": "m6-review-" + uuid.uuid4().hex[:12], "call_id": "claude-review",
            "round": None, "agent": "claude", "role": "reviewer", "model": args.model,
            "reasoning_effort": args.effort, "authorization_ref": "m6-review", "workspace": str(workspace),
            "allowed_paths": [], "instruction_files": [str(HARNESS_ROOT / "AGENTS.md"), str(workspace / "AGENTS.md")],
            "verification_commands": [], "timeout_seconds": 21600,
            "task": "Critically review this snapshot against docs/architecture.md, docs/usage.md, "
                    "docs/validation.md, and docs/sharing.md. "
                    "Focus on preflight_run.py, run_report.py, inspect_run.py, caller_dialog.py, finalize_run.py, "
                    "run_store.py, provenance.py, and modules/tests/background_m6_check.py, live_m6_check.py, test_m6.py. "
                    "Check no-write inspection, event/artifact integrity, status interpretation, approval boundaries, "
                    "background lifecycle/cancellation, compatibility and missing meaningful tests. "
                    "Read only; do not execute or edit anything. Report actionable defects with path/line. "
                    "Known boundaries: static preflight does not prove authentication or sandbox availability; "
                    "background stop uses a cooperative per-job marker, no PID signals; no automatic resume; "
                    "CLI partial stdout is buffered; historical source without a manifest stays unknown. "
                    "Check validation claims against their stated scope; historical results do not approve this snapshot. "
                    "Return changes_requested for material defects, otherwise approved; separate future scope from defects."}
        commit(args.prepare.resolve(), {"controller_root": str(root), "request": request, "input_ref": ref, "source_sha256": hashes})
        print(json.dumps({"plan": str(args.prepare.resolve()), "run_id": request["run_id"], "model_called": False}))
        return 0
    if not args.authorizations or args.model or args.effort or args.input_ref:
        parser.error("execution requires only request and authorizations")
    plan = loads(args.request.read_bytes())
    root = Path(plan["controller_root"]).resolve()
    if root.parent.parent != HARNESS_ROOT / "modules/tests/fixtures" or not root.parent.name.startswith("m6-review-"):
        raise ContractError("not an isolated M6 review controller")
    for name, expected in plan["source_sha256"].items():
        if digest((Path(plan["request"]["workspace"]) / name).read_bytes()) != expected:
            raise ContractError("review snapshot changed: " + name)
    result = run_once(plan["request"], loads(args.authorizations.read_bytes()), harness_root=root)
    evidence = HARNESS_ROOT / "runs" / (plan["request"]["run_id"] + "-evidence")
    evidence.mkdir(parents=True)
    commit(evidence / "summary.json", {"source_sha256": plan["source_sha256"], "outcome": result.as_dict(), "controller_root": str(root)})
    commit(evidence / "fixture-manifest.json", {"path": str(root.parent), "retention": "keep", "reason": "M6 cross-tool review"})
    print(json.dumps({"result": result.result, "evidence": str(evidence)}, ensure_ascii=False, indent=2))
    return 0 if result.result["call_status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
