"""Simple Windows-oriented Tkinter UI; no external GUI framework required."""

from __future__ import annotations

import hashlib
import math
import json
import queue
import threading
import traceback
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (JST, automatic_model_top, automatic_worker_count,
                   execute_group, expand_observations,
                   group_starts, load_starts_csv, write_group,
                   validate_time_controls)
from .era5 import (
    ARCO_PRESSURE_FIELDS, ARCO_SURFACE_FIELDS, HYSPLIT_PRESSURE_FIELDS,
    NORTHERN_HEMISPHERE_BBOX, PRESSURE_LEVELS, PRESSURE_FIELDS, SURFACE_FIELDS,
)
from .plot import create_map
from .platform_guard import require_native_windows, prevent_automatic_system_sleep
from .validation import check_coverage
from .meteo import plan_archive_windows
from .met_diagnostics import MET_VARIABLES, DEFAULT_MET_VARIABLES, selected_variables
from .portable import default_work_root, find_hysplit_executable
from .csv_export import export_run_csv, period_csv_name
from .time_windows import TimeWindow, find_window, windows_for_starts
from .batch_meteorology import preparation_count, prepare_meteorology
from .progress import TaskProgress
from .work_budget import WorkBudget
from .app_settings import load_settings, save_settings, settings_path
from .retention import cleanup_successful_meteorology, cleanup_successful_run
from .arl_catalog import files_for_window

PROVIDERS = {
    "GDAS1 (1°)": "gdas1",
    "GFS (0.25°)": "gfs0p25",
    "ローカルARL": "local",
    "ERA5 CDS (0.25°)": "era5_cds",
    "ERA5 Google ARCO (0.25°)": "arco_era5",
}
UTC = timezone.utc
ERA5_FIELD_PROFILES = {
    "HYSPLIT必須項目のみ（推奨・高速）": "hysplit",
    "全変数を保存（大容量）": "allvars",
}


def discover_native_hysplit() -> Path:
    """Locate a separately installed NOAA HYSPLIT on the current Windows PC."""
    return find_hysplit_executable()


class App(tk.Tk):
    def __init__(self):
        require_native_windows()
        super().__init__()
        self.title(f"HYSPLIT Batch Trajectories {__version__}")
        self.geometry("920x760")
        self.minsize(760, 620)
        self.events = queue.Queue()
        self.busy = False
        self.last_run = None
        self._settings_path = settings_path()
        saved_settings = load_settings(self._settings_path)
        saved_fields = saved_settings.get("fields", {})
        if not isinstance(saved_fields, dict):
            saved_fields = {}
        detected_executable = discover_native_hysplit()
        work_root = default_work_root()
        defaults = {
            "executable": str(detected_executable), "root": str(work_root),
            "csv": "", "distance_km": "0", "time_shift_minutes": "0",
            "hours": "48", "height_ref": "MSL", "model_top_m": "",
            "output_interval_minutes": "10",
            "integration_step_minutes": "0",
            "workers": "自動", "max_points": "100",
            "provider": next(iter(PROVIDERS)), "local_met": "",
            "era5_field_scope": "HYSPLIT必須項目のみ（推奨・高速）",
        }
        for key in defaults:
            value = saved_fields.get(key)
            if isinstance(value, str):
                defaults[key] = value
        if defaults["provider"] not in PROVIDERS:
            defaults["provider"] = next(iter(PROVIDERS))
        if defaults["height_ref"] not in ("AGL", "MSL"):
            defaults["height_ref"] = "MSL"
        if defaults["era5_field_scope"] not in ERA5_FIELD_PROFILES:
            defaults["era5_field_scope"] = next(iter(ERA5_FIELD_PROFILES))
        workers = defaults["workers"]
        if workers != "自動" and not (workers.isdigit() and int(workers) >= 1):
            defaults["workers"] = "自動"
        self.v = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        saved_diagnostics = saved_settings.get("met_diagnostics")
        if isinstance(saved_diagnostics, list):
            selected_diagnostics = set(
                value for value in saved_diagnostics if isinstance(value, str)
            )
        else:
            selected_diagnostics = set(DEFAULT_MET_VARIABLES)
        self.met_checks = {
            option.flag: tk.BooleanVar(value=(option.flag in selected_diagnostics))
            for option in MET_VARIABLES
        }
        self.show_advanced_timestep = tk.BooleanVar(
            value=bool(saved_settings.get("show_advanced_timestep", False))
        )
        self.keep_met_sources = tk.BooleanVar(
            value=bool(saved_settings.get(
                "keep_met_sources", saved_settings.get("keep_met_cache", False)
            ))
        )
        self._widgets()
        if self.show_advanced_timestep.get():
            self.advanced_time_frame.grid()
        if saved_fields:
            self._log("前回の実行設定を復元しました。")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    def _settings_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "fields": {key: variable.get() for key, variable in self.v.items()},
            "met_diagnostics": [
                flag for flag, variable in self.met_checks.items() if variable.get()
            ],
            "show_advanced_timestep": self.show_advanced_timestep.get(),
            "keep_met_sources": self.keep_met_sources.get(),
        }

    def _save_settings(self) -> bool:
        try:
            save_settings(self._settings_payload(), self._settings_path)
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._log(f"設定を保存できませんでした: {exc}")
            return False

    def _on_close(self) -> None:
        self._save_settings()
        self.destroy()

    def _entry(self, parent, label, key, row, width=24, browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=4)
        ttk.Entry(parent, textvariable=self.v[key], width=width).grid(
            row=row, column=1, sticky="ew", padx=5, pady=4)
        if browse:
            ttk.Button(parent, text="参照…", command=browse).grid(row=row, column=2, sticky="w", padx=5)

    def _group(self, parent, text):
        group = ttk.LabelFrame(parent, text=text, padding=8)
        group.pack(fill="x", padx=10, pady=6)
        group.columnconfigure(1, weight=1)
        return group

    def _update_era5_field_scope_state(self):
        is_era5 = self.v["provider"].get() in (
            "ERA5 CDS (0.25°)", "ERA5 Google ARCO (0.25°)"
        )
        self.era5_scope_combo.configure(state="readonly" if is_era5 else "disabled")

    def _widgets(self):
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True)
        setup = ttk.Frame(tabs)
        meteo = ttk.Frame(tabs)
        tabs.add(setup, text="1. 後方流跡線の設定")
        tabs.add(meteo, text="2. 気象データ")
        self.tabs = tabs

        # Scroll settings on compact Windows laptop displays.
        canvas = tk.Canvas(setup, highlightthickness=0)
        settings_scroll = ttk.Scrollbar(setup, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=settings_scroll.set)
        settings_scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        setup_content = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=setup_content, anchor="nw")
        setup_content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))

        loc = self._group(setup_content, "HYSPLIT と保存先")
        self._entry(loc, "HYSPLIT実行ファイル (.exe)", "executable", 0, 65, self._pick_executable)
        self._entry(loc, "作業・データ保存フォルダ", "root", 1, 65, self._pick_root)
        bases = self._group(setup_content, "観測CSV（JST）")
        self._entry(bases, "CSV（時刻, 緯度, 経度, 高度m）", "csv", 0, 65, self._pick_csv)
        ttk.Label(bases, text="ヘッダーあり・なし両対応。例: 2026/10/10 09:00,35,135,500",
                  foreground="#495b6c").grid(
                      row=1, column=0, columnspan=3, sticky="w", padx=5)

        settings = self._group(setup_content, "観測時刻・位置の感度解析")
        self._entry(settings, "水平ずらし距離 [km] (0=なし)", "distance_km", 0)
        self._entry(settings, "観測時刻の前後に試す [分] (0=なし)", "time_shift_minutes", 1)
        ttk.Label(settings, text="距離を入れると中心＋北・北東・東・南東・南・南西・西・北西の9地点。").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=5)
        ttk.Label(settings, text="時間を入れると観測時刻の前・同時刻・後の3通り。").grid(
            row=3, column=0, columnspan=3, sticky="w", padx=5)

        run_settings = self._group(setup_content, "後方流跡線の計算条件")
        self._entry(run_settings, "何時間前まで遡るか [h]", "hours", 0)
        self._entry(run_settings, "流跡線の出力間隔 [分]（標準10分）",
                    "output_interval_minutes", 1)
        ttk.Label(run_settings, text="CSVの高度の基準").grid(row=2, column=0, sticky="w", padx=5)
        ttk.Combobox(run_settings, textvariable=self.v["height_ref"], values=["AGL", "MSL"],
                     state="readonly", width=10).grid(row=2, column=1, sticky="w", padx=5)
        self._entry(
            run_settings, "モデル上端 [m AGL]（空欄は自動）", "model_top_m", 3
        )
        ttk.Label(run_settings, text="同時に動かす計算プロセス数").grid(
            row=4, column=0, sticky="w", padx=5, pady=4
        )
        ttk.Combobox(
            run_settings, textvariable=self.v["workers"],
            values=["自動"] + [str(value) for value in range(1, 33)],
            state="normal", width=10,
        ).grid(row=4, column=1, sticky="w", padx=5, pady=4)
        self._entry(run_settings, "1実行当たり最大開始点数", "max_points", 5)
        ttk.Checkbutton(
            run_settings, text="詳細設定：計算時間刻みを固定する",
            variable=self.show_advanced_timestep,
            command=self._toggle_advanced_time,
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=5, pady=4)
        self.advanced_time_frame = ttk.Frame(run_settings)
        self.advanced_time_frame.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.advanced_time_frame.grid_remove()
        self._entry(
            self.advanced_time_frame, "計算時間刻み DELT [分] (0=自動)",
            "integration_step_minutes", 0,
        )
        ttk.Label(
            self.advanced_time_frame,
            text="通常は自動（0）のまま。固定値は60分と出力間隔を割り切る整数のみ。",
            foreground="#855000",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=5)

        extras = self._group(setup_content, "流跡線上に出力する気象変数")
        ttk.Label(
            extras,
            text="気圧 [hPa] と地表標高 [m MSL] は常に保存します。"
                 "以下は必要な項目だけチェックしてください。",
            foreground="#465d71",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(2, 7))
        for index, option in enumerate(MET_VARIABLES):
            ttk.Checkbutton(
                extras, text=f"{option.japanese_label} [{option.unit}]",
                variable=self.met_checks[option.flag],
            ).grid(row=index // 2 + 1, column=index % 2,
                   sticky="w", padx=(10, 22), pady=4)

        source = self._group(meteo, "データソースの選択")
        ttk.Label(source, text="気象データ").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.provider_combo = ttk.Combobox(
            source, textvariable=self.v["provider"], values=list(PROVIDERS),
            state="readonly", width=59,
        )
        self.provider_combo.grid(row=0, column=1, sticky="ew", padx=5)
        self.provider_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._update_era5_field_scope_state()
        )
        self._entry(source, "ローカルARLファイル (複数選択可)", "local_met", 1, 55, self._pick_arl)
        ttk.Label(source, text="ERA5で保存する変数").grid(
            row=2, column=0, sticky="w", padx=5, pady=4
        )
        self.era5_scope_combo = ttk.Combobox(
            source, textvariable=self.v["era5_field_scope"],
            values=list(ERA5_FIELD_PROFILES), state="readonly", width=59,
        )
        self.era5_scope_combo.grid(row=2, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(
            source,
            text="必須項目のみは37気圧面を維持し、6気圧面変数と7地上変数だけ保存します。",
            foreground="#465d71",
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=5)
        ttk.Label(
            source,
            text="ERA5（CDS/Google ARCO）は北半球全域（0～90°N、全経度）の0.25°データを保存します。"
                 " 取得範囲の自動切出しはしません。通信量・ディスク容量に注意してください。",
            foreground="#855000",
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=5)
        ttk.Checkbutton(
            source,
            text="ERA5の変換元NetCDF/GRIBも残す（再変換用・大容量）",
            variable=self.keep_met_sources,
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=5, pady=(4, 0))
        self._update_era5_field_scope_state()

        notes = self._group(meteo, "対応状況と重要事項")
        for line in [
            "GDAS1：全球 約1°格子。NOAAのARLデータを取得して計算。",
            "GFS 0.25°：全球 0.25°格子。NOAAのARLデータを取得して計算。",
            "ローカルARL: 手元のARLファイルで計算します。",
            "ERA5 CDSを使うには、Copernicus CDSの利用登録・規約同意・API認証設定が必要です。",
            "ERA5変数：必須13項目または多変数を選択（CDSは16気圧面+7地上、ARCOは全収録変数）。",
            "ERA5 CDS：北半球0.25°・37気圧面。GRIB→ARL変換は試験的。",
            "ERA5 Google ARCO：北半球0.25°・37気圧面。NetCDF→GRIB1→ARL変換は試験的。",
            "grib2arl.exeとfiledates.exeはHYSPLITの実行フォルダから自動検出します。",
            "出力：気象期間ごとにTrajectory CSVを分けて保存。別途オフライン図とGeoJSONも保存。",
            "気象データは期間ごとに順次準備し、準備できた期間の計算中に次の期間を取得します。",
            "正常終了後はグループ別作業フォルダとログを削除し、入力CSV・結果・実行記録・再計算用ARLを残します。",
            "ERA5の変換元NetCDF/GRIBは設定により削除します。失敗時は再開・原因確認用に残します。",
            "北半球ERA5も大容量です。保存先ドライブの空き容量を確認してください。"
            " 取得範囲や気圧面をプログラムが勝手に縮小することはありません。",
            "長時間処理中も進捗を表示します。完了まではアプリを閉じず、PCを手動でスリープさせないでください。",
        ]:
            ttk.Label(notes, text="• " + line, wraplength=820, justify="left").pack(anchor="w", pady=2)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=12, pady=5)
        self.buttons = [
            ttk.Button(bottom, text="計算内容をプレビュー", command=self.preview),
            ttk.Button(bottom, text="気象データを取得", command=lambda: self.start_task("download")),
            ttk.Button(bottom, text="一括計算を開始", command=lambda: self.start_task("run")),
            ttk.Button(bottom, text="結果を地図で表示", command=self.plot_results),
            ttk.Button(bottom, text="入力の説明", command=self.show_help),
        ]
        for button in self.buttons:
            button.pack(side="left", padx=5)
        ttk.Label(self, text="ログ（計算結果は保存フォルダの runs/<実行ID> に出力）").pack(anchor="w", padx=15)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, command=self.log.yview)
        scrollbar.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scrollbar.set)

        meter_frame = ttk.Frame(self)
        meter_frame.pack(fill="x", padx=14, pady=(0, 10))
        self.progress_status_var = tk.StringVar(value="待機中")
        ttk.Label(meter_frame, textvariable=self.progress_status_var).pack(anchor="w")
        ttk.Label(meter_frame, text="全体：完了した処理数", foreground="#465d71").pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            meter_frame, orient="horizontal", mode="determinate", maximum=100,
        )
        self.progress_bar.pack(fill="x", pady=(2, 4))
        ttk.Label(meter_frame, text="現在の処理（容量が分かる場合は転送割合）",
                  foreground="#465d71").pack(anchor="w")
        self.file_progress_bar = ttk.Progressbar(
            meter_frame, orient="horizontal", mode="indeterminate", maximum=100,
        )
        self.file_progress_bar.pack(fill="x", pady=(2, 0))
        self._file_bar_spinning = False

        self._log("観測CSVの時刻はJSTとして読み取ります。内部でUTCへ変換します。")
        detected_executable = Path(self.v["executable"].get())
        if not detected_executable.is_file():
            self._log(
                "HYSPLIT本体が見つかりません。NOAA公式サイトからWindows版を別途導入し、"
                "「HYSPLIT実行ファイル」でhyts_std.exeを指定してください。"
                "NOAA利用規約により本アプリには同梱していません。"
            )
        elif not (detected_executable.parent.parent / "bdyfiles").is_dir():
            self._log(
                "HYSPLITのbdyfilesフォルダがありません。"
                "正規インストールのexec/hyts_std.exeを選択してください。"
            )


    def _toggle_advanced_time(self):
        if self.show_advanced_timestep.get():
            self.advanced_time_frame.grid()
        else:
            self.advanced_time_frame.grid_remove()
            # Hiding the advanced override restores the safe automatic step.
            self.v["integration_step_minutes"].set("0")

    def _pick_executable(self):
        value = filedialog.askopenfilename(title="hyts_std.exe", filetypes=[("EXE", "*.exe"), ("All files", "*.*")])
        if value:
            self.v["executable"].set(value)

    def _pick_root(self):
        value = filedialog.askdirectory(title="HYSPLIT作業フォルダ")
        if value:
            self.v["root"].set(value)

    def _pick_csv(self):
        value = filedialog.askopenfilename(title="開始点CSV", filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if value:
            self.v["csv"].set(value)

    def _pick_arl(self):
        values = filedialog.askopenfilenames(title="ARLファイルを複数選択")
        if values:
            self.v["local_met"].set(";".join(values))
            self.v["provider"].set("ローカルARL")

    def _log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", str(line) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _data(self):
        """Collect and validate everything in the GUI thread before a worker starts."""
        conf = {k: var.get().strip() for k, var in self.v.items()}
        try:
            conf["era5_field_profile"] = ERA5_FIELD_PROFILES[conf["era5_field_scope"]]
        except KeyError as exc:
            raise ValueError("ERA5の変数範囲を選択してください。") from exc
        conf["met_diagnostics"] = [
            option.flag for option in MET_VARIABLES if self.met_checks[option.flag].get()
        ]
        conf["keep_met_sources"] = self.keep_met_sources.get()
        selected_variables(conf["met_diagnostics"])
        bases = load_starts_csv(Path(conf["csv"]))
        points = expand_observations(
            bases, float(conf["distance_km"]), float(conf["time_shift_minutes"]))
        if conf["height_ref"] == "AGL" and any(point.height_m < 0 for point in points):
            raise ValueError("AGL開始高度は0 m以上にしてください。MSLなら負の高度も指定できます。")
        groups = group_starts(points, max_points=int(conf["max_points"]))
        back_hours = int(conf["hours"])
        tout, delt = validate_time_controls(
            conf["output_interval_minutes"],
            conf["integration_step_minutes"],
        )
        conf["output_interval_minutes"] = str(tout)
        conf["integration_step_minutes"] = str(delt)
        if back_hours <= 0 or back_hours > 9999:
            raise ValueError("後方計算時間は1～9999時間で指定してください。")
        # HYSPLIT CONTROL requires a model top in m AGL. Do not secretly
        # force a 20 km ceiling: show the value and allow an explicit override.
        # The auto choice is the native 25 km scaling-height reference or
        # 1 km above the highest start, whichever is higher.
        raw_top = conf["model_top_m"]
        if raw_top:
            try:
                internal_top = float(raw_top)
            except ValueError as exc:
                raise ValueError("モデル上端はメートル単位の数値で指定してください。") from exc
            if not math.isfinite(internal_top) or internal_top <= max(
                p.height_m for p in points
            ):
                raise ValueError(
                    "モデル上端は有限の数値で、最大開始高度より高く設定してください。"
                )
        else:
            internal_top = automatic_model_top(points)
            if conf["provider"] in (
                "ERA5 CDS (0.25°)", "ERA5 Google ARCO (0.25°)"
            ):
                # All 37 ERA5 pressure levels extend to 1 hPa (~48 km).
                # The native HYSPLIT solver was verified with a 50 km top;
                # a 25 km auto-top would silently terminate higher paths.
                internal_top = max(internal_top, 50000.0)
        conf["internal_model_top_m_agl"] = str(internal_top)
        if conf["height_ref"] not in ("AGL", "MSL"):
            raise ValueError("高度の基準は AGL または MSL です。")
        if conf["workers"] == "自動":
            conf["effective_workers"] = str(automatic_worker_count(len(groups)))
        else:
            try:
                requested_workers = int(conf["workers"])
            except ValueError as exc:
                raise ValueError("同時実行数は「自動」または1以上の整数です。") from exc
            if requested_workers < 1:
                raise ValueError("同時実行数は「自動」または1以上の整数です。")
            conf["effective_workers"] = str(min(requested_workers, len(groups)))
        earliest = min(p.when for p in points) - timedelta(hours=back_hours)
        latest = max(p.when for p in points)
        provider = PROVIDERS[conf["provider"]]
        # User-selected domain: 0-90 N with every longitude; no launch-based crop.
        # Both ERA5 sources preserve all 37 levels and the selected science fields.
        bbox = NORTHERN_HEMISPHERE_BBOX
        return conf, points, groups, earliest, latest, provider, bbox

    def preview(self):
        try:
            conf, points, groups, earliest, latest, provider, bbox = self._data()
            self._log(f"プレビュー: CSV {len(load_starts_csv(Path(conf['csv']))):,}行 → "
                      f"{len(points):,}本 / {len(groups):,}回実行")
            tout = int(conf["output_interval_minutes"])
            estimate = len(points) * (int(conf["hours"]) * 60 // tout + 1)
            self._log(
                f"出力間隔: {tout}分 / 計算刻み: "
                f"{'自動' if conf['integration_step_minutes'] == '0' else conf['integration_step_minutes'] + '分（固定）'}"
                f" / 推定CSV地点数: 約{estimate:,}行"
            )
            chosen = selected_variables(conf["met_diagnostics"])
            self._log(
                "出力する気象変数: 気圧・地表標高（常時）" +
                ("、" + "、".join(item.japanese_label for item in chosen) if chosen else "")
            )
            windows = windows_for_starts(
                (g.when for g in groups), int(conf["hours"])
            )
            self._log(
                f"モデル上端: {float(conf['internal_model_top_m_agl']):g} m AGL"
                + ("（自動）" if not conf["model_top_m"] else "（指定値）")
            )
            self._log(
                f"HYSPLIT同時実行数: {conf['effective_workers']}"
                + ("（自動、CPUと実行グループ数から選択）" if conf["workers"] == "自動" else "（指定値）")
            )
            self._log(f"必要気象期間: {len(windows)}個の独立した期間"
                "（間の空白日時は取得しません）"
            )
            for i, window in enumerate(windows, 1):
                self._log(
                    f"  {i}: {window.start.astimezone(JST):%Y-%m-%d %H:%M} ～ "
                    f"{window.end.astimezone(JST):%Y-%m-%d %H:%M} JST"
                )
            if provider in ("gdas1", "gfs0p25"):
                plan = plan_archive_windows(provider, windows)
                self._log(f"NOAA {provider}: {len(plan)} 個の固有ファイル取得・キャッシュ利用予定: " +
                          ", ".join(p.name for p in plan[:8]) + (" ..." if len(plan) > 8 else ""))
                self._log(
                    "NOAAは公開S3を優先し、転送状況と速度をログに表示します。"
                    "取得済みの気象データは同じ保存先なら再利用します。"
                )
                if provider == "gfs0p25":
                    self._log("注意: GFS 0.25° は1日あたり約3GB以上（保存領域・帯域を確認）。")
            elif provider == "era5_cds":
                self._log("ERA5(CDS): 北半球0.25°（0～90°N、全経度）を取得・保存します。"
                          "利用にはCopernicus CDSのAPI認証設定が必要です。"
                          "GRIB1をWindowsのgrib2arl.exeで変換します。")
            elif provider == "arco_era5":
                self._log(f"Google ARCO: 0.25° NetCDFを取得しWindowsのecCodesでGRIB1化、"
                          "grib2arl.exeでARLに変換します（試験的）。北半球全域・37気圧面。")
            else:
                self._log("ローカルARLファイル: 実行前にファイル存在確認します。")
            if provider in ("era5_cds", "arco_era5"):
                self._log(
                    "ERA5保存変数: " + (
                        "HYSPLIT必須13項目（6気圧面+7地上）"
                        if conf["era5_field_profile"] == "hysplit"
                        else "取得元の追加変数を含む全項目"
                    )
                )
        except Exception as exc:
            messagebox.showerror("入力エラー", str(exc))

    def start_task(self, action):
        if self.busy:
            return
        try:
            data = self._data()
            if action == "run" and data[5] == "era5_cds":
                exec_dir = Path(data[0]["executable"]).resolve().parent
                missing = [name for name in ("grib2arl.exe", "filedates.exe")
                           if not (exec_dir / name).is_file()]
                if missing:
                    raise FileNotFoundError("ERA5変換に必要なWindowsツールがありません: " + ", ".join(missing))
            if action == "run":
                executable = Path(data[0]["executable"]).resolve()
                if not executable.is_file():
                    raise FileNotFoundError(f"HYSPLIT実行ファイルがありません: {executable}")
                bdyfiles = executable.parent.parent / "bdyfiles"
                if not bdyfiles.is_dir():
                    raise FileNotFoundError(f"HYSPLITのbdyfilesがありません: {bdyfiles}")
        except Exception as exc:
            messagebox.showerror("入力エラー", str(exc))
            return
        self._save_settings()
        self.busy = True
        self.progress_status_var.set("計算準備中")
        self.progress_bar["value"] = 0
        self._set_file_progress(False)
        for button in self.buttons:
            button.configure(state="disabled")
        threading.Thread(target=self._background, args=(action, data), daemon=True).start()

    def _notify(self, message):
        self.events.put(("log", message))

    def _progress_event(self, event: dict[str, object]) -> None:
        self.events.put(("progress", event))

    def _set_file_progress(self, active: bool, part: float | None = None):
        if self._file_bar_spinning:
            self.file_progress_bar.stop()
            self._file_bar_spinning = False
        if not active:
            self.file_progress_bar.configure(mode="determinate")
            self.file_progress_bar["value"] = 0
        elif part is None:
            self.file_progress_bar.configure(mode="indeterminate")
            self.file_progress_bar.start(60)
            self._file_bar_spinning = True
        else:
            self.file_progress_bar.configure(mode="determinate")
            self.file_progress_bar["value"] = max(0, min(100, 100 * part))

    def _apply_progress(self, status: dict[str, object]) -> None:
        done, total = int(status["done"]), int(status["total"])
        self.progress_bar["value"] = 100.0 * done / total
        self.progress_status_var.set(
            f"処理 {done}/{total}：{status['label']}"
        )
        self._set_file_progress(
            bool(status["active"]), status.get("part")
        )

    def _background(self, action, data):
        with prevent_automatic_system_sleep():
            self._background_active(action, data)

    def _background_active(self, action, data):
        try:
            conf, points, groups, earliest, latest, provider, bbox = data
            root = Path(conf["root"]).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            met_dir = root / "met" / provider
            windows = windows_for_starts(
                (g.when for g in groups), int(conf["hours"])
            )
            progress = TaskProgress(
                preparation_count(provider, windows, action=action)
                + (2 * len(windows) + len(groups) + 1 if action == "run" else 0),
                send=self._progress_event,
            )
            self._notify(
                f"Required meteorology: {len(windows)} separate UTC time windows "
                "(unused gaps are not downloaded)"
            )
            for i, window in enumerate(windows, 1):
                self._notify(
                    f"Period {i}/{len(windows)} JST: "
                    f"{window.start.astimezone(JST):%Y-%m-%d %H:%M} - "
                    f"{window.end.astimezone(JST):%Y-%m-%d %H:%M}"
                )
            if action == "run":
                self._run_period_pipeline(
                    conf, points, groups, earliest, latest, provider, bbox,
                    root, met_dir, windows, progress,
                )
                return
            prepare_meteorology(
                provider, windows, met_dir, bbox, action=action,
                local_met=conf["local_met"],
                hysplit_executable=Path(conf["executable"]),
                notify=self._notify,
                progress=progress,
                era5_field_profile=conf["era5_field_profile"],
                keep_sources=bool(conf.get("keep_met_sources", False)),
            )
            self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))
            self._notify(traceback.format_exc(limit=3))

    def _run_period_pipeline(self, conf, points, groups, earliest, latest,
                             provider, bbox, root, met_dir, windows, progress):
        """Prepare one meteorology window at a time while running ready periods."""
        executable = Path(conf["executable"]).resolve()
        filedates = executable.parent / "filedates.exe"
        bdyfiles = executable.parent.parent / "bdyfiles"
        if not bdyfiles.is_dir():
            raise FileNotFoundError(f"HYSPLIT bdyfiles が見つかりません: {bdyfiles}")

        run_id = datetime.now(JST).strftime("%Y%m%dT%H%M%SJST") + "_" + uuid.uuid4().hex[:6]
        run_root = root / "runs" / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        observation_bytes = Path(conf["csv"]).read_bytes()
        (run_root / "observations.csv").write_bytes(observation_bytes)
        period_manifest = [
            {
                "period": index,
                "window_utc": [window.start.isoformat(), window.end.isoformat()],
                "window_jst": [window.start.astimezone(JST).isoformat(),
                               window.end.astimezone(JST).isoformat()],
                "status": "waiting_for_meteorology",
                "meteorology": [],
                "results_csv": None,
            }
            for index, window in enumerate(windows, 1)
        ]
        metadata = {
            "settings": conf,
            "meteorology": [],
            "observation_csv_copy": "observations.csv",
            "keep_meteorology_sources": bool(conf.get("keep_met_sources", False)),
            "observation_csv_sha256": hashlib.sha256(observation_bytes).hexdigest(),
            "start_count": len(points),
            "execution_count": len(groups),
            "utc_bounds": [earliest.isoformat(), latest.isoformat()],
            "run_id_timezone": "JST (UTC+09:00)",
            "group_id_timezone": "JST (UTC+09:00)",
            "required_windows_utc": [
                [window.start.isoformat(), window.end.isoformat()]
                for window in windows
            ],
            "required_windows_jst": [
                [window.start.astimezone(JST).isoformat(),
                 window.end.astimezone(JST).isoformat()]
                for window in windows
            ],
            "meteorology_by_window": [],
            "periods": period_manifest,
            "arl_times_verified": False,
            "warning": (
                "Time checks do not verify ARL grid extent, vertical fields "
                "or physical correctness."
            ),
            "results_csv_format": "one CSV per meteorological period",
        }
        if provider in ("era5_cds", "arco_era5"):
            metadata["era5_source_archive"] = {
                "horizontal_domain": (
                    "Northern Hemisphere 0-90 deg N; all longitudes; "
                    "ERA5 0.25-degree grid"
                ),
                "requested_north_west_south_east": list(NORTHERN_HEMISPHERE_BBOX),
                "pressure_levels_hpa": [int(level) for level in PRESSURE_LEVELS],
                "pressure_level_count": len(PRESSURE_LEVELS),
                "source_pressure_variables": (
                    list(HYSPLIT_PRESSURE_FIELDS
                         if conf["era5_field_profile"] == "hysplit"
                         else PRESSURE_FIELDS)
                    if provider == "era5_cds"
                    else (
                        list(ARCO_PRESSURE_FIELDS)
                        + ["relative_humidity or specific_humidity (source-dependent)"]
                        if conf["era5_field_profile"] == "hysplit"
                        else "all spatial variables available in the selected ARCO Zarr"
                    )
                ),
                "source_surface_variables": (
                    list(SURFACE_FIELDS) if provider == "era5_cds"
                    else (
                        list(ARCO_SURFACE_FIELDS)
                        if conf["era5_field_profile"] == "hysplit"
                        else "all spatial variables available in the selected ARCO Zarr"
                    )
                ),
                "era5_field_profile": conf["era5_field_profile"],
                "hysplit_ARL_pressure_layers": len(PRESSURE_LEVELS),
                "hysplit_ARL_surface_layers": 1,
                "separate_original_and_model_inputs": True,
            }

        manifest_path = run_root / "manifest.json"
        manifest_lock = threading.Lock()

        def save_manifest():
            with manifest_lock:
                partial = manifest_path.with_name("manifest.json.part")
                partial.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                partial.replace(manifest_path)

        def update_period(index, **changes):
            with manifest_lock:
                period_manifest[index - 1].update(changes)
                partial = manifest_path.with_name("manifest.json.part")
                partial.write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                partial.replace(manifest_path)

        save_manifest()
        state_lock = threading.Lock()
        states = {
            window: {
                "folders": [], "futures": [], "results": [], "expected": 0,
                "submission_done": False, "export_queued": False,
                "exported": None, "error": None,
            }
            for window in windows
        }
        reports = {}
        ready_meteorology = {}
        completed_groups = 0
        worker_count = int(conf["effective_workers"])
        work_budget = WorkBudget(worker_count)
        run_pool = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="hysplit-period",
        )
        export_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="period-csv",
        )

        def finish_period(window):
            index = windows.index(window)
            with state_lock:
                state = states[window]
                results = list(state["results"])
                folders = list(state["folders"])
            failed = [folder for folder, success in results if not success]
            if failed:
                error = RuntimeError(
                    f"{len(failed)} HYSPLIT groups failed in period {index + 1}"
                )
                with state_lock:
                    states[window]["error"] = error
                update_period(
                    index + 1, status="failed_hysplit",
                    failed_groups=[folder.name for folder in failed],
                )
                progress.complete(
                    f"期間 {index + 1}/{len(windows)}：計算失敗のためCSVなし"
                )
                self._notify(
                    f"Period {index + 1}/{len(windows)} had failed HYSPLIT groups; "
                    "its CSV was not written. Other periods continue."
                )
                return
            try:
                exported = export_run_csv(
                    run_root,
                    height_reference=conf["height_ref"],
                    group_folders=folders,
                    selected_diagnostics=conf["met_diagnostics"],
                    output_name=period_csv_name(index + 1, window),
                )
            except Exception as exc:
                with state_lock:
                    states[window]["error"] = exc
                update_period(index + 1, status="failed_csv_export", error=str(exc))
                progress.complete(
                    f"期間 {index + 1}/{len(windows)}：CSV出力失敗"
                )
                self._notify(f"Period {index + 1}/{len(windows)} CSV export failed: {exc}")
                return
            with state_lock:
                states[window]["exported"] = exported
            update_period(
                index + 1,
                status="complete",
                results_csv=exported.path.name,
                results_trajectories=exported.trajectories,
                results_endpoints=exported.endpoints,
                results_missing_terrain_endpoints=exported.missing_terrain_endpoints,
            )
            progress.complete(
                f"期間 {index + 1}/{len(windows)}：CSV出力完了"
            )
            self._notify(
                f"Period {index + 1}/{len(windows)} CSV: {exported.path.name} | "
                f"{exported.trajectories} trajectories, {exported.endpoints} endpoints"
            )

        def maybe_queue_export(window):
            should_queue = False
            with state_lock:
                state = states[window]
                if (state["submission_done"]
                        and len(state["results"]) == state["expected"]
                        and not state["export_queued"]):
                    state["export_queued"] = True
                    should_queue = True
            if should_queue:
                export_pool.submit(finish_period, window)

        def on_group_done(window, folder, future):
            nonlocal completed_groups
            try:
                result = future.result()
            except Exception as exc:
                result = (folder, False)
                self._notify(f"HYSPLIT worker error in {folder.name}: {exc}")
            with state_lock:
                states[window]["results"].append(result)
                completed_groups += 1
                completed = completed_groups
            progress.complete(
                f"HYSPLIT流跡線 {completed}/{len(groups)} グループ完了"
            )
            maybe_queue_export(window)

        def run_group_with_budget(folder):
            with work_budget.trajectory_slot():
                return execute_group(executable, folder, self._notify)

        def on_window_ready(window, met_paths):
            index = windows.index(window)
            label = f"期間 {index + 1}/{len(windows)}"
            progress.start(f"{label}：気象データ時刻を検証")
            report = check_coverage(
                list(met_paths), window.start, window.end, filedates,
                max_gap_hours=1.0 if provider in ("era5_cds","arco_era5") else 6.0,
            )
            for message in report.messages:
                self._notify(f"Meteorology {index + 1}/{len(windows)}: {message}")
            if provider in ("era5_cds", "arco_era5") and not report.verified:
                raise RuntimeError(
                    f"ERA5 period {index + 1}: native ARL time coverage unverified"
                )
            reports[window] = report
            ready_meteorology[window] = tuple(met_paths)
            progress.complete(f"{label}：気象データ時刻検証完了")
            update_period(
                index + 1,
                status="starting_trajectories",
                meteorology=[str(path) for path in met_paths],
                arl_times_verified=report.verified,
                arl_times_first=report.first.isoformat() if report.first else None,
                arl_times_last=report.last.isoformat() if report.last else None,
            )

            period_groups = [
                group for group in groups
                if find_window(windows, group.when, int(conf["hours"])) == window
            ]
            if not period_groups:
                raise RuntimeError(f"No HYSPLIT groups belong to {window}")
            folders = [
                write_group(
                    group, run_root,
                    files_for_window(met_paths, TimeWindow(group.when-timedelta(hours=int(conf["hours"])),group.when))
                    if provider in ("era5_cds","arco_era5") else list(met_paths),
                    int(conf["hours"]),
                    float(conf["internal_model_top_m_agl"]), conf["height_ref"],
                    bdyfiles, diagnostics=conf["met_diagnostics"],
                    output_interval_minutes=int(conf["output_interval_minutes"]),
                    integration_step_minutes=int(conf["integration_step_minutes"]),
                    single_grid=provider in ("era5_cds","arco_era5","gdas1","gfs0p25"),
                )
                for group in period_groups
            ]
            with state_lock:
                state = states[window]
                state["folders"] = folders
                state["expected"] = len(folders)
                state["submission_done"] = False
            progress.start(
                f"{label}：HYSPLIT計算を開始（{len(folders)}グループ）"
            )
            self._notify(
                f"Period {index + 1}/{len(windows)} meteorology verified; "
                f"starting {len(folders)} HYSPLIT groups while the next period downloads."
            )
            for folder in folders:
                future = run_pool.submit(run_group_with_budget, folder)
                with state_lock:
                    state["futures"].append(future)
                future.add_done_callback(
                    lambda done, _window=window, _folder=folder:
                    on_group_done(_window, _folder, done)
                )
            with state_lock:
                state["submission_done"] = True
            maybe_queue_export(window)

        preparation_error = None
        prepared = None
        try:
            prepared = prepare_meteorology(
                provider, windows, met_dir, bbox, action="run",
                local_met=conf["local_met"],
                hysplit_executable=executable,
                notify=self._notify,
                progress=progress,
                era5_field_profile=conf["era5_field_profile"],
                keep_sources=bool(conf.get("keep_met_sources", False)),
                on_window_ready=on_window_ready,
                conversion_slot=work_budget.conversion_slot,
            )
        except Exception as exc:
            preparation_error = exc
        finally:
            run_pool.shutdown(wait=True)
            export_pool.shutdown(wait=True)

        period_errors = []
        exports = []
        for index, window in enumerate(windows, 1):
            with state_lock:
                state = states[window]
                error = state["error"]
                exported = state["exported"]
                results = list(state["results"])
                submission_done = state["submission_done"]
            if error:
                period_errors.append(f"period {index}: {error}")
            if exported:
                exports.append(exported)
            if not submission_done:
                period_errors.append(f"period {index}: meteorology was not ready")
            if len(results) != state["expected"]:
                period_errors.append(f"period {index}: not all HYSPLIT groups finished")

        meteorology_by_window = {
            window: ready_meteorology.get(window, ()) for window in windows
        }
        metadata["meteorology"] = [
            str(path)
            for window in windows for path in meteorology_by_window[window]
        ]
        metadata["meteorology_by_window"] = [
            {
                "window_utc": [window.start.isoformat(), window.end.isoformat()],
                "files": [str(path) for path in meteorology_by_window[window]],
            }
            for window in windows if meteorology_by_window[window]
        ]
        metadata["arl_times_verified"] = (
            len(reports) == len(windows) and all(report.verified for report in reports.values())
        )
        metadata["arl_times_first"] = min(
            (report.first for report in reports.values() if report.first), default=None
        ).isoformat() if any(report.first for report in reports.values()) else None
        metadata["arl_times_last"] = max(
            (report.last for report in reports.values() if report.last), default=None
        ).isoformat() if any(report.last for report in reports.values()) else None
        metadata["results_csv_by_period"] = [
            {
                "period": index,
                "file": exported.path.name,
                "trajectories": exported.trajectories,
                "endpoints": exported.endpoints,
            }
            for index, window in enumerate(windows, 1)
            for exported in [states[window]["exported"]]
            if exported is not None
        ]
        metadata["results_trajectories"] = sum(item.trajectories for item in exports)
        metadata["results_endpoints"] = sum(item.endpoints for item in exports)
        metadata["results_missing_terrain_endpoints"] = sum(
            item.missing_terrain_endpoints for item in exports
        )
        metadata["output_interval_minutes"] = int(conf["output_interval_minutes"])
        metadata["integration_step_minutes"] = int(conf["integration_step_minutes"])
        metadata["integration_step_control"] = (
            "automatic" if conf["integration_step_minutes"] == "0" else "fixed"
        )
        metadata["meteorological_output"] = [
            {
                "setup_flag": option.flag,
                "tdump_label": option.tdump_label,
                "csv_column": option.csv_column,
                "unit": option.unit,
            }
            for option in selected_variables(conf["met_diagnostics"])
        ]
        metadata["results_height_convention"] = {
            "tdump": "KAGL=1 (AGL meters)",
            "terrain": "TM_TERR=1 (model-derived TERR_MSL)",
            "msl_estimate": "trajectory_height_agl_m + terrain_elevation_msl_m",
            "geopotential_estimate": "R*h/(R+h), R=6371008.8 m, spherical approximation",
            "not_measured_era5_geopotential": True,
        }
        metadata["run_status"] = (
            "complete" if not preparation_error and not period_errors else "partial_or_failed"
        )
        if preparation_error:
            metadata["error"] = str(preparation_error)
        elif period_errors:
            metadata["error"] = "; ".join(period_errors)
        save_manifest()

        if preparation_error or period_errors:
            detail = str(preparation_error) if preparation_error else "; ".join(period_errors)
            raise RuntimeError(
                f"期間別計算が完了しませんでした: {detail}. "
                f"完了済み期間のCSVは {run_root} に残っています。"
            ) from preparation_error

        self._notify(
            f"FINISHED: {len(groups)} groups successful. "
            f"{len(exports)} period CSVs: {run_root}"
        )
        progress.start("オフライン地図とGeoJSONを作成中")
        try:
            page, geojson, shown, total = create_map(run_root)
            self._notify(f"Map: {page} / GeoJSON: {geojson} ({shown}/{total} trajectories)")
            progress.complete("地図・GeoJSON作成完了")
        except Exception as exc:
            self._notify(f"Map not generated: {exc}")
            progress.complete("地図の生成はスキップ（期間別CSVは完成）")

        # Confirm the compact rerun bundle before deleting per-group files.
        # A later trajectory rerun needs the original starts, settings, and
        # verified ARL files; source NetCDF/GRIB archives are optional.
        observation_copy = run_root / str(metadata["observation_csv_copy"])
        if (not observation_copy.is_file()
                or hashlib.sha256(observation_copy.read_bytes()).hexdigest()
                != metadata["observation_csv_sha256"]):
            raise RuntimeError(
                "再計算用の観測CSV控えが見つからないか、チェックサムが一致しません。"
                "作業ファイルは削除しませんでした。"
            )
        result_paths = [Path(item.path).resolve() for item in exports]
        if not result_paths or any(
            not path.is_file() or path.stat().st_size == 0
            for path in result_paths
        ):
            raise RuntimeError(
                "期間別の結果CSVが揃っていないため、作業ファイルは削除しませんでした。"
            )
        arl_paths = list(dict.fromkeys(
            Path(path).resolve() for path in metadata["meteorology"]
        ))
        if not arl_paths or any(
            not path.is_file() or path.stat().st_size == 0
            for path in arl_paths
        ):
            raise RuntimeError(
                "再計算用の気象ARLが揃っていないため、作業ファイルは削除しませんでした。"
            )
        arl_provenance = []
        missing_arl_provenance = []
        if provider in ("era5_cds", "arco_era5"):
            for arl_path in arl_paths:
                provenance_path = arl_path.with_suffix(".json")
                try:
                    provenance = json.loads(
                        provenance_path.read_text(encoding="utf-8")
                    )
                    valid_provenance = (
                        str(provenance.get("validation", "")).startswith(
                            "NOAA Windows filedates"
                        )
                        and int(provenance.get("time_count", 0) or 0) > 0
                    )
                except (OSError, UnicodeError, ValueError, TypeError):
                    valid_provenance = False
                if not valid_provenance:
                    missing_arl_provenance.append(str(provenance_path))
                    self._notify(
                        f"ARLのJSON検証記録がありません: {provenance_path}。"
                        "ARL本体のfiledates検証結果を使い、計算結果の保存を続けます。"
                    )
                else:
                    arl_provenance.append(provenance_path)
        metadata["recalculation_assets"] = {
            "ready": True,
            "observation_csv": observation_copy.name,
            "settings_and_periods": manifest_path.name,
            "meteorology_arl": [str(path) for path in arl_paths],
            "meteorology_validation_records": [
                str(path) for path in arl_provenance
            ],
            "meteorology_validation_records_missing": missing_arl_provenance,
            "result_csvs": [path.name for path in result_paths],
            "source_netcdf_grib_required_for_trajectory_rerun": False,
        }
        save_manifest()

        group_folders = [
            folder for window in windows for folder in states[window]["folders"]
        ]
        run_cleanup = cleanup_successful_run(
            run_root, group_folders, notify=self._notify,
        )
        metadata["run_working_files_cleanup"] = run_cleanup
        if provider not in ("local","era5_cds","arco_era5"):
            cache_cleanup = cleanup_successful_meteorology(
                met_dir,
                (path for window in windows
                 for path in meteorology_by_window.get(window, ())),
                keep_sources=bool(conf.get("keep_met_sources", False)),
                notify=self._notify,
            )
            metadata["meteorology_intermediate_cleanup"] = cache_cleanup
            metadata["meteorology_cache_retained"] = True
            metadata["meteorology_sources_retained"] = bool(
                conf.get("keep_met_sources", False)
            )
        else:
            metadata["meteorology_cache_retained"] = True
            metadata["meteorology_sources_retained"] = (
                True if provider=="local" else
                bool(conf.get("keep_met_sources",False) or conf.get("era5_field_profile")=="allvars")
            )
            if provider in ("era5_cds","arco_era5"):
                metadata["existing_meteorology_sources_preserved"]=True
        save_manifest()
        self.events.put(("done", str(run_root)))

    def show_help(self):
        messagebox.showinfo(
            "入力の説明",
            "【観測CSV（JST）】\n"
            "時刻, 緯度, 経度, 高度m の4列。ヘッダーあり・なし両対応。\n"
            "例: 2026/10/10 09:00,35,135,500\n"
            "タイムゾーン無指定は日本時間、明示的な+09:00・Zはその指定を優先。\n\n"
            "秒は最も近い分に丸めます（30秒以上は切り上げ）。\n\n"
            "【水平ずらし】\n"
            "0kmは観測位置のみ。5kmは中心・上下左右・斜めの9地点。\n"
            "斜めも中心から5kmです。\n\n"
            "【観測時刻の前後】\n"
            "0分は観測時刻のみ。30分は前30分・観測時刻・後30分の3通り。\n\n"
            "【出力間隔と計算時間刻み】\n流跡線を保存する間隔は標準10分です。"
            "後方24時間・10分間隔なら通常145地点を出力します。\n"
            "計算時間刻みは通常DELT=0で自動設定します。詳細設定でのみ固定値を使用できます。\n\n"
            "【後方計算時間】\nCSVの時刻から何時間過去へ遡るか。\n\n"
            "【モデル上端】\n空欄の場合、ERA5全37気圧面では50,000m AGL、"
            "他のデータ源では25,000m AGLを基準とし、CSV最大高度"
            "+1,000mよりも高い値を使用します。"
            "直接指定する場合はCONTROLのm AGLを入力します。\n"
            "上端に達すると流跡線が終了します。"
            "25kmを超える指定ではHYSPLIT内部の鉛直スケーリングも変わります。\n\n"
            "【同時計算プロセス数】\n同時に実行するWindows版HYSPLITの数。"
            "流跡線の総本数ではありません。\n\n"
            "【出力する気象変数】\n温位・気温・湿度・水蒸気混合比・東西風・南北風・"
            "混合層高度をチェックで選択します。\n"
            "気圧・地表標高は常時出力します。\n"
            "比湿は湿潤空気1kg当たり、水蒸気混合比は乾燥空気1kg当たりのg/kgです。\n\n"
            "【出力】\n気象期間ごとにTrajectory_period_*.csvを出力。"
            "標高、AGL高度、推定MSL高度、推定ジオポテンシャル高度を別列で出力。\n\n"
            "【データソース】\n"
            "GDAS1/GFS: NOAA ARLを自動取得しWindows上で計算。\n"
            "ローカルARL: 保存済みARLを使用。\n"
            "ERA5(CDS): GRIB1をWindowsのgrib2arl.exeで変換して計算（試験的）。\n"
            "ERA5(Google ARCO): NetCDF→GRIB1→ARLをWindowsで変換（試験的）。"
        )

    def plot_results(self):
        initial = self.last_run or (Path(self.v["root"].get()) / "runs")
        folder = filedialog.askdirectory(title="HYSPLITの実行結果フォルダ (runs/実行ID)",
                                          initialdir=str(initial))
        if not folder:
            return
        try:
            page, geojson, shown, total = create_map(Path(folder))
            self._log(f"地図: {page} / GeoJSON: {geojson} ({shown}/{total} trajectories)")
            webbrowser.open(page.resolve().as_uri())
        except (OSError, ValueError) as exc:
            messagebox.showerror("地図生成エラー", str(exc))

    def _poll(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "log":
                    self._log(payload)
                elif event == "progress":
                    self._apply_progress(payload)
                elif event in ("done", "error"):
                    self.busy = False
                    self._set_file_progress(False)
                    if event == "error":
                        self.progress_status_var.set("処理失敗：" + str(payload)[:150])
                    else:
                        self.progress_status_var.set("完了")
                    for button in self.buttons:
                        button.configure(state="normal")
                    if event == "done":
                        if payload:
                            self.last_run = Path(payload)
                        self._log("完了しました。")
                    else:
                        self._log("ERROR: " + payload)
                        messagebox.showerror("処理エラー", payload)
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    require_native_windows()
    App().mainloop()
