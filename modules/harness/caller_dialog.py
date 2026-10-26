"""Bind one original input and caller identity to one shared dialog call_id."""
import fcntl
from modules.save_dialog import CorruptRecord, find_call
from .contracts import ContractError, loads
from .run_store import commit, digest, safe_path


def binding(request, message):
    ref = request["caller_dialog"]
    if ref["message_id"] != message["message_id"]:
        raise ContractError("caller dialog must refer to the original intake message")
    return {"schema_version": 1, "call_id": ref["call_id"], "message_id": message["message_id"],
            "input_ref": request["input_ref"], "question_sha256": digest(message["content"].encode("utf-8")),
            "report_identity": request["report_identity"]}


def check_available(root, request, message):
    """Read-only advisory check; callers must repeat it under the binding lock."""
    if request.get("caller_dialog") is None:
        return
    expected = binding(request, message)
    directory = safe_path(root, "runs", ".caller-dialogs")
    for file in sorted(directory.glob("*.json")):
        existing = loads(safe_path(directory, file.name).read_bytes())
        if (existing["call_id"] == expected["call_id"] or existing["message_id"] == expected["message_id"]
                or existing["input_ref"] == expected["input_ref"]):
            if existing != expected:
                raise ContractError("caller dialog is already bound to a different input, call_id or identity")
    dialog_root = safe_path(root, "dialog")
    try:
        existing_dialog = find_call(dialog_root, expected["call_id"])[0] if dialog_root.exists() else None
    except CorruptRecord as exc:
        raise ContractError(str(exc)) from exc
    if existing_dialog is not None:
        raise ContractError("caller dialog is already finalized; start a new user request instead")


def bind_caller_dialog(root, request, message):
    """Called only after approval validation, before any model or run is started."""
    if request.get("caller_dialog") is None:
        return
    expected = binding(request, message)
    directory = safe_path(root, "runs", ".caller-dialogs")
    directory.mkdir(parents=True, exist_ok=True)
    with safe_path(directory, ".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        check_available(root, request, message)
        commit(safe_path(directory, expected["call_id"] + ".json"), expected)


def check_binding(root, request, message):
    expected = binding(request, message)
    path = safe_path(root, "runs", ".caller-dialogs", expected["call_id"] + ".json")
    if loads(path.read_bytes()) != expected:
        raise ContractError("caller dialog binding differs from the approved input or identity")
