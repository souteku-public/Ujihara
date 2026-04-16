"""
srt_probe.py — SRT + ffprobe コア解析ロジック（GUI / Web 共通）

SRT URL フォーマット: srt://IP:PORT?mode=caller&latency=MS[&passphrase=PASS]
ffprobe を subprocess で実行し、JSON を返す。
"""

import json
import os
import subprocess
import sys
import urllib.parse
from typing import Any, Dict, List, Optional

# Latency 自動スキャン時に試みる値（ミリ秒）
LATENCY_SCAN_VALUES: List[int] = [120, 200, 500, 1000, 2000, 3000]

# 各接続試行のデフォルトタイムアウト（秒）
PROBE_TIMEOUT_SEC: int = 8


def _ffprobe_path() -> str:
    """
    ffprobe の実行パスを返す。
    PyInstaller でビルドした .exe の場合は同梱の ffprobe.exe を優先する。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller が展開する一時フォルダ内を探す
        bundled = os.path.join(sys._MEIPASS, "ffprobe.exe")  # type: ignore[attr-defined]
        if os.path.exists(bundled):
            return bundled
    return "ffprobe"  # PATH から探す（通常実行時）


def build_srt_url(ip: str, port: int, passphrase: str, latency_ms: int) -> str:
    """SRT Caller URL を組み立てる。"""
    params: Dict[str, str] = {
        "mode": "caller",
        "latency": str(latency_ms),  # SRT ライブラリは ms 単位
    }
    if passphrase:
        params["passphrase"] = urllib.parse.quote(passphrase, safe="")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"srt://{ip}:{port}?{query}"


def probe_stream(
    ip: str,
    port: int,
    passphrase: str,
    latency_ms: int,
    timeout_sec: int = PROBE_TIMEOUT_SEC,
) -> Optional[Dict[str, Any]]:
    """
    ffprobe で SRT ストリームを解析する。

    Returns:
        成功時: ffprobe の解析結果 dict（streams / format を含む）
        失敗時: None
    Raises:
        FileNotFoundError: ffprobe が PATH 上に見つからない場合
    """
    url = build_srt_url(ip, port, passphrase, latency_ms)
    cmd = [
        _ffprobe_path(),
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise  # 呼び出し元で ffprobe 未インストールとして扱う

    try:
        stdout, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None

    if proc.returncode != 0 or not stdout.strip():
        return None

    try:
        data: Dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError:
        return None

    # streams か format のどちらかが存在すれば成功とみなす
    if data.get("streams") or data.get("format"):
        return data
    return None
