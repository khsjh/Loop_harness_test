"""Read verified event snapshots; never repair records while reporting."""
from pathlib import Path

from .contracts import ContractError, loads
from .finalize_run import frozen_record, validate_record
from .provenance import source_manifest
from .recording import json_bytes
from .run_store import RunStore, digest, run_path, safe_path
from .verification import metadata_ok


def pending_call_recordings(path):
    """Recovery receipts whose call result has not been committed yet (repair_recording's input)."""
    pending = []
    for file in sorted(safe_path(path, "recovery").glob("*.json")):
        receipt = loads(safe_path(path, "recovery", file.name).read_bytes())
        if "recording_retry" not in receipt:
            continue
        result = receipt["recording_retry"]["result"]
        saved = safe_path(path, "calls", "call-" + result["call_id"], "result.json")
        if not saved.exists() or saved.read_bytes() != json_bytes(result):
            pending.append({"call_id": result["call_id"], "receipt": str(file)})
    return pending


def evidence_bundle(root, path):
    """List unverified pointers only; never follow them or reuse summary verdicts."""
    references, warnings = [], []
    names = ["fixture-manifest.json", "summary.json", "completion.json"]
    names += sorted(p.name for p in path.glob("*-report-final.json"))
    for name in names:
        try:
            file = safe_path(path, name)
            if not file.is_file():
                continue
            data = loads(file.read_bytes())
            if type(data) is not dict:
                raise ContractError("expected a JSON object")
        except (OSError, ValueError, TypeError) as exc:
            warnings.append({"file": name, "error": str(exc)})
            continue
        records = [("$", data, None)]
        if "outcome" in data:
            records.append(("$.outcome", data["outcome"], data.get("controller_root")))
        if type(data.get("runs")) is list:
            records += [(f"$.runs[{index}]", record, None) for index, record in enumerate(data["runs"])]
        for location, record, enclosing_controller in records:
            try:
                if type(record) is not dict:
                    raise ContractError("expected a reference object")
                run_id = record.get("run_id")
                if run_id is None:
                    continue
                local_path = run_path(root, run_id)
                controller = record.get("controller_root", enclosing_controller)
                recorded_path = record.get("run_path")
                if controller is None and type(recorded_path) is str:
                    recorded = Path(recorded_path)
                    if recorded.is_absolute() and recorded.parent.name == "runs" and recorded.name == run_id:
                        controller = str(recorded.parent.parent)
                if controller is None and local_path.is_dir():
                    controller = str(root)
                if controller is None:
                    raise ContractError(f"cannot resolve controller for run_id: {run_id}")
                if (type(controller) is not str or "\0" in controller or
                        not Path(controller).is_absolute() or ".." in Path(controller).parts):
                    raise ContractError("invalid controller reference")
                reference = {"run_id": run_id, "controller_root": controller,
                    "argv": ["python3", "-B", "-m", "modules.harness.inspect_run", run_id,
                             "--controller", controller, "--report"]}
                if reference not in references:
                    references.append(reference)
            except (OSError, ValueError, TypeError) as exc:
                warnings.append({"file": name, "record": location, "error": str(exc)})
    return {"path": str(path), "files": sorted(p.name for p in path.iterdir()),
            "run_references": references, "reference_warnings": warnings,
            "references_verified": False}


def report(root, run_id):
    root = Path(root).resolve()
    result = {"schema_version": 1, "run_id": run_id, "report_status": "error", "execution": None,
              "integrity": {"status": "unverified"}, "next_actions": []}
    try:
        path = run_path(root, run_id)
        if not path.is_dir():
            raise ContractError("unknown run")
        request_path = safe_path(path, "run-request.json")
        if not request_path.exists():
            # An M5 run with a missing request must not be mistaken for an M3 record.
            if safe_path(path, "events").exists():
                raise ContractError("missing run-request.json")
            call_path = safe_path(path, "calls")
            if call_path.is_dir():
                calls = sorted(p.name for p in call_path.glob("call-*"))
                result.update(report_status="incomplete", kind="legacy_single_call", calls=calls)
                reason = "legacy call has no loop event chain"
            elif path.name.endswith("-evidence"):
                result.update(report_status="ok", kind="evidence_bundle", evidence=evidence_bundle(root, path))
                reason = "summary bundle only; inspect referenced runs to verify execution"
            else:
                result.update(report_status="incomplete", kind="unknown_record")
                reason = "no loop request, call directory or evidence-bundle name"
            result["integrity"] = {"status": "not_checked", "reason": reason}
            return result
        store = RunStore(root, run_id=run_id)
        artifacts = {}
        for event in store.events:
            artifacts.update(event["artifacts"])

        def read(name):
            if name not in artifacts:
                return None
            raw = safe_path(path, name).read_bytes()
            if digest(raw) != artifacts[name]:
                raise ContractError("artifact changed during report: " + name)
            return loads(raw)

        state = store.state
        result.update(kind="loop", run_path=str(path), report_status="ok",
                      as_of_sequence=state["sequence"], as_of_event=store.head)
        result["execution"] = {k: state.get(k) for k in ("status", "reason_code", "task_outcome", "task_result",
            "phase", "rounds_started", "max_rounds", "active_operation", "started_at", "ended_at", "elapsed_seconds",
            "code_state_id", "observed_code_state_id", "recovery_required")}
        request = read("run-request.json")
        if request is None:
            result.update(report_status="incomplete")
            result["integrity"] = {"status": "partial", "reason": "request not committed to events yet"}
            return result
        result["settings"] = {k: request.get(k) for k in ("assignments", "limits", "timeout_policy", "report_identity")}
        result["source"] = {"at_execution": read("source-manifest.json"), "at_inspection": source_manifest()}
        result["instruction_hashes"] = (read("approval.json") or {}).get("instruction_hashes")
        calls, checks = [], []
        final_id = state.get("observed_code_state_id") or state.get("code_state_id")
        for event in store.events:
            for name in event["artifacts"]:
                if name.startswith("calls/") and name.endswith("/result.json"):
                    call = read(name)
                    calls.append({"path": str(path / name), "call_id": call.get("call_id"), "role": call.get("role"),
                        "call_status": call.get("call_status"), "actual_model": call.get("actual_model", "unknown"),
                        "reported_models": call.get("reported_models", []),
                        "requested_model": call.get("execution", {}).get("requested_model"),
                        "cli_version": call.get("execution", {}).get("cli_version", "unknown"),
                        "verdict": (call.get("response") or {}).get("verdict"),
                        "code_state_id": event["state"].get("code_state_id") if call.get("role") == "reviewer" else None,
                        "round": event["state"].get("rounds_started")})
                if name.startswith("rounds/") and "/verification/" in name and name.endswith("/result.json"):
                    verification = read(name)
                    # The executor's process rule; code state is judged separately below.
                    checks.append({"path": str(path / name), **verification,
                        "passed": metadata_ok(verification["process"]),
                        "matches_final_code": verification["code_state_id"] == final_id})
        result["calls"], result["verification"] = calls, checks
        result["final_code_reviews"] = [c for c in calls if c["role"] == "reviewer" and c["code_state_id"] == final_id]
        result["timeout_history"] = [{"path": str(path / n), "data": read(n)} for n in sorted(artifacts)
            if n.endswith(("timeout-feedback.json", "timeout-decision.json", "timeout-applied.json"))]
        owner = request.get("dialog_owner", "harness")
        recording_status = state.get("recording_status", "unknown")
        result["dialog"] = {"owner": owner, "recording_status": recording_status,
                            "call_id": state.get("dialog_call_id"), "receipt": None}
        saved = read("final/dialog-receipt.json")
        if saved is not None:
            receipt, question, answer, owner = frozen_record(store)
            actual, _ = validate_record(store, Path(saved["path"]), receipt, question,
                                         answer if owner == "harness" else None)
            if saved != actual:
                raise ContractError("dialog changed since its recorded receipt")
            result["dialog"]["receipt"] = saved
        elif recording_status == "saved":
            raise ContractError("saved dialog has no verified receipt")
        result["pending_call_recordings"] = pending_call_recordings(path)
        action = ["python3", "-B", "-m", "modules.harness.inspect_run", run_id, "--controller", str(root)]
        if state.get("recovery_required"):
            result["next_actions"].append({"reason": "confirm process cleanup before storage repair", "argv": action + ["--recover-state"]})
        if recording_status == "pending_caller":
            result["next_actions"].append({"reason": "caller must save the actual final answer once, then attach each run",
                                           "argv": action + ["--attach-caller-dialog", "<saved-dialog-path>"]})
        dialog_retry = recording_status == "error" or (
            state.get("status") is not None and recording_status not in {"saved", "pending_caller"})
        # A saved final dialog does not mean every call record was committed.
        calls_retry = state.get("status") is not None and result["pending_call_recordings"]
        if dialog_retry or calls_retry:
            reason = "retry storage only" + (" (unsaved call records)" if calls_retry else "")
            result["next_actions"].append({"reason": reason, "argv": action + ["--retry-recording"]})
        if state["status"] is None:
            result["next_actions"].append({"reason": "run is not terminal; inspect controller liveness before recovery", "argv": action})
        result["integrity"] = {"status": "verified", "events": len(store.events), "artifacts": len(artifacts),
                               "scope": "recorded snapshot; not an independent authentication of its author"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Do not retain an apparently successful execution beside failed integrity.
        result.update(report_status="error", execution=None, next_actions=[], error=str(exc))
        result["integrity"] = {"status": "error"}
    return result
