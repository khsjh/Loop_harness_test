"""Observe and stop an approved real M6 model process through the public CLI.

This opt-in integration observer creates no approval and never signals a PID.
Its evidence directory must be new. Caller dialog attachment happens separately.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from modules.harness.contracts import loads
from modules.harness.process_runner import group_alive
from modules.harness.recording import HARNESS_ROOT
from modules.harness.run_store import commit
from modules.harness.workspace_guard import WorkspaceGuard, identity_alive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    request = loads(args.request.read_bytes())
    run_id = request["run_id"]
    args.evidence.mkdir(parents=True, exist_ok=False)

    def cli(action, name, *extra):
        argv = [sys.executable, "-B", "-m", "modules.tests.background_m6_check",
                action, "--controller", str(HARNESS_ROOT), *extra]
        result = subprocess.run(argv, cwd=HARNESS_ROOT, capture_output=True, timeout=30)
        commit(args.evidence / (name + "-command.json"), {"argv": argv, "returncode": result.returncode})
        commit(args.evidence / (name + ".stdout"), result.stdout, raw=True)
        commit(args.evidence / (name + ".stderr"), result.stderr, raw=True)
        if result.returncode:
            raise RuntimeError(f"{action} failed: {result.stderr.decode(errors='replace')} {result.stdout.decode(errors='replace')}")
        return loads(result.stdout)

    started = False
    stopped = False
    try:
        launch = cli("start", "start", "--request", str(args.request.resolve()),
                     "--approval", str(args.approval.resolve()))
        started = True
        print(json.dumps(launch), flush=True)
        guard_path = WorkspaceGuard(HARNESS_ROOT, request["workspace"], run_id).path
        deadline = time.monotonic() + 90
        model = None
        while time.monotonic() < deadline:
            if guard_path.exists():
                guard = loads(guard_path.read_bytes())
                candidate = guard.get("process") if guard["run_id"] == run_id else None
                if identity_alive(candidate):
                    try:
                        argv = Path(f"/proc/{candidate['pid']}/cmdline").read_bytes().split(b"\0")
                    except OSError:
                        continue
                    is_model = (b"exec" in argv and b"--json" in argv) or b"--print" in argv
                    if is_model and not {b"--help", b"--version"} & set(argv):
                        time.sleep(2)
                        if identity_alive(candidate):
                            model = candidate
                            break
            exit_path = HARNESS_ROOT / "runs/.m6-jobs" / run_id / "exit.json"
            if exit_path.exists():
                raise RuntimeError("worker exited before a live model process could be observed")
            time.sleep(.1)
        if model is None:
            raise RuntimeError("no live model process observed within 90 seconds")
        commit(args.evidence / "model-before-stop.json", {"identity": model, "alive": True,
               "argv": [arg.decode(errors="replace") for arg in argv if arg],
               "observation": "real CLI process alive for at least two seconds; not proof of a completed API response"})
        before = cli("status", "before", "--run-id", run_id)
        commit(args.evidence / "before.json", before)
        if before["status"] != "running":
            raise RuntimeError("worker is not running before stop")
        stop = cli("stop", "stop", "--run-id", run_id)
        stopped = True
        if not stop.get("cancel_requested") or stop.get("signal_sent"):
            raise RuntimeError("expected cooperative cancellation request")
        deadline = time.monotonic() + 30
        exit_path = HARNESS_ROOT / "runs/.m6-jobs" / run_id / "exit.json"
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        final = cli("status", "final-0", "--run-id", run_id)
        # exit.json is committed just before the worker itself returns.
        for attempt in range(1, 11):
            if not final["controller_alive"]:
                break
            time.sleep(.2)
            final = cli("status", f"final-{attempt}", "--run-id", run_id)
        commit(args.evidence / "final.json", final)
        guard = loads(guard_path.read_bytes())
        observed = {"model_alive": identity_alive(model), "process_group_alive": group_alive(model["pid"]),
                    "guard": guard, "exit": final.get("exit"), "worker_status": final["status"]}
        commit(args.evidence / "cleanup.json", observed)
        exit_record = final.get("exit") or {}
        run = final.get("run") or {}
        if (final["status"] != "finished" or final["controller_alive"] or
                exit_record.get("status") != "failed" or exit_record.get("reason_code") != "interrupted" or
                exit_record.get("recovery_required") or observed["model_alive"] or
                observed["process_group_alive"] or guard["active"] or guard["process"] is not None or
                run.get("integrity", {}).get("status") != "verified" or
                run.get("dialog", {}).get("recording_status") != "pending_caller"):
            raise RuntimeError("cancellation/cleanup assertions failed; inspect evidence")
        print(json.dumps({"verified": True, "run_id": run_id, "expected_outcome": "failed/interrupted",
                          "dialog": "pending_caller", "evidence": str(args.evidence.resolve())}), flush=True)
        return 0
    finally:
        if started and not stopped:
            cli("stop", "cleanup-stop", "--run-id", run_id)


if __name__ == "__main__":
    sys.exit(main())
