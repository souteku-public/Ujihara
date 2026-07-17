"""
srt_probe.py — SRT + ffprobe コア解析ロジック（GUI / Web 共通）

SRT URL フォーマット: srt://IP:PORT?mode=caller&latency=MS[&passphrase=PASS]
ffprobe を subprocess で実行し、JSON を返す。
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Latency 自動スキャン時に試みる値（ミリ秒）
LATENCY_SCAN_VALUES: List[int] = [120, 200, 500, 1000, 2000, 3000]

# 各接続試行のデフォルトタイムアウト（秒）
PROBE_TIMEOUT_SEC: int = 8

# ビットレート変動解析（CBR/VBR 判定）のデフォルト測定時間（秒）
BITRATE_WINDOW_SEC: int = 10

# 変動係数（CV = 標準偏差 / 平均）による CBR/VBR 判定しきい値
#   CV < CBR_CV_THRESHOLD           → CBR（固定ビットレート）
#   CBR_CV ～ NEAR_CBR_CV           → 準CBR / 制約付きVBR
#   CV >= NEAR_CBR_CV               → VBR（可変ビットレート）
CBR_CV_THRESHOLD: float = 0.05
NEAR_CBR_CV_THRESHOLD: float = 0.15


def _ffprobe_path() -> str:
    """
    ffprobe の実行パスを解決する。優先順位:
      1. PyInstaller 同梱（sys._MEIPASS 内）
      2. .exe と同じフォルダ
      3. システム PATH
    """
    name = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"

    if getattr(sys, "frozen", False):
        # 1. PyInstaller が展開する一時フォルダ内
        bundled = os.path.join(sys._MEIPASS, name)  # type: ignore[attr-defined]
        if os.path.exists(bundled):
            return bundled

        # 2. .exe ファイルと同じフォルダ
        beside = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.exists(beside):
            return beside

    return "ffprobe"  # 3. PATH から探す


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


# ==========================================================================
# ビットレート変動解析（CBR / VBR 判定）
# ==========================================================================
#
# 判定の考え方:
#   CBR / VBR はストリームのメタデータには通常記録されていない。
#   そのため、一定時間パケットを受信し、パケットのタイムスタンプとサイズから
#   「一定時間ごとのビットレート」を算出し、その時間的なばらつき（変動係数 CV）を
#   見ることで統計的に推定する。
#     - CBR: 各時間ビンのビットレートがほぼ一定 → CV が小さい
#     - VBR: シーンやフレーム種別に応じて増減する → CV が大きい
#
#   ※ あくまで統計的な推定であり、測定時間が短い / GOP 構造によっては
#     判定が揺れることがある（測定時間を延ばすほど精度が上がる）。


def _classify_bitrate_mode(cv: float, usable_bins: int) -> Tuple[str, str, str]:
    """
    変動係数と有効ビン数から (コード, 日本語ラベル, 信頼度) を返す。
    """
    if usable_bins < 4:
        return "UNKNOWN", "判定不可 (データ不足)", "low"
    if cv < CBR_CV_THRESHOLD:
        return "CBR", "CBR (固定ビットレート)", ("high" if usable_bins >= 8 else "medium")
    if cv < NEAR_CBR_CV_THRESHOLD:
        return "NEAR_CBR", "準CBR / 制約付きVBR", "medium"
    return "VBR", "VBR (可変ビットレート)", ("high" if usable_bins >= 8 else "medium")


def _variability(packets: List[Tuple[float, int]], bin_sec: float) -> Dict[str, Any]:
    """
    (時刻, バイト数) のリストから、時間ビンごとのビットレート統計を計算する。
    """
    if not packets:
        return {
            "mode": "UNKNOWN", "mode_label": "判定不可 (データ不足)", "confidence": "low",
            "cv": 0.0, "cv_pct": 0.0, "mean_bps": 0.0, "min_bps": 0.0, "max_bps": 0.0,
            "peak_avg_ratio": 0.0, "usable_bins": 0, "rates_bps": [], "measured_sec": 0.0,
        }

    t0 = min(t for t, _ in packets)
    t1 = max(t for t, _ in packets)

    # 時間ビンごとにバイト数を集計
    bins: Dict[int, int] = {}
    for t, size in packets:
        idx = int((t - t0) // bin_sec)
        bins[idx] = bins.get(idx, 0) + size

    idxs = sorted(bins)
    # 最初と最後のビンは受信の切れ目で不完全になりやすいので除外
    core = idxs[1:-1] if len(idxs) >= 3 else idxs

    rates = [bins[i] * 8.0 / bin_sec for i in core]  # bits/sec
    n = len(rates)

    if n >= 1:
        mean = sum(rates) / n
    else:
        mean = 0.0

    if n >= 2 and mean > 0:
        var = sum((r - mean) ** 2 for r in rates) / (n - 1)  # 標本分散
        std = var ** 0.5
        cv = std / mean
    else:
        std = 0.0
        cv = 0.0

    mn = min(rates) if rates else 0.0
    mx = max(rates) if rates else 0.0
    peak_ratio = (mx / mean) if mean > 0 else 0.0

    code, label, conf = _classify_bitrate_mode(cv, n)

    return {
        "mode": code,
        "mode_label": label,
        "confidence": conf,
        "cv": cv,
        "cv_pct": cv * 100.0,
        "mean_bps": mean,
        "min_bps": mn,
        "max_bps": mx,
        "peak_avg_ratio": peak_ratio,
        "usable_bins": n,
        "rates_bps": rates,
        "measured_sec": t1 - t0,
    }


def _compute_bitrate_stats(
    packets: List[Tuple[int, str, float, int]],
    bin_sec: float,
    window_sec: float,
) -> Dict[str, Any]:
    """
    収集したパケット群からストリームごと + 全体のビットレート統計を組み立てる。
    packets: (stream_index, codec_type, 時刻, バイト数) のリスト
    """
    per_stream: Dict[int, Dict[str, Any]] = {}
    for si, ct, t, sz in packets:
        entry = per_stream.setdefault(si, {"codec_type": ct, "pkts": []})
        entry["pkts"].append((t, sz))

    streams_out: List[Dict[str, Any]] = []
    for si in sorted(per_stream):
        info = per_stream[si]
        stats = _variability(info["pkts"], bin_sec)
        stats["stream_index"] = si
        stats["codec_type"] = info["codec_type"]
        streams_out.append(stats)

    # 全ストリーム合算（＝コンテナ全体のペイロード変動の目安）
    all_pkts = [(t, sz) for _, _, t, sz in packets]
    overall = _variability(all_pkts, bin_sec)
    overall["stream_index"] = "all"
    overall["codec_type"] = "combined"

    return {
        "streams": streams_out,
        "overall": overall,
        "window_sec": window_sec,
        "bin_sec": bin_sec,
    }


def analyze_bitrate_variability(
    ip: str,
    port: int,
    passphrase: str,
    latency_ms: int,
    window_sec: float = BITRATE_WINDOW_SEC,
    bin_sec: float = 1.0,
    connect_timeout: float = 12.0,
) -> Iterator[Dict[str, Any]]:
    """
    SRT ストリームを一定時間受信し、パケットのサイズとタイムスタンプから
    CBR / VBR を統計的に判定する（ジェネレータ）。

    Yields:
        {"type": "progress", "elapsed": x, "target": window_sec}
        {"type": "result", "streams": [...], "overall": {...}, ...}
        {"type": "error", "msg": "..."}
    Raises:
        FileNotFoundError: ffprobe が見つからない場合
    """
    url = build_srt_url(ip, port, passphrase, latency_ms)
    cmd = [
        _ffprobe_path(),
        "-v", "quiet",
        # パケット単位でストリーム番号・種別・時刻・サイズを取得。
        # compact 形式は 1 パケット 1 行の key=value で、途中終了しても行単位で安全にパースできる。
        "-show_entries", "packet=stream_index,codec_type,pts_time,dts_time,size",
        "-of", "compact=p=0",
        url,
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise

    packets: List[Tuple[int, str, float, int]] = []
    first_t: Optional[float] = None
    last_progress = -1.0
    got_any = False
    connect_deadline = time.monotonic() + connect_timeout

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now = time.monotonic()
            line = line.strip()
            if not line:
                if not got_any and now > connect_deadline:
                    break
                continue

            # key=value を | 区切りでパース（順序に依存しない）
            fields: Dict[str, str] = {}
            for token in line.split("|"):
                if "=" in token:
                    k, v = token.split("=", 1)
                    fields[k] = v

            try:
                si = int(fields.get("stream_index", ""))
                sz = int(fields.get("size", ""))
            except ValueError:
                continue

            ct = fields.get("codec_type", "unknown")

            # pts_time を優先、無ければ dts_time
            t_str = fields.get("pts_time", "")
            if t_str in ("", "N/A"):
                t_str = fields.get("dts_time", "")
            if t_str in ("", "N/A"):
                continue
            try:
                t = float(t_str)
            except ValueError:
                continue

            got_any = True
            if first_t is None:
                first_t = t
            elapsed = t - first_t

            # PTS が巻き戻る等の異常値はスキップ
            if elapsed < 0:
                continue

            packets.append((si, ct, t, sz))

            if elapsed - last_progress >= 0.5:
                last_progress = elapsed
                yield {
                    "type": "progress",
                    "elapsed": round(min(elapsed, window_sec), 1),
                    "target": window_sec,
                }

            if elapsed >= window_sec:
                break

            if not got_any and now > connect_deadline:
                break
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass

    if not packets:
        yield {
            "type": "error",
            "msg": (
                "ビットレート解析用のパケットを取得できませんでした。\n"
                "接続が維持できているか、指定した Latency が正しいかを確認してください。"
            ),
        }
        return

    result = _compute_bitrate_stats(packets, bin_sec, window_sec)
    result["type"] = "result"
    yield result
