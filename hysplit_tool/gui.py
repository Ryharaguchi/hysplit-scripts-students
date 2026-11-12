"""Simple Windows-oriented Tkinter UI; no external GUI framework required."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (JST, automatic_model_top, execute_groups, expand_observations,
                   group_starts, load_starts_csv, write_group)
from .era5 import download_cds, download_google_arco
from .native_era5 import convert_cds_grib1_to_arl
from .arco_convert import convert_arco_netcdf_to_arl
from .plot import create_map
from .platform_guard import require_native_windows
from .validation import check_coverage
from .meteo import download_noaa, local_arl_files, plan_archives
from .portable import default_work_root, find_hysplit_executable
from .csv_export import export_run_csv

PROVIDERS = {
    "GDAS1 (1°)": "gdas1",
    "GFS (0.25°)": "gfs0p25",
    "ローカルARL": "local",
    "ERA5 CDS (0.25°)": "era5_cds",
    "ERA5 Google ARCO (0.25°)": "arco_era5",
}
UTC = timezone.utc


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
        detected_executable = discover_native_hysplit()
        work_root = default_work_root()
        defaults = {
            "executable": str(detected_executable), "root": str(work_root),
            "csv": "", "distance_km": "0", "time_shift_minutes": "0",
            "hours": "48", "height_ref": "MSL",
            "workers": "2", "max_points": "100",
            "provider": next(iter(PROVIDERS)), "local_met": "",
        }
        self.v = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        self._widgets()
        self.after(100, self._poll)

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
        ttk.Label(run_settings, text="CSVの高度の基準").grid(row=1, column=0, sticky="w", padx=5)
        ttk.Combobox(run_settings, textvariable=self.v["height_ref"], values=["AGL", "MSL"],
                     state="readonly", width=10).grid(row=1, column=1, sticky="w", padx=5)
        self._entry(run_settings, "同時に動かす計算プロセス数 (1～32)", "workers", 2)
        self._entry(run_settings, "1実行当たり最大開始点数", "max_points", 3)

        source = self._group(meteo, "データソースの選択")
        ttk.Label(source, text="気象データ").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Combobox(source, textvariable=self.v["provider"], values=list(PROVIDERS),
                     state="readonly", width=59).grid(row=0, column=1, sticky="ew", padx=5)
        self._entry(source, "ローカルARLファイル (複数選択可)", "local_met", 1, 55, self._pick_arl)
        ttk.Label(source,
                  text="ERA5取得領域は開始点の周囲から自動設定します。"
                       "長い流跡線が領域外へ出る可能性は残ります。",
                  foreground="#855000").grid(
                      row=2, column=0, columnspan=3, sticky="w", padx=5)

        notes = self._group(meteo, "対応状況と重要事項")
        for line in [
            "GDAS1：全球 約1°格子。NOAAのARLデータを取得して計算。",
            "GFS 0.25°：全球 0.25°格子。NOAAのARLデータを取得して計算。",
            "ローカルARL: 手元のARLファイルで計算します。",
            "ERA5 CDS：0.25°×0.25°の配布格子（大気解析の実効分解能は約31 km）。GRIB1をARL変換（試験的）。",
            "ERA5 Google ARCO：0.25°×0.25°格子。NetCDF→GRIB1→ARL変換（Windows、試験的）。",
            "grib2arl.exeとfiledates.exeはHYSPLITの実行フォルダから自動検出します。",
            "出力：全流跡線を1つのtrajectories.csvに集約。別途オフライン図とGeoJSONも保存。",
            "取得には大量のディスク容量・通信量が必要です。観測期間+後方計算期間を対象にします。",
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
        bases = load_starts_csv(Path(conf["csv"]))
        points = expand_observations(
            bases, float(conf["distance_km"]), float(conf["time_shift_minutes"]))
        groups = group_starts(points, max_points=int(conf["max_points"]))
        back_hours = int(conf["hours"])
        if back_hours <= 0 or back_hours > 9999:
            raise ValueError("後方計算時間は1～9999時間で指定してください。")
        # HYSPLIT CONTROL *requires* a vertical model-top value. Keep it
        # internal, at least 20 km AGL or 1 km above the largest CSV start.
        internal_top = automatic_model_top(points)
        conf["internal_model_top_m_agl"] = str(internal_top)
        if conf["height_ref"] not in ("AGL", "MSL"):
            raise ValueError("高度の基準は AGL または MSL です。")
        if not 1 <= int(conf["workers"]) <= 32:
            raise ValueError("並列数は1～32です。")
        earliest = min(p.when for p in points) - timedelta(hours=back_hours)
        latest = max(p.when for p in points)
        provider = PROVIDERS[conf["provider"]]
        # Auto-compute regional ERA5 subset; long backward paths can exit this area.
        north = min(90.0, max(p.latitude for p in points) + 25)
        south = max(-90.0, min(p.latitude for p in points) - 25)
        west = max(-180.0, min(p.longitude for p in points) - 35)
        east = min(180.0, max(p.longitude for p in points) + 35)
        bbox = (north, west, south, east)
        if not (-90 <= bbox[2] < bbox[0] <= 90 and -180 <= bbox[1] < bbox[3] <= 180):
            raise ValueError("ERA5領域の範囲が不正です。N> S、W< Eで指定してください")
        return conf, points, groups, earliest, latest, provider, bbox

    def preview(self):
        try:
            conf, points, groups, earliest, latest, provider, bbox = self._data()
            self._log(f"プレビュー: CSV {len(load_starts_csv(Path(conf['csv']))):,}行 → "
                      f"{len(points):,}本 / {len(groups):,}回実行")
            self._log(f"必要気象期間(JST): {earliest.astimezone(JST):%Y-%m-%d %H:%M} ～ "
                      f"{latest.astimezone(JST):%Y-%m-%d %H:%M}")
            if provider in ("gdas1", "gfs0p25"):
                plan = plan_archives(provider, earliest, latest)
                self._log(f"NOAA {provider}: {len(plan)} ファイル取得・キャッシュ利用予定: " +
                          ", ".join(p.name for p in plan[:8]) + (" ..." if len(plan) > 8 else ""))
                if provider == "gfs0p25":
                    self._log("注意: GFS 0.25° は1日あたり約3GB以上（保存領域・帯域を確認）。")
            elif provider == "era5_cds":
                self._log(f"ERA5(CDS): bbox={bbox}。GRIB1をWindowsのgrib2arl.exeで変換します。")
            elif provider == "arco_era5":
                self._log(f"Google ARCO: 0.25° NetCDFを取得しWindowsのecCodesでGRIB1化、"
                          f"grib2arl.exeでARLに変換します（試験的）。自動領域={bbox}")
            else:
                self._log("ローカルARLファイル: 実行前にファイル存在確認します。")
        except Exception as exc:
            messagebox.showerror("入力エラー", str(exc))

    def start_task(self, action):
        if self.busy:
            return
        try:
            data = self._data()
            if data[5] == "era5_cds":
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
        self.busy = True
        for button in self.buttons:
            button.configure(state="disabled")
        threading.Thread(target=self._background, args=(action, data), daemon=True).start()

    def _notify(self, message):
        self.events.put(("log", message))

    def _background(self, action, data):
        try:
            conf, points, groups, earliest, latest, provider, bbox = data
            root = Path(conf["root"]).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            met_dir = root / "met" / provider
            if provider in ("gdas1", "gfs0p25"):
                met_paths = download_noaa(provider, earliest, latest, met_dir, notify=self._notify)
            elif provider == "local":
                met_paths = local_arl_files(conf["local_met"])
                self._notify(f"Local ARL: {len(met_paths)} files selected")
            elif provider == "era5_cds":
                results = download_cds(earliest, latest, met_dir, bbox, notify=self._notify)
                self._notify(f"ERA5 GRIB downloaded: {len(results)} files")
                met_paths = convert_cds_grib1_to_arl(
                    results, met_dir / "arl", earliest, latest,
                    hysplit_executable=Path(conf["executable"]), notify=self._notify)
                self._notify(f"ERA5 ARL conversion complete: {len(met_paths)} files")
            elif provider == "arco_era5":
                result = download_google_arco(earliest, latest, met_dir, bbox, notify=self._notify)
                self._notify(f"ARCO ERA5 NetCDF ready: {result}")
                met_paths = convert_arco_netcdf_to_arl(
                    result, met_dir / "arl", earliest, latest,
                    hysplit_executable=Path(conf["executable"]), notify=self._notify)
                self._notify(f"ARCO NetCDF -> ARL complete: {len(met_paths)} files")
            else:
                raise ValueError(f"Unsupported meteo source: {provider}")
            if action == "run":
                filedates = Path(conf["executable"]).resolve().parent / "filedates.exe"
                coverage = check_coverage(met_paths, earliest, latest, filedates)
                for message in coverage.messages:
                    self._notify("Meteorology: " + message)
                if provider in ("era5_cds", "arco_era5") and not coverage.verified:
                    raise RuntimeError("ERA5 must pass ARL time coverage validation before running")
                run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
                run_root = root / "runs" / run_id
                run_root.mkdir(parents=True, exist_ok=False)
                # Keep the exact source observation CSV for scientific provenance.
                observation_bytes = Path(conf["csv"]).read_bytes()
                (run_root / "observations.csv").write_bytes(observation_bytes)
                metadata = {"settings": conf, "meteorology": [str(p) for p in met_paths],
                            "observation_csv_copy": "observations.csv",
                            "observation_csv_sha256": hashlib.sha256(observation_bytes).hexdigest(),
                            "start_count": len(points), "execution_count": len(groups),
                            "utc_bounds": [earliest.isoformat(), latest.isoformat()],
                            "arl_times_verified": coverage.verified,
                            "arl_times_first": coverage.first.isoformat() if coverage.first else None,
                            "arl_times_last": coverage.last.isoformat() if coverage.last else None,
                            "warning": "Time checks do not verify ARL grid extent, vertical fields or physical correctness."}
                (run_root / "manifest.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                # Resolve HYSPLIT's installation-dependent terrain/land-use files.
                # Each separate working directory needs its own ASCDATA.CFG.
                executable = Path(conf["executable"]).resolve()
                bdyfiles = executable.parent.parent / "bdyfiles"
                if not bdyfiles.is_dir():
                    raise FileNotFoundError(
                        f"HYSPLIT bdyfiles が見つかりません: {bdyfiles}. "
                        "HYSPLITの正式なインストールフォルダ内の hyts_std.exe を指定してください。"
                    )
                folders = [write_group(g, run_root, met_paths, int(conf["hours"]),
                                       float(conf["internal_model_top_m_agl"]),
                                       conf["height_ref"], bdyfiles)
                           for g in groups]
                self._notify(f"Prepared {len(folders)} runs in {run_root}")
                results = execute_groups(Path(conf["executable"]), folders,
                                         int(conf["workers"]), notify=self._notify)
                failures = sum(not success for _, success in results)
                if failures:
                    raise RuntimeError(
                        f"{failures} HYSPLIT runs failed. See per-group MESSAGE/run.log; "
                        "no complete batch CSV was written."
                    )
                exported = export_run_csv(run_root, height_reference=conf["height_ref"],
                                          group_folders=folders)
                metadata["results_csv"] = exported.path.name
                metadata["results_trajectories"] = exported.trajectories
                metadata["results_endpoints"] = exported.endpoints
                metadata["results_missing_terrain_endpoints"] = exported.missing_terrain_endpoints
                metadata["results_height_convention"] = {
                    "tdump": "KAGL=1 (AGL meters)",
                    "terrain": "TM_TERR=1 (model-derived TERR_MSL)",
                    "msl_estimate": "trajectory_height_agl_m + terrain_elevation_msl_m",
                    "geopotential_estimate": "R*h/(R+h), R=6371008.8 m, spherical approximation",
                    "not_measured_era5_geopotential": True,
                }
                (run_root / "manifest.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                self._notify(
                    f"Batch CSV: {exported.path} | {exported.trajectories} trajectories, "
                    f"{exported.endpoints} endpoints; "
                    f"{exported.missing_terrain_endpoints} rows missing terrain elevation"
                )
                self._notify(f"FINISHED: {len(results)} groups successful. Output: {run_root}")
                try:
                    page, geojson, shown, total = create_map(
                        run_root)
                    self._notify(f"Map: {page} / GeoJSON: {geojson} ({shown}/{total} trajectories)")
                except (FileNotFoundError, ValueError, OSError) as exc:
                    self._notify(f"Map not generated: {exc}")
                self.events.put(("done", str(run_root)))
            else:
                self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))
            self._notify(traceback.format_exc(limit=3))

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
            "【後方計算時間】\nCSVの時刻から何時間過去へ遡るか。\n\n"
            "【モデル上端】\nHYSPLIT内部で自動設定するため入力は不要です。"
            "20,000m AGL以上、またはCSV最大高度+1,000m以上を確保します。\n\n"
            "【同時計算プロセス数】\n同時に実行するWindows版HYSPLITの数。"
            "流跡線の総本数ではありません。\n\n"
            "【出力】\n一括計算ごとにtrajectories.csvに全軌跡を集約。"
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
                elif event in ("done", "error"):
                    self.busy = False
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
