"""Windows launcher safety checks; all backend launches and port checks are mocked."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import BridgeError
import start_comfy


@unittest.skipUnless(os.name == "nt", "The installed portable backend uses Windows process flags and locks")
class StartComfyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-comfy-launch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.portable = self.root / "package" / "runtime" / "ComfyUI_windows_portable"
        self.comfy_root = self.portable / "ComfyUI"
        self.comfy_root.mkdir(parents=True)
        (self.comfy_root / "main.py").touch()
        self.python = self.portable / "python_embeded" / "python.exe"
        self.python.parent.mkdir()
        self.python.touch()
        self.gpu = "GPU-00000000-1111-2222-3333-444444444444"
        self.gpu_file = self.portable.parent / "selected-gpu.txt"
        self.gpu_file.write_text(self.gpu, encoding="utf-8")
        self.state = self.root / ".state"
        self.config = {"comfy_url": "http://127.0.0.1:8188", "comfy_root": str(self.comfy_root),
                       "python": str(self.python)}
        self.port = self.enterContext(patch.object(start_comfy, "_port_is_open", return_value=False))
        self.popen = self.enterContext(patch.object(start_comfy.subprocess, "Popen",
                                                   return_value=SimpleNamespace(pid=12345)))
        self.process_running = self.enterContext(patch.object(start_comfy, "_process_is_running", return_value=False))

    def previous_record(self, pid=22222):
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / "comfyui-launch.json"
        path.write_text(json.dumps({"pid": pid, "log_path": "previous.log"}), encoding="utf-8")
        return path

    def test_launch_uses_installed_python_uuid_and_detached_safe_flags(self):
        result = start_comfy.start_backend(self.config, self.state)
        self.assertEqual(result, {"state": "starting", "pid": 12345,
                                  "log_path": str(self.state / "comfyui-launch.log")})
        command = self.popen.call_args.args[0]
        options = self.popen.call_args.kwargs
        self.assertEqual(command[:3], [str(self.python), "-s", str(self.comfy_root / "main.py")])
        self.assertIn("--lowvram", command)
        self.assertIn("--disable-pinned-memory", command)
        self.assertEqual(command[command.index("--cuda-device") + 1], self.gpu)
        self.assertEqual(command[command.index("--reserve-vram") + 1], "1.5")
        self.assertEqual(command[command.index("--user-directory") + 1], str(self.comfy_root / "user"))
        self.assertNotIn("--auto-launch", command)
        self.assertIn("--disable-auto-launch", command)
        self.assertFalse(options["shell"])
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["cwd"], str(self.portable))
        self.assertEqual(options["env"]["CUDA_VISIBLE_DEVICES"], self.gpu)
        self.assertEqual(options["creationflags"], subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        record = json.loads((self.state / "comfyui-launch.json").read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], 12345)

    def test_occupied_port_refuses_a_second_backend(self):
        self.port.return_value = True
        with self.assertRaisesRegex(BridgeError, "8188"):
            start_comfy.start_backend(self.config, self.state)
        self.popen.assert_not_called()

    def test_missing_gpu_file_uses_default_and_preserves_inherited_cuda_selection(self):
        self.gpu_file.unlink()
        with patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "inherited-device"}):
            start_comfy.start_backend(self.config, self.state)
        self.assertNotIn("--cuda-device", self.popen.call_args.args[0])
        self.assertEqual(self.popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], "inherited-device")

    def test_missing_gpu_file_does_not_invent_cuda_environment(self):
        self.gpu_file.unlink()
        with patch.dict(os.environ, {}, clear=True):
            start_comfy.start_backend(dict(self.config, gpu_uuid=""), self.state)
        self.assertNotIn("--cuda-device", self.popen.call_args.args[0])
        self.assertNotIn("CUDA_VISIBLE_DEVICES", self.popen.call_args.kwargs["env"])

    def test_configured_gpu_takes_precedence_over_bundle_selection(self):
        selected = "GPU-55555555-6666-7777-8888-999999999999"
        self.gpu_file.write_text("invalid bundle selection", encoding="utf-8")
        start_comfy.start_backend(dict(self.config, gpu_uuid=selected), self.state)
        command = self.popen.call_args.args[0]
        self.assertEqual(command[command.index("--cuda-device") + 1], selected)
        self.assertEqual(self.popen.call_args.kwargs["env"]["CUDA_VISIBLE_DEVICES"], selected)

    def test_invalid_configured_gpu_never_launches(self):
        for gpu in ("--device 0", "GPU-not-a-uuid", 1, False, {}):
            with self.subTest(gpu=gpu):
                with self.assertRaisesRegex(BridgeError, "gpu_uuid.*UUID"):
                    start_comfy.start_backend(dict(self.config, gpu_uuid=gpu), self.state)
        self.popen.assert_not_called()

    def test_running_record_protects_initialization_before_port_bind(self):
        self.previous_record()
        self.process_running.return_value = True
        with self.assertRaisesRegex(BridgeError, "22222"):
            start_comfy.start_backend(self.config, self.state)
        self.process_running.assert_called_once_with(22222)
        self.popen.assert_not_called()

    def test_dead_record_does_not_permanently_block_restart(self):
        self.previous_record()
        start_comfy.start_backend(self.config, self.state)
        self.process_running.assert_called_once_with(22222)
        self.popen.assert_called_once()
        record = json.loads((self.state / "comfyui-launch.json").read_text(encoding="utf-8"))
        self.assertEqual(record["pid"], 12345)

    def test_invalid_gpu_and_missing_installation_never_launch(self):
        self.gpu_file.write_text("--device 0", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "UUID"):
            start_comfy.start_backend(self.config, self.state)
        self.gpu_file.write_text(self.gpu, encoding="utf-8")
        self.python.unlink()
        with self.assertRaisesRegex(BridgeError, "Python"):
            start_comfy.start_backend(self.config, self.state)
        self.popen.assert_not_called()

    def test_remote_or_unexpected_endpoint_never_launches(self):
        for endpoint in ("http://192.168.1.3:8188", "http://127.0.0.1:8189", "http://127.0.0.1:8188/path",
                         "http://user:password@localhost:8188", "http://localhost:bad"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(BridgeError):
                    start_comfy.start_backend(dict(self.config, comfy_url=endpoint), self.state)
        self.popen.assert_not_called()

    def test_file_lock_blocks_concurrent_start_then_releases(self):
        self.state.mkdir()
        with start_comfy._startup_lock(self.state):
            with self.assertRaisesRegex(BridgeError, "另一个请求"):
                start_comfy.start_backend(self.config, self.state)
        self.popen.assert_not_called()
        start_comfy.start_backend(self.config, self.state)
        self.popen.assert_called_once()

    def test_failed_spawn_releases_lock_for_retry(self):
        self.popen.side_effect = OSError("mock creation failure")
        with self.assertRaisesRegex(BridgeError, "mock creation failure"):
            start_comfy.start_backend(self.config, self.state)
        self.popen.side_effect = None
        self.assertEqual(start_comfy.start_backend(self.config, self.state)["state"], "starting")
        self.assertEqual(self.popen.call_count, 2)


class PortableEntryTests(unittest.TestCase):
    def test_missing_config_explains_setup_before_any_backend_start(self):
        with tempfile.TemporaryDirectory(prefix="codex-missing-config-") as directory:
            missing = str(Path(directory) / "config.json")
            result = subprocess.run([sys.executable, str(ROOT / "start_comfy.py"), "--config", missing],
                                    cwd=self_test_directory(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        detail = json.loads(result.stderr.decode("utf-8"))["error"]
        self.assertIn("install.ps1", detail)
        self.assertIn("config.example.json", detail)

    def test_direct_script_help_imports_bridge_and_emits_utf8(self):
        # --help exits before any backend startup. This catches isolated Python's
        # omission of the script directory from sys.path and console encodings.
        result = subprocess.run([sys.executable, str(ROOT / "start_comfy.py"), "--help"],
                                cwd=self_test_directory(), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn("启动本机已安装", result.stdout.decode("utf-8"))


def self_test_directory():
    return str(ROOT / "tests")


if __name__ == "__main__":
    unittest.main()
