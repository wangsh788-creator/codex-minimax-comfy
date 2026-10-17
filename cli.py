"""Local command-line entry point, also useful before Codex reloads its tools."""
import argparse
import json
import sys
from pathlib import Path

# The portable ComfyUI Python deliberately excludes the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge import VideoBridge


def main():
    parser = argparse.ArgumentParser(description="Codex MiniMax-H3 / ComfyUI bridge")
    parser.add_argument("--config", help="Path to config.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    commands.add_parser("start")
    generate = commands.add_parser("generate")
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--seconds", type=float, default=5)
    generate.add_argument("--width", type=int, default=864)
    generate.add_argument("--height", type=int, default=480)
    generate.add_argument("--seed", type=int)
    generate.add_argument("--output-name")
    generate.add_argument("--no-audio", action="store_true")
    for command in ("status", "wait", "cancel"):
        sub = commands.add_parser(command)
        sub.add_argument("job_id")
        if command == "wait":
            sub.add_argument("--timeout-seconds", type=float, default=30)
    args = vars(parser.parse_args())
    command = args.pop("command")
    config = args.pop("config")
    if command == "generate":
        args["include_audio"] = not args.pop("no_audio")
    try:
        result = getattr(VideoBridge(config), command)(**args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if command == "health" and not result["ready"] else 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
