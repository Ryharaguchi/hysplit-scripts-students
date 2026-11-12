HYSPLIT Batch Trajectories — Windows x64 ポータブル版
====================================================

1. ZIPをPCのフォルダに展開してください。
   HYSPLIT_Batch\HYSPLIT_Batch.exe をダブルクリックして起動します。
   HYSPLIT_Batch.exeだけを取り出さず、_internalフォルダも一緒に保持してください。

2. Python、WSL、PowerShell、Gitなどの開発環境は不要です。
   Windows 10/11 (64ビット) を想定しています。Windows以外には対応しません。

3. 【重要】NOAAのHYSPLIT本体は含まれていません。
   NOAA HYSPLIT利用規約により、許可のない再配布が禁止されています。
   各PCでNOAA公式からWindows版を別途ダウンロード・導入し、
   GUIの「HYSPLIT実行ファイル」でhyts_std.exeを指定してください。
   通常は C:\hysplit\exec\hyts_std.exe です。
   正式なHYSPLITインストールには exec / bdyfiles 等のフォルダが必要です。
   HYSPLIT本体と気象ファイルの配布・コピーはNOAAの利用条件に従ってください。

   NOAA公式ダウンロード:
   https://www.arl.noaa.gov/hysplit/getrun-hysplit/
   NOAA使用・配布規約:
   https://hysplit2.arl.noaa.gov/HYSPLIT_agreement.php

4. データ出力先の初期値:
   %LOCALAPPDATA%\HYSPLIT_Batch
   保存先はGUIから変更できます。D:ドライブは必須ではありません。

5. samplesフォルダにJST時刻の観測CSVの例があります。
   4列: 時刻(JST)、緯度、経度、高度(m)。
   ヘッダーの有無はどちらにも対応しています。

6. 気象データの選択
   GDAS1 (1°) / GFS (0.25°):
      インターネットからARLを取得し、各PCでWindows版HYSPLITを実行。
   ローカルARL:
      PCに保存されている既存のARL気象ファイルを指定。
   ERA5 CDS (0.25°):
      Copernicus CDSへの登録、利用規約同意、API設定が別途必要。
   ERA5 Google ARCO (0.25°):
      Google Cloudの公開ZarrからNetCDFを取得し、Windowsの同梱Python
      ecCodesでGRIB1化。HYSPLITに付属のgrib2arl.exeでARLへ変換。
      非常に大量のダウンロードになる可能性があります。
   NOAA READYのWebサービスへ数値計算を委託する方式ではありません。

7. 流跡線の出力間隔はGUIで1～1,440分から指定できます（初期値10分）。
   内部計算時間刻みは通常自動（DELT=0）。固定値は詳細設定から指定します。
   1回の計算結果は runs\<実行ID>\trajectories.csv にまとめられます。
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
