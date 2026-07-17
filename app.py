#!/usr/bin/env python3
"""
SRT Listener Analyzer — Flask Web アプリ

起動方法:
  pip install flask
  python app.py
  → ブラウザで http://127.0.0.1:8765 にアクセス

スタンドアロン .exe ビルド (PyInstaller):
  pip install pyinstaller
  # Windows
  pyinstaller --onefile --windowed --add-data "templates;templates" --name srt_analyzer_web app.py
  # macOS / Linux
  pyinstaller --onefile --add-data "templates:templates" --name srt_analyzer_web app.py

環境変数:
  SRT_ANALYZER_PORT  — リッスンポート（デフォルト: 8765）
  SRT_ANALYZER_HOST  — リッスンホスト（デフォルト: 127.0.0.1）
"""

import json
import os
import subprocess
import sys
import threading
import webbrowser
from typing import Generator

from flask import Flask, Response, render_template, request, stream_with_context

import srt_probe

# --------------------------------------------------------------------------
# PyInstaller フリーズ時のテンプレートパス解決
# --------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _BASE_DIR = sys._MEIPASS  # type: ignore[attr-defined]
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(_BASE_DIR, "templates"))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _sse(data: dict) -> str:
    """Python dict を Server-Sent Events の data 行に変換する。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.route("/api/check_ffprobe")
def check_ffprobe():
    """ffprobe の有無とバージョンを返す。"""
    try:
        result = subprocess.run(
            [srt_probe._ffprobe_path(), "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            first_line = (result.stdout.splitlines() or [""])[0]
            return {"available": True, "version": first_line}
        return {"available": False, "version": ""}
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "version": ""}


@app.route("/api/analyze")
def analyze():
    """
    SRT ストリームを解析し、Server-Sent Events でリアルタイムに進行状況を返す。

    Query params:
      ip          : SRT Listener の IP アドレス（必須）
      port        : ポート番号（デフォルト: 9998）
      passphrase  : パスフレーズ（省略可）
      auto_scan   : "true" で自動スキャン（デフォルト: true）
      latency_ms  : 固定 Latency (ms)（auto_scan=false 時に使用）
      timeout     : ffprobe タイムアウト秒数（デフォルト: 8）
    """
    ip = request.args.get("ip", "").strip()
    if not ip:
        return {"error": "ip is required"}, 400

    try:
        port = int(request.args.get("port", 9998))
        latency_ms = int(request.args.get("latency_ms", 200))
        timeout = max(2, min(60, int(request.args.get("timeout", srt_probe.PROBE_TIMEOUT_SEC))))
    except ValueError:
        return {"error": "invalid numeric parameter"}, 400

    passphrase = request.args.get("passphrase", "")
    auto_scan = request.args.get("auto_scan", "true").lower() != "false"

    def generate() -> Generator[str, None, None]:
        latencies = srt_probe.LATENCY_SCAN_VALUES if auto_scan else [latency_ms]
        total = len(latencies)

        for i, lat in enumerate(latencies):
            # 進行状況を送信
            yield _sse({
                "type": "progress",
                "current": i + 1,
                "total": total,
                "latency": lat,
                "msg": f"[{i + 1}/{total}] Latency {lat} ms で接続を試行中…",
            })

            try:
                result = srt_probe.probe_stream(ip, port, passphrase, lat, timeout)
            except FileNotFoundError:
                yield _sse({
                    "type": "error",
                    "msg": (
                        "ffprobe が見つかりません。\n"
                        "ffmpeg をインストールして PATH に追加してください。\n"
                        "https://ffmpeg.org/download.html"
                    ),
                })
                return

            if result is not None:
                n_streams = len(result.get("streams", []))
                url = srt_probe.build_srt_url(ip, port, passphrase, lat)
                result["_latency_ms"] = lat
                result["_srt_url"] = url
                yield _sse({
                    "type": "result",
                    "data": result,
                    "msg": f"接続成功 — {n_streams} ストリーム検出 (Latency: {lat} ms)",
                })
                return

            yield _sse({
                "type": "progress_fail",
                "latency": lat,
                "msg": f"  → Latency {lat} ms: タイムアウトまたは接続拒否",
            })

        yield _sse({
            "type": "error",
            "msg": (
                "接続失敗: すべての Latency 値で接続できませんでした。\n"
                "IP・ポート・パスフレーズを確認し、SRT Listener が起動していることを確認してください。"
            ),
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx リバースプロキシ対策
        },
    )


@app.route("/api/bitrate")
def bitrate():
    """
    SRT ストリームを一定時間受信し、CBR / VBR を統計的に判定する（SSE）。

    Query params:
      ip          : SRT Listener の IP アドレス（必須）
      port        : ポート番号（デフォルト: 9998）
      passphrase  : パスフレーズ（省略可）
      latency_ms  : 接続に使用する Latency (ms)（接続成功時の値を渡す）
      window      : 測定時間（秒）。3〜60、デフォルト 10
    """
    ip = request.args.get("ip", "").strip()
    if not ip:
        return {"error": "ip is required"}, 400

    try:
        port = int(request.args.get("port", 9998))
        latency_ms = int(request.args.get("latency_ms", 200))
        window = max(3, min(60, int(request.args.get("window", srt_probe.BITRATE_WINDOW_SEC))))
    except ValueError:
        return {"error": "invalid numeric parameter"}, 400

    passphrase = request.args.get("passphrase", "")

    def generate() -> Generator[str, None, None]:
        yield _sse({
            "type": "progress",
            "elapsed": 0,
            "target": window,
            "msg": f"ビットレート測定を開始（最大 {window} 秒）…",
        })
        try:
            for ev in srt_probe.analyze_bitrate_variability(
                ip, port, passphrase, latency_ms, window_sec=window
            ):
                yield _sse(ev)
        except FileNotFoundError:
            yield _sse({
                "type": "error",
                "msg": (
                    "ffprobe が見つかりません。\n"
                    "ffmpeg をインストールして PATH に追加してください。\n"
                    "https://ffmpeg.org/download.html"
                ),
            })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _open_browser(host: str, port: int) -> None:
    webbrowser.open(f"http://{host}:{port}")


def main() -> None:
    host = os.environ.get("SRT_ANALYZER_HOST", "127.0.0.1")
    port = int(os.environ.get("SRT_ANALYZER_PORT", 8765))

    print("=" * 55)
    print("  SRT Listener Analyzer")
    print("=" * 55)
    print(f"  URL : http://{host}:{port}")
    print("  終了: Ctrl+C")
    print("=" * 55)

    # PyInstaller 実行ファイルの場合はブラウザを自動起動
    if getattr(sys, "frozen", False):
        threading.Timer(1.5, _open_browser, args=[host, port]).start()

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
