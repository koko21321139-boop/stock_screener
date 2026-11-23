# stock-screener（成長株スクリーナー）

ミネルヴィニの**トレンドテンプレート**と O'Neil の **CAN-SLIM** で、東証プライムの銘柄を毎日スクリーニングし、iPhone で一覧表示するアプリです。データはすべて無料のものを使います。

## しくみ

```
平日 16:37（日本時間）
  GitHub Actions（.github/workflows/screen.yml）が scripts/screen.py を実行
    → JPX の「東証上場銘柄一覧」で対象銘柄を決める（週 1 回更新）
    → Yahoo Finance（yfinance）から 2 年分の日足を取得
    → 条件を判定し、data/screen.json と data/history.json に保存してコミット
  GitHub Pages が index.html と data/ を配信
```

## 判定の中身

**トレンドテンプレート（8 条件）**：株価 > 150日線・200日線／150日線 > 200日線／200日線が 1 か月以上上向き／50日線 > 150日線・200日線／株価 > 50日線／52週安値から +30% 以上／52週高値から −25% 以内／RS 70 以上

- **RS**：直近 3 か月を 2 倍に重み付けした 1 年の騰落率を、対象銘柄内で 1〜99 に順位付け（IBD の RS レーティングに近い考え方）
- **VCP候補**：直近 60 日を 20 日ずつに分けた値幅が段々小さくなり（最後は 10% 以内）、10 日平均出来高が 50 日平均の 8 割未満
- **ブレイク**：終値が直近 20 日の高値（ピボット）を上回り、出来高が 50 日平均の 1.5 倍以上

**CAN-SLIM**（トレンドテンプレートを 7 条件以上満たす銘柄だけ。決算データは 7 日ごとに取り直す）

| 文字 | 目安 | データ |
|---|---|---|
| C | 直近四半期 EPS が前年同期比 +25% 以上 | yfinance（四半期 EPS、無ければ earningsQuarterlyGrowth） |
| A | 年間 EPS が直近 2 年とも +20% 以上、かつ年率 +20% 以上 | yfinance（年次損益計算書） |
| N | 52 週高値から 5% 以内 | 株価 |
| S | 出来高が 50 日平均の 1.4 倍以上、またはブレイク | 株価 |
| L | RS 80 以上 | 株価 |
| I | 機関投資家の保有 10% 以上 | yfinance（heldPercentInstitutions） |
| M | 地合いが上昇トレンド（TOPIX ETF 1306 が 50日・200日線の上、50日 > 200日、売り抜け日 4 日以下） | 株価 |

データが取れない文字は点線の枠で表示します（満たしていない扱いにはしません）。

## 設定（任意。Settings → Secrets and variables → Actions → Variables）

- `UNIVERSE`：`prime`（既定）／`topix500`／`all`（プライム・スタンダード・グロース）
- `MIN_TURNOVER`：20 日平均売買代金の下限（円。既定 100000000 = 1 億円）。少なすぎる銘柄を除く

## moomoo のウォッチリストに同期（任意）

毎日の通過銘柄（RS の高い順に最大 100 銘柄）を、moomoo アプリのウォッチリストの 1 グループに自動で入れます。通過しなくなった日本株はそのグループから外します。売買の権限は使いません。

1. moomoo アプリで、ウォッチリストのグループ「スクリーナー」を作る（このグループは自動で入れ替わるので、手で入れた日本株も外れることがある）
2. https://open.moomoo.com/dashboard → Console → AppKey Management で AppKey を作り、署名方式は Ed25519、公開鍵を登録。権限は相場（quote）のみにし、取引（trade）は付けない
3. GitHub の Secrets に `MOOMOO_APP_KEY`（AppKey ID）と `MOOMOO_PRIVATE_KEY`（秘密鍵の PEM 全文）を登録
4. 鍵は `python tools/make_moomoo_key.py` で作れる（秘密鍵は Secrets に入れたら手元から消す）

グループ名を変えるときは Variables の `MOOMOO_GROUP`、上限数は `MOOMOO_MAX` で指定します。

## ファイル

| パス | 役割 |
|---|---|
| `scripts/screen.py` | 判定プログラム |
| `scripts/moomoo_sync.py` | moomoo のウォッチリストへの同期 |
| `.github/workflows/screen.yml` | 自動実行の設定（`cron` は UTC。日本時間 − 9 時間） |
| `data/screen.json` | 当日の判定結果 |
| `data/history.json` | 日ごとの通過銘柄（直近 120 日。「◯日連続」「新規通過」に使用） |
| `data/fundamentals.json` | 決算データのキャッシュ |
| `data/universe.json` | 対象銘柄の一覧 |
| `index.html` ほか | アプリの画面 |

## 注意

- yfinance は Yahoo Finance の非公式な取得方法です。仕様変更で突然取れなくなることがあります。日本株の決算データは欠けや遅れが多めです。足りないときは、J-Quants（JPX 公式）などの有料データへの切り替えを検討してください。
- 判定は機械的なもので、売買の推奨ではありません。チャートと決算を自分で確認してください。
