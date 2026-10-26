"""Codex exec: isolated settings, role sandbox, JSONL plus a fresh final file."""
import json
from pathlib import Path

from .common import AdapterError, model_metadata, read_final
from ..contracts import ContractError, loads

EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
DISABLED_FEATURES = (
    "apps", "plugins", "remote_plugin", "hooks", "multi_agent", "multi_agent_v2",
    "memories", "goals", "browser_use", "browser_use_external", "computer_use",
    "skill_mcp_dependency_install",
)
REQUIRED_FLAGS = {"--ignore-user-config", "--output-schema", "--output-last-message",
                  "--ephemeral", "--sandbox", "--json", "--model", "--cd"}


def check_configuration(request):
    if request["reasoning_effort"] not in EFFORTS:
        raise AdapterError("unsupported_configuration", "reasoning_effort", "unsupported Codex effort")
    # --ignore-user-config does not promise to ignore project configuration.
    workspace = Path(request["workspace"]).resolve()
    for directory in (workspace, *workspace.parents):
        if directory == Path.home():
            continue
        if (directory / ".codex/config.toml").exists():
            raise AdapterError("unsupported_configuration", "project_config",
                               "M3 requires a target without project Codex config")


def command(request, call_dir, executable="codex"):
    argv = [executable, "--ask-for-approval", "never", "--no-daemon", "exec",
            "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--json",
            "--model", request["model"], "--cd", request["workspace"],
            "--sandbox", "workspace-write" if request["role"] == "implementer" else "read-only",
            "--output-schema", str(call_dir / "response-schema.json"),
            "--output-last-message", str(call_dir / "agent-final.txt")]
    settings = {
        "model_reasoning_effort": request["reasoning_effort"],
        "mcp_servers": {}, "web_search": "disabled",
        "sandbox_workspace_write.exclude_slash_tmp": True,
        "sandbox_workspace_write.exclude_tmpdir_env_var": True,
        "sandbox_workspace_write.network_access": False,
        "sandbox_workspace_write.writable_roots": [],
    }
    for key, value in settings.items():
        # JSON literals used here are also TOML values (only scalar/empty containers).
        argv += ["-c", key + "=" + json.dumps(value)]
    for feature in DISABLED_FEATURES:
        argv += ["--disable", feature]
    return argv + ["-"]


def extract(stdout, call_dir):
    events = []
    for line in stdout.splitlines():
        if line.strip():
            event = loads(line)
            if type(event) is not dict or type(event.get("type")) is not str:
                raise ContractError("invalid Codex JSONL event")
            events.append(event)
    for event in events:
        if event["type"] in {"error", "turn.failed"}:
            raise AdapterError("process_error", "cli_failure", "Codex reported an error event")
    if not any(e["type"] == "turn.completed" for e in events):
        raise ContractError("Codex turn.completed event missing")
    messages = []
    reports = []
    for i, event in enumerate(events):
        item = event.get("item")
        if event["type"] == "item.completed" and type(item) is dict and item.get("type") == "agent_message":
            if type(item.get("text")) is not str:
                raise ContractError("invalid Codex agent_message")
            messages.append(item["text"])
        # Only CLI metadata, never model-authored message text.
        meta = event.get("payload") if event["type"] == "session_meta" else event
        if event["type"] in {"session_meta", "thread.started", "turn.started", "turn.completed"}:
            if type(meta) is dict and type(meta.get("model")) is str and meta["model"].strip():
                reports.append({"model": meta["model"], "source": f"stdout event {i}.model"})
    final = read_final(call_dir / "agent-final.txt")
    response = loads(final)
    if not messages or loads(messages[-1]) != response:
        raise ContractError("Codex final file and last agent_message disagree")
    return response, model_metadata(reports)
