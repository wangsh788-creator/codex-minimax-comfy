---
name: minimax-video
description: Generate local text-to-video with sound using the installed MiniMax H3 and ComfyUI. Use when the user asks Codex to create a video from a text description, especially with MiniMaxH3 or ComfyUI.
---

Use the `minimax_comfy` MCP tools supplied by this plugin to produce an MP4 on this computer. These tools use the existing model files and do not require cloud generation credentials.

1. Call `video_health`. If ComfyUI is offline, call `video_start` once. It starts the existing backend in Stable low-VRAM mode without opening a browser. Check health again while it starts; a live launch PID prevents a second launch. Report missing files, an unrelated listener, or startup errors instead of installing models or terminating another service.
2. Call `video_generate` with the user's prompt. When unspecified, use 5 seconds, 864×480, 24 fps, 8 turbo steps, and `include_audio: true`. Preserve the requested subjects, actions, style, and sound. For portrait video use 480×864. The current workflow limits total pixels to 864×480; both dimensions must be multiples of 32. `seconds` accepts 1–15; 5–15 is the normal model range, shorter clips are useful for previews.
3. Save the returned `job_id`. Call `video_wait` with at most 55 seconds per call, giving concise progress updates while generation continues. A wait timeout leaves the task running. Poll until `state` is `completed`, `failed`, or `cancelled`.
4. On completion, use the absolute `files[].path` to show the MP4 inline or provide a clickable file link. State the actual duration, which can differ from the requested duration because H3 requires frame counts of `17n+5`. Five requested seconds becomes 124 frames, about 5.167 seconds.

If submission is not confirmed, `video_generate` returns an MCP error containing the 32-character job ID and the phrase “提交未确认”. Extract that ID and call `video_status`; its saved state is `submission_unknown`. Never automatically resubmit. If status is `unknown`, explain that the backend may have restarted and check the queue before proposing another generation. On `failed`, report the node error and stop repeating the same failing request. Use `video_cancel` only for the user's requested job; it cannot cancel arbitrary ComfyUI tasks.

If the MCP tools have not loaded in the current chat, read [the plugin README](../../README.md) for the local CLI fallback. The generated `.mcp.json` beside that README records the actual script, interpreter, and configuration paths for this installation. Use those paths rather than assuming the current working directory is the plugin directory. Do not assume a Python from PATH has ComfyUI's model libraries; the bridge itself only needs the standard library.
