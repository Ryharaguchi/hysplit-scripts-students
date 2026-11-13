# HYSPLIT 後方流跡線一括計算（Windows）

**Windowsネイティブ専用。WSL/WSL2・Linux・macOSは実行対象外です。**
計算者が作成した観測CSVから、各行の時刻・緯度・経度・高度を開始条件として、HYSPLITの後方流跡線を大量に計算するTkinter GUIです。NOAA READYのWeb上で数値計算を実行するものではありません。

## Google DriveのHYSPLIT結果保存先（Windows H:）

計算・気象データの作業先は各PCのデスクトップ/HYSPLIT_Workです。Google DriveがH:に接続されている場合、結果コピー先の既定値は H:/マイドライブ/HYSPLIT です。既定で無効化する変更は行いません。GUIで保存先はいつでも変更できます。

具体的には、1つの統合 trajectories.csv、観測CSVの原本 observations.csv、manifest.json、各グループのCONTROL/SETUP.CFG/tdump/MESSAGE/run.log、オフライン地図とGeoJSONをコピーします。成果物はSHA-256を使って照合し、途中で失敗した場合には完成したフォルダと誤認させないよう一時フォルダから確定します。ファイル名が同じ既存の結果を上書きしません。

**気象データの取得とHYSPLIT計算はローカルで実行**します。大きなGDAS/GFS/ERA5気象ファイルとモデル本体はGoogle Driveへ自動コピーしません。Google Driveの仮想ドライブはクラウドとの同期や空き容量によって読み書きが遅くなるため、計算処理はローカルに置いたままにしています。

Google Driveが利用できない場合や容量不足の場合は、ローカルの結果を消さずにGUIへコピー失敗の警告を表示します。コピー先欄を空欄にすれば同期を無効化でき、別のGoogle Driveアカウントがある場合は「参照」で任意のフォルダへ変更できます。**H:が見つからないPCで勝手にG:へフォールバックはしません。** G:とH:が異なるGoogle Driveアカウントの可能性があるためです。

WindowsのGoogle Driveフォルダへの書き込みが成功したことと、Googleのサーバーへのクラウド同期が完了したことは同義ではありません。Google Drive for desktopで同期完了を確認してください。

### 同じ利用者の別PCへの持ち運び

H:/マイドライブ/配布 の直下にはGUIの HYSPLIT_Batch とモデルの HYSPLIT を並べます。GUIのHYSPLIT_Batch.exeは隣接するHYSPLIT/exec/hyts_std.exe、grib2arl.exe、filedates.exeとbdyfilesを自動検出します。

別PCでは「配布」フォルダ全体をデスクトップにコピーし、配布/HYSPLIT_Batch/HYSPLIT_Batch.exe を起動します。作業・気象データの保存先はデスクトップの HYSPLIT_Work です。日本語の「配布」などを含むパスは、HYSPLITのFortran用ASCII設定に対して必要な小さな地形ファイルを作業フォルダへコピーし、相対パスで参照します。日本語フォルダ名の変更は不要です。

これは同じ利用者のPC間での移動を目的とするもので、第三者への再配布・公開共有リンクを意図していません。[NOAA HYSPLIT利用規約](https://hysplit2.arl.noaa.gov/HYSPLIT_agreement.php)に従ってご利用ください。

このPCのプライベートな別PC用フォルダはH:/マイドライブ/配布です。アプリ本体に加え、同じ利用者が手元で使用しているHYSPLITの実行ファイル群とbdyfilesを配置し、別PCのデスクトップにフォルダごとコピーして使用します。これは第三者への再配布を意図していません。NOAAの利用条件に従い第三者には共有しないでください。

## Windows EXE配布版（Pythonインストール不要）

Windows x64では、ポータブル配布フォルダの HYSPLIT_Batch.exe をダブルクリックすればGUIを起動できます。実行先のPCにはPython・Git・PowerShell・WSLは不要です。EXEファイルだけを取り出さず、配布フォルダの _internal も保持してください。

**HYSPLIT_Batch.exe単体の配布ZIPにはモデル本体を同梱しません。** 同じ利用者のPC間移動用に作成する H:/マイドライブ/配布 フォルダでは、HYSPLIT_Batch と、現在このPCに導入済みのHYSPLIT実行ファイル一式およびbdyfilesを兄弟フォルダとして配置します。アプリは隣接するHYSPLIT/exec/hyts_std.exeを自動検出します。[NOAA利用規約](https://hysplit2.arl.noaa.gov/HYSPLIT_agreement.php)には無許可の再配布の禁止が明記されています。個人間の受け渡しや共有リンク公開は行わないでください。

デフォルトの計算・気象データ保存先はWindowsのデスクトップ/HYSPLIT_Workです。Dドライブや開発者アカウントには依存しません。観測CSVはJST入力、実行ごとの結果CSVは trajectories.csv に集約します。ERA5 Google ARCO用のxarray、netCDF4、ecCodes、gcsfsはEXEに含めますが、気象データの取得にはネット接続が必要です。

診断: HYSPLIT_Batch.exe --self-test diagnostics.json

開発者向けWindows PowerShellビルドは build_windows_exe.ps1 を実行します。Windows用Python仮想環境にPyInstallerと requirements-era5.txt の依存パッケージをインストールしてください。配布ZIPは releases/HYSPLIT_Batch_Windows_x64.zip です。Windows用EXEはコード署名されていません。

## 起動

Windows PowerShellから：

```powershell
cd D:\Codex\hysplit-scripts-students
py -3 run_gui.py
```

NOAA HYSPLIT Windows版の`hyts_std.exe`が必要です。既存の`Desktop\backwardtrajectory\HYSPLIT\exec`を自動検出します。過去のソース実行例での作業フォルダは`D:\Codex\hysplit-batch`です。元のHYSPLITフォルダ、既存の観測データ・出力データは書き換えません。

## ダウンロード速度・別PCでの通信エラー

NOAAのデータ取得先は公開S3を優先し、READY Webアーカイブへフォールバックします。取得量と速度はGUIログに表示します。HTTP RangeとETag/Last-Modifiedが利用可能なら途中再開、通信切断やHTTP 429/5xxは最大3回再試行します。

- GDAS1 1°：1週間分のアーカイブが約585 MB。計算対象が短くても週間ファイルを取得します。
- GFS 0.25°：1日分が約3.1 GB。数日分の後方計算では10 GB以上になる場合があります。
- Google ARCO ERA5 0.25°：公式の気圧面Zarrは1変数×1時間×37層のチャンクが公称154 MBで、小領域を切り出す場合も多量の通信が必要です。取得済みNetCDFはリモートZarrへ接続する前に検証・再利用します。
- ERA5 CDS：データ要求の処理待ちとGRIB変換があります。別のWindowsユーザーでは各自の認証ファイル、CDSライセンス同意が必要です。Windowsのユーザーフォルダに .cdsapirc を用意してください。

GUIの「気象データを取得」はGRIB/NetCDFのダウンロードのみで、ERA5もHYSPLIT本体を導入せず実行できます。「一括計算を開始」で変換とモデル計算まで行います。

ダウンロードデータは設定した作業フォルダの met 配下に保存し、同じパスを再利用すると不要な取得を省けます。別PCにはキャッシュが自動共有されないため、許可された気象データを新しいPCの同じ作業フォルダにコピーする方法があります。NOAA HYSPLIT本体の再配布は禁止されています。

別PCの接続エラーでは、GUIログのHTTPステータス・URL・エラー詳細を確認してください。HTTPSプロキシ、企業のTLS検査、Windowsの日時と信頼CA証明書、保存先の書込権限、空き容量などが要因になり得ます。SSL検証を無効化する回避策は使用しません。

参考：[NOAA公開アーカイブ](https://www.ready.noaa.gov/archives.php) / [NOAA公開S3](https://noaa-oar-arl-hysplit-pds.s3.amazonaws.com/index.html) / [Google ARCO ERA5](https://github.com/google-research/arco-era5)

## Excelで修正したCSVの互換性

観測CSVは先頭4列を順序で読み込みます：日時(JST)、緯度、経度、高度[m]。ヘッダーは任意です。Excelで日時が 2026/10/10 9:00 のように1桁時刻になる場合に対応しました。日付シリアル値、UTF-8 BOM、CP932、UTF-16、タブ・セミコロン区切り、余分な末尾空列も読めます。

推奨：Excelの「名前を付けて保存」→「CSV UTF-8（コンマ区切り）」で保存してください。日時の1列目を先に「文字列」に設定してから編集すると意図しない変換を減らせます。XLSXを拡張子だけCSVに変更してもCSVになりません。科学表記になった日時は桁が失われている可能性があり、計算せず明示的なエラーにします。

エラー時は、GUIに表示されたCSV行番号と保存したCSVをテキストエディターで開いた内容を確認してください。読み込んだ元の観測CSVは計算フォルダの observations.csv へ原本保存します。

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

## モデル上端と同時計算数

**モデル上端のGUI入力は削除しました。** HYSPLITのCONTROLファイルには鉛直計算範囲の上限が必要なため、内部では20,000 m AGL以上かつCSVの最大初期高度＋1,000 m以上を、1,000 m単位に切り上げて設定します。実際の設定は計算結果のmanifest.jsonに記録します。CSVの高度は流跡線の**開始高度**であり、流跡線が後方へ移動した全期間の高度を示すものではありません。内部上端を高くしても、気象データに含まれない高高度の風が作られるわけではありません。

**同時に動かす計算プロセス数**（旧「並列実行数」）は、Windows上で同時起動する `hyts_std.exe` の数です。CSVの行数や最終的な流跡線本数とは異なります。同一開始時刻の複数軌跡は1つのプロセス内でまとめられるため、流跡線数81本でもプロセスの実行数が3になる場合があります。並列化はディスクI/Oやメモリ使用量が増えるので、通常はまず1～2で動作確認してください。

## 流跡線の出力間隔と内部計算時間刻み

GUIの「後方流跡線の計算条件」に「流跡線の出力間隔 [分]」を追加しました。初期値は10分、入力範囲は1～1,440分です。これは流跡線上の位置と選択した気象変数をtdump・trajectories.csvへ保存する間隔（HYSPLIT TOUT）で、観測CSVの開始時刻や後方計算時間とは別です。

| 出力間隔 TOUT | 後方24時間・1流跡線の出力地点数（開始地点を含む） |
|---|---:|
| 60分 | 25 |
| 30分 | 49 |
| **10分（初期値）** | **145** |
| 5分 | 289 |
| 1分 | 1,441 |

計算内容のプレビューに全流跡線のCSV地点数の概算を表示します。間隔を短くするとCSV容量も増えるため、大量の観測点や感度解析では保存容量に注意してください。

**内部計算時間刻み（HYSPLIT DELT）は自動（0）が標準です。** 風速や格子間隔に応じてHYSPLITが内部時間刻みを選びます。研究上の比較のために固定したい場合のみ、「詳細設定：計算時間刻みを固定する」をチェックし、DELTを指定します。固定値は1～60分で、60分とTOUTの両方を割り切る整数に限定します。例：TOUT=10分ならDELT=1、2、5、10分。整合しない値はモデル実行前に拒否されます。詳細設定を閉じれば自動へ戻ります。

設定は各計算グループのSETUP.CFG（例：TOUT=10、DELT=0.0）とmanifest.jsonへ保存します。従来のtdumpは経過時間（時間）を小数第2位程度に丸めるため、最終CSVのage_hoursは開始時刻・各地点のUTC日時差で再計算し、小数第6位まで保存します。例：－10分＝－0.166667時間。

ERA5が1時間ごとに提供する再解析場は、出力間隔を10分にしても10分ごとに独立した観測・解析場へ変わるわけではありません。HYSPLITが気象場を補間して計算します。通常はDELT自動のまま使用してください。

NOAA公式：[流跡線出力間隔 TOUT](https://hysplit2.arl.noaa.gov/hysplitusersguide/S613.htm) ／ [内部計算時間刻み DELT](https://hysplit2.arl.noaa.gov/hysplitusersguide/S610.htm)。

## 流跡線上に出力する気象変数（チェックボックス）

GUIの「1. 後方流跡線の設定」→「流跡線上に出力する気象変数」で、以下の8項目を個別にON/OFFできます。初期値は8項目すべてONです。チェックしない項目は最終CSVの列として出力しません。一方、位置・時刻・気圧と地表標高は基本列として常時記録します。

| GUI表示 | HYSPLIT設定 | tdumpラベル | trajectories.csvの列 | 単位 |
|---|---|---|---|---|
| 温位 | TM_TPOT | THETA | potential_temperature_k | K |
| 気温 | TM_TAMB | AIR_TEMP | air_temperature_k | K |
| 相対湿度 | TM_RELH | RELHUMID | relative_humidity_percent | % |
| 比湿 | TM_SPHU | SPCHUMID | specific_humidity_g_per_kg_moist_air | g/kg（湿潤空気） |
| 水蒸気混合比 | TM_MIXR | H2OMIXRA | water_vapor_mixing_ratio_g_per_kg_dry_air | g/kg（乾燥空気） |
| 東西風速U | TM_UWND | UWIND | eastward_wind_m_s | m/s |
| 南北風速V | TM_VWND | VWIND | northward_wind_m_s | m/s |
| 混合層高度 | TM_MIXD | MIXDEPTH | mixing_layer_depth_m | m |

計算時、SETUP.CFGに選択したTM_* = 1を記録します。気圧TM_PRES = 1と地表標高TM_TERR = 1は常時有効です。数値出力の桁数を増やすためTFMT = 2を指定します。HYSPLITの出力変数の順序はモデル側で固定されていますが、CSVでは診断ラベル名で照合し、常に一定の列順で保存します。tdumpに選択した変数が存在しない場合は、不完全な結果CSVを作らずエラーとします。

比湿は水蒸気質量を湿潤空気全体の質量で割ったもの、水蒸気混合比は乾燥空気の質量で割ったものです。小さい値では近くなりますが同義ではありません。湿度や混合層高度などの物理値は、GDAS・GFS・ERA5の入力により欠測・導出・計算方法の違いがあり得ます。チェックしただけで物理的妥当性が保証されるわけではありません。

Windowsネイティブhyts_std.exeとERA5由来ARLで8項目を有効にし、後方3時間の実計算で10個のtdump診断変数（気圧・地表標高と8項目）と27列の統合CSVを確認しました。

参照：[NOAA HYSPLIT — Meteorology Output Along Trajectory](https://hysplit2.arl.noaa.gov/hysplitusersguide/S614.htm) / [NOAA Trajectory Output Format](https://hysplit2.arl.noaa.gov/hysplitusersguide/S263.htm)

## 気象データの選択

| GUIの選択肢 | 入手元 | 取得・配布格子 | 形式とWindows計算 |
|---|---|---|---|
| GDAS1 (1°) | NOAA | **約1° × 1°** | ARLを直接ダウンロードして計算 |
| GFS (0.25°) | NOAA | **0.25° × 0.25°** | ARLを直接ダウンロードして計算 |
| ローカルARL | 利用者が指定 | **ファイル依存** | 既存のARLを直接計算 |
| ERA5 CDS (0.25°) | Copernicus CDS | **0.25° × 0.25°（緯度経度再格子化）** | GRIB1が得られた場合に限りWindowsのgrib2arl.exeで変換（試験的） |
| ERA5 Google ARCO (0.25°) | Google Cloud | **0.25° × 0.25°（再格子化した配布格子）** | NetCDF→GRIB1（Windows ecCodes）→Windows grib2arl.exe→ARL→HYSPLIT（試験的） |

**ERA5の水平解像度と配布格子は同一概念ではありません。** ERA5の大気再解析HRESの元の実効的な水平解像度は約31 km（ネイティブ表現で0.28125°相当）。CDSとGoogle ARCOから使用する0.25°等角格子は再格子化された**配布格子**です。0.25°を選んでも元の気象解析の実効的な解像度が改善されるわけではありません。ERA5 CDSは現在0.25°の格子で明示的に要求しています。取得領域はGUIに表示せず、全開始点の周辺から自動推定します。長い後方流跡線がこの矩形領域を出る可能性があり、科学的なカバレッジ保証ではありません。

**`api2arl`、`filedates.exe`をGUIから選ばせません。** HYSPLITの`hyts_std.exe`が入っている`exec`フォルダから、GRIB1変換用の`grib2arl.exe`とARL時間検査用の`filedates.exe`を自動検出します。

ERA5 (CDS)ではCopernicus CDSへの登録・利用規約の同意・API設定（`~/.cdsapirc`）とPythonの`cdsapi`が必要です。

```powershell
py -3 -m pip install -r requirements-era5.txt
```

CDS用の`cdsapi`だけを導入する場合は`py -3 -m pip install cdsapi`でも構いません。取得形式がGRIB2の場合は、GRIB1用の`grib2arl.exe`へ渡さず明示的に停止します。変換中にデータが不足する、時間を検査できない場合もモデル実行を停止します。

ERA5 CDSのGRIB1自動変換は**既存のWindows版変換ツールに基づく実験的機能**です。全てのCDSのGRIBで動作する保証はありません。変換後の気象場の妥当性を、元のERA5変数・HYSPLIT`MESSAGE`・別条件の基準計算と照合してください。

### Google ARCO：比湿から相対湿度への変換

Google ARCOの0.25°気圧面データに relative_humidity（相対湿度）がなく、specific_humidity（比湿、kg/kg）だけがある場合にも対応します。NetCDFの気温 temperature（K）、比湿 specific_humidity（kg/kg）、気圧面の圧力（hPa）から相対湿度（%）を導出した上で、GRIB1のECMWFパラメータ157（RELH）へ変換します。

水蒸気分圧は比湿 q と気圧 p（Pa）から e=p*q/(ε+(1-ε)*q)、ε=0.621981 とします。飽和水蒸気圧は水（273.16 K以上）と氷（250.16 K以下）、中間温度では二乗混合とするECMWF系の式に従い、RH=100*e/es_mixed(T) と計算します。100%を超える相対湿度を自動的に100%へ丸めません。欠損値、不正な比湿・気温はエラーとして停止します。

これらのRHは**元データに収録された相対湿度ではなく計算値**です。ARL出力に対応するメタデータJSONにも、直接RHを取得したのか、比湿から導出したのかを記録します。

参考：[ECMWF Metview: relative_humidity_from_specific_humidity](https://metview.readthedocs.io/en/latest/api/functions/relative_humidity_from_specific_humidity.html)

### Google ARCO ERA5のNetCDF → ARL（Windowsのみ）

Google ARCO ERA5 (0.25°)を選択すると、Google Cloudの0.25°緯度経度格子Zarrから、対象時刻・観測地点周辺の領域、必要な気象変数と次の12気圧面をNetCDFへ切り出します。

- 気圧面：1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100 hPa。
- 気圧面の必須変数：ジオポテンシャル、気温、東西・南北風、鉛直速度、相対湿度。
- 地表の必須変数：2 m気温、10 m東西・南北風、地表気圧、1時間の積算降水量、境界層高度、地表ジオポテンシャル。
- NetCDFをWindowsネイティブのecCodesでGRIB1へ符号化し、HYSPLIT同梱のgrib2arl.exeでARLへ変換します。
- 変換前に時刻・必須気圧面・変数・0.25°格子・欠損値を検査し、変換後はfiledates.exeでUTCの時間範囲と1時間ごとの連続性を検査します。失敗時はモデル計算を停止します。

この機能は**Google ARCO ERA5特化のNetCDF変換**であり、任意のNetCDFをARLに変換する汎用ツールではありません。NOAAのarw2arl、cdf2arlなども特定のデータセット用であり、そのまま流用するものではありません。

**Windows実機での確認：** 既存のERA5気象値を保存した3時間のNetCDFを使い、NetCDF→GRIB1→ARL→HYSPLITで後方2時間の流跡線を計算できました。GUIからNetCDF変換と結果CSVを作る処理も検証済みです。ただしGoogle Cloudからの実際のZarrネットワーク取得は、取得量が非常に大きくなるため現時点では未検証です。任意地点・任意期間で物理的な再現性まで検証されたという意味ではありません。

**重要な制限：** Zarrのチャンク単位は非常に大きいため、狭い緯度経度領域でも大量の読み込みになる可能性があります。自動決定されるERA5取得領域が全ての後方流跡線を含む保証はなく、結果のMESSAGE等で気象領域外への逸脱を確認してください。

## 1回の計算結果はCSVを1ファイルに集約

**1回の一括実行で1つの結果CSV `runs/<実行ID>/trajectories.csv` を生成します。** 複数のHYSPLITプロセス・複数時刻・複数地点・複数高度の結果を1つに結合し、**各流跡線の各出力時刻がCSVの1行**になります。入力CSVの行数と結果CSVの行数は異なります。81本の流跡線 × 1時間ごとの25点であれば2,025行です。

| 主な出力列 | 意味 |
|---|---|
| `source_csv_row`, `run_group`, `trajectory_number` | 元の観測CSV行と計算グループ／流跡線番号 |
| `start_time_jst`, `endpoint_time_jst` | 初期・各地点のJST日時（UTC日時も別列） |
| `latitude_deg`, `longitude_deg`, `age_hours` | 位置と初期時刻からの経過時間（過去方向は負） |
| `pressure_hpa` | HYSPLITの出力した地点気圧 |
| `terrain_elevation_msl_m` | **HYSPLITのTERR_MSL診断変数による地表標高**（MSL、実測ではない） |
| `trajectory_height_agl_m` | 流跡線の地上高（AGL、`KAGL=1`） |
| `trajectory_height_msl_m_est` | **推定MSL高度＝AGL高度＋地表標高** |
| `trajectory_geopotential_height_m_est` | **推定ジオポテンシャル高度**（地球を球とした重力近似による換算） |

最後の2列は**計算値そのものではなく導出した概算**です。ERA5のジオポテンシャル値（m²/s²）を流跡線の位置で補間したものではありません。推定ジオポテンシャル高度は、MSLの幾何学的高度の近似値 h に対し `H ≈ R h/(R+h)`（`R=6,371,008.8 m`）を適用しています。緯度依存の重力・ジオイド差は反映しません。地表標高の診断が欠落する場合、MSLと推定ジオポテンシャル高度の列を**空欄**とし、値を捏造しません。

実行時は `SETUP.CFG` に `KAGL=1`, `TM_TERR=1`, `TM_PRES=1` を明示し、地表標高・気圧を `tdump` に書き出します。元の観測CSVは `observations.csv` として保存し、`manifest.json` にはSHA-256を記録します。HYSPLITの内部生成ファイル `CONTROL`, `starts.csv`, `tdump`, `MESSAGE`, `run.log` なども診断・再現性のため残しますが、**結果の数表として利用するCSVは `trajectories.csv` 1つだけ**です。

計算の一部が失敗した場合、完了済みの一部データを全件成功と誤認させないため、完全版CSVは生成せずエラーを表示します。別途 `map/index.html`（外部通信しない簡易緯度経度SVG）と `map/trajectories.geojson` を保存します。地図は表示の負荷を抑えるためプレビューする本数に内部上限がありますが、結果CSVには全件を出力します。

### 公式参考資料

- [NOAA HYSPLITの計算領域上端](https://ready.arl.noaa.gov/hysplitusersguide/S262.htm)
- [NOAA 高度のMSL/AGL切替（KMSL/KAGL）](https://www.ready.noaa.gov/hysplitusersguide/S611.htm)
- [NOAA 地表標高の出力（TM_TERR）](https://hysplit2.arl.noaa.gov/hysplitusersguide/S614.htm)
- [ECMWF ERA5のジオポテンシャルと幾何学高度](https://confluence.ecmwf.int/pages/viewpage.action?pageId=226496719)
- [Copernicus ERA5 pressure levels（0.25°）](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-pressure-levels)
- [Google Research ARCO ERA5（0.25°等角格子）](https://github.com/google-research/arco-era5)
- [NOAA NetCDF/GRIBからのARL変換ソース](https://github.com/noaa-oar-arl/hysplit_data2arl)

## テスト

```powershell
cd D:\Codex\hysplit-scripts-students
py -3 -m unittest discover -s tests -v
```

WindowsネイティブのGUI・HYSPLIT実行は検証対象です。ERA5/CDSのダウンロード・GRIB変換は利用データ、認証、フォーマットに左右されます。単体テストの成功だけで気象場の物理的な正しさを保証するものではありません。

参考：[NOAA HYSPLIT](https://www.arl.noaa.gov/hysplit/getrun-hysplit/) ／ [NOAA meteorological conversion](https://hysplit2.arl.noaa.gov/HYSPLIT_data2arl.php) ／ [ERA5 CDS](https://cds.climate.copernicus.eu/) ／ [Google ARCO ERA5](https://github.com/google-research/arco-era5)
