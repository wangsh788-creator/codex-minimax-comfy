"""Dependency-free JSON-lines MCP transport for the local video bridge.

Run with ``python mcp_server.py``. Only protocol messages are written to stdout.
The backend is loaded lazily so MCP discovery does not start ComfyUI or a model.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Any, TextIO

# ComfyUI's embedded Windows Python uses ._pth isolation and omits this folder.
sys.path.insert(0, str(Path(__file__).resolve().parent))


SERVER_NAME = "minimax-comfy-video"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", PROTOCOL_VERSION}
MAX_MESSAGE_CHARS = 2 * 1024 * 1024
MAX_WAIT_SECONDS = 55


class ProtocolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _tool(name: str, description: str, properties: dict, required: list[str], *, read_only: bool = True, destructive: bool = False, idempotent: bool | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": read_only or destructive if idempotent is None else idempotent,
            "openWorldHint": False,
        },
    }


JOB_ID = {"type": "string", "minLength": 1, "description": "Job ID returned by video_generate."}
TOOLS = [
    _tool("video_health", "Check the installed video backends, ComfyUI connection, and workflow readiness.", {}, []),
    _tool("video_start", "Start the installed local ComfyUI Stable backend in its existing low-VRAM mode. Already running instances are reused; poll video_health until ready.", {}, [], read_only=False, idempotent=True),
    _tool(
        "video_generate",
        "Submit text-to-video generation and return a job ID. Poll video_status or video_wait for the result; avoid resubmitting the same request while it is running.",
        {
            "prompt": {"type": "string", "minLength": 1, "description": "Describe the scene, subjects, motion, camera, and visual style."},
            "seconds": {"type": "number", "minimum": 1, "maximum": 15, "default": 5},
            "width": {"type": "integer", "minimum": 32, "maximum": 1920, "multipleOf": 32, "default": 864, "description": "Multiple of 32; width times height must not exceed 864 times 480 pixels."},
            "height": {"type": "integer", "minimum": 32, "maximum": 1920, "multipleOf": 32, "default": 480, "description": "Multiple of 32; width times height must not exceed 864 times 480 pixels."},
            "seed": {"type": "integer", "minimum": 0, "maximum": 2**64 - 1, "description": "Optional reproducible random seed."},
            "output_name": {"type": "string", "minLength": 1, "description": "Optional output file name or prefix, not a path."},
            "include_audio": {"type": "boolean", "default": True},
        },
        ["prompt"],
        read_only=False,
    ),
    _tool("video_status", "Get a video job's current status and any completed output file paths.", {"job_id": JOB_ID}, ["job_id"]),
    _tool(
        "video_wait",
        "Wait briefly for a job, returning its latest status. If still running, call again; each call waits at most 55 seconds.",
        {"job_id": JOB_ID, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": 30}},
        ["job_id"],
    ),
    _tool("video_cancel", "Cancel the specified video job if the backend supports safely cancelling that job.", {"job_id": JOB_ID}, ["job_id"], read_only=False, destructive=True),
]
TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}
GENERATE_DEFAULTS = {"seconds": 5, "width": 864, "height": 480, "include_audio": True}


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _validate_arguments(name: str, arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        raise ProtocolError(-32602, "Tool arguments must be an object.")
    schema = TOOL_BY_NAME[name]["inputSchema"]
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    if unknown:
        raise ProtocolError(-32602, "Unknown tool argument(s): " + ", ".join(sorted(unknown)))
    for required in schema["required"]:
        if required not in arguments:
            raise ProtocolError(-32602, f"Missing required argument: {required}")
    for key, value in arguments.items():
        rule = properties[key]
        kind = rule["type"]
        if kind == "string":
            valid = isinstance(value, str) and bool(value.strip())
        elif kind == "boolean":
            valid = isinstance(value, bool)
        elif kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            if valid and isinstance(value, float):
                valid = math.isfinite(value)
        else:
            valid = False
        if not valid:
            raise ProtocolError(-32602, f"Argument {key} must be a valid {kind}.")
        if "minimum" in rule and value < rule["minimum"]:
            raise ProtocolError(-32602, f"Argument {key} must be at least {rule['minimum']}.")
        if "maximum" in rule and value > rule["maximum"]:
            raise ProtocolError(-32602, f"Argument {key} must be at most {rule['maximum']}.")
        if "multipleOf" in rule and value % rule["multipleOf"]:
            raise ProtocolError(-32602, f"Argument {key} must be a multiple of {rule['multipleOf']}.")
    result = dict(arguments)
    if name == "video_generate":
        result = {**GENERATE_DEFAULTS, **result}
        if result["width"] * result["height"] > 864 * 480:
            raise ProtocolError(-32602, "width times height must not exceed 414720 pixels (864 times 480).")
    elif name == "video_wait":
        result.setdefault("timeout_seconds", 30)
    return result


class MCPServer:
    def __init__(self, bridge: Any = None):
        self._bridge = bridge

    def _backend(self) -> Any:
        if self._bridge is None:
            from bridge import VideoBridge

            self._bridge = VideoBridge()
        return self._bridge

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOL_BY_NAME:
            raise ProtocolError(-32602, "Unknown tool name.")
        arguments = _validate_arguments(name, params.get("arguments", {}))
        method = name.removeprefix("video_")
        try:
            result = getattr(self._backend(), method)(**arguments)
            # structuredContent must be an object; text remains readable by older clients.
            structured = result if isinstance(result, dict) else {"result": result}
            text_result = json.dumps(structured, ensure_ascii=False, allow_nan=False)
            return {"content": [{"type": "text", "text": text_result}], "structuredContent": structured, "isError": False}
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            }

    def handle_message(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid JSON-RPC request.")
        request_id = message.get("id")
        if isinstance(request_id, bool) or not (request_id is None or isinstance(request_id, (str, int, float))) or (isinstance(request_id, float) and not math.isfinite(request_id)):
            return _error(None, -32600, "Invalid JSON-RPC request ID.")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _error(request_id, -32600, "Invalid JSON-RPC request.")
        # Notifications have no response and must not trigger untrackable generation.
        if "id" not in message:
            return None
        method = message["method"]
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Request params must be an object.")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
                result = {
                    "protocolVersion": version,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Use video_health to check readiness. Submit with video_generate, then poll the returned job ID with video_status or video_wait. Show the final absolute video file path to the user.",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            else:
                raise ProtocolError(-32601, "Method not found: " + method)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except ProtocolError as exc:
            return _error(request_id, exc.code, exc.message)

    def serve(self, input_stream: TextIO, output_stream: TextIO) -> None:
        while True:
            line = input_stream.readline(MAX_MESSAGE_CHARS + 1)
            if not line:
                break
            if len(line) > MAX_MESSAGE_CHARS:
                while line and not line.endswith("\n"):
                    line = input_stream.readline(MAX_MESSAGE_CHARS + 1)
                response = _error(None, -32700, "MCP message exceeds the size limit.")
            else:
                try:
                    message = json.loads(line, parse_constant=_reject_nonfinite)
                except (ValueError, RecursionError):
                    response = _error(None, -32700, "Parse error: expected one JSON object per line.")
                else:
                    response = self.handle_message(message)
            if response is not None:
                output_stream.write(json.dumps(response, ensure_ascii=False, allow_nan=False) + "\n")
                output_stream.flush()


def _reject_nonfinite(value: str) -> None:
    raise ValueError("Non-finite JSON number: " + value)


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    MCPServer().serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
