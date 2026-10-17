# Codex MiniMax-H3 / ComfyUI 本地文生视频插件

让 Codex 通过本机 ComfyUI 调用 MiniMax-H3，生成带音频的 MP4。插件提供 MCP 工具和 `$minimax-video` 技能，支持启动后端、提交任务、查询进度、取消和保存视频。桥接代码仅使用 Python 标准库，不需要云端生成密钥。

这是可从 GitHub 克隆并安装的本地插件。公开仓库不会自动进入 Codex 官方插件商店；每位用户仍需安装插件，并准备自己的 ComfyUI、模型和兼容 GPU。

## 使用前准备

- 已安装 Codex，并能在终端运行 `codex plugin --help`。
- 已有能运行 MiniMax-H3 的 ComfyUI，包含 H3 节点、音视频编码节点及其 Python 依赖。
- 已下载下表中的五个权重，显卡和显存能够运行该工作流。降低输出分辨率并不能保证任意显卡都能运行这些模型。

| 配置项 | 默认文件名 | ComfyUI 中的位置 |
| --- | --- | --- |
| `unet` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | `models/diffusion_models/` |
| `clip` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | `models/text_encoders/` |
| `video_vae` | `minimax_h3_video_vae_fp16.safetensors` | `models/vae/` |
| `audio_vae` | `minimax_h3_audio_vae_fp32.safetensors` | `models/vae/` |
| `lora` | `minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` | `models/loras/` |

插件不会下载 ComfyUI、节点或模型。`video_health` 会检查本机文件、后端连接和工作流节点。

可使用 [TianyiDataScience/MiniMax-H3-OneClick](https://github.com/TianyiDataScience/MiniMax-H3-OneClick) 部署模型环境，也可连接已有的兼容 ComfyUI。本项目提供独立的 Codex 桥接工具；上游部署代码、节点与模型分别遵守各自许可。

## Windows 安装

在 PowerShell 中克隆仓库，并将 ComfyUI 路径改为自己电脑上的实际位置：

```powershell
git clone https://github.com/wangsh788-creator/codex-minimax-comfy.git
cd codex-minimax-comfy
.\install.ps1 -ComfyRoot 'D:\ComfyUI_windows_portable\ComfyUI'
```

`-ComfyRoot` 必须指向包含 `main.py` 的 ComfyUI 目录。对于 Windows Portable，安装脚本会查找同级 `python_embeded/python.exe`；其他安装方式请显式提供运行 ComfyUI 的 Python：

```powershell
.\install.ps1 -ComfyRoot 'D:\AI\ComfyUI' -Python 'D:\AI\ComfyUI\.venv\Scripts\python.exe'
```

脚本生成本机 `config.json` 和 `.mcp.json`，并登记 `local-minimax-video` 本地 marketplace 和 `minimax-comfy` 插件。配置包含本机路径，已排除在 Git 提交之外。`-ConfigureOnly` 只生成本地配置，适合检查配置和运行测试，不会登记插件。

安装完成后新建 Codex 对话；若插件未刷新，重启 Codex。请保留项目目录，移动目录后重新运行安装脚本。更新代码可运行 `git pull`，然后重新运行 `./install.ps1` 刷新插件缓存；模型文件名、服务地址等设置可在本机 `config.json` 中调整，公开模板见 [config.example.json](config.example.json)。

## 在 Codex 中生成视频

```text
使用 $minimax-video，生成约5秒视频：阳光照进窗边，
一只橘猫缓缓转头看向镜头，写实风格，保留环境音。
```

默认 864×480、24 fps、8 步、带音频；竖屏可用 480×864。可在请求中指定是否保留音轨和随机种子。当前桥接代码固定使用 24 fps、8 步，宽高须为 32 的倍数，总像素不得超过 864×480。

请求时长为 1–15 秒，常规建议 5–15 秒。H3 会将帧数对齐到 `17n+5`，因此实际时长可能不同，例如请求 5 秒会得到约 5.167 秒。

| MCP 工具 | 用途 |
| --- | --- |
| `video_health` | 检查连接、节点和五个模型 |
| `video_start` | 后台启动已有 ComfyUI，已运行则复用 |
| `video_generate` | 提交生成任务，返回 `job_id` |
| `video_status` | 查询任务状态，取回已完成的 MP4 |
| `video_wait` | 单次最多等待 55 秒，超时后任务继续运行 |
| `video_cancel` | 按编号取消本插件创建的任务 |

自动启动使用现有 ComfyUI 的低显存模式；没有 `selected-gpu` 文件时使用默认 GPU。取消需要后端支持按任务编号取消；不支持时会明确报错。提交未确认时应查询原 `job_id`，避免重复生成。

成品默认保存在插件目录 `outputs/`，ComfyUI 原件位于其 `output/video/Codex_H3/`。任务记录、工作流和启动日志保存在 `.state/`，不要删除运行中任务的记录。默认服务地址为 `http://127.0.0.1:8188`。

## 命令行与兼容性

也可以使用安装时指定的 Python 直接调用 CLI：

```powershell
$videoPython = 'D:\ComfyUI_windows_portable\python_embeded\python.exe'
& $videoPython .\cli.py health
& $videoPython .\cli.py start
& $videoPython .\cli.py generate --prompt 'An orange kitten turning toward the camera in warm sunlight' --seconds 5
& $videoPython .\cli.py status <job_id>
& $videoPython .\cli.py wait <job_id> --timeout-seconds 30
& $videoPython .\cli.py cancel <job_id>
```

目前安装、自动启动及真实生成链路已在 Windows 和 ComfyUI 0.34.0 上验证。HTTP/MCP 桥接使用 Python 标准库，其他系统可参考 `config.example.json` 配置、手动启动 ComfyUI 后调用 CLI；跨系统安装和自动启动尚未验证。

已完成一次 448×256、39 帧、1.625 秒、8 步的真实生成，输出包含 H.264 视频和 32 kHz AAC 音频。本机该次生成约 72 秒，默认 480P 长片尚未做压力测试，不能据此推算其他机器的耗时。

运行协议及行为回归测试：

```powershell
python -m unittest discover -s tests -v
```

## 许可与贡献

插件源码采用 [MIT 许可](LICENSE)，允许使用、修改和再分发。ComfyUI、节点和模型仍分别遵守各自上游许可。

欢迎通过 [Issues](https://github.com/wangsh788-creator/codex-minimax-comfy/issues) 报告问题或提交 Pull Request。提交内容仅应包含插件源码、技能、模板、测试和文档；本机配置、模型权重、任务记录、日志与生成视频已通过 `.gitignore` 排除。
