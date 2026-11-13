HYSPLIT Batch Trajectories — Windows x64 ポータブル版
====================================================

1. ZIPをPCのフォルダに展開してください。
   HYSPLIT_Batch\HYSPLIT_Batch.exe をダブルクリックして起動します。
   HYSPLIT_Batch.exeだけを取り出さず、_internalフォルダも一緒に保持してください。

2. Python、WSL、PowerShell、Gitなどの開発環境は不要です。
   Windows 10/11 (64ビット) を想定しています。Windows以外には対応しません。

3. GUI単体のZIPにはNOAAのHYSPLIT本体は含まれていません。
   同じ利用者のPC間移動用H:の「配布」には
   HYSPLIT_Batchの隣にHYSPLIT/execとbdyfilesを配置します。
   NOAA HYSPLIT利用規約により、許可のない再配布が禁止されています。
   「配布」を全体ごとデスクトップにコピーすると、GUIが隣の
   HYSPLIT/exec/hyts_std.exeを自動検出します。
   通常は C:\hysplit\exec\hyts_std.exe です。
   正式なHYSPLITインストールには exec / bdyfiles 等のフォルダが必要です。
   HYSPLIT本体と気象ファイルの配布・コピーはNOAAの利用条件に従ってください。

   NOAA公式ダウンロード:
   https://www.arl.noaa.gov/hysplit/getrun-hysplit/
   NOAA使用・配布規約:
   https://hysplit2.arl.noaa.gov/HYSPLIT_agreement.php

4. データ出力先の初期値:
   Windowsデスクトップ\HYSPLIT_Work
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

13. Google Driveへの計算結果コピー:
    H:にマイドライブがある場合、結果コピー先の初期値はHYSPLITです。
    必要ならGUIからH:\マイドライブ\HYSPLIT等を指定してください。
    計算終了後に runs フォルダへ
    trajectories.csv、tdump、計算ログ、地図などをコピーします。
    GDAS/GFS/ERA5気象ファイルやNOAA HYSPLIT本体はコピーしません。
    計算・気象データ取得は必ずローカルで実行されます。
    コピーが失敗してもローカルの計算結果は残ります。
    Google Drive側のクラウド同期完了はGoogle Driveアプリで確認してください。
    コピー先はGUIから変更できます。

14. 同じ利用者の別PCへ移行する場合:
    H:\マイドライブ\配布 以下のHYSPLIT_BatchとHYSPLITを
    デスクトップへセットでコピーしてください。モデル計算と気象
    ダウンロードはデスクトップ\HYSPLIT_Workで行います。
    HYSPLIT本体についてはNOAAの利用条件を遵守し、第三者への
    再配布や共有リンクでの配布を行わないでください。

15. HYSPLITとGUIを同じ利用者のPC間で移す場合は、
    配布フォルダ全体を別PCのデスクトップへコピーしてください。
    HYSPLIT_Batch.exeを単独で抜き出さず、_internalと隣接する
    HYSPLIT/exec、HYSPLIT/bdyfilesも保持してください。
    日本語名「配布」を変更する必要はありません。
    作業・気象データ保存先はデスクトップのHYSPLIT_Workです。
