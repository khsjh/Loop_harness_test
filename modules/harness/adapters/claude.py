"""Claude print JSON: restricted file tools, no plugins/MCP/shell tools."""
import json

from .common import AdapterError, model_metadata
from ..contracts import ContractError, response_schema, loads

EFFORTS = {"low", "medium", "high", "xhigh", "max"}
REQUIRED_FLAGS = {"--restricted", "--safe-mode", "--tools", "--allowedTools",
                  "--permission-mode", "--permission-prompts", "--json-schema",
                  "--output-format", "--strict-mcp-config", "--no-session-persistence",
                  "--disable-slash-commands", "--effort", "--model"}


def check_configuration(request):
    if request["reasoning_effort"] not in EFFORTS:
        raise AdapterError("unsupported_configuration", "reasoning_effort", "unsupported Claude effort")


def command(request, call_dir, executable="claude"):
    tools = "Read,Glob,Grep"
    if request["role"] == "implementer":
        tools += ",Edit,Write"
    return [executable, "--print", "--restricted", "--safe-mode",
            "--model", request["model"], "--effort", request["reasoning_effort"],
            "--output-format", "json", "--json-schema", json.dumps(response_schema(request)),
            "--tools", tools, "--allowedTools", tools,
            "--permission-mode", "acceptEdits" if request["role"] == "implementer" else "dontAsk",
            "--permission-prompts", "none", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--disable-slash-commands", "--no-chrome"]


def extract(stdout, call_dir):
    outer = loads(stdout)
    if type(outer) is not dict or outer.get("type") != "result":
        raise ContractError("Claude result envelope missing")
    if outer.get("is_error") is True or (type(outer.get("subtype")) is str and outer["subtype"] != "success"):
        raise AdapterError("process_error", "cli_failure", "Claude reported an unsuccessful result")
    if outer.get("is_error") is not False or outer.get("subtype") != "success":
        raise ContractError("Claude envelope lacks explicit success markers")
    if "structured_output" not in outer:
        raise ContractError("Claude structured_output missing")
    reports = []
    if type(outer.get("modelUsage")) is dict:
        for model in outer["modelUsage"]:
            if model.strip():
                reports.append({"model": model, "source": "stdout.modelUsage"})
    return outer["structured_output"], model_metadata(reports)
