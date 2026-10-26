"""Read-only static checks. Readiness is neither approval nor a runtime probe."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import sys

from .adapters.common import AdapterError
from .caller_dialog import check_available
from .contracts import ContractError, loads
from .recording import HARNESS_ROOT
from .run_contracts import check_rules, load_approval, validate_run
from .run_store import RunStore, run_path, safe_path
from .verification import BOOTSTRAP_PATHS
from .workspace_guard import identity_alive, overlaps

ERRORS = (OSError, ValueError, KeyError, TypeError, AdapterError)


def preflight(request, approval_path=None, *, root=HARNESS_ROOT, which=shutil.which):
    root = Path(root).resolve()
    checks = []

    def check(name, operation):
        try:
            detail = operation()
        except ERRORS as exc:
            checks.append({"name": name, "status": "blocked", "detail": str(exc)})
            return False
        checks.append({"name": name, "status": "passed", "detail": detail})
        return True

    def available_run():
        if run_path(root, request["run_id"]).exists():
            raise ContractError("run_id already exists; inspect it instead of rerunning")

    def environment():
        if sys.platform != "linux" or platform.machine() != "x86_64":
            raise ContractError("supported verification environment is Linux x86_64")
        binaries = {name: which(name) for name in ("codex", "claude", "bwrap")}
        if not all(binaries.values()):
            raise ContractError("missing executables: " + ", ".join(k for k, v in binaries.items() if not v))
        for name in BOOTSTRAP_PATHS:
            if not Path(name).is_file():
                raise ContractError("missing sandbox bootstrap: " + name)
        for command in request["verification_commands"]:
            name = command["argv"][0]
            path = Path(name) if Path(name).is_absolute() else (
                Path(command["cwd"]) / name if "/" in name else Path(which(name, path="/usr/bin:/bin") or "/missing"))
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ContractError("verification executable unavailable: " + name)
        return binaries

    def guards():
        directory = safe_path(root, "runs", ".guards")
        for path in directory.glob("*.json"):
            guard = loads(safe_path(directory, path.name).read_bytes())
            if overlaps(Path(request["workspace"]), Path(guard["workspace"])):
                terminal = False
                if not guard["active"] and safe_path(root, "runs", guard["run_id"], "run-request.json").exists():
                    terminal = RunStore(root, run_id=guard["run_id"]).state["status"] is not None
                if guard["active"] or (identity_alive(guard.get("controller")) and not terminal):
                    raise ContractError("workspace busy or cleanup unconfirmed: " + guard["run_id"])
        return "advisory only; execution acquires the workspace lease again"

    valid = check("request", lambda: (validate_run(request, root), "valid")[1])
    summary = None
    if valid:
        summary = {key: request[key] for key in ("run_id", "workspace", "allowed_paths", "assignments",
                   "limits", "timeout_policy", "dialog_owner", "caller_dialog", "report_identity")}
        summary["verification"] = [{key: c[key] for key in ("cwd", "timeout_seconds", "protected_paths", "runtime_paths")}
                                   for c in request["verification_commands"]]
        check("run_id", available_run)
        check("local_environment", environment)
        check("workspace_guard", guards)
        if approval_path is not None:
            def approval():
                approved, message = load_approval(request, Path(approval_path).resolve(), root)
                check_rules(request, approved)
                check_available(root, request, message)
                return "scope, original input, instructions and caller binding checked"
            check("approval", approval)
        else:
            checks.append({"name": "approval", "status": "not_checked", "detail": "supply --approval"})
    for name in ("authentication", "model_availability", "network", "sandbox_execution"):
        checks.append({"name": name, "status": "not_checked", "detail": "static check does not execute processes"})
    status = "blocked" if any(c["status"] == "blocked" for c in checks) else "incomplete" if approval_path is None else "ready"
    return {"schema_version": 1, "status": status, "scope": "static_preflight", "checks": checks,
            "request": summary, "model_called": False, "approval_created": False,
            "execution_rechecks_required": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--controller", type=Path, default=HARNESS_ROOT)
    args = parser.parse_args(argv)
    try:
        result = preflight(loads(args.request.read_bytes()), args.approval, root=args.controller)
    except ERRORS as exc:
        result = {"status": "blocked", "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"ready": 0, "blocked": 1, "incomplete": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
