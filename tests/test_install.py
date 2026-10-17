"""Exercise setup in a clean copy; never register a real Codex plugin."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is needed for the Windows installer")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="minimax-public-setup-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "插件 with spaces"
        self.plugin.mkdir()
        for filename in ("install.ps1", "config.example.json"):
            shutil.copyfile(ROOT / filename, self.plugin / filename)
        self.portable = self.root / "AI portable"
        self.comfy = self.portable / "ComfyUI"
        self.comfy.mkdir(parents=True)
        (self.comfy / "main.py").touch()
        self.python = self.portable / "python_embeded" / "python.exe"
        self.python.parent.mkdir()
        self.python.touch()

    def run_setup(self, *arguments):
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(self.plugin / "install.ps1"),
             "-ConfigureOnly", *map(str, arguments)],
            cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )

    def config(self):
        return json.loads((self.plugin / "config.json").read_text(encoding="utf-8"))

    def test_clean_clone_infers_portable_python_and_generates_local_mcp(self):
        result = self.run_setup("-ComfyRoot", self.comfy)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.config()
        self.assertEqual(Path(config["comfy_root"]), self.comfy)
        self.assertEqual(Path(config["python"]), self.python)
        spec = json.loads((self.plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["minimax_comfy"]
        self.assertEqual(Path(spec["command"]), self.python)
        self.assertEqual(Path(spec["args"][0]), self.plugin / "mcp_server.py")
        self.assertEqual(Path(spec["cwd"]), self.plugin)
        self.assertEqual(Path(spec["env"]["MINIMAX_VIDEO_CONFIG"]), self.plugin / "config.json")
        self.assertIn("Codex settings were not changed", result.stdout)

    def test_explicit_python_supports_an_installation_without_portable_layout(self):
        custom_python = self.root / "custom Python.exe"
        custom_python.touch()
        result = self.run_setup("-ComfyRoot", self.comfy, "-Python", custom_python)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(self.config()["python"]), custom_python)

    def test_rerun_preserves_existing_local_settings(self):
        config = json.loads((self.plugin / "config.example.json").read_text(encoding="utf-8"))
        config.update(comfy_root=str(self.comfy), python=str(self.python), output_dir="my videos",
                      gpu_uuid="GPU-00000000-1111-2222-3333-444444444444", custom_setting="keep me")
        (self.plugin / "config.json").write_text(json.dumps(config), encoding="utf-8")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config(), config)

    def test_missing_root_explains_required_arguments_without_writing_config(self):
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ComfyRoot", result.stderr)
        self.assertFalse((self.plugin / "config.json").exists())
        self.assertFalse((self.plugin / ".mcp.json").exists())

    def test_invalid_installation_does_not_leave_partial_local_configuration(self):
        wrong_root = self.root / "wrong ComfyUI"
        wrong_root.mkdir()
        result = self.run_setup("-ComfyRoot", wrong_root, "-Python", self.python)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("main.py", result.stderr)
        self.assertFalse((self.plugin / "config.json").exists())
        self.assertFalse((self.plugin / ".mcp.json").exists())


if __name__ == "__main__":
    unittest.main()
