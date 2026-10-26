"""Manage one approved M6 loop in the background; no scheduler or automatic resume.

start --request REQUEST --approval APPROVAL --controller ROOT
status|stop --controller ROOT --run-id ID
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

from modules.harness.adapters.common import AdapterError
from modules.harness.contracts import ContractError, loads
from modules.harness.preflight_run import preflight
from modules.harness.provenance import source_manifest
from modules.harness.recording import HARNESS_ROOT
from modules.harness.run_contracts import identifier
from modules.harness.run_loop import execute
from modules.harness.run_report import report
from modules.harness.run_store import commit, safe_path, digest
from modules.harness.process_runner import now
from modules.harness.workspace_guard import identity_alive, process_identity


def job_path(root, run_id):
    identifier(run_id)
    return safe_path(root, "runs", ".m6-jobs", run_id)


def worker_manifest():
    # The worker is a test entry point outside modules/harness; bind it too.
    return {**source_manifest(), "modules/tests/background_m6_check.py": digest(Path(__file__).read_bytes())}


def start(root, request, approval):
    root, approval = Path(root).resolve(), Path(approval).resolve()
    checked = preflight(request, approval, root=root)
    if checked["status"] != "ready":
        raise ContractError("background preflight blocked: " + json.dumps(checked, ensure_ascii=False))
    directory = job_path(root, request["run_id"])
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)  # Exclusive reservation; even a failed launch is not replayed.
    commit(directory / "job.json", {"root": str(root), "request": request, "approval": str(approval),
                                    "created_at": now(), "source_manifest": worker_manifest()})
    try:
        with (directory / "controller.log").open("xb") as log:
            child = subprocess.Popen([sys.executable, "-B", "-m", "modules.tests.background_m6_check", "worker",
                "--controller", str(root), "--run-id", request["run_id"]], cwd=HARNESS_ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True)
        # Persist worker identity in the worker, before any model can start.
        return {"job_id": request["run_id"], "launch_pid": child.pid, "job_path": str(directory),
                "log": str(directory / "controller.log"), "status": "starting"}
    except OSError as exc:
        commit(directory / "launch-error.json", {"error": str(exc), "at": now()})
        raise


def status(root, run_id):
    directory = job_path(root, run_id)
    job = loads(safe_path(directory, "job.json").read_bytes())
    identity_path, exit_path = safe_path(directory, "identity.json"), safe_path(directory, "exit.json")
    identity = loads(identity_path.read_bytes()) if identity_path.exists() else None
    exit_record = loads(exit_path.read_bytes()) if exit_path.exists() else None
    error_path = safe_path(directory, "launch-error.json")
    launch_error = loads(error_path.read_bytes()) if error_path.exists() else None
    alive = identity_alive(identity)
    if alive:
        # A zombie cannot receive a graceful cancellation or continue work.
        try:
            proc_state = Path(f"/proc/{identity['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0]
            alive = proc_state != "Z"
        except (OSError, IndexError):
            alive = False
    state = ("finished" if exit_record is not None else "running" if alive else "interrupted" if identity
             else "launch_failed" if launch_error is not None else "starting_or_unconfirmed")
    return {"job_id": run_id, "job_path": str(directory), "controller": identity,
            "controller_alive": alive, "status": state, "exit": exit_record, "launch_error": launch_error,
            "log": str(directory / "controller.log"), "created_at": job["created_at"],
            "run": report(root, run_id) if safe_path(root, "runs", run_id).exists() else None}


def stop(root, run_id):
    current = status(root, run_id)
    if current["status"] in {"finished", "interrupted", "launch_failed"}:
        return {**current, "cancel_requested": False, "signal_sent": False}
    # A job-specific cooperative request cannot signal a reused PID. It also
    # handles cancellation during startup, before identity.json is committed.
    commit(job_path(root, run_id) / "stop-request.json", {"cancel": True})
    return {"job_id": run_id, "cancel_requested": True, "signal_sent": False, "status": "stop_requested",
            "note": "poll status for terminal outcome and cleanup; stop does not imply success"}


def worker(root, run_id):
    directory = job_path(root, run_id)
    # An interrupted or already completed worker is never run again.
    with safe_path(directory, "worker-claimed").open("xb") as claim:
        claim.write(b"1")
        claim.flush()
        os.fsync(claim.fileno())
    cancelled = False
    def cancel(signum, frame):
        nonlocal cancelled
        cancelled = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, cancel)
    identity = process_identity(os.getpid())
    if identity is None:
        raise ContractError("cannot identify background worker")
    commit(directory / "identity.json", identity)
    result = {"status": "failed", "reason_code": "worker_error"}
    try:
        job = loads(safe_path(directory, "job.json").read_bytes())
        if job["root"] != str(Path(root).resolve()) or job["request"]["run_id"] != run_id:
            raise ContractError("job identity mismatch")
        if job["source_manifest"] != worker_manifest():
            raise ContractError("harness source changed between launch and worker start")
        result = execute(job["request"], Path(job["approval"]), root=root,
                         cancel_check=lambda: cancelled or safe_path(directory, "stop-request.json").exists())
    except (OSError, ValueError, KeyError, TypeError, AdapterError) as exc:
        result["error"] = str(exc)
    except Exception as exc:
        # Keep the cause in exit.json; the traceback still reaches controller.log.
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        commit(directory / "exit.json", {"at": now(), "status": result["status"],
            "reason_code": result.get("reason_code"), "recovery_required": result.get("recovery_required"),
            "error": result.get("error")})
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if result["status"] == "success" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "status", "stop", "worker"])
    parser.add_argument("--controller", required=True, type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args(argv)
    root = args.controller.resolve()
    try:
        if args.action == "start":
            if not args.request or not args.approval or args.run_id:
                parser.error("start requires --request and --approval, without --run-id")
            result = start(root, loads(args.request.read_bytes()), args.approval)
        else:
            if not args.run_id or args.request or args.approval:
                parser.error("status/stop/worker require --run-id, without request or approval")
            if args.action == "worker":
                return worker(root, args.run_id)
            result = (status if args.action == "status" else stop)(root, args.run_id)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
