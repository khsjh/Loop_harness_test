"""Resolve caller-owned authorization evidence without inferring user approval.

Sources establish traceability and integrity, not whether a natural-language task
is authorized. The trusted caller must still check the original instruction and scope.
"""
import hashlib
from pathlib import Path
import re

from modules.save_dialog import REQUIRED_META, SEP, SUB, content_id
from .contracts import ContractError, loads, nonempty

SOURCE_LIMIT = 2 * 1024 * 1024


def load_authorizations(path, workspace):
    path = Path(path).resolve()
    if path.is_relative_to(Path(workspace).resolve()):
        raise ContractError("approval registry must be outside the target workspace")
    return loads(path.read_bytes())


def resolve_source(source, instruction, workspace):
    if type(source) is not dict or source.get("kind") not in ("dialog", "user_message"):
        raise ContractError("authorization source kind must be dialog or user_message")
    id_key = "call_id" if source["kind"] == "dialog" else "message_id"
    if set(source) != {"kind", "path", "sha256", id_key}:
        raise ContractError("invalid authorization source fields")
    for key in ("path", id_key):
        nonempty(source[key], "authorization source " + key)
    if type(source["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
        raise ContractError("authorization source requires a SHA-256 digest")
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", source[id_key]):
        raise ContractError("invalid authorization source identifier")
    path = Path(source["path"])
    if not path.is_absolute() or not path.is_file():
        raise ContractError("authorization source must be an existing absolute file")
    if path.resolve().is_relative_to(Path(workspace).resolve()):
        raise ContractError("authorization source must be outside the target workspace")
    try:
        with path.open("rb") as file:
            raw = file.read(SOURCE_LIMIT + 1)
        if len(raw) > SOURCE_LIMIT:
            raise ContractError("authorization source exceeds 2 MiB")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"cannot read authorization source: {exc}") from exc
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ContractError("authorization source SHA-256 mismatch")
    if source["kind"] == "user_message":
        message = loads(text)
        fields = {"schema_version", "message_id", "role", "content", "user_asked_at"}
        if type(message) is not dict or set(message) != fields:
            raise ContractError("invalid user_message source fields")
        if type(message["schema_version"]) is not int or message["schema_version"] != 1:
            raise ContractError("invalid user_message source version")
        if message["role"] != "user" or message["message_id"] != source[id_key]:
            raise ContractError("authorization source role or message_id mismatch")
        if message["content"] != instruction:
            raise ContractError("authorization user instruction differs from source message")
        if message["user_asked_at"] is not None:
            nonempty(message["user_asked_at"], "user_asked_at")
    else:
        prefix = SEP + "\n[RECORD]\n"
        if not text.startswith(prefix):
            raise ContractError("authorization dialog must use the current RECORD format")
        header, separator, body = text[len(prefix):].partition("\n" + SUB + "\n[QUESTION]\n")
        if not separator:
            raise ContractError("authorization dialog header is incomplete")
        meta = {}
        for line in header.splitlines():
            key, sep, value = line.partition(": ")
            if not sep or key in meta:
                raise ContractError("authorization dialog header is malformed")
            meta[key] = value
        if not set(REQUIRED_META) <= set(meta) or meta.get("call_id") != source[id_key]:
            raise ContractError("authorization dialog metadata or call_id mismatch")
        question_prefix = instruction + "\n" + SUB + "\n[ANSWER]\n"
        footer = "\n" + SEP + "\n"
        if not body.startswith(question_prefix) or not body.endswith(footer):
            raise ContractError("authorization user instruction differs from dialog question")
        answer = body[len(question_prefix):-len(footer)]
        # The hash disambiguates delimiter examples embedded in a question/answer.
        digest = content_id(meta["ai"], meta["model"], meta.get("run_id"), instruction, answer)
        if digest != meta["content_id"]:
            raise ContractError("authorization dialog content_id mismatch")
    return text
