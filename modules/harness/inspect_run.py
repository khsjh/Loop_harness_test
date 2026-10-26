"""Read/recover run records without rerunning a model or verification command."""
import argparse
import json
from pathlib import Path
import sys

from .contracts import ContractError, loads
from .finalize_run import attach_caller_record, prepare_final, save_final
from .recording import HARNESS_ROOT, retry_recording
from .run_store import RunStore, commit, replace_json, run_path
from .workspace_guard import recover_guard


def inspect(root, run_id):
    path = run_path(root, run_id)
    if not (path / "run-request.json").exists():
        from .run_report import report
        return report(root, run_id)
    store = RunStore(root, run_id=run_id)
    return {"kind": "m5_run", "state": store.state, "run_path": str(path)}


def recover(root, run_id):
    store = RunStore(root, run_id=run_id)
    with store.writer():
        request = loads((store.path / "run-request.json").read_bytes())
        stopped = recover_guard(root, request["workspace"], run_id)
        if store.state["status"] is None:
            if not list((store.path / "final").iterdir()) and (store.path / "input.json").exists():
                message = loads((store.path / "input.json").read_bytes())
                prepare_final(store, request, message, "failed", "interrupted_unconfirmed",
                              "중단된 작업은 재실행하지 않았습니다. 마지막 단계: " + store.state["last_valid_phase"])
                store.event("final_answer_frozen", artifacts=[store.path / "final/question.txt", store.path / "final/answer.txt",
                                                              store.path / "final/dialog-request.json"])
            recording = "pending_caller" if request.get("dialog_owner") == "caller" else store.state["recording_status"]
            store.finish("failed", "interrupted_unconfirmed", recovery_required=not stopped, recording_status=recording)
        else:
            replace_json(store.path / "state.json", store.state)
            # A crash between the terminal event and its snapshot can be repaired.
            terminal_events = [loads(p.read_bytes())["state"] for p in sorted((store.path / "events").glob("*.json"))
                               if loads(p.read_bytes())["event"] == "terminated"]
            if terminal_events:
                commit(store.path / "terminal.json", terminal_events[0])
            if stopped and store.state.get("recovery_required"):
                store.event("recovery_confirmed", {"recovery_required": False})
        return {"state": store.state, "processes_confirmed_stopped": stopped, "model_reexecuted": False}


def repair_recording(root, run_id):
    store = RunStore(root, run_id=run_id)
    with store.writer():
        if store.state["status"] is None:
            raise ContractError("recover interrupted state before retrying storage")
        if store.state.get("recovery_required"):
            request = loads((store.path / "run-request.json").read_bytes())
            if not recover_guard(root, request["workspace"], run_id):
                raise ContractError("process cleanup still unconfirmed")
        repaired = []
        for file in sorted((store.path / "recovery").glob("*.json")):
            receipt = loads(file.read_bytes())
            if "recording_retry" not in receipt:
                continue
            outcome = retry_recording(receipt, Path(root).resolve())
            if outcome.result["call_status"] == "recording_error":
                return {"state": store.state, "error": outcome.as_dict(), "model_reexecuted": False}
            repaired.append(outcome.result["call_id"])
        final = store.path / "final/dialog-request.json"
        saved = save_final(store) if final.exists() else {"saved": False, "error": "no frozen final Q/A; manual report required"}
        if saved["saved"]:
            store.event("recording_repaired", {"recording_status": "saved", "recovery_required": False},
                        artifacts=[store.path / "final/dialog-receipt.json", store.path / "final/input-dialog-check.json"])
        elif saved.get("status") == "pending_caller":
            store.event("recording_repaired", {"recording_status": "pending_caller", "recovery_required": False})
        return {"state": store.state, "dialog": saved, "repaired_calls": repaired, "model_reexecuted": False}


def attach_caller_dialog(root, run_id, path):
    """Confirm the caller's already saved final answer; never create a dialog here."""
    store = RunStore(root, run_id=run_id)
    with store.writer():
        if store.state["status"] is None:
            raise ContractError("run must terminate before attaching the caller's final answer")
        saved = attach_caller_record(store, Path(path).absolute())
        store.event("caller_dialog_recorded", {"recording_status": "saved"},
                    artifacts=[store.path / "final/dialog-receipt.json", store.path / "final/input-dialog-check.json"])
        return {"state": store.state, "dialog": saved, "model_reexecuted": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--controller", type=Path, default=HARNESS_ROOT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--recover-state", action="store_true")
    mode.add_argument("--retry-recording", action="store_true")
    mode.add_argument("--attach-caller-dialog", type=Path, metavar="DIALOG_PATH")
    args = parser.parse_args(argv)
    root = args.controller.resolve()
    try:
        if args.report:
            from .run_report import report
            result = report(root, args.run_id)
        elif args.attach_caller_dialog:
            result = attach_caller_dialog(root, args.run_id, args.attach_caller_dialog)
        else:
            operation = recover if args.recover_state else repair_recording if args.retry_recording else inspect
            result = operation(root, args.run_id)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    dialog = result.get("dialog", {})
    if args.report:
        return {"ok": 0, "incomplete": 2, "error": 1}[result["report_status"]]
    return 1 if result.get("error") or (dialog and not dialog["saved"] and dialog.get("status") != "pending_caller") else 0


if __name__ == "__main__":
    sys.exit(main())
