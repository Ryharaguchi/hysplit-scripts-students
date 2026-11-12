# HYSPLIT 後方流跡線一括計算（Windows）

**Windowsネイティブ専用。WSL/WSL2・Linux・macOSは実行対象外です。**
計算者が作成した観測CSVから、各行の時刻・緯度・経度・高度を開始条件として、HYSPLITの後方流跡線を大量に計算するTkinter GUIです。NOAA READYのWeb上で数値計算を実行するものではありません。

## 起動

Windows PowerShellから：

```powershell
cd D:\Codex\hysplit-scripts-students
py -3 run_gui.py
```

NOAA HYSPLIT Windows版の`hyts_std.exe`が必要です。既存の`Desktop\backwardtrajectory\HYSPLIT\exec`を自動検出します。作業フォルダの初期値は`D:\Codex\hysplit-batch`です。元のHYSPLITフォルダ、既存の観測データ・出力データは書き換えません。

## 観測CSV（JST）

CSVは**列の名前でなく順序で読み込み**ます。必須の4列は次の順です。

| 列 | 内容 | 入力例 |
|---|---|---|
| 1 | 観測日時（通常JST） | `2026/10/10 09:00` |
| 2 | 緯度（度、北＋） | `35.00` |
| 3 | 経度（度、東＋） | `135.00` |
| 4 | 高度（m） | `500` |

ヘッダーは**あってもなくても可**。ヘッダーの文字列は自由で、2～4列が数字でない行を先頭ヘッダーとして自動判定します。日本語のExcel CSV（CP932/Shift-JIS）とUTF-8（BOM付き含む）を読み込めます。

`samples/starts.csv`（ヘッダーあり）：

```csv
時刻,緯度,経度,高度
2026/10/10 09:00,35,135,500
2026/10/10 09:01,35.01,135.02,600
```

`samples/starts_no_header.csv`（ヘッダーなし）：

```csv
2026/10/10 09:00,35,135,500
2026-10-10 09:01,35.01,135.02,600
```

日時は次の記法を認識します：`YYYY/MM/DD HH:MM`、`YYYY/MM/DD HH:MM:SS`、`YYYY-MM-DD HH:MM[:SS]`、`YYYY.MM.DD HH:MM`、`YYYYMMDDHHMM`、`YYYYMMDDHHMMSS`、ISO 8601（`T`、`Z`、`+09:00`等）。タイムゾーンが省略されると**JST (UTC+09:00)** と解釈します。明示的なタイムゾーンがあればその指定を使います。`JST`/`UTC`末尾表記も使えます。

HYSPLITの開始時刻は分単位なので、CSVに秒が含まれる場合は**30秒未満を切り捨て、30秒以上を次の分に切り上げ**ます。計算で使用する値は内部の`CONTROL`にUTCで記録し、元のCSVも計算フォルダに保存します。

## 位置・時刻の感度解析

GUIの「水平ずらし距離 [km]」は距離1つだけを指定します。

- `0`：観測位置の1地点だけ
- `5`：観測位置＋北・北東・東・南東・南・南西・西・北西にそれぞれ5 km離れた計9地点

**斜めも中心から5 km**です。8方向は北を0°として45°刻みの方位角で、球面大円計算から求めます。高度は変えません。

GUIの「観測時刻の前後に試す [分]」は、各CSV行の観測時刻からの差を指定します。

- `0`：観測時刻のみ
- `30`：観測時刻の30分前・観測時刻・30分後の3時刻

「開始UTC」「終了UTC」「時刻間隔」はありません。**CSVの各行が計算の開始時刻**です。「何時間前まで遡るか [h]」は、例えば`48`なら各開始時刻から過去48時間を計算する設定です。

CSVが6行、水平距離5 km、時刻差30分なら、`6×9×3=162`本の流跡線を計算します。同じ開始時刻の流跡線は可能な範囲で1回のHYSPLIT実行にまとめます。

## 高度

CSVの高度欄をそのまま初期高度に使用します。500～1000 mを100 m間隔で計算する場合は、CSVに500, 600, 700, 800, 900, 1000 mの**6行を作成**してください（位置・時刻は行ごとに独立）。GUIでは高度範囲の入力欄を使用しません。

高度基準は`AGL`（地上高）または`MSL`（平均海面基準の高度）を明示してください。GNSS楕円体高を使用する場合は、必要な基準変換を事前に行ってください。モデルの格子と鉛直解像度は、開始高度の100 m刻みとは異なります。

## 気象データの選択

| データソース | 取得 | WindowsでのARL変換 | HYSPLIT実行 |
|---|---|---|---|
| GDAS1 | NOAA ARLを取得 | 不要 | 対応 |
| GFS 0.25° | NOAA ARLを取得 | 不要 | 対応 |
| ローカルARL | 既存のARLファイルを指定 | 不要 | 対応 |
| ERA5 (CDS) | CDSからGRIBを取得 | **GRIB1に限り、Windows同梱のgrib2arl.exeで変換（実験的）** | 変換・時間検証成功時のみ |
| ERA5 (Google ARCO) | Google CloudからNetCDFを取得 | **未実装** | 未対応 |

**`api2arl`、`filedates.exe`をGUIから選ばせません。** HYSPLITの`hyts_std.exe`が入っている`exec`フォルダから、GRIB1変換用の`grib2arl.exe`とARL時間検査用の`filedates.exe`を自動検出します。

ERA5 (CDS)ではCopernicus CDSへの登録・利用規約の同意・API設定（`~/.cdsapirc`）とPythonの`cdsapi`が必要です。

```powershell
py -3 -m pip install -r requirements-era5.txt
```

CDS用の`cdsapi`だけを導入する場合は`py -3 -m pip install cdsapi`でも構いません。取得形式がGRIB2の場合は、GRIB1用の`grib2arl.exe`へ渡さず明示的に停止します。変換中にデータが不足する、時間を検査できない場合もモデル実行を停止します。

ERA5 CDSのGRIB1自動変換は**既存のWindows版変換ツールに基づく実験的機能**です。全てのCDSのGRIBで動作する保証はありません。変換後の気象場の妥当性を、元のERA5変数・HYSPLIT`MESSAGE`・別条件の基準計算と照合してください。

Google ARCO ERA5はZarr/NetCDFで、GRIB1/GRIB2の変換とは別の格子・鉛直座標・気象変数処理が必要です。未検証のNetCDFをARLと偽って計算する処理は提供しません。

## 表示・保存

実行フォルダに`CONTROL`、`SETUP.CFG`、`ASCDATA.CFG`、`starts.csv`、`tdump`、`MESSAGE`、`run.log`、`manifest.json`を保存します。結果から`map/index.html`（完全オフラインの簡易緯度・経度SVG図）と`map/trajectories.geojson`を生成します。地図にOpenStreetMap・Leaflet・CDNは使いません。海岸線・地形図は表示しません。QGISにGeoJSONを読み込めば地理情報と重ね合わせられます。

## テスト

```powershell
cd D:\Codex\hysplit-scripts-students
py -3 -m unittest discover -s tests -v
```

WindowsネイティブのGUI・HYSPLIT実行は検証対象です。ERA5/CDSのダウンロード・GRIB変換は利用データ、認証、フォーマットに左右されます。単体テストの成功だけで気象場の物理的な正しさを保証するものではありません。

参考：[NOAA HYSPLIT](https://www.arl.noaa.gov/hysplit/getrun-hysplit/) ／ [NOAA meteorological conversion](https://hysplit2.arl.noaa.gov/HYSPLIT_data2arl.php) ／ [ERA5 CDS](https://cds.climate.copernicus.eu/) ／ [Google ARCO ERA5](https://github.com/google-research/arco-era5)
