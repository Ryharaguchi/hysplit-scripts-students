# HYSPLIT Batch Trajectories for Students

Windowsで多数の**後方流跡線（backward trajectories）**を計算するための、Python/Tkinter製GUIと気象データ取得ツールです。ラジオゾンデ専用ではなく、地点・高度・開始時刻の格子状設定と観測CSVの両方に対応しています。

**現時点の完成範囲**: NOAA GDAS1/GFS 0.25°のARL形式データ取得・キャッシュ・HYSPLIT一括実行、ローカルARLの利用、ERA5/CDSのGRIB取得、Google Cloud ARCO ERA5からのNetCDF取得。**ERA5からARLへの変換は未実装**のため、ERA5を選んだ状態ではHYSPLITを実行しません。

## 必要環境

- Windows 10/11、Python 3.10以降（GUIは標準の`tkinter`、NOAA取得には追加Python依存なし）
- インストール済みの[NOAA HYSPLIT Windows版](https://www.arl.noaa.gov/hysplit/getrun-hysplit/)と`hyts_std.exe`および`bdyfiles`フォルダ
- 気象データ保存用ストレージ：GDAS1は週当たり約0.6 GB、GFS 0.25°は日当たり約3 GB以上。長期間では大容量になります
- ERA5 CDS: 別途Copernicus Climate Data Store利用登録、対象データの利用規約への同意、`~/.cdsapirc`設定、およびオプションのPython依存
- Google ARCO ERA5: オプションのPython依存、公開Google Cloudのデータへアクセスできる通信環境。ネットワーク転送料等の条件を事前に確認してください

## 起動

Windowsで、リポジトリをダウンロード/クローンした後、PowerShellで：

```powershell
cd hysplit-scripts-students
py -3 run_gui.py
```

1. 「1. 後方流跡線の設定」でHYSPLITの実行ファイル・保存先・地点・日時（**UTC**）・高度・位置/時間のずらし量を入力します。緯度は北緯正、経度は東経正、水平ずらしは東km・北kmです。
2. 「2. 気象データ」で通常は`GDAS1`を選びます。最初に「計算内容をプレビュー」で本数と必要な気象ファイル数を確認します。
3. 「一括計算を開始」を押すと、必要なARLファイルを自動取得し、同じ開始時刻の点をまとめた`CONTROL`/`SETUP.CFG`/`ASCDATA.CFG`を生成し、複数のHYSPLITプロセスで計算します。`tdump`、`MESSAGE`、`run.log`と各開始点を記した`starts.csv`が保存されます。
4. まず本数を減らした設定で実行を確認し、並列数を段階的に増やしてください。

### 開始点の指定方法

**固定地点**: `開始UTC`、`終了UTC`（同一でもよい）、`時刻間隔[h]`、`緯度・経度`、`高度 [m]`を入力。`500:10000:100`は500～10,000 mを100 m刻み、**96高度**です。`500,1500,3000`のような任意値も可能。

**観測CSV**: `samples/starts.csv`を参照してください。必須列は`time_utc,latitude,longitude,height_m`です。1行が1つの計算開始点となり、高度ごとに異なる位置と時刻を使えます。時刻のタイムゾーン指定がない場合も**UTCとして扱います**（ローカル時刻の自動変換はしません）。`YYYY-MM-DD HH:MM`およびISO 8601のUTC日時を受け付けます。開始分にも対応しています。

どちらの方式も`水平ずらし`、`開始時刻ずらし`を乗算して計算ケースを増やせます。例えば`0,0;5,0;-5,0;0,5;0,-5`と`-1,0,1`を指定すると各元の開始点について15本です。計算本数上限は10万本、`CONTROL`当たりの開始点数上限はGUIで変更できます（初期値100）。同一時刻の点のみ1実行にまとめ、複数時刻では独立した実行を行います。

**高度基準**: `AGL`（地上高）/`MSL`（海抜高度）を明示。`SETUP.CFG`の`KMSL`を適切に設定します。ラジオゾンデのGNSS楕円体高を使用するときは海抜高度への変換が必要です。HYSPLITの出力高度は標準でAGLです。

## 気象データの対応状況

| 選択 | 入手元 | 形式 | ワンクリック計算 | 備考 |
|---|---|---|---|---|
| GDAS1 | NOAA READYアーカイブ | ARL | **対応** | 全球1°、週ファイル、初期推奨 |
| GFS 0.25° | NOAA READYアーカイブ | ARL | **対応** | 全球、日ファイル、大容量 |
| Local ARL | 利用者が指定するファイル | ARL | **対応** | データの形式・期間は別途利用者が確認 |
| ERA5 CDS | Copernicus CDS API | GRIB | **未対応（取得のみ）** | CDS認証とARL変換が必要 |
| Google ARCO ERA5 | Google Cloud公開Zarr | NetCDFに切り出し | **未対応（取得のみ）** | 高層データ含む。ARL変換が必要 |

データ取得は「開始時刻の最小値−後方計算時間」から「開始時刻の最大値」までを基準に計画します。GDAS/GFSでは境界を含むように気象ファイルを追加して取得します。既存ファイルはサイズが0でない限り再取得せず、ダウンロード中は`.part`に保存して完了後に改名します。**ファイル名からの期間推定はデータ本体の有効時間・欠損・鉛直変数の検証ではありません。** `MESSAGE`と`run.log`を確認してください。

### ERA5 CDSを使うとき

```powershell
py -3 -m pip install -r requirements-era5.txt
```

[Copernicus CDS](https://cds.climate.copernicus.eu/)に登録し、APIアクセスの設定を行ってください。GUIの`ERA5 CDS`を選び「気象データを取得」を押すと、指定期間の高層6変数・複数気圧面と地表変数をGRIB形式で保存します。**GRIBはHYSPLITのARL形式ではありません**。必要な変数・地表条件・鉛直面・時間を確認の上、適切な変換ソフトウェアでARL化した後に`Local ARL`を使用してください。現時点では自動変換・物理的な整合性チェックは実装していません。

### Google Cloud ARCO ERA5を使うとき

Google Researchの[ARCO ERA5](https://github.com/google-research/arco-era5)（`gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3`）の気圧面データ・地表データから、指定範囲・期間をNetCDFとして切り出します。GUIで`ERA5領域 N,W,S,E`（緯度・経度の矩形）を指定可能。空欄時には開始点を囲む領域を推定しますが、**過去72時間の流跡線がその領域に収まる保証はありません**。領域はユーザーが十分に広く設定してください。Google CloudのZarrチャンク構造により、狭い領域でも大きなデータ転送が生じることがあります。ARL変換は未対応です。

Google Earth Engineの`ECMWF/ERA5/HOURLY`は地表単一レベル中心のデータで、単独では高層流跡線の気象場として不十分です。代わりにGoogle Cloud公開ARCO ERA5の高層データを対象にしています。

## 保存先

```
<保存フォルダ>/
  met/gdas1/gdas1.oct26.w2   # 再利用可能な気象キャッシュ
  runs/<実行ID>/
    manifest.json            # 設定・ファイル・計算件数
    20261010T0000Z_00000/
      CONTROL                # 各時刻・バッチごとの設定
      SETUP.CFG              # MSL/AGLを設定
      ASCDATA.CFG            # HYSPLITのbdyfiles絶対パス
      starts.csv             # tdump内のtrajectory番号と開始点の対応表
      tdump                  # 後方流跡線出力
      MESSAGE                # HYSPLITの診断メッセージ（HYSPLIT生成）
      run.log                # 標準出力・標準エラー
```

## テスト

```powershell
py -3 -m unittest discover -s tests -v
```

単体テストはHYSPLIT本体や気象APIなしで実行でき、時刻境界、高度分割、オフセット、気象ファイル名の生成、並列実行（POSIXのモック）を検査します。**実際のWindows上の`hyts_std.exe`や本物のERA5 API・Google Cloudへの接続試験はまだ実施していません。** 物理的に正しい結果が得られることを保証するものではなく、NOAA HYSPLIT GUIによる同一条件の基準計算との照合が必要です。

## 公式資料

- [HYSPLITインストール](https://www.arl.noaa.gov/hysplit/getrun-hysplit/)
- [CONTROLファイル仕様](https://hysplit2.arl.noaa.gov/hysplitusersguide/S262.htm)
- [KMSL・高度基準](https://www.ready.noaa.gov/hysplitusersguide/S621.htm)
- [ASCDATA.CFGと境界ファイル](https://ready2.arl.noaa.gov/hysplitusersguide/S444.htm)
- [NOAA READY気象データアーカイブ](https://hysplit2.arl.noaa.gov/archives.php)
- [HYSPLIT気象データ変換の注意点](https://hysplit2.arl.noaa.gov/documents/Tutorial/view/meteo_cnvrt.html)
- [Google Research ARCO ERA5](https://github.com/google-research/arco-era5)
- [Google Earth Engine ERA5 Hourly（地表単一レベル）](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY)

## 制約・今後の改善候補

1. ERA5 GRIB/Zarr/NetCDFからARLへの自動変換・変数/座標検査（データセット別に変換手順の実証が必要）。
2. ARL実ファイルの時刻範囲と変数セットの検査、欠損処理、計算結果へのメタデータ照合。
3. 大容量ダウンロードの進行率、空き容量の事前検査、途中再開、キャンセル。
4. HYSPLIT標準GUIとの照合、同条件でのWindows回帰試験、気象場による感度・解像度評価。

※ このリポジトリはNOAA公式ソフトウェアそのものではなく、`hyts_std.exe`や気象アーカイブのバイナリデータは同梱しません。