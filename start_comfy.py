"""Start the installed Windows ComfyUI backend without opening a console or browser."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
# Windows Portable's isolated Python does not add the script directory to sys.path.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge import BridgeError, load_config


GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def _port_is_open():
    try:
        with socket.create_connection(("127.0.0.1", 8188), timeout=0.5):
            return True
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        # Failure to inspect the port is not evidence that it is safe to start.
        raise BridgeError(f"无法检查本机 8188 端口：{exc}") from exc


def _process_is_running(pid):
    """Query a recorded process; never send signals or terminate anything."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: process no longer exists.
            return False
        raise BridgeError(f"无法确认先前启动进程 {pid} 的状态（Windows 错误 {error}）；未重复启动。")
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise BridgeError(f"无法读取先前启动进程 {pid} 的状态；未重复启动。")
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@contextmanager
def _startup_lock(state_dir):
    """A short OS lock is automatically released if the launcher crashes."""
    import msvcrt

    lock_path = state_dir / "comfyui-start.lock"
    # Keep the one-byte file in place so all callers lock the same file object.
    # Deleting it after unlocking would race another caller acquiring its lock.
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BridgeError("另一个请求正在启动 ComfyUI；请稍后检查健康状态。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _check_previous_launch(record_path):
    if not record_path.exists():
        return
    try:
        previous = json.loads(record_path.read_text(encoding="utf-8"))
        pid = previous["pid"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BridgeError(f"启动记录无法读取：{record_path}；请先核对进程和启动日志。") from exc
    if _process_is_running(pid):
        raise BridgeError(f"ComfyUI 已由插件启动（PID {pid}），仍在初始化；请检查健康状态或 {previous.get('log_path', '启动日志')}。")
    # A dead startup process must not leave a permanent launch block.
    record_path.unlink()


def start_backend(config, state_dir):
    """Launch once and return immediately; callers check readiness separately."""
    if os.name != "nt":
        raise BridgeError("此启动器使用已安装的 Windows ComfyUI Portable，仅支持 Windows。")
    try:
        endpoint = urlsplit(config.get("comfy_url", "http://127.0.0.1:8188"))
        valid_endpoint = (endpoint.scheme == "http" and endpoint.hostname in ("127.0.0.1", "localhost")
                          and endpoint.port == 8188 and not endpoint.path and not endpoint.query
                          and not endpoint.fragment and not endpoint.username and not endpoint.password)
    except ValueError as exc:
        raise BridgeError("comfy_url 不是有效的本机地址。") from exc
    if not valid_endpoint:
        raise BridgeError("自动启动仅支持 http://127.0.0.1:8188 或 http://localhost:8188。")

    comfy_root = Path(config["comfy_root"]).expanduser().resolve()
    python = Path(config["python"]).expanduser().resolve()
    portable = comfy_root.parent
    main = comfy_root / "main.py"
    gpu_file = portable.parent / "selected-gpu.txt"
    if not python.is_file() or not main.is_file():
        raise BridgeError("config.json 指向的 ComfyUI 或嵌入式 Python 不存在；未启动。")
    gpu = config.get("gpu_uuid")
    if gpu is not None and not isinstance(gpu, str):
        raise BridgeError("config.json 的 gpu_uuid 必须是有效的 NVIDIA GPU UUID 或空字符串。")
    gpu = gpu.strip() if gpu else ""
    if gpu:
        if not GPU_UUID.fullmatch(gpu):
            raise BridgeError("config.json 的 gpu_uuid 没有有效的 NVIDIA GPU UUID。")
    else:
        try:
            gpu = gpu_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            # Standard Portable installs have no bundle-specific GPU selection file.
            gpu = None
        except OSError as exc:
            raise BridgeError(f"无法读取已安装环境的显卡选择：{gpu_file}") from exc
        if gpu is not None and not GPU_UUID.fullmatch(gpu):
            raise BridgeError(f"显卡选择文件没有有效的 NVIDIA GPU UUID：{gpu_file}")

    state_dir = Path(state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "comfyui-launch.log"
    record_path = state_dir / "comfyui-launch.json"
    with _startup_lock(state_dir):
        if _port_is_open():
            raise BridgeError("127.0.0.1:8188 已有服务监听；未启动第二个进程，也未终止现有服务。")
        # The backend may take several seconds to bind its port. A PID record
        # protects that gap after the short OS lock has been released.
        _check_previous_launch(record_path)
        arguments = [str(python), "-s", str(main), "--windows-standalone-build", "--disable-auto-launch",
                     "--user-directory", str(comfy_root / "user"),
                     "--preview-method", "none", "--lowvram", "--reserve-vram", "1.5",
                     "--disable-pinned-memory"]
        environment = os.environ.copy()
        if gpu:
            arguments.extend(["--cuda-device", gpu])
            environment["CUDA_VISIBLE_DEVICES"] = gpu
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
        try:
            with log_path.open("ab", buffering=0) as log:
                log.write(f"\n--- Codex ComfyUI launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
                process = subprocess.Popen(arguments, shell=False, cwd=str(portable), env=environment,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=flags, close_fds=True)
            result = {"state": "starting", "pid": process.pid, "log_path": str(log_path)}
            record = dict(result, started_at=time.time(), main=str(main))
            temporary = record_path.with_suffix(f".{os.getpid()}.tmp")
            try:
                temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(record_path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise BridgeError(f"启动 ComfyUI 失败：{exc}。日志：{log_path}") from exc
        return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="启动本机已安装的 MiniMax H3 ComfyUI 后端")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()
    try:
        config_path = args.config.resolve()
        config = load_config(config_path)
        state_dir = Path(config.get("state_dir", ".state")).expanduser()
        if not state_dir.is_absolute():
            state_dir = config_path.parent / state_dir
        print(json.dumps(start_backend(config, state_dir), ensure_ascii=False, indent=2))
        return 0
    except (BridgeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
