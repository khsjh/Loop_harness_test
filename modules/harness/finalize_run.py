"""Freeze the run summary and honor the approved owner of the final user Q/A."""
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from modules.save_dialog import CorruptRecord, SUB, SEP, content_id, find_call, record_block
from .contracts import ContractError, loads
from .process_runner import run_process
from .run_store import commit, digest, safe_path
from .verification import process_ok
from .caller_dialog import check_binding


def prepare_final(store, request, message, outcome, reason, details):
    directory = store.path / "final"
    answer = (f"실행: {request['run_id']}\n작업 판정: {outcome}\n사유: {reason}\n"
              f"시작한 라운드: {store.state['rounds_started']}\n{details}")
    question = message["content"].encode("utf-8")
    answer_raw = answer.encode("utf-8")
    commit(directory / "question.txt", question, raw=True)
    commit(directory / "answer.txt", answer_raw, raw=True)
    identity = request["report_identity"]
    asked = message["user_asked_at"]
    if asked is not None:
        asked = datetime.fromisoformat(asked).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    receipt = {"schema_version": 1, "run_id": request["run_id"], "call_id": store.state["dialog_call_id"],
               "ai": identity["ai"], "model": identity["model"], "asked": asked,
               "question_sha256": digest(question), "answer_sha256": digest(answer_raw),
               "message_id": message["message_id"], "input_ref": request["input_ref"]}
    # Missing owner is supported only when recovering an already existing legacy run.
    # New execution requests require it through validate_run before creating a run.
    if "dialog_owner" in request:
        receipt.update(schema_version=2, dialog_owner=request["dialog_owner"])
    if "caller_dialog" in request:
        receipt.update(schema_version=3, caller_dialog=request["caller_dialog"])
    commit(directory / "dialog-request.json", receipt)
    return answer


def frozen_record(store):
    """Validate the frozen handoff against the persisted request, not caller input."""
    directory = store.path / "final"
    receipt = loads((directory / "dialog-request.json").read_bytes())
    question = (directory / "question.txt").read_bytes()
    answer = (directory / "answer.txt").read_bytes()
    if digest(question) != receipt["question_sha256"] or digest(answer) != receipt["answer_sha256"]:
        raise ContractError("frozen Q/A changed; refusing retry")
    request = loads((store.path / "run-request.json").read_bytes())
    owner = request.get("dialog_owner", "harness")
    if owner not in {"harness", "caller"}:
        raise ContractError("invalid persisted dialog owner")
    if "caller_dialog" in request:
        if (receipt.get("schema_version") != 3 or receipt.get("dialog_owner") != owner
                or receipt.get("caller_dialog") != request["caller_dialog"]):
            raise ContractError("frozen caller dialog differs from approved request")
        if request["caller_dialog"] is not None:
            message = loads((store.path / "input.json").read_bytes())
            check_binding(store.root, request, message)
            if receipt["message_id"] != request["caller_dialog"]["message_id"]:
                raise ContractError("frozen dialog refers to a different original message")
    elif "dialog_owner" in request:
        if receipt.get("schema_version") != 2 or receipt.get("dialog_owner") != owner:
            raise ContractError("frozen dialog owner differs from approved request")
    elif receipt.get("schema_version") != 1 or "dialog_owner" in receipt:
        raise ContractError("legacy dialog ownership cannot be changed during recovery")
    identity = request["report_identity"]
    if (receipt["run_id"] != request["run_id"] or receipt["call_id"] != store.state["dialog_call_id"]
            or any(receipt[k] != identity[k] for k in ("ai", "model"))):
        raise ContractError("frozen dialog identity differs from approved request")
    return receipt, question, answer, owner


def validate_record(store, path, receipt, question, expected_answer=None):
    """Caller answers may differ from the summary; their question and identity may not."""
    path = Path(path)
    if path.parent != store.root / "dialog" or path.is_symlink():
        raise ContractError("dialog must be a regular record under the controller dialog directory")
    path = safe_path(store.root, "dialog", path.name)
    try:
        meta = record_block(path)
    except CorruptRecord as exc:
        raise ContractError(f"cannot attach corrupt dialog: {exc}") from exc
    shared = receipt.get("caller_dialog") is not None
    dialog_run_id = None if shared else receipt["run_id"]
    if (meta is None or any(meta.get(k) != receipt[k] for k in ("ai", "model", "call_id"))
            or meta.get("run_id") != dialog_run_id):
        raise ContractError("saved dialog identity mismatch")
    raw = path.read_bytes()
    _, separator, body = raw.partition(("\n" + SUB + "\n[QUESTION]\n").encode())
    prefix = question + ("\n" + SUB + "\n[ANSWER]\n").encode()
    footer = ("\n" + SEP + "\n").encode()
    if not separator or not body.startswith(prefix) or not body.endswith(footer):
        raise ContractError("saved dialog differs from frozen input")
    actual_answer = body[len(prefix):-len(footer)]
    if not actual_answer.strip() or (expected_answer is not None and actual_answer != expected_answer):
        raise ContractError("saved dialog differs from frozen answer")
    expected_id = content_id(receipt["ai"], receipt["model"], dialog_run_id,
                             question.decode("utf-8"), actual_answer.decode("utf-8"))
    if meta["content_id"] != expected_id:
        raise ContractError("saved dialog content hash mismatch")
    saved = {"saved": True, "path": str(path), "sha256": digest(raw), "content_id": expected_id,
             "call_id": receipt["call_id"]}
    return saved, actual_answer


def verify_record(store, path, receipt, question, expected_answer=None):
    saved, actual_answer = validate_record(store, path, receipt, question, expected_answer)
    commit(store.path / "final/dialog-receipt.json", saved)
    commit(store.path / "final/input-dialog-check.json", {
        "message_id": receipt["message_id"], "input_ref": receipt["input_ref"],
        "question_sha256": receipt["question_sha256"], "answer_sha256": digest(actual_answer),
        "dialog": saved, "matches": True, "independent_user_authentication": False})
    return saved


def attach_caller_record(store, path):
    receipt, question, _, owner = frozen_record(store)
    if owner != "caller":
        raise ContractError("only caller-owned runs accept a caller dialog")
    return verify_record(store, path, receipt, question)


def save_final(store, *, runner=run_process):
    directory = store.path / "final"
    receipt, question, answer, owner = frozen_record(store)
    if owner == "caller":
        recorded = directory / "dialog-receipt.json"
        if recorded.exists():
            return attach_caller_record(store, loads(recorded.read_bytes())["path"])
        if receipt.get("caller_dialog") and (store.root / "dialog").exists():
            try:
                existing, _ = find_call(safe_path(store.root, "dialog"), receipt["call_id"])
            except CorruptRecord as exc:
                raise ContractError(str(exc)) from exc
            if existing is not None:
                return attach_caller_record(store, existing)
        # Never call the saver or turn an execution summary into a caller's answer.
        return {"saved": False, "status": "pending_caller", "owner": "caller",
                "run_id": receipt["run_id"], "call_id": receipt["call_id"],
                "message_id": receipt["message_id"],
                "dialog_run_id": None if receipt.get("caller_dialog") else receipt["run_id"],
                "ai": receipt["ai"], "model": receipt["model"], "asked": receipt["asked"],
                "question_file": str(directory / "question.txt"), "summary_file": str(directory / "answer.txt")}
    argv = [sys.executable, "-B", str(store.root / "modules/save_dialog.py"),
            "--ai", receipt["ai"], "--model", receipt["model"], "--run-id", receipt["run_id"],
            "--call-id", receipt["call_id"], "--question-file", str(directory / "question.txt"),
            "--answer-file", str(directory / "answer.txt")]
    if receipt["asked"] is not None:
        argv += ["--asked", receipt["asked"]]
    process = runner(argv, cwd=store.root, timeout_seconds=10)
    attempt = {"process": process.metadata(), "stdout": process.stdout.decode("utf-8", "replace"),
               "stderr": process.stderr.decode("utf-8", "replace")}
    # Every save attempt remains evidence; retries do not overwrite the first error.
    existing = sorted(directory.glob("save-attempt-*.json"))
    commit(directory / f"save-attempt-{len(existing) + 1:04}.json", attempt)
    if not process_ok(process):
        return {"saved": False, "error": attempt, "conflict": process.returncode == 2}
    path = Path(process.stdout.decode("utf-8").strip())
    return verify_record(store, path, receipt, question, expected_answer=answer)
