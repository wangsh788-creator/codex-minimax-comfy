"""MiniMax H3 -> ComfyUI bridge. Python standard library only; no model downloads."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

ROOT = Path(__file__).resolve().parent
MODEL_FOLDERS = {"unet": "diffusion_models", "clip": "text_encoders",
                 "video_vae": "vae", "audio_vae": "vae", "lora": "loras"}
REQUIRED_NODES = ("UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
                  "MiniMaxH3ImageToVideo", "RandomNoise", "KSamplerSelect",
                  "BasicGuider", "BasicScheduler", "SamplerCustomAdvanced",
                  "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo")


class BridgeError(RuntimeError):
    pass


class ComfyHTTPError(BridgeError):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(f"ComfyUI HTTP {status_code}: {detail}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now():
    return datetime.now(timezone.utc).isoformat()


def load_config(config_path):
    """Read local settings and anchor installation paths to their config file."""
    config_path = Path(config_path).expanduser().resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise BridgeError(f"找不到配置文件 {config_path}。请运行 install.ps1，或复制 config.example.json 为 config.json 并填写本机路径。") from exc
    for key in ("comfy_root", "python"):
        if key in config:
            path = Path(config[key]).expanduser()
            config[key] = str((config_path.parent / path).resolve() if not path.is_absolute() else path.resolve())
    return config


class VideoBridge:
    def __init__(self, config_path=None):
        self.config_path = Path(config_path or os.environ.get("MINIMAX_VIDEO_CONFIG", ROOT / "config.json")).expanduser().resolve()
        self.config = load_config(self.config_path)
        self.url = self.config["comfy_url"].rstrip("/")
        parts = urlsplit(self.url)
        if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1")
                or parts.username or parts.password or parts.query or parts.fragment or parts.path):
            raise BridgeError("comfy_url 必须是本机 HTTP 地址，例如 http://127.0.0.1:8188")
        self.comfy_root = Path(self.config["comfy_root"]).resolve()
        self.output_dir = self._resolve(self.config.get("output_dir", "outputs"))
        self.state_dir = self._resolve(self.config.get("state_dir", ".state"))
        self.models = self.config["models"]
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _resolve(self, value):
        path = Path(value).expanduser()
        return (self.config_path.parent / path).resolve() if not path.is_absolute() else path.resolve()

    def _request(self, route, data=None, timeout=15):
        payload = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = Request(self.url + route, data=payload,
                          headers={"Content-Type": "application/json"} if payload else {})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read()
                # /queue and /interrupt return an empty HTTP 200 response.
                if not body.strip():
                    return {}
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise BridgeError("本机服务未返回有效的 ComfyUI JSON 响应。") from exc
                if not isinstance(result, dict):
                    raise BridgeError("本机服务未返回有效的 ComfyUI JSON 对象。")
                return result
        except HTTPError as exc:
            detail = exc.read(32768).decode("utf-8", errors="replace")
            raise ComfyHTTPError(exc.code, detail) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BridgeError(f"无法连接 ComfyUI ({self.url}): {exc}。请调用 video_start，或先启动配置的 ComfyUI 服务。") from exc

    def _check_identity(self, stats):
        argv = stats.get("system", {}).get("argv", [])
        expected = os.path.normcase(str(self.comfy_root / "main.py"))
        candidate = None
        if isinstance(argv, (list, tuple)) and argv and isinstance(argv[0], str) and argv[0]:
            supplied = Path(argv[0])
            if supplied.is_absolute():
                candidate = supplied.resolve()
            elif (not supplied.drive and not supplied.root and ".." not in supplied.parts
                  and os.path.normcase(supplied.name) == os.path.normcase("main.py")):
                if len(supplied.parts) == 1:
                    candidate = (self.comfy_root / supplied).resolve()
                elif (len(supplied.parts) == 2
                      and os.path.normcase(supplied.parts[0]) == os.path.normcase(self.comfy_root.name)):
                    candidate = (self.comfy_root.parent / supplied).resolve()
                # /system_stats exposes sys.argv but no process cwd. Standard
                # relative launch forms cannot prove the backend's directory;
                # health() additionally validates its API, nodes, and models.
        if candidate is None or os.path.normcase(str(candidate)) != expected:
            raise BridgeError("8188 端口运行的 ComfyUI 与 config.json 的 comfy_root 不一致；未提交任务。")

    def health(self):
        try:
            stats = self._request("/system_stats", timeout=5)
            self._check_identity(stats)
            objects = self._request("/object_info")
            queue = self._request("/queue")
        except BridgeError as exc:
            return {"ready": False, "comfy_url": self.url, "error": str(exc)}
        model_files = {key: (self.comfy_root / "models" / folder / self.models[key]).is_file()
                       for key, folder in MODEL_FOLDERS.items()}
        missing_nodes = [key for key in REQUIRED_NODES if key not in objects]
        # Files alone are insufficient: the live server must list the configured weights.
        selections = {"unet": ("UNETLoader", "unet_name"), "clip": ("CLIPLoader", "clip_name"),
                      "video_vae": ("VAELoader", "vae_name"), "audio_vae": ("VAELoader", "vae_name"),
                      "lora": ("LoraLoaderModelOnly", "lora_name")}
        missing_selections = []
        for key, (node, field) in selections.items():
            values = objects.get(node, {}).get("input", {}).get("required", {}).get(field, [[]])[0]
            if self.models[key] not in values:
                missing_selections.append(key)
        return {"ready": all(model_files.values()) and not missing_nodes and not missing_selections,
                "comfy_url": self.url, "comfyui_version": stats["system"].get("comfyui_version"),
                "devices": [{"name": d.get("name"), "vram_total_gb": round(d.get("vram_total", 0) / 2**30, 2)}
                            for d in stats.get("devices", [])],
                "models": model_files, "missing_nodes": missing_nodes,
                "missing_model_selections": missing_selections,
                "running": len(queue.get("queue_running", [])), "queued": len(queue.get("queue_pending", [])),
                "output_dir": str(self.output_dir),
                "defaults": {"width": 864, "height": 480, "seconds": 5, "fps": 24, "steps": 8}}

    def start(self):
        try:
            stats = self._request("/system_stats", timeout=3)
        except BridgeError:
            # The launcher checks for any existing listener before spawning;
            # an occupied or unrecognized local service must never be replaced.
            from start_comfy import start_backend

            return start_backend(self.config, self.state_dir)
        self._check_identity(stats)
        result = self.health()
        return {"state": "ready" if result["ready"] else "not_ready", "already_running": True, **result}

    @staticmethod
    def validate(prompt, seconds, width, height, seed, output_name, include_audio):
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
            raise BridgeError("prompt 必须是 1–12000 字符的非空文本。")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 1 <= seconds <= 15:
            raise BridgeError("seconds 必须在 1–15 之间；H3 会将帧数对齐为 17n+5。")
        for name, value in (("width", width), ("height", height)):
            if isinstance(value, bool) or not isinstance(value, int) or not 32 <= value <= 1920 or value % 32:
                raise BridgeError(f"{name} 必须是 32–1920 之间的 32 的倍数。")
        if width * height > 864 * 480:
            raise BridgeError("当前工作流限制总像素不超过 864×480；竖屏可用 480×864。")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**64):
            raise BridgeError("seed 必须是 0..2^64-1 的整数。")
        if output_name is not None and (not isinstance(output_name, str) or not output_name.strip() or len(output_name) > 80
                                        or re.search(r'[<>:"/\\|?*\x00-\x1f]', output_name) or output_name.endswith((".", " "))):
            raise BridgeError("output_name 必须是最多 80 字符的文件名标签，不能含目录或 Windows 保留字符。")
        if not isinstance(include_audio, bool):
            raise BridgeError("include_audio 必须是布尔值。")

    def build_workflow(self, prompt, width, height, frames, seed, prefix, include_audio=True):
        def node(kind, **inputs):
            return {"class_type": kind, "inputs": inputs}
        m = self.models
        workflow = {
            "1": node("UNETLoader", unet_name=m["unet"], weight_dtype="default"),
            "2": node("LoraLoaderModelOnly", model=["1", 0], lora_name=m["lora"], strength_model=1.0),
            "3": node("CLIPLoader", clip_name=m["clip"], type="minimax", device="default"),
            "4": node("VAELoader", vae_name=m["video_vae"]),
            "5": node("MiniMaxH3ImageToVideo", clip=["3", 0], vae=["4", 0], prompt=prompt,
                      width=width, height=height, length=frames),
            "6": node("RandomNoise", noise_seed=seed),
            "7": node("KSamplerSelect", sampler_name="res_multistep"),
            "8": node("BasicGuider", model=["2", 0], conditioning=["5", 0]),
            "9": node("BasicScheduler", model=["2", 0], scheduler="simple", steps=8, denoise=1.0),
            "10": node("SamplerCustomAdvanced", noise=["6", 0], guider=["8", 0], sampler=["7", 0],
                       sigmas=["9", 0], latent_image=["5", 1]),
            "11": node("VAEDecode", samples=["10", 0], vae=["4", 0]),
            "14": node("CreateVideo", images=["11", 0], fps=24.0, bit_depth=8),
            "15": node("SaveVideo", video=["14", 0], filename_prefix=prefix, format="mp4", **{"format.codec": "auto"}),
        }
        if include_audio:
            workflow["12"] = node("VAELoader", vae_name=m["audio_vae"])
            workflow["13"] = node("VAEDecodeAudio", samples=["10", 0], vae=["12", 0])
            workflow["14"]["inputs"]["audio"] = ["13", 0]
        return workflow

    def _save(self, job):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / f'{job["job_id"]}.json'
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def _load(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise BridgeError("job_id 必须是 video_generate 返回的 32 位任务编号。")
        try:
            return json.loads((self.state_dir / f"{job_id}.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BridgeError(f"本插件没有任务 {job_id}；不会查询或中断其他人的任务。") from exc

    def generate(self, prompt, seconds=5, width=864, height=480, seed=None, output_name=None, include_audio=True):
        self.validate(prompt, seconds, width, height, seed, output_name, include_audio)
        check = self.health()
        if not check["ready"]:
            raise BridgeError("ComfyUI/H3 尚未就绪: " + json.dumps(check, ensure_ascii=False))
        seed = secrets.randbits(64) if seed is None else seed
        frames = max(5, round(seconds * 24))
        frames += (5 - frames % 17) % 17
        job_id = uuid.uuid4().hex
        label = output_name or "minimax-h3"
        job = {"job_id": job_id, "state": "submitting", "created_at": now(), "prompt_id": None,
               "prompt": prompt.strip(), "seed": seed, "width": width, "height": height,
               "requested_seconds": seconds, "actual_seconds": frames / 24, "frames": frames,
               "fps": 24, "steps": 8, "include_audio": include_audio, "output_name": label}
        workflow = self.build_workflow(job["prompt"], width, height, frames, seed,
                                       f"video/Codex_H3/{job_id}", include_audio)
        job["workflow"] = workflow
        self._save(job)
        try:
            receipt = self._request("/prompt", {"prompt": workflow, "client_id": job_id,
                                                "extra_data": {"minimax_video_job_id": job_id}}, timeout=30)
            if not isinstance(receipt.get("prompt_id"), str) or not receipt["prompt_id"]:
                raise BridgeError("ComfyUI 未接受完整工作流: " + json.dumps(receipt, ensure_ascii=False))
        except BridgeError as exc:
            # A definitive validation/auth rejection cannot have queued this job.
            rejected = isinstance(exc, ComfyHTTPError) and exc.status_code in (400, 401, 403, 404, 405, 413, 415, 422)
            job["state"] = "failed" if rejected else "submission_unknown"
            job["error"] = str(exc)
            self._save(job)
            if rejected:
                raise BridgeError(f"任务 {job_id} 被 ComfyUI 拒绝，未进入队列。{exc}") from exc
            raise BridgeError(f"任务 {job_id} 提交未确认。请先 status 查询，避免重复提交。{exc}") from exc
        job.update(prompt_id=receipt["prompt_id"], state="queued")
        if receipt.get("node_errors"):
            job["node_warnings"] = receipt["node_errors"]
        self._save(job)
        return self._public(job)

    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if k not in ("workflow",)}

    def _download(self, job, history):
        files = []
        # SaveVideo is our sole output node. Never follow paths/URLs supplied in arbitrary nodes.
        result = history.get("outputs", {}).get("15", {})
        for entries in result.values():
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict) or item.get("type", "output") != "output":
                    continue
                filename = item.get("filename", "")
                if not isinstance(filename, str) or not filename.lower().endswith(".mp4"):
                    continue
                subfolder = item.get("subfolder", "")
                if (not isinstance(subfolder, str) or Path(filename).name != filename or "\\" in filename or ":" in filename
                        or any(p in ("..", ".") for p in subfolder.replace("\\", "/").split("/"))
                        or subfolder.startswith(("/", "\\")) or ":" in subfolder):
                    raise BridgeError("ComfyUI 返回了不安全的输出文件路径。")
                self.output_dir.mkdir(parents=True, exist_ok=True)
                target = self.output_dir / f'{job["output_name"]}-{job["job_id"][:12]}-{len(files) + 1}.mp4'
                if not target.exists() or not target.stat().st_size:
                    route = "/view?" + urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
                    temporary = target.with_suffix(f".{uuid.uuid4().hex}.part")
                    try:
                        with self.opener.open(self.url + route, timeout=60) as source, temporary.open("wb") as dest:
                            while chunk := source.read(1024 * 1024):
                                dest.write(chunk)
                        if not temporary.stat().st_size:
                            raise BridgeError("ComfyUI 返回的视频文件为空。")
                        temporary.replace(target)
                    except (URLError, OSError, TimeoutError, BridgeError) as exc:
                        temporary.unlink(missing_ok=True)
                        raise BridgeError(f"视频已生成，但下载失败；可重新 status: {exc}") from exc
                files.append({"path": str(target), "bytes": target.stat().st_size,
                              "comfy_filename": filename, "comfy_subfolder": subfolder})
        if not files:
            raise BridgeError("ComfyUI 任务已结束，但 SaveVideo 没有返回 MP4；请检查工作流历史。")
        return files

    def status(self, job_id):
        job = self._load(job_id)
        if job["state"] in ("failed", "cancelled"):
            return self._public(job)
        if job["state"] == "completed" and job.get("files") and all(Path(f["path"]).is_file() and Path(f["path"]).stat().st_size for f in job["files"]):
            return self._public(job)
        queue = self._request("/queue")
        if not job.get("prompt_id"):
            # Recover a lost POST response without ever automatically submitting a second render.
            histories = self._request("/history?max_items=100")
            candidates = queue.get("queue_running", []) + queue.get("queue_pending", [])
            candidates += [item["prompt"] for item in histories.values() if isinstance(item, dict) and isinstance(item.get("prompt"), list)]
            for row in candidates:
                if len(row) >= 4 and isinstance(row[3], dict) and row[3].get("minimax_video_job_id") == job_id:
                    job["prompt_id"] = row[1]
                    job.pop("error", None)
                    break
            if not job.get("prompt_id"):
                return self._public(job)
        prompt_id = job["prompt_id"]
        history = self._request("/history/" + quote(prompt_id, safe="")).get(prompt_id)
        if history:
            status = history.get("status") or {}
            messages = status.get("messages") or []
            failures = [(entry[0], entry[1]) for entry in messages
                        if isinstance(entry, (list, tuple)) and len(entry) == 2
                        and entry[0] in ("execution_error", "execution_interrupted") and isinstance(entry[1], dict)]
            if failures or status.get("status_str") == "error":
                kind, detail = failures[-1] if failures else ("execution_error", {})
                job["state"] = "cancelled" if kind == "execution_interrupted" else "failed"
                job["error"] = {k: detail.get(k) for k in ("node_id", "node_type", "exception_type", "exception_message") if detail.get(k)}
            elif status.get("completed"):
                job["files"] = self._download(job, history)
                job["state"] = "completed"
                job.setdefault("finished_at", now())
            else:
                job["state"] = "running"
        else:
            running = [row[1] for row in queue.get("queue_running", [])]
            pending = [row[1] for row in queue.get("queue_pending", [])]
            if prompt_id in running:
                job["state"] = "cancelling" if job.get("cancel_requested") else "running"
                job.pop("queue_position", None)
            elif prompt_id in pending:
                job.update(state="queued", queue_position=pending.index(prompt_id) + 1)
            elif job.get("cancel_requested"):
                job["state"] = "cancelled"
            else:
                job["state"] = "unknown"
                job["note"] = "ComfyUI 队列与历史均没有此任务，服务可能已重启。不会自动重复生成。"
        if job["state"] != "unknown":
            job.pop("note", None)
        job["updated_at"] = now()
        self._save(job)
        return self._public(job)

    def wait(self, job_id, timeout_seconds=30):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 55:
            raise BridgeError("单次 wait 最多 55 秒；超时不会取消任务。")
        deadline = time.monotonic() + timeout_seconds
        while True:
            result = self.status(job_id)
            if result["state"] not in ("queued", "running", "cancelling") or time.monotonic() >= deadline:
                return result
            time.sleep(min(2, max(0, deadline - time.monotonic())))

    def cancel(self, job_id):
        job = self._load(job_id)
        if job["state"] in ("failed", "cancelled"):
            return self._public(job)
        if not job.get("prompt_id"):
            self.status(job_id)
            job = self._load(job_id)
        if not job.get("prompt_id"):
            raise BridgeError("任务提交状态不明，未中断任何任务。请先核对 ComfyUI 队列。")
        queue = self._request("/queue")
        prompt_id = job["prompt_id"]
        if prompt_id not in [row[1] for row in queue.get("queue_pending", []) + queue.get("queue_running", [])]:
            return self.status(job_id)
        # Supported ComfyUI versions implement an atomic ID-specific cancel route.
        # /interrupt with prompt_id checks a snapshot before a global interrupt,
        # which could otherwise hit the next job if this one finishes in between.
        try:
            receipt = self._request("/api/jobs/" + quote(prompt_id, safe="") + "/cancel", {})
        except ComfyHTTPError as exc:
            if exc.status_code in (404, 405):
                raise BridgeError("此 ComfyUI 版本不支持安全按任务取消，请在 ComfyUI 界面取消。") from exc
            raise
        if not receipt.get("cancelled"):
            return self.status(job_id)
        job["cancel_requested"] = True
        job["state"] = "cancelling"
        self._save(job)
        return self.status(job_id)
