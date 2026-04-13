#!/usr/bin/env python3
"""
SRT Listener Analyzer GUI
SRT Listenerに接続してffprobeでコーデック情報を解析するGUIアプリ。

依存関係:
  - PySide6
  - ffprobe (ffmpegパッケージに同梱)

使い方:
  python srt_analyzer.py
"""

import json
import subprocess
import sys
import urllib.parse
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
)


# Latency自動スキャン時に試みる値（ミリ秒）
LATENCY_SCAN_VALUES = [120, 200, 500, 1000, 2000, 3000]

# 各接続試行のタイムアウト（秒）
PROBE_TIMEOUT_SEC = 8


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

class SRTProbeWorker(QObject):
    """
    別スレッドでSRTストリームをffprobeで解析するワーカー。

    Signals:
        progress_text: ログに追加するメッセージ
        scan_progress:  (current, total, current_latency_ms)
        probe_result:   解析成功時のffprobe JSONデータ（拡張フィールド付き）
        probe_error:    全試行失敗時のエラーメッセージ
        finished:       処理完了（成否問わず）
    """

    progress_text = Signal(str)
    scan_progress = Signal(int, int, int)
    probe_result = Signal(dict)
    probe_error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        ip: str,
        port: int,
        passphrase: str,
        latency_ms: int,
        auto_scan: bool,
        probe_timeout: int = PROBE_TIMEOUT_SEC,
    ) -> None:
        super().__init__()
        self.ip = ip
        self.port = port
        self.passphrase = passphrase
        self.latency_ms = latency_ms
        self.auto_scan = auto_scan
        self.probe_timeout = probe_timeout
        self._cancel = False
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        """実行中の試行をキャンセルする。"""
        self._cancel = True
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_url(self, latency_ms: int) -> str:
        """SRT URLを構築する。"""
        params: Dict[str, str] = {
            "mode": "caller",
            "latency": str(latency_ms),  # SRTプロトコルはmsを使用
        }
        if self.passphrase:
            # パスフレーズをURLエンコード
            params["passphrase"] = urllib.parse.quote(self.passphrase, safe="")
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"srt://{self.ip}:{self.port}?{query}"

    def _run_ffprobe(self, latency_ms: int) -> Optional[Dict[str, Any]]:
        """
        ffprobeを実行してストリーム情報を取得する。
        成功時はパース済みJSONを返し、失敗時はNoneを返す。
        """
        url = self._build_url(latency_ms)
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            "-show_programs",
            url,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                stdout, stderr = self._proc.communicate(timeout=self.probe_timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.communicate()
                self._proc = None
                return None

            rc = self._proc.returncode
            self._proc = None

            if rc == 0 and stdout.strip():
                data = json.loads(stdout)
                # streamsが空でも取得成功とみなす（format情報のみでも有効）
                if data.get("streams") or data.get("format"):
                    return data
            return None

        except FileNotFoundError:
            # ffprobeが見つからない場合は上位で捕捉
            raise
        except (json.JSONDecodeError, OSError):
            self._proc = None
            return None

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """スレッドエントリポイント。"""
        latencies = LATENCY_SCAN_VALUES if self.auto_scan else [self.latency_ms]
        total = len(latencies)

        try:
            for i, lat in enumerate(latencies):
                if self._cancel:
                    self.progress_text.emit("キャンセルされました")
                    self.finished.emit()
                    return

                self.scan_progress.emit(i + 1, total, lat)
                self.progress_text.emit(f"[{i + 1}/{total}] Latency {lat} ms で接続を試行中…")

                result = self._run_ffprobe(lat)

                if self._cancel:
                    self.progress_text.emit("キャンセルされました")
                    self.finished.emit()
                    return

                if result is not None:
                    n_streams = len(result.get("streams", []))
                    self.progress_text.emit(
                        f"接続成功 — {n_streams} ストリームを検出 (Latency: {lat} ms)"
                    )
                    result["_latency_ms"] = lat
                    result["_srt_url"] = self._build_url(lat)
                    self.probe_result.emit(result)
                    self.finished.emit()
                    return

                self.progress_text.emit(f"  → Latency {lat} ms: タイムアウトまたは接続拒否")

        except FileNotFoundError:
            self.probe_error.emit(
                "ffprobe が見つかりません。\nffmpeg パッケージをインストールして PATH に追加してください。"
            )
            self.finished.emit()
            return

        self.probe_error.emit(
            "接続失敗: すべての Latency 値で接続できませんでした。\n"
            "IP・ポート・パスフレーズを確認し、SRT Listenerが起動していることを確認してください。"
        )
        self.finished.emit()


# ---------------------------------------------------------------------------
# Stream Table
# ---------------------------------------------------------------------------

# 全カラム定義: (ヘッダーラベル, 内部キー)
# 内部キーが "_" で始まる場合は計算フィールド
_ALL_COLUMNS: List[tuple] = [
    ("Index",          "index"),
    ("Type",           "_type"),
    ("Codec",          "codec_name"),
    ("Long Name",      "codec_long_name"),
    ("Profile",        "profile"),
    ("Level",          "level"),
    ("Resolution",     "_resolution"),
    ("Frame Rate",     "_fps"),
    ("Pixel Format",   "pix_fmt"),
    ("Color Space",    "color_space"),
    ("Color Transfer", "color_transfer"),
    ("Color Prim.",    "color_primaries"),
    ("Sample Rate",    "_sample_rate"),
    ("Channels",       "channels"),
    ("Ch. Layout",     "channel_layout"),
    ("Sample Fmt",     "sample_fmt"),
    ("Bit Rate",       "_bitrate"),
    ("Duration",       "_duration"),
    ("Language",       "_language"),
]


class StreamTableWidget(QTableWidget):
    """ストリーム情報を表示するテーブルウィジェット。"""

    # ビデオ行の背景色
    _VIDEO_BG = QColor(210, 225, 255)
    # オーディオ行の背景色
    _AUDIO_BG = QColor(210, 245, 210)
    # その他行の背景色（白）
    _OTHER_BG = QColor(255, 255, 255)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        headers = [col[0] for col in _ALL_COLUMNS]
        self.setColumnCount(len(headers))
        self.setHorizontalHeaderLabels(headers)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.horizontalHeader().setStretchLastSection(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(False)  # 手動で色付けするため無効
        self.verticalHeader().setVisible(False)
        self.setWordWrap(False)

    # ------------------------------------------------------------------
    # Value extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_bitrate(br_str: str) -> str:
        try:
            br = int(br_str)
        except (ValueError, TypeError):
            return str(br_str)
        if br >= 1_000_000:
            return f"{br / 1_000_000:.2f} Mbps"
        if br >= 1_000:
            return f"{br / 1_000:.1f} kbps"
        return f"{br} bps"

    @staticmethod
    def _fmt_fps(fps_str: str) -> str:
        """'num/den' 形式をfps小数に変換する。"""
        if not fps_str or fps_str == "0/0":
            return "-"
        try:
            num, den = fps_str.split("/")
            val = int(num) / int(den)
            # 整数に近ければ整数表示
            if abs(val - round(val)) < 0.01:
                return str(round(val))
            return f"{val:.3f}"
        except (ValueError, ZeroDivisionError):
            return fps_str

    def _get_cell(self, stream: Dict[str, Any], key: str) -> str:
        """計算フィールドを含む任意キーの表示値を返す。"""
        if key == "_type":
            t = stream.get("codec_type", "unknown")
            return {"video": "VIDEO", "audio": "AUDIO", "data": "DATA",
                    "subtitle": "SUB"}.get(t, t.upper())

        if key == "_resolution":
            w, h = stream.get("width"), stream.get("height")
            if w and h:
                sar = stream.get("sample_aspect_ratio", "")
                dar = stream.get("display_aspect_ratio", "")
                s = f"{w}x{h}"
                if dar and dar != "0:0":
                    s += f" ({dar})"
                return s
            return "-"

        if key == "_fps":
            # r_frame_rate が優先。0/0の場合は avg_frame_rate を試みる
            v = self._fmt_fps(stream.get("r_frame_rate", ""))
            if v == "-":
                v = self._fmt_fps(stream.get("avg_frame_rate", ""))
            return v

        if key == "_bitrate":
            br = stream.get("bit_rate")
            if br:
                return self._fmt_bitrate(br)
            return "-"

        if key == "_sample_rate":
            sr = stream.get("sample_rate")
            if sr:
                try:
                    hz = int(sr)
                    return f"{hz:,} Hz"
                except ValueError:
                    return str(sr)
            return "-"

        if key == "_duration":
            d = stream.get("duration")
            if d:
                try:
                    sec = float(d)
                    h = int(sec // 3600)
                    m = int((sec % 3600) // 60)
                    s = sec % 60
                    return f"{h:02d}:{m:02d}:{s:05.2f}"
                except ValueError:
                    return str(d)
            return "-"

        if key == "_language":
            tags = stream.get("tags", {})
            return tags.get("language", tags.get("LANGUAGE", "-"))

        val = stream.get(key)
        if val is None:
            return "-"
        if isinstance(val, int) and val == -99:
            return "-"
        return str(val)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def populate(self, streams: List[Dict[str, Any]]) -> None:
        """ストリームリストでテーブルを更新する。"""
        self.setRowCount(len(streams))
        for row, stream in enumerate(streams):
            codec_type = stream.get("codec_type", "")
            bg = (
                self._VIDEO_BG if codec_type == "video"
                else self._AUDIO_BG if codec_type == "audio"
                else self._OTHER_BG
            )
            for col, (_, key) in enumerate(_ALL_COLUMNS):
                value = self._get_cell(stream, key)
                item = QTableWidgetItem(value)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setBackground(bg)
                self.setItem(row, col, item)

        self.resizeColumnsToContents()
        # 最低幅を確保
        for col in range(self.columnCount()):
            if self.columnWidth(col) < 60:
                self.setColumnWidth(col, 60)


# ---------------------------------------------------------------------------
# Format Info Table
# ---------------------------------------------------------------------------

class FormatTableWidget(QTableWidget):
    """フォーマット（コンテナ）情報を表示するテーブルウィジェット。"""

    _KEYS = [
        ("フォーマット名",        "format_name"),
        ("フォーマット詳細",      "format_long_name"),
        ("開始時刻 (s)",          "start_time"),
        ("継続時間 (s)",          "duration"),
        ("サイズ (bytes)",        "size"),
        ("ビットレート",          "_bitrate"),
        ("ストリーム数",          "nb_streams"),
        ("プログラム数",          "nb_programs"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHorizontalHeaderLabels(["項目", "値"])
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)

    def populate(self, fmt: Dict[str, Any]) -> None:
        rows = []
        for label, key in self._KEYS:
            if key == "_bitrate":
                br = fmt.get("bit_rate")
                if br:
                    try:
                        val = StreamTableWidget._fmt_bitrate(br)
                    except Exception:
                        val = str(br)
                else:
                    val = "-"
            else:
                val = fmt.get(key, "-")
                if val is None:
                    val = "-"
                val = str(val)
            rows.append((label, val))

        # タグも表示
        tags = fmt.get("tags", {})
        for tk, tv in tags.items():
            rows.append((f"[タグ] {tk}", str(tv)))

        self.setRowCount(len(rows))
        for r, (label, val) in enumerate(rows):
            self.setItem(r, 0, QTableWidgetItem(label))
            self.setItem(r, 1, QTableWidgetItem(val))


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SRT Listener Analyzer")
        self.setMinimumSize(1100, 720)
        self._worker: Optional[SRTProbeWorker] = None
        self._thread: Optional[QThread] = None
        self._setup_ui()
        self._check_ffprobe()

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # === 接続設定グループ ===
        conn_group = QGroupBox("接続設定")
        conn_outer = QHBoxLayout(conn_group)
        conn_outer.setSpacing(12)

        # -- 接続パラメータ --
        param_form = QFormLayout()
        param_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.ip_edit = QLineEdit("127.0.0.1")
        self.ip_edit.setPlaceholderText("例: 192.168.1.100")
        self.ip_edit.setMinimumWidth(200)
        param_form.addRow("IP アドレス:", self.ip_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(9998)
        param_form.addRow("ポート:", self.port_spin)

        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("（オプション）")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setMinimumWidth(200)
        param_form.addRow("パスフレーズ:", self.pass_edit)

        conn_outer.addLayout(param_form)

        # セパレータ
        def _vline() -> QFrame:
            f = QFrame()
            f.setFrameShape(QFrame.Shape.VLine)
            f.setFrameShadow(QFrame.Shadow.Sunken)
            return f

        conn_outer.addWidget(_vline())

        # -- Latency設定 --
        lat_form = QFormLayout()
        lat_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.autoscan_check = QCheckBox("自動スキャン")
        self.autoscan_check.setChecked(True)
        self.autoscan_check.toggled.connect(self._on_autoscan_toggled)
        lat_form.addRow("モード:", self.autoscan_check)

        self.latency_spin = QSpinBox()
        self.latency_spin.setRange(20, 60_000)
        self.latency_spin.setValue(200)
        self.latency_spin.setSuffix(" ms")
        self.latency_spin.setEnabled(False)
        lat_form.addRow("固定 Latency:", self.latency_spin)

        self.scan_hint = QLabel(
            "スキャン順: " + " → ".join(f"{v} ms" for v in LATENCY_SCAN_VALUES)
        )
        self.scan_hint.setStyleSheet("color: #666; font-size: 11px;")
        lat_form.addRow("", self.scan_hint)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(2, 60)
        self.timeout_spin.setValue(PROBE_TIMEOUT_SEC)
        self.timeout_spin.setSuffix(" 秒")
        lat_form.addRow("タイムアウト:", self.timeout_spin)

        conn_outer.addLayout(lat_form)
        conn_outer.addWidget(_vline())

        # -- 操作ボタン --
        btn_col = QVBoxLayout()
        btn_col.setSpacing(6)

        self.connect_btn = QPushButton("  接続 / 解析  ")
        self.connect_btn.setMinimumHeight(44)
        self.connect_btn.setMinimumWidth(130)
        self.connect_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #1565C0; color: white;"
            "  font-weight: bold; font-size: 13px;"
            "  border-radius: 5px; padding: 4px 12px;"
            "}"
            "QPushButton:hover { background-color: #0D47A1; }"
            "QPushButton:disabled { background-color: #90CAF9; color: #fff; }"
        )
        self.connect_btn.clicked.connect(self._on_connect)
        btn_col.addWidget(self.connect_btn)

        self.cancel_btn = QPushButton("キャンセル")
        self.cancel_btn.setMinimumHeight(36)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setStyleSheet(
            "QPushButton {"
            "  background-color: #C62828; color: white;"
            "  font-size: 12px; border-radius: 5px; padding: 4px 8px;"
            "}"
            "QPushButton:hover { background-color: #B71C1C; }"
            "QPushButton:disabled { background-color: #EF9A9A; color: #fff; }"
        )
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_col.addWidget(self.cancel_btn)

        btn_col.addStretch()
        conn_outer.addLayout(btn_col)
        conn_outer.addStretch()

        root.addWidget(conn_group)

        # === プログレスバー ===
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(LATENCY_SCAN_VALUES))
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("待機中")
        self.progress_bar.setFixedHeight(22)
        root.addWidget(self.progress_bar)

        # === 結果タブ ===
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, stretch=1)

        # ストリーム情報タブ
        self.stream_table = StreamTableWidget()
        self.tabs.addTab(self.stream_table, "ストリーム情報")

        # フォーマット情報タブ
        self.format_table = FormatTableWidget()
        self.tabs.addTab(self.format_table, "フォーマット情報")

        # Raw JSON タブ
        self.raw_text = QTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setFont(QFont("Monospace", 10))
        self.tabs.addTab(self.raw_text, "Raw JSON")

        # ログタブ
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Monospace", 9))
        self.tabs.addTab(self.log_text, "ログ")

        # === ステータスバー ===
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("準備完了 — IP・ポートを入力して「接続 / 解析」を押してください")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_autoscan_toggled(self, checked: bool) -> None:
        self.latency_spin.setEnabled(not checked)
        self.scan_hint.setVisible(checked)

    def _log(self, msg: str) -> None:
        self.log_text.append(msg)
        self.status_bar.showMessage(msg)

    def _on_connect(self) -> None:
        ip = self.ip_edit.text().strip()
        if not ip:
            QMessageBox.warning(self, "入力エラー", "IP アドレスを入力してください。")
            return

        port = self.port_spin.value()
        passphrase = self.pass_edit.text()
        auto_scan = self.autoscan_check.isChecked()
        latency_ms = self.latency_spin.value()
        timeout = self.timeout_spin.value()

        # 前回の結果をクリア
        self.stream_table.setRowCount(0)
        self.format_table.setRowCount(0)
        self.raw_text.clear()
        self.log_text.clear()

        max_steps = len(LATENCY_SCAN_VALUES) if auto_scan else 1
        self.progress_bar.setRange(0, max_steps)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("接続中…")

        self.connect_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self._thread = QThread()
        self._worker = SRTProbeWorker(ip, port, passphrase, latency_ms, auto_scan, timeout)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.progress_text.connect(self._log)
        self._worker.scan_progress.connect(self._on_scan_progress)
        self._worker.probe_result.connect(self._on_result)
        self._worker.probe_error.connect(self._on_error)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)

        self._thread.start()

    def _on_cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
        self._log("キャンセル中…")
        self.cancel_btn.setEnabled(False)

    def _on_scan_progress(self, current: int, total: int, latency: int) -> None:
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current - 1)  # 試行中は前のstepを示す
        self.progress_bar.setFormat(f"Latency {latency} ms を試行中… ({current}/{total})")

    def _on_result(self, data: Dict[str, Any]) -> None:
        streams: List[Dict[str, Any]] = data.get("streams", [])
        fmt: Dict[str, Any] = data.get("format", {})
        latency = data.get("_latency_ms", "?")
        url = data.get("_srt_url", "")

        n_video = sum(1 for s in streams if s.get("codec_type") == "video")
        n_audio = sum(1 for s in streams if s.get("codec_type") == "audio")

        self.stream_table.populate(streams)
        self.format_table.populate(fmt)

        # Raw JSONから内部フィールドを除外して表示
        display_data = {k: v for k, v in data.items() if not k.startswith("_")}
        self.raw_text.setPlainText(json.dumps(display_data, indent=2, ensure_ascii=False))

        self.progress_bar.setMaximum(self.progress_bar.maximum())
        self.progress_bar.setValue(self.progress_bar.maximum())
        self.progress_bar.setFormat(f"解析完了 (Latency: {latency} ms)")

        self.status_bar.showMessage(
            f"解析成功 — 映像 {n_video} / 音声 {n_audio} ストリーム検出  |  接続 URL: {url}"
        )
        self.tabs.setCurrentIndex(0)

        self._log(f"SRT URL: {url}")

    def _on_error(self, msg: str) -> None:
        self._log(f"[ERROR] {msg}")
        self.progress_bar.setFormat("失敗")
        QMessageBox.critical(self, "接続エラー", msg)

    def _on_worker_finished(self) -> None:
        self.connect_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Startup check
    # ------------------------------------------------------------------

    def _check_ffprobe(self) -> None:
        """ffprobeが利用可能かどうかを起動時に確認する。"""
        try:
            result = subprocess.run(
                ["ffprobe", "-version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                # バージョン行を抽出
                first_line = result.stdout.splitlines()[0] if result.stdout else "（不明）"
                self._log(f"ffprobe 検出: {first_line}")
            else:
                self._warn_ffprobe_missing()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._warn_ffprobe_missing()

    def _warn_ffprobe_missing(self) -> None:
        self._log("[WARNING] ffprobe が見つかりません。ffmpeg をインストールしてください。")
        self.status_bar.showMessage("警告: ffprobe が見つかりません — ffmpeg をインストールしてください")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # フォント設定（日本語フォントを優先）
    font = QFont()
    for family in ("Noto Sans CJK JP", "Yu Gothic UI", "Meiryo UI", "sans-serif"):
        font.setFamily(family)
        break
    font.setPointSize(10)
    app.setFont(font)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
