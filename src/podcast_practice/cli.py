"""uv run podcast-practice setup / build / serve / doctor / render."""

from __future__ import annotations

import argparse
import functools
import json
import sys
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from filelock import Timeout

from . import __version__
from .errors import PracticeError
from .pipeline import BuildOptions, build
from .render import render_player, update_library
from .runtime import MODELS, Runtime


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="本機 MFA podcast 英文聽力練習工具")
    command.add_argument("--version", action="version", version=__version__)
    command.add_argument(
        "--runtime-dir",
        type=Path,
        default=Path(".runtime"),
        help="工具、模型及快取目錄（預設 .runtime）",
    )
    sub = command.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup", help="首次安裝本地 MFA、ffmpeg 與模型")
    setup.add_argument("--mfa-prefix", type=Path, help="重用既有 MFA Conda 環境")
    setup.add_argument("--model-dir", type=Path, help="重用既有 MFA_ROOT_DIR")
    setup.add_argument("--asr-cache", type=Path, help="重用既有 faster-whisper 模型快取")
    setup.add_argument("--asr-model", default="base.en", help="粗定位模型（預設 base.en）")
    setup.add_argument("--skip-asr", action="store_true", help="只安裝 MFA；build 需使用 --no-asr")
    create = sub.add_parser("build", help="指定不同音檔和原稿，產生一集離線播放器")
    create.add_argument("audio", type=Path)
    create.add_argument("transcript", type=Path)
    create.add_argument("--title", help="播放器標題；預設音檔檔名")
    create.add_argument("--output", type=Path, help="指定單集輸出目錄")
    create.add_argument("--library", type=Path, default=Path("library"), help="練習庫目錄")
    create.add_argument("--speaker", action="append", default=[], help="明確指定講者名稱（可重複）")
    create.add_argument("--asr-model", help="粗定位模型；預設沿用 setup 的設定")
    create.add_argument("--no-asr", action="store_true", help="只跑 MFA，略過粗定位辨識檢查")
    create.add_argument("--offline", action="store_true", help="禁止粗定位模型連線下載")
    create.add_argument("--force", action="store_true", help="覆寫輸出；保留相同輸入的計算快取")
    create.add_argument("--windows", type=Path, help="手動修正的句子搜尋範圍 JSON")
    create.add_argument(
        "--allow-low-confidence", action="store_true", help="允許低吻合率繼續，需人工核對"
    )
    create.add_argument("--open", action="store_true", help="完成後用預設瀏覽器開啟")
    serve = sub.add_parser("serve", help="開啟本機練習庫，瀏覽每天的內容")
    serve.add_argument("--library", type=Path, default=Path("library"))
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--open", action="store_true")
    sub.add_parser("doctor", help="檢查本地環境與模型")
    render = sub.add_parser("render", help="使用既有 alignment.json 重建播放器，不重新對齊")
    render.add_argument("episode", type=Path, help="含 alignment.json 和 audio.mp3 的單集目錄")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            root = args.library.expanduser().resolve()
            update_library(root)
            handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
            with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
                url = f"http://127.0.0.1:{server.server_port}/"
                print(f"練習庫：{url}\n按 Ctrl+C 結束。", flush=True)
                if args.open:
                    webbrowser.open(url)
                server.serve_forever()
        elif args.command == "render":
            folder = args.episode.expanduser().resolve()
            data = json.loads((folder / "alignment.json").read_text(encoding="utf-8"))
            render_player(data, folder / "audio.mp3", folder / "player.html")
            print(f"播放器已更新：{folder / 'player.html'}")
        else:
            runtime = Runtime(args.runtime_dir)
            with runtime.lock:
                if args.command == "setup":
                    runtime.install(
                        mfa_prefix=args.mfa_prefix,
                        model_dir=args.model_dir,
                        asr_cache=args.asr_cache,
                        asr_model=args.asr_model,
                        skip_asr=args.skip_asr,
                    )
                elif args.command == "doctor":
                    print(f"Python：{sys.version.split()[0]}\nRuntime：{runtime.root}")
                    print(f"MFA：{runtime.prefix}\nASR cache：{runtime.asr_cache}")
                    for kind, model in MODELS.items():
                        status = "OK" if runtime.model_path(kind).exists() else "MISSING"
                        print(f"{kind} {model}: {status}")
                    runtime.require_ready()
                    runtime.run([runtime.mfa, "version"])
                    print("MFA 與 ffmpeg 就緒。")
                elif args.command == "build":
                    destination = build(
                        runtime,
                        BuildOptions(
                            audio=args.audio,
                            transcript=args.transcript,
                            title=args.title,
                            output=args.output,
                            library=args.library,
                            speakers=tuple(args.speaker),
                            asr_model=args.asr_model,
                            no_asr=args.no_asr,
                            offline=args.offline,
                            force=args.force,
                            windows=args.windows,
                            allow_low_confidence=args.allow_low_confidence,
                        ),
                    )
                    if args.open:
                        webbrowser.open((destination / "player.html").as_uri())
        return 0
    except Timeout:
        print("另一個 setup/build 正在使用本地環境。請等它完成後再執行。", file=sys.stderr)
        return 2
    except (PracticeError, OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已停止；處理快取保留，之後可用同一指令繼續。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
