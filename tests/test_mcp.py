"""Protocol and dispatch tests; no running backend or installed model is required."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import MAX_MESSAGE_CHARS, MCPServer, PROTOCOL_VERSION, TOOLS


class FakeBridge:
    def __init__(self):
        self.calls = []

    def health(self):
        self.calls.append(("health", {}))
        return {"ready": True}

    def generate(self, **kwargs):
        self.calls.append(("generate", kwargs))
        return {"job_id": "job-中文", "status": "queued", "actual_seconds": 5.166666666666667}

    def start(self):
        self.calls.append(("start", {}))
        return {"state": "starting"}

    def status(self, job_id):
        self.calls.append(("status", {"job_id": job_id}))
        return {"job_id": job_id, "status": "running"}

    def wait(self, job_id, timeout_seconds=30):
        self.calls.append(("wait", {"job_id": job_id, "timeout_seconds": timeout_seconds}))
        return {"job_id": job_id, "status": "completed", "output_path": "C:/视频/output.mp4"}

    def cancel(self, job_id):
        self.calls.append(("cancel", {"job_id": job_id}))
        return {"job_id": job_id, "status": "cancelled"}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.server = MCPServer(self.bridge)

    def request(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.server.handle_message(message)

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments if arguments is not None else {}})

    def assert_invalid(self, name, arguments):
        result = self.call(name, arguments)
        self.assertEqual(result["error"]["code"], -32602)
        self.assertEqual(self.bridge.calls, [])

    def test_initialize_and_protocol_negotiation(self):
        for requested, expected in [("2024-11-05", "2024-11-05"), ("2025-06-18", "2025-06-18"), ("unknown", PROTOCOL_VERSION)]:
            with self.subTest(requested=requested):
                response = self.request("initialize", {"protocolVersion": requested, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
                self.assertEqual(response["result"]["protocolVersion"], expected)
                self.assertIn("tools", response["result"]["capabilities"])
        self.assertEqual(self.bridge.calls, [])

    def test_notification_is_silent_and_does_not_generate(self):
        self.assertIsNone(self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle_message({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "video_generate", "arguments": {"prompt": "test"}}}))
        self.assertEqual(self.bridge.calls, [])

    def test_ping_and_empty_catalogs(self):
        self.assertEqual(self.request("ping", request_id="ping") , {"jsonrpc": "2.0", "id": "ping", "result": {}})
        for method, key in [("resources/list", "resources"), ("resources/templates/list", "resourceTemplates"), ("prompts/list", "prompts")]:
            with self.subTest(method=method):
                self.assertEqual(self.request(method)["result"], {key: []})

    def test_tool_catalog_schema_and_annotations(self):
        tools = self.request("tools/list")["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {"video_health", "video_start", "video_generate", "video_status", "video_wait", "video_cancel"})
        generate = next(tool for tool in tools if tool["name"] == "video_generate")
        self.assertEqual(generate["inputSchema"]["required"], ["prompt"])
        self.assertEqual(generate["inputSchema"]["properties"]["width"]["multipleOf"], 32)
        self.assertEqual(generate["inputSchema"]["properties"]["seed"]["maximum"], 2**64 - 1)
        self.assertEqual(generate["inputSchema"]["properties"]["seconds"]["maximum"], 15)
        self.assertFalse(generate["annotations"]["readOnlyHint"])
        self.assertFalse(generate["annotations"]["idempotentHint"])
        self.assertTrue(next(tool for tool in tools if tool["name"] == "video_cancel")["annotations"]["destructiveHint"])
        self.assertEqual(tools, TOOLS)

    def test_start_dispatch_and_idempotent_hint(self):
        result = self.call("video_start")["result"]
        self.assertEqual(result["structuredContent"], {"state": "starting"})
        self.assertEqual(self.bridge.calls, [("start", {})])
        tool = next(tool for tool in TOOLS if tool["name"] == "video_start")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["idempotentHint"])

    def test_health_dispatch(self):
        result = self.call("video_health")["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"], {"ready": True})
        self.assertEqual(json.loads(result["content"][0]["text"]), {"ready": True})
        self.assertEqual(self.bridge.calls, [("health", {})])

    def test_generate_default_arguments(self):
        response = self.call("video_generate", {"prompt": "一只小猫在花园中奔跑"})
        self.assertEqual(response["result"]["structuredContent"]["job_id"], "job-中文")
        self.assertEqual(self.bridge.calls, [("generate", {"prompt": "一只小猫在花园中奔跑", "seconds": 5, "width": 864, "height": 480, "include_audio": True})])

    def test_generate_optional_arguments_and_boundaries(self):
        args = {"prompt": "test", "seconds": 1.5, "width": 640, "height": 640, "seed": 2**64 - 1, "output_name": "clip", "include_audio": False}
        self.assertFalse(self.call("video_generate", args)["result"]["isError"])
        self.assertEqual(self.bridge.calls[-1], ("generate", args))

    def test_invalid_generation_arguments(self):
        bad_arguments = [
            {}, {"prompt": ""}, {"prompt": " \n\t"}, {"prompt": 123},
            {"prompt": "test", "extra": True},
            {"prompt": "test", "seconds": 0}, {"prompt": "test", "seconds": 16},
            {"prompt": "test", "seconds": True}, {"prompt": "test", "seconds": float("inf")},
            {"prompt": "test", "width": 0}, {"prompt": "test", "width": 865},
            {"prompt": "test", "height": 31}, {"prompt": "test", "height": 1921},
            {"prompt": "test", "width": 1920, "height": 1920},
            {"prompt": "test", "seed": -1}, {"prompt": "test", "seed": 2**64},
            {"prompt": "test", "seed": True}, {"prompt": "test", "seed": 1.5},
            {"prompt": "test", "include_audio": "true"},
            {"prompt": "test", "output_name": ""},
        ]
        for arguments in bad_arguments:
            with self.subTest(arguments=arguments):
                self.assert_invalid("video_generate", arguments)

    def test_status_wait_and_cancel_dispatch(self):
        for name, arguments in [("video_status", {"job_id": "job-1"}), ("video_wait", {"job_id": "job-1"}), ("video_wait", {"job_id": "job-2", "timeout_seconds": 55}), ("video_cancel", {"job_id": "job-1"})]:
            with self.subTest(name=name, arguments=arguments):
                self.assertFalse(self.call(name, arguments)["result"]["isError"])
        self.assertEqual(self.bridge.calls, [
            ("status", {"job_id": "job-1"}),
            ("wait", {"job_id": "job-1", "timeout_seconds": 30}),
            ("wait", {"job_id": "job-2", "timeout_seconds": 55}),
            ("cancel", {"job_id": "job-1"}),
        ])

    def test_wait_zero_and_invalid_timeout(self):
        self.assertFalse(self.call("video_wait", {"job_id": "job", "timeout_seconds": 0})["result"]["isError"])
        self.bridge.calls.clear()
        for timeout in [-1, 56, True, "30", float("nan")]:
            with self.subTest(timeout=timeout):
                self.assert_invalid("video_wait", {"job_id": "job", "timeout_seconds": timeout})

    def test_invalid_job_ids(self):
        for name in ["video_status", "video_wait", "video_cancel"]:
            for arguments in [{}, {"job_id": ""}, {"job_id": 123}, {"job_id": None}]:
                with self.subTest(name=name, arguments=arguments):
                    self.assert_invalid(name, arguments)

    def test_backend_errors_are_tool_errors(self):
        def fail():
            raise ConnectionError("ComfyUI unavailable")
        self.bridge.health = fail
        response = self.call("video_health")
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("ConnectionError: ComfyUI unavailable", response["result"]["content"][0]["text"])

    def test_nonobject_backend_result_is_wrapped(self):
        self.bridge.health = lambda: "ready"
        self.assertEqual(self.call("video_health")["result"]["structuredContent"], {"result": "ready"})

    def test_unserializable_backend_result_is_tool_error(self):
        self.bridge.health = lambda: {"path": Path("a.mp4")}
        self.assertTrue(self.call("video_health")["result"]["isError"])

    def test_unknown_methods_tools_and_params(self):
        self.assertEqual(self.request("missing")["error"]["code"], -32601)
        self.assertEqual(self.call("missing")["error"]["code"], -32602)
        self.assertEqual(self.request("tools/call", [])["error"]["code"], -32602)
        self.assertEqual(self.request("tools/call", {"name": "video_health", "arguments": []})["error"]["code"], -32602)
        self.assertEqual(self.bridge.calls, [])

    def test_invalid_requests(self):
        messages = [None, [], "test", {}, {"jsonrpc": "1.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": [], "method": "ping"}, {"jsonrpc": "2.0", "id": True, "method": "ping"}, {"jsonrpc": "2.0", "id": float("inf"), "method": "ping"}]
        for message in messages:
            with self.subTest(message=message):
                self.assertEqual(self.server.handle_message(message)["error"]["code"], -32600)

    def test_jsonlines_utf8_and_parse_recovery(self):
        requests = [
            "{bad json}",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": "中文", "method": "tools/call", "params": {"name": "video_generate", "arguments": {"prompt": "中文场景"}}}, ensure_ascii=False),
            '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
            '{"jsonrpc":"2.0","id":1e309,"method":"ping"}',
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}),
        ]
        output = io.StringIO()
        self.server.serve(io.StringIO("\n".join(requests) + "\n"), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 5)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], "中文")
        self.assertIn("中文", output.getvalue())
        self.assertEqual(responses[2]["error"]["code"], -32700)
        self.assertEqual(responses[3]["error"]["code"], -32600)
        self.assertEqual(responses[4]["result"], {})

    def test_oversized_line_recovers_for_next_request(self):
        output = io.StringIO()
        self.server.serve(io.StringIO("x" * (MAX_MESSAGE_CHARS + 100) + '\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_executable_emits_only_jsonlines_without_importing_backend(self):
        script = Path(__file__).resolve().parents[1] / "mcp_server.py"
        result = subprocess.run([sys.executable, str(script)], input='{"jsonrpc":"2.0","id":1,"method":"ping"}\n', text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), ['{"jsonrpc": "2.0", "id": 1, "result": {}}'])

    def test_installed_embedded_python_can_import_bridge_and_call_health(self):
        root = Path(__file__).resolve().parents[1]
        config_path = root / "config.json"
        if not config_path.exists():
            self.skipTest("Local installation configuration is not present.")
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        python = Path(config.get("python", ""))
        if not python.is_file():
            self.skipTest("ComfyUI's embedded Python is not installed on this machine.")
        result = subprocess.run(
            [str(python), str(root / "mcp_server.py")],
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "video_health", "arguments": {}}}) + "\n",
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=60,
        )
        response = json.loads(result.stdout)
        self.assertFalse(response["result"]["isError"], response)
        self.assertIn("ready", response["result"]["structuredContent"])
        self.assertIn("comfy_url", response["result"]["structuredContent"])


if __name__ == "__main__":
    unittest.main()
