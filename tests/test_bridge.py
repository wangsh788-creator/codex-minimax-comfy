"""Behavioral bridge tests. HTTP is mocked; no rendering job is submitted."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import BridgeError, ComfyHTTPError, VideoBridge


JOB_ID = "a" * 32
PROMPT_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class Response(io.BytesIO):
    pass


def row(prompt_id=PROMPT_ID, job_id=JOB_ID):
    return [1, prompt_id, {}, {"minimax_video_job_id": job_id}, ["15"]]


def completed_history(filename="render_00001_.mp4", subfolder="video/Codex_H3/job"):
    return {
        "status": {"completed": True, "status_str": "success", "messages": [["execution_success", {}]]},
        "outputs": {"15": {"images": [{"filename": filename, "subfolder": subfolder, "type": "output"}]}},
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows TMP may contain an 8.3 alias; compare canonical paths.
        self.root = Path(self.temp.name).resolve()
        root = self.root
        config = {
            "comfy_url": "http://127.0.0.1:8188",
            "comfy_root": str(root / "comfy"),
            "output_dir": "outputs", "state_dir": "state",
            "models": {"unet": "model.safetensors", "clip": "clip.safetensors", "video_vae": "video.safetensors", "audio_vae": "audio.safetensors", "lora": "lora.safetensors"},
        }
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.bridge = VideoBridge(path)

    def test_missing_config_explains_local_setup(self):
        missing = self.root / "missing-config.json"
        with self.assertRaises(BridgeError) as raised:
            VideoBridge(missing)
        self.assertIn("install.ps1", str(raised.exception))
        self.assertIn("config.example.json", str(raised.exception))
        self.assertIn(str(missing), str(raised.exception))

    def test_relative_installation_paths_are_anchored_to_config_and_passed_to_launcher(self):
        root = self.root
        settings = root / "配置 directory"
        settings.mkdir()
        config_path = settings / "config.json"
        config = dict(self.bridge.config, comfy_root="../ComfyUI", python="../python/python.exe",
                      output_dir="clips", state_dir="jobs")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        bridge = VideoBridge(config_path)
        self.assertEqual(bridge.comfy_root, (root / "ComfyUI").resolve())
        self.assertEqual(bridge.config["comfy_root"], str((root / "ComfyUI").resolve()))
        self.assertEqual(bridge.config["python"], str((root / "python" / "python.exe").resolve()))
        self.assertEqual(bridge.output_dir, settings / "clips")
        self.assertEqual(bridge.state_dir, settings / "jobs")
        bridge._request = Mock(side_effect=BridgeError("connection refused"))
        launcher = Mock(return_value={"state": "starting"})
        with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=launcher)}):
            self.assertEqual(bridge.start()["state"], "starting")
        launcher.assert_called_once_with(bridge.config, bridge.state_dir)
        self.assertTrue(Path(launcher.call_args.args[0]["comfy_root"]).is_absolute())
        self.assertTrue(Path(launcher.call_args.args[0]["python"]).is_absolute())

    def save_job(self, state="queued", prompt_id=PROMPT_ID, **kwargs):
        job = {"job_id": JOB_ID, "state": state, "prompt_id": prompt_id, "output_name": "测试", "workflow": {}, **kwargs}
        self.bridge._save(job)
        return job

    def stub_generate(self, receipt=None, error=None):
        self.bridge.health = Mock(return_value={"ready": True})
        self.bridge._request = Mock(side_effect=error) if error else Mock(return_value=receipt or {"prompt_id": PROMPT_ID, "node_errors": {}})

    def test_empty_http_200_responses_are_valid(self):
        self.bridge.opener.open = Mock(return_value=Response(b""))
        self.assertEqual(self.bridge._request("/queue", {"delete": [PROMPT_ID]}), {})
        request = self.bridge.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"delete": [PROMPT_ID]})

    def test_invalid_local_http_json_is_a_bridge_error(self):
        for body in [b"<html>unrelated server</html>", b"[]", b"null"]:
            with self.subTest(body=body):
                self.bridge.opener.open = Mock(return_value=Response(body))
                with self.assertRaises(BridgeError):
                    self.bridge._request("/system_stats")

    def test_http_rejection_is_distinct_from_lost_connection(self):
        self.bridge.opener.open = Mock(side_effect=HTTPError("http://127.0.0.1:8188/prompt", 400, "Bad request", {}, Response(b'{"error":"invalid graph"}')))
        with self.assertRaises(ComfyHTTPError) as raised:
            self.bridge._request("/prompt", {})
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("invalid graph", str(raised.exception))

    def test_invalid_generation_never_calls_health_or_http(self):
        self.stub_generate()
        with self.assertRaises(BridgeError):
            self.bridge.generate("test", seconds=16)
        self.bridge.health.assert_not_called()
        self.bridge._request.assert_not_called()

    def test_audio_option_controls_the_audio_decode_and_mux_branch(self):
        audio = self.bridge.build_workflow("test", 864, 480, 124, 0, "prefix", True)
        silent = self.bridge.build_workflow("test", 864, 480, 124, 0, "prefix", False)
        self.assertEqual(audio["13"]["class_type"], "VAEDecodeAudio")
        self.assertEqual(audio["14"]["inputs"]["audio"], ["13", 0])
        self.assertNotIn("audio", silent["14"]["inputs"])
        self.assertNotIn("13", silent)

    def test_generate_persists_metadata_and_quantized_duration(self):
        self.stub_generate()
        result = self.bridge.generate("场景", seed=0)
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["frames"] % 17, 5)
        self.assertGreaterEqual(result["actual_seconds"], 5)
        self.assertEqual(result["actual_seconds"], result["frames"] / 24)
        self.assertEqual(result["seed"], 0)
        self.assertNotIn("workflow", result)
        self.assertIn("workflow", self.bridge._load(result["job_id"]))
        self.assertEqual(self.bridge._request.call_count, 1)

    def test_accepted_receipt_with_node_warnings_is_not_resubmitted(self):
        warnings = {"unused_node": {"errors": ["not used"]}}
        self.stub_generate({"prompt_id": PROMPT_ID, "node_errors": warnings})
        result = self.bridge.generate("test")
        self.assertEqual(result["state"], "queued")
        self.assertEqual(result["node_warnings"], warnings)
        self.bridge._request.assert_called_once()

    def test_network_timeout_is_persisted_as_unknown_and_never_retried(self):
        self.stub_generate(error=BridgeError("response timed out"))
        with self.assertRaisesRegex(BridgeError, "提交未确认"):
            self.bridge.generate("test")
        self.bridge._request.assert_called_once()
        files = list(self.bridge.state_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        saved = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "submission_unknown")
        self.assertIsNone(saved["prompt_id"])

    def test_explicit_http_validation_rejection_is_terminal(self):
        self.stub_generate(error=ComfyHTTPError(400, "bad workflow"))
        with self.assertRaisesRegex(BridgeError, "未进入队列"):
            self.bridge.generate("test")
        saved = json.loads(next(self.bridge.state_dir.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved["state"], "failed")
        self.bridge._request.reset_mock()
        self.assertEqual(self.bridge.status(saved["job_id"])["state"], "failed")
        self.bridge._request.assert_not_called()

    def test_unknown_submission_recovers_only_matching_own_metadata(self):
        self.save_job("submission_unknown", prompt_id=None, error="lost response")
        queue = {"queue_running": [row(OTHER_ID, "b" * 32), row()], "queue_pending": []}
        self.bridge._request = Mock(side_effect=[queue, {}, {}])
        result = self.bridge.status(JOB_ID)
        self.assertEqual(result["prompt_id"], PROMPT_ID)
        self.assertEqual(result["state"], "running")
        self.assertNotIn("error", result)
        self.assertTrue(all(len(call.args) == 1 for call in self.bridge._request.call_args_list))

    def test_missing_queue_and_history_stays_unknown_without_resubmitting(self):
        self.save_job()
        self.bridge._request = Mock(side_effect=[{"queue_running": [], "queue_pending": []}, {}])
        result = self.bridge.status(JOB_ID)
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(all(len(call.args) == 1 for call in self.bridge._request.call_args_list))

    def test_history_failure_returns_diagnostic_without_downloading(self):
        self.save_job()
        history = {"status": {"completed": False, "status_str": "error", "messages": [[], ["execution_error", {"node_id": "10", "exception_message": "out of memory"}]]}}
        self.bridge._request = Mock(side_effect=[{}, {PROMPT_ID: history}])
        self.bridge.opener.open = Mock()
        result = self.bridge.status(JOB_ID)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error"]["exception_message"], "out of memory")
        self.bridge.opener.open.assert_not_called()

    def test_native_savevideo_output_is_downloaded_once_and_cached(self):
        self.save_job()
        data = b"\x00\x00\x00\x18ftypmp42test-video-and-audio"
        self.bridge._request = Mock(side_effect=[{}, {PROMPT_ID: completed_history()}])
        self.bridge.opener.open = Mock(return_value=Response(data))
        result = self.bridge.status(JOB_ID)
        target = Path(result["files"][0]["path"])
        self.assertEqual(result["state"], "completed")
        self.assertTrue(target.is_absolute())
        self.assertEqual(target.read_bytes(), data)
        self.assertIn("/view?", self.bridge.opener.open.call_args.args[0])
        self.bridge._request.reset_mock()
        self.assertEqual(self.bridge.status(JOB_ID)["files"], result["files"])
        self.bridge._request.assert_not_called()
        self.bridge.opener.open.assert_called_once()

    def test_download_refuses_traversal_in_savevideo_metadata(self):
        self.bridge.opener.open = Mock()
        job = self.save_job()
        for filename, subfolder in [("../outside.mp4", ""), ("outside.mp4", "../secret"), ("outside.mp4", "C:/secret"), ("outside.mp4", "/secret")]:
            with self.subTest(filename=filename, subfolder=subfolder):
                with self.assertRaisesRegex(BridgeError, "不安全"):
                    self.bridge._download(job, completed_history(filename, subfolder))
        self.bridge.opener.open.assert_not_called()

    def test_download_ignores_other_nodes_and_non_output_files(self):
        job = self.save_job()
        history = completed_history()
        history["outputs"]["15"]["images"][0]["type"] = "temp"
        history["outputs"]["evil"] = {"videos": [{"filename": "outside.mp4", "subfolder": "", "type": "output"}]}
        self.bridge.opener.open = Mock()
        with self.assertRaisesRegex(BridgeError, "没有返回 MP4"):
            self.bridge._download(job, history)
        self.bridge.opener.open.assert_not_called()

    def test_empty_or_failed_download_does_not_leave_partial_files(self):
        job = self.save_job()
        for response in [Response(b""), URLError("lost connection")]:
            with self.subTest(response=response):
                self.bridge.opener.open = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
                with self.assertRaises(BridgeError):
                    self.bridge._download(job, completed_history())
                self.assertEqual(list(self.bridge.output_dir.glob("*")), [])

    def test_cancel_is_atomic_and_scoped_to_owned_job(self):
        self.save_job("running")
        calls = []
        dispatched = False

        def request(route, data=None, **kwargs):
            nonlocal dispatched
            calls.append((route, data))
            if route == "/queue":
                return {"queue_running": [row(OTHER_ID, "b" * 32)] + ([] if dispatched else [row()]), "queue_pending": []}
            if route == f"/api/jobs/{PROMPT_ID}/cancel":
                self.assertEqual(data, {})
                dispatched = True
                return {"cancelled": True}
            if route == f"/history/{PROMPT_ID}":
                return {}
            self.fail(f"Unexpected HTTP route: {route}")

        self.bridge._request = request
        result = self.bridge.cancel(JOB_ID)
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["cancel_requested"])
        self.assertEqual([route for route, data in calls if data is not None], [f"/api/jobs/{PROMPT_ID}/cancel"])

    def test_cancel_does_not_touch_an_unrelated_running_job(self):
        self.save_job()
        self.bridge._request = Mock(side_effect=[{"queue_running": [row(OTHER_ID, "b" * 32)], "queue_pending": []}, {"queue_running": [row(OTHER_ID, "b" * 32)], "queue_pending": []}, {}])
        result = self.bridge.cancel(JOB_ID)
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(all(len(call.args) == 1 for call in self.bridge._request.call_args_list))

    def test_cancel_unsupported_backend_never_uses_global_interrupt(self):
        self.save_job("running")
        for code in (404, 405):
            with self.subTest(status=code):
                self.bridge._request = Mock(side_effect=[{"queue_running": [row()], "queue_pending": []},
                                                        ComfyHTTPError(code, "unsupported")])
                with self.assertRaisesRegex(BridgeError, "不支持安全按任务取消.*ComfyUI 界面"):
                    self.bridge.cancel(JOB_ID)
                self.assertEqual([call.args[0] for call in self.bridge._request.call_args_list],
                                 ["/queue", f"/api/jobs/{PROMPT_ID}/cancel"])
                self.assertNotIn("cancel_requested", self.bridge._load(JOB_ID))

    def test_cancel_other_http_errors_are_preserved(self):
        self.save_job("running")
        self.bridge._request = Mock(side_effect=[{"queue_running": [row()], "queue_pending": []},
                                                ComfyHTTPError(500, "backend failed")])
        with self.assertRaises(ComfyHTTPError) as raised:
            self.bridge.cancel(JOB_ID)
        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(self.bridge._request.call_count, 2)

    def test_cancel_noop_does_not_mark_completed_job_cancelled(self):
        self.save_job()
        self.bridge._request = Mock(side_effect=[{"queue_running": [row()], "queue_pending": []}, {"cancelled": False}, {}, {PROMPT_ID: completed_history()}])
        self.bridge._download = Mock(return_value=[{"path": "C:/video.mp4", "bytes": 100}])
        result = self.bridge.cancel(JOB_ID)
        self.assertEqual(result["state"], "completed")
        self.assertNotIn("cancel_requested", result)

    def test_cancel_unknown_submission_never_interrupts_anything(self):
        self.save_job("submission_unknown", prompt_id=None)
        self.bridge._request = Mock(side_effect=[{}, {}])
        with self.assertRaisesRegex(BridgeError, "未中断任何任务"):
            self.bridge.cancel(JOB_ID)
        self.assertTrue(all(len(call.args) == 1 for call in self.bridge._request.call_args_list))

    def test_cancel_already_rejected_submission_is_an_idempotent_noop(self):
        self.save_job("failed", prompt_id=None, error="ComfyUI rejected the graph")
        self.bridge._request = Mock()
        self.assertEqual(self.bridge.cancel(JOB_ID)["state"], "failed")
        self.bridge._request.assert_not_called()

    def test_start_reuses_matching_backend_without_spawning(self):
        self.bridge._request = Mock(return_value={"system": {"argv": [str(self.bridge.comfy_root / "main.py")]}})
        self.bridge.health = Mock(return_value={"ready": True, "comfy_url": self.bridge.url})
        with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=Mock())}):
            result = self.bridge.start()
            self.assertEqual(result["state"], "ready")
            self.assertTrue(result["already_running"])
            sys.modules["start_comfy"].start_backend.assert_not_called()

    def test_standard_relative_backend_launch_paths_are_reused(self):
        for entry in ("main.py", f"{self.bridge.comfy_root.name}/main.py"):
            with self.subTest(entry=entry):
                self.bridge._request = Mock(return_value={"system": {"argv": [entry]}})
                self.bridge.health = Mock(return_value={"ready": True})
                launcher = Mock()
                with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=launcher)}):
                    result = self.bridge.start()
                self.assertTrue(result["already_running"])
                self.assertEqual(result["state"], "ready")
                self.bridge.health.assert_called_once_with()
                launcher.assert_not_called()

    def test_relative_backend_paths_with_traversal_or_unexpected_parents_are_rejected(self):
        for entry in ("../main.py", f"{self.bridge.comfy_root.name}/../main.py", "other/main.py",
                      f"other/{self.bridge.comfy_root.name}/main.py", "other.py", "C:main.py",
                      "", None):
            with self.subTest(entry=entry):
                self.bridge._request = Mock(return_value={"system": {"argv": [entry]}})
                self.bridge.health = Mock()
                launcher = Mock()
                with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=launcher)}):
                    with self.assertRaisesRegex(BridgeError, "不一致"):
                        self.bridge.start()
                self.bridge.health.assert_not_called()
                launcher.assert_not_called()

    def test_start_refuses_running_comfy_from_another_directory(self):
        self.bridge._request = Mock(return_value={"system": {"argv": [str(self.root / "other" / "main.py")]}})
        launcher = Mock()
        with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=launcher)}):
            with self.assertRaisesRegex(BridgeError, "不一致"):
                self.bridge.start()
        launcher.assert_not_called()

    def test_start_delegates_unreachable_backend_to_listener_checking_launcher(self):
        self.bridge._request = Mock(side_effect=BridgeError("connection refused"))
        launcher = Mock(return_value={"state": "starting"})
        with patch.dict(sys.modules, {"start_comfy": types.SimpleNamespace(start_backend=launcher)}):
            self.assertEqual(self.bridge.start(), {"state": "starting"})
        launcher.assert_called_once_with(self.bridge.config, self.bridge.state_dir)


if __name__ == "__main__":
    unittest.main()
