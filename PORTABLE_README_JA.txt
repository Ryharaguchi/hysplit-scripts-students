HYSPLIT Batch Trajectories — Windows x64 ポータブル版
====================================================

1. ZIPをPCのフォルダに展開してください。
   展開先フォルダの HYSPLIT_Batch.exe をダブルクリックして起動します。
   HYSPLIT_Batch.exeだけを取り出さず、_internalフォルダも一緒に保持してください。

2. Python、WSL、PowerShell、Gitなどの開発環境は不要です。
   Windows 10/11 (64ビット) を想定しています。Windows以外には対応しません。

3. この統合ZIPにはアプリとHYSPLIT本体が含まれます。
   展開後、HYSPLIT\exec\hyts_std.exe、grib2arl.exe、filedates.exeと
   HYSPLIT\bdyfilesが同じフォルダ内にあることを確認してください。
   アプリは隣接するHYSPLIT\exec\hyts_std.exeを自動検出します。
   ZIP全体を展開し、HYSPLIT_Batch.exeだけを別の場所へ移動しないでください。
   NOAAの使用条件に従って利用してください。

   NOAA公式ダウンロード:
   https://www.arl.noaa.gov/hysplit/getrun-hysplit/
   NOAA使用・配布規約:
   https://www.ready.noaa.gov/HYSPLIT_agreement.php

4. データ出力先の初期値:
   Windowsデスクトップ\HYSPLIT_Work
   保存先はGUIから変更できます。D:ドライブは必須ではありません。

   入力した計算条件は実行開始時とアプリ終了時にこのPCのユーザー設定へ保存され、
   次回起動時に復元されます。気象データや計算結果は引き続きHYSPLIT_Workに保存します。

5. samplesフォルダにJST時刻の観測CSVの例があります。
   4列: 時刻(JST)、緯度、経度、高度(m)。
   ヘッダーの有無はどちらにも対応しています。

6. 気象データの選択
   GDAS1 (1°) / GFS (0.25°):
      インターネットからARLを取得し、各PCでWindows版HYSPLITを実行。
   ローカルARL:
      PCに保存されている既存のARL気象ファイルを指定。
   ERA5 CDS (0.25°):
      Copernicus CDSへの登録・利用規約同意・API認証設定とcdsapiが別途必要。
   ERA5 Google ARCO (0.25°):
      Google Cloudの公開ZarrからNetCDFを取得し、Windowsの同梱Python
      ecCodesでGRIB1化。HYSPLITに付属のgrib2arl.exeでARLへ変換。
      非常に大量のダウンロードになる可能性があります。
   NOAA READYのWebサービスへ数値計算を委託する方式ではありません。

7. 流跡線の出力間隔はGUIで1～1,440分から指定できます（初期値10分）。
   内部計算時間刻みは通常自動（DELT=0）。固定値は詳細設定から指定します。
   計算結果は runs\<実行ID>\Trajectory_period_*.csv として期間ごとに出力されます。
   各計算フォルダのtdumpやMESSAGEも、再現性確認用に残ります。

8. 依存ライブラリの診断方法:
   コマンドプロンプト / PowerShellで
     .\HYSPLIT_Batch.exe --self-test diagnostics.json
   と実行すると、同じフォルダのdiagnostics.jsonでパッケージ内
   ライブラリとTkinter、HYSPLIT検出状況を確認できます。
   HYSPLITが未導入でもGUI自体の動作診断はできます。

9. 本パッケージにはNOAA HYSPLITの実行ファイル・bdyfiles・
   ERA5やGDASの大容量気象データは同梱していません。
   実行ファイルは署名されていないため、Windows SmartScreenによる
   警告が表示される場合があります。配布元とファイルのSHA256を確認してください。

モデルに関する権利・再配布条件はNOAAの規約に従います。

10. NOAAの大容量データ：GDAS1は週間約585 MB、GFS 0.25°は日別約3.1 GB。
    S3公開アーカイブ優先、転送状況表示、通信切断時の再試行と途中再開に対応。
    Google ARCOは小領域でも大きなZarrチャンクの転送を要します。
    気象データのキャッシュは保存フォルダ内のmetにあります。

11. Excelで編集したCSVは「CSV UTF-8（コンマ区切り）」で保存してください。
    先頭4列は日時・緯度・経度・高度[m]の順です。9:00のような
    1桁時刻やExcelの日付シリアル値、UTF-16、タブ区切りにも対応。
    xlsxはCSVではありません。日時の科学表記への変換を避けてください。

12. 別PCで失敗するときは、HYSPLIT正規インストール、CDS認証、
    HTTPSプロキシや信頼CA証明書、書込権限、空き容量を確認してください。

13. 同じ利用者の別PCへ移行する場合:
    H:\マイドライブ\配布 以下のHYSPLIT_BatchとHYSPLITを
    デスクトップへセットでコピーしてください。モデル計算と気象
    ダウンロードはデスクトップ\HYSPLIT_Workで行います。
    HYSPLIT本体についてはNOAAの利用条件を遵守し、第三者への
    再配布や共有リンクでの配布を行わないでください。

14. HYSPLITとGUIを同じ利用者のPC間で移す場合は、
    配布フォルダ全体を別PCのデスクトップへコピーしてください。
    HYSPLIT_Batch.exeを単独で抜き出さず、_internalと隣接する
    HYSPLIT/exec、HYSPLIT/bdyfilesも保持してください。
    日本語名「配布」を変更する必要はありません。
    作業・気象データ保存先はデスクトップのHYSPLIT_Workです。

16. 複数日時の必要気象期間だけを取得します。
    後方計算に不要な空白期間をダウンロードしません。
    GDAS1/GFSは必要な週間/日別ARLファイルのみ、
    ERA5 CDS/Google ARCOは離れた期間ごとに取得・変換します。
    取得済みデータはmetフォルダで再利用します。

17. GUIの進捗表示は全体の完了工程数と現在の転送/処理を区別します。
    NOAAでは取得容量が分かる場合に割合を表示します。
    連続実行中はWindowsの自動アイドルスリープ抑制を要求します。
    実行中はアプリを閉じず、電源を維持してください。

18. 実行ID・計算グループのフォルダ名はJSTです。
    内部計算と気象データの有効時刻は従来どおりUTCです。
    過去に作ったUTC命名の結果フォルダは変更しません。

19. Google ARCO ERA5でNetCDF書き出しが停止したように見える場合:
    元データの1変数×1時間のチャンクに全球37気圧面があり、
    約154MB（展開後）のデータ読み込みが必要です。
    更新版は1時間・1変数ずつ処理し、X/Y時間をGUIに表示します。
    途中で中断した場合も検証済みの部分NetCDFから再開できます。
    気象ファイルは実際の有効時刻・格子・変数で索引します。
    既存ARLが複数あれば自動選択し、不足時間のみ取得済みGRIBやNetCDFから変換、
    それもなければダウンロードします。既存データを手動移行する必要はありません。
    UTC日単位を基本に処理し、変換失敗時は小さい単位に分割します。
    計算期間の独自上限は設けません。
    取得済みの元ファイルは削除しません。新規作成の変換中間ファイルは
    対応するARL検証後に整理し、全変数アーカイブは保持します。
    新規の必須項目のみの元データも残す場合はGUIの
    「ERA5の変換元NetCDF/GRIBも残す」を選択します。
    中断時は元データと検証済みARL、エラーログを保持します。
    同じ保存先を別のアプリが書き込み中の場合は更新を競合させません。
    ただし通信量が大きい問題自体は残ります。
    数日間の地域データにはERA5 CDS（認証が必要）も検討してください。

20. 他PCでcffi/_pycparserとzlib: incorrect header checkが出た場合:
    NetCDF破損と決め付けず、EXEと_internalをセットで
    ローカルのデスクトップにコピーし直してください。
    配布EXEの --self-test diagnostics.json では、
    grib1_backend.ok と packaging_passed を確認できます。
    この診断ではecCodesの本物のGRIB1メッセージも生成します。
    コピー元Google Driveの同期終了を確認してください。
    観測CSVや計算済みデータを削除する必要はありません。

21. ERA5取得は北半球全域（0～90°N、全経度）0.25°×37気圧面（1000～1 hPa）。
    ERA5は「HYSPLIT必須項目のみ（6気圧面+7地上）」または
    「全変数」を選択できます。必須項目のみでも37気圧面を維持します。
    CDSの全変数はこのアプリ指定の16気圧面+7地上変数、
    ARCOの全変数は公開Zarrの全収録変数です。全変数モードは大容量です。
    CDSを使うにはCopernicus CDSの利用登録・規約同意・API認証設定が必要です。
    HYSPLIT変換は37気圧面+地表の38層指定です。
    モデル上端はGUIで指定可。空欄ならERA5 50km AGL、
    その他25km AGLまたは開始高度+1kmの高い方です。
    北半球保存も大容量です。必要なローカル保存容量を確保してください。

22. 旧データ・実行中ジョブへの影響:
    旧地域限定・12層NetCDF等は削除しません。ただし未保存の
    北半球で未保存の格子・追加気圧面・変数は新たな取得が必要です。
    元のGoogle全球Zarrチャンクは永続保存されていません。
    既存の全球・旧版キャッシュは削除せず、新しい北半球キャッシュと区別します。
    実行中アプリは変更せず、実行終了後に新しいGUIを導入してください。

23. Windows grib2arl.exe がCFG_ARL読込時に一時失敗した場合:
    同じGRIBを新しい一時フォルダで1回だけ自動再試行します。
    失敗が続く場合は、変換ログにMESSAGEとCFG_ARLの診断情報を残します。
    成功時に変換器の警告があればGUIログにも表示します。
    surface pressure not found / estimated from terrain height の警告は、
    地表気圧がARLに入らず地形から推定されることを示します。
