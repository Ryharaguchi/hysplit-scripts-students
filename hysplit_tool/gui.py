"""Simple Windows-oriented Tkinter UI; no external GUI framework required."""

from __future__ import annotations

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
from .core import (execute_groups, expand_starts, fixed_starts, group_starts,
                   load_starts_csv, parse_heights, parse_time_offsets,
                   parse_utc, parse_xy_offsets, write_group)
from .era5 import download_cds, download_google_arco
from .era5_convert import convert_cds_to_arl
from .plot import create_map
from .validation import check_coverage
from .meteo import download_noaa, local_arl_files, plan_archives

PROVIDERS = {
    "GDAS1 | NOAA 全球1°・ARL（推奨）": "gdas1",
    "GFS 0.25° | NOAA 全球・ARL（大容量）": "gfs0p25",
    "手元のARLファイルを使用": "local",
    "ERA5 | Copernicus CDS（外部api2arlで変換・実行）": "era5_cds",
    "ERA5 | Google ARCO（取得のみ・NetCDF）": "arco_era5",
}
UTC = timezone.utc


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"HYSPLIT Batch Trajectories {__version__}")
        self.geometry("920x760")
        self.minsize(760, 620)
        self.events = queue.Queue()
        self.busy = False
        self.last_run = None
        default_time = (datetime.now(UTC) - timedelta(days=14)).strftime("%Y-%m-%d 00:00")
        defaults = {
            "executable": r"C:\hysplit\exec\hyts_std.exe",
            "root": str(Path.home() / "HYSPLIT-batch"),
            "mode": "manual", "csv": "",
            "start": default_time, "end": default_time, "interval": "6",
            "lat": "35.0", "lon": "135.0", "heights": "500:10000:100",
            "xy": "0,0;5,0;-5,0;0,5;0,-5", "dt": "-1,0,1",
            "hours": "48", "top": "20000", "height_ref": "MSL",
            "workers": "2", "max_points": "100",
            "provider": next(iter(PROVIDERS)), "local_met": "", "bbox": "",
            "api2arl": "", "filedates": r"C:\\hysplit\\exec\\filedates.exe",
            "map_limit": "500",
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

        bases = self._group(setup_content, "開始点（UTC、緯度・経度、標高）")
        modes = ttk.Frame(bases)
        modes.grid(row=0, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Radiobutton(modes, text="固定地点 × 高度 × 時刻", variable=self.v["mode"], value="manual").pack(side="left", padx=4)
        ttk.Radiobutton(modes, text="観測CSV（地点ごとに位置・時刻・高度）", variable=self.v["mode"], value="csv").pack(side="left", padx=4)
        self._entry(bases, "観測CSV (任意)", "csv", 1, 65, self._pick_csv)
        self._entry(bases, "開始UTC (YYYY-MM-DD HH:MM)", "start", 2)
        self._entry(bases, "終了UTC (同時刻可)", "end", 3)
        self._entry(bases, "時刻間隔 [h]", "interval", 4)
        self._entry(bases, "緯度 [°N]", "lat", 5)
        self._entry(bases, "経度 [°E]", "lon", 6)
        self._entry(bases, "高度[m] (例 500:10000:100)", "heights", 7)

        settings = self._group(setup_content, "感度解析・実行設定")
        self._entry(settings, "水平ずらし (東km,北km;...)", "xy", 0, 60)
        self._entry(settings, "開始時刻ずらし [h] (例 -1,0,1)", "dt", 1)
        self._entry(settings, "後方計算時間 [h]", "hours", 2)
        self._entry(settings, "モデル上端 [m]", "top", 3)
        ttk.Label(settings, text="入力高度の基準").grid(row=4, column=0, sticky="w", padx=5, pady=4)
        ttk.Combobox(settings, textvariable=self.v["height_ref"], values=["AGL", "MSL"],
                     state="readonly", width=10).grid(row=4, column=1, sticky="w", padx=5)
        self._entry(settings, "並列実行数 (1～32)", "workers", 5)
        self._entry(settings, "1実行当たり最大開始点数", "max_points", 6)

        source = self._group(meteo, "データソースの選択")
        ttk.Label(source, text="気象データ").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        ttk.Combobox(source, textvariable=self.v["provider"], values=list(PROVIDERS),
                     state="readonly", width=59).grid(row=0, column=1, sticky="ew", padx=5)
        self._entry(source, "ローカルARLファイル (複数選択可)", "local_met", 1, 55, self._pick_arl)
        self._entry(source, "ERA5の領域 N,W,S,E (空欄は自動)", "bbox", 2, 45)
        self._entry(source, "api2arl実行ファイル (WSLは wsl:/...)", "api2arl", 4, 65)
        self._entry(source, "NOAA filedates 実行ファイル", "filedates", 5, 65, self._pick_filedates)
        self._entry(source, "地図表示の最大流跡線数", "map_limit", 6)
        ttk.Label(source, text="ERA5の領域は開始点周辺の推定値です。後方経路を必ず含むとは限りません。",
                  foreground="#855000").grid(row=3, column=0, columnspan=3, sticky="w", padx=5)

        notes = self._group(meteo, "対応状況と重要事項")
        for line in [
            "NOAA GDAS1（全球1°）: ARLを自動取得して実行可能。週ファイルは約0.6GB。",
            "NOAA GFS 0.25°（全球）: ARLを自動取得して実行可能。日ファイル約3GB以上。",
            "ローカルARL: 手持ちのARL形式気象ファイルを使用して実行可能。",
            "ERA5 CDS: ecCodesと外部api2arlがあればGRIB→ARLに変換。filedatesによる時刻検証必須。",
            "Google ARCO ERA5: Google Cloud公開ZarrからNetCDFを抽出。ARL変換は未実装。",
            "ERA5は変数・座標変換を伴います。変換後もNOAA基準計算との照合が必要です。",
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
        self._log("UTCで入力してください。初めに『計算内容をプレビュー』で計算数と気象データを確認してください。")

    def _pick_executable(self):
        value = filedialog.askopenfilename(title="hyts_std.exe", filetypes=[("EXE", "*.exe"), ("All files", "*.*")])
        if value:
            self.v["executable"].set(value)

    def _pick_filedates(self):
        value = filedialog.askopenfilename(title="NOAA filedates", filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if value:
            self.v["filedates"].set(value)

    def _pick_root(self):
        value = filedialog.askdirectory(title="HYSPLIT作業フォルダ")
        if value:
            self.v["root"].set(value)

    def _pick_csv(self):
        value = filedialog.askopenfilename(title="開始点CSV", filetypes=[("CSV", "*.csv"), ("All files", "*.*")])
        if value:
            self.v["csv"].set(value)
            self.v["mode"].set("csv")

    def _pick_arl(self):
        values = filedialog.askopenfilenames(title="ARLファイルを複数選択")
        if values:
            self.v["local_met"].set(";".join(values))
            self.v["provider"].set("手元のARLファイルを使用")

    def _log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", str(line) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _data(self):
        """Collect and validate everything in the GUI thread before a worker starts."""
        conf = {k: var.get().strip() for k, var in self.v.items()}
        if conf["mode"] == "csv":
            bases = load_starts_csv(Path(conf["csv"]))
        else:
            bases = fixed_starts(parse_utc(conf["start"]), parse_utc(conf["end"]),
                                 float(conf["interval"]), float(conf["lat"]),
                                 float(conf["lon"]), parse_heights(conf["heights"]))
        points = expand_starts(bases, parse_xy_offsets(conf["xy"]), parse_time_offsets(conf["dt"]))
        groups = group_starts(points, max_points=int(conf["max_points"]))
        back_hours = int(conf["hours"])
        top = float(conf["top"])
        if back_hours <= 0 or back_hours > 9999 or top <= 0 or any(p.height_m >= top for p in points):
            raise ValueError("後方時間 (1..9999 h) とモデル上端（初期高度より高い値）を確認してください。")
        if conf["height_ref"] not in ("AGL", "MSL"):
            raise ValueError("高度の基準は AGL または MSL です。")
        if not 1 <= int(conf["workers"]) <= 32:
            raise ValueError("並列数は1～32です。")
        if not 1 <= int(conf["map_limit"]) <= 5000:
            raise ValueError("地図表示上限は1～5000です。")
        earliest = min(p.when for p in points) - timedelta(hours=back_hours)
        latest = max(p.when for p in points)
        provider = PROVIDERS[conf["provider"]]
        if conf["bbox"]:
            bbox = tuple(float(v.strip()) for v in conf["bbox"].split(","))
            if len(bbox) != 4:
                raise ValueError("ERA5領域は N,W,S,E の4値です")
        else:
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
            self._log(f"プレビュー: {len(points):,} 本 / {len(groups):,} HYSPLIT実行 / "
                      f"対象UTC {earliest:%Y-%m-%d %H:%M} ～ {latest:%Y-%m-%d %H:%M}")
            if provider in ("gdas1", "gfs0p25"):
                plan = plan_archives(provider, earliest, latest)
                self._log(f"NOAA {provider}: {len(plan)} ファイル取得・キャッシュ利用予定: " +
                          ", ".join(p.name for p in plan[:8]) + (" ..." if len(plan) > 8 else ""))
                if provider == "gfs0p25":
                    self._log("注意: GFS 0.25° は1日あたり約3GB以上（保存領域・帯域を確認）。")
            elif provider == "era5_cds":
                self._log(f"ERA5 CDS: bbox={bbox}. 外部api2arl・filedates設定時は変換して実行できます。")
            elif provider == "arco_era5":
                self._log(f"Google ARCO: NetCDFの取得のみ / ARL自動変換未対応 / bbox={bbox}")
            else:
                self._log("ローカルARLファイル: 実行前にファイル存在確認します。")
        except Exception as exc:
            messagebox.showerror("入力エラー", str(exc))

    def start_task(self, action):
        if self.busy:
            return
        try:
            data = self._data()
            if action == "run" and data[5] == "arco_era5":
                raise ValueError("Google ARCOのNetCDF→ARL変換は未対応です。CDSまたはNOAAを選択してください。")
            if action == "run" and data[5] == "era5_cds":
                if not data[0]["api2arl"]:
                    raise ValueError("ERA5実行にはNOAA api2arlのパスが必要です。")
                if not Path(data[0]["filedates"]).is_file():
                    raise ValueError("ERA5実行にはNOAA filedatesによる変換結果の時刻検証が必要です。")
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
                self._notify(f"ERA5 GRIB downloaded ({len(results)} files).")
                if action == "run":
                    met_paths = convert_cds_to_arl(
                        results, met_dir / "arl", earliest, latest,
                        converter=conf["api2arl"], filedates=Path(conf["filedates"]),
                        notify=self._notify)
                else:
                    self._notify("ERA5 GRIB ready. api2arl conversion is run only when starting HYSPLIT.")
                    met_paths = []
            elif provider == "arco_era5":
                result = download_google_arco(earliest, latest, met_dir, bbox, notify=self._notify)
                self._notify(f"ARCO ERA5 saved: {result}. NetCDF is NOT HYSPLIT ARL input.")
                met_paths = []
            else:
                raise ValueError(f"Unsupported meteo source: {provider}")
            if action == "run":
                coverage = check_coverage(met_paths, earliest, latest, Path(conf["filedates"]))
                for message in coverage.messages:
                    self._notify("Meteorology: " + message)
                if provider == "era5_cds" and not coverage.verified:
                    raise RuntimeError("ERA5 must pass ARL time coverage validation before running")
                run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
                run_root = root / "runs" / run_id
                run_root.mkdir(parents=True, exist_ok=False)
                metadata = {"settings": conf, "meteorology": [str(p) for p in met_paths],
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
                                       float(conf["top"]), conf["height_ref"], bdyfiles)
                           for g in groups]
                self._notify(f"Prepared {len(folders)} runs in {run_root}")
                results = execute_groups(Path(conf["executable"]), folders,
                                         int(conf["workers"]), notify=self._notify)
                failures = sum(not success for _, success in results)
                self._notify(f"FINISHED: {len(results)-failures}/{len(results)} successful. Output: {run_root}")
                try:
                    page, geojson, shown, total = create_map(
                        run_root, max_trajectories=int(conf["map_limit"]))
                    self._notify(f"Map: {page} / GeoJSON: {geojson} ({shown}/{total} trajectories)")
                except (FileNotFoundError, ValueError, OSError) as exc:
                    self._notify(f"Map not generated: {exc}")
                self.events.put(("done", str(run_root)))
            else:
                self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", str(exc)))
            self._notify(traceback.format_exc(limit=3))

    def plot_results(self):
        initial = self.last_run or (Path(self.v["root"].get()) / "runs")
        folder = filedialog.askdirectory(title="HYSPLITの実行結果フォルダ (runs/実行ID)",
                                          initialdir=str(initial))
        if not folder:
            return
        try:
            page, geojson, shown, total = create_map(Path(folder), max_trajectories=int(self.v["map_limit"].get()))
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
    App().mainloop()
