# poteto-monitor コードレビュー（2026-10-07）

対象: `Poteto-Groove/poteto-monitor` のコミット `c2bbf13`（README.md の更新）時点。リポジトリの全ファイル（約 2,500 行）が対象です。

## 0. 概要

### 進め方

- コードを全行読み、疑わしい箇所は再現スクリプトで確かめました。UI は実ブラウザ（Playwright + Chromium）で操作して確認しました。
- 既存テストは **23 件すべて通過**。どれもテストでは見つからない種類の問題です。
- 外部 API の仕様は、2026-10 時点の公式ドキュメントを検索で確認しました（[付録 B](#付録-b-参考資料)）。

### 確認方法の凡例

| 記号 | 意味 |
|---|---|
| ✅ | 実際に動かして再現した |
| 📖 | コードを読んで確定した（動かしてはいない） |
| ❓ | 未検証・要確認 |

### 件数

| 区分 | 件数 |
|---|---|
| 1. バグ | 14 |
| 2. セキュリティ | 7 |
| 3. 入力検証 | 3 |
| 4. 効率化 | 11 |
| 5. 足りない機能 | 10 |
| 6. 細かい指摘 | 14 |
| 7. 未検証 | 1 |

### 特に重要な 5 件

1. **B-1** 設定画面がマウスやタップで操作できない
2. **B-2** 何も変えずに保存しただけで監視対象が消える
3. **B-3** ダッシュボードの変化率がほぼ常に「+0.00%」
4. **B-4** 常駐モードでは急変アラートがほぼ鳴らない
5. **B-6** API が 1 つ失敗すると全銘柄の更新が止まる

---

## 1. バグ

### B-1 設定ドロワーをクリックすると即閉じる（操作不能）✅ 重大

- **現象**: 設定画面の入力欄・ボタン・✕ をクリック（タップ）すると、その場でドロワーが閉じる。
- **原因**: 背景の `.drawer-scrim`（`index.html:87`）が、DOM 上でパネルより後ろにある。両方とも `position` 指定で z-index がないため、背景がパネルの上に描画される（`styles.css:130-136`）。クリックは背景の `data-close` に届く。
- **確認結果**: Chromium の 1280px と 390px の両方で、保存ボタン・入力欄・✕ の中心にある最前面の要素は `drawer-scrim` だった。
- **対応案**:
  ```css
  .drawer-panel { z-index: 1; }
  ```

### B-2 UI で保存すると監視対象が消える・`key` が消える ✅ 重大

- **現象**:
  - README が案内している `"pair": "USD/JPY"` 形式で書いた為替・レート比の行は、設定画面で入力欄が空になる。
  - 灰色で見えている USD / JPY はプレースホルダーなので、入力済みに見える。
  - 保存すると、その行が黙って削除される。
  - テンプレートにない項目（`key` など）も保存で消える。
- **原因**: `addWatchRow()` が `pair` を展開しない（`app.js:172-194`）。`collectWatch()` は必須欄が空の行を `continue` で捨てる（`app.js:216, 223`）。
- **確認結果**: `pair` 形式の為替・レート比と、`key: "btc-main"` 付きの暗号資産の 3 件で、**何も変えずに保存** → 1 件に減り、`key` も消えた。
- **影響**:
  - 履歴（`history.json`）の key が変わり、過去データとつながらなくなる。
  - 同じ銘柄を `key` で区別している設定は、保存すると「重複キー」エラーになる。
- **対応案**:
  - 行に元のエントリを保持し、編集した項目だけ上書きする。
  - `pair` を `base`/`quote`（`num`/`den`）に展開して表示する。
  - 必須欄が空の行は捨てずに、エラーとして表示する。

### B-3 ダッシュボードの変化率がほぼ常に「+0.00%」 ✅ 高

- **原因**: 取得ごとに `state.previous` を今回の値で上書きする（`poller.py:75`）。一方、表示用のスナップショットは毎回 `previous` から変化率を計算し直す（`state.py:46`）。
- **影響**: ページを開いた直後・SSE の再接続時・`GET /api/state`・取得エラー時の配信で、全銘柄が 0.00% になる。
- **確認結果**: 取得を 2 回実行すると、配信時は `[None, 10.0]`。その直後に取ったスナップショットは `0.0` だった。実画面でも、読み込み直後は全カードが「➡ +0.00%」。
- **対応案**: `update()` の時点で変化率を確定させて保持する。`previous` から都度計算しない。

### B-4 常駐モードでは急変アラートがほぼ鳴らない 📖 高

- **原因**: アラート判定も、定期レポートの「前回比」も、**直前の取得（既定 60 秒前）** と比べている（`poller.py:38, 46`）。
- **影響**:
  - 「60 秒で 10%」はほぼ起きないので、事実上鳴らない。
  - 1 時間かけて 15% 動いても、60 秒ごとの変化が 10% 未満なら通知されない。
  - 毎時実行だった頃の「1 時間前比」から、常駐化で意味が変わっている。
- **対応案**: 時間窓での比較（1 時間前比・24 時間前比）とクールダウン（F-1）。土台として履歴の保存（E-8）が必要。

### B-5 基準通貨を変えると誤アラートが出る ✅ 高

- **現象**: UI で `base_currency` を usd → jpy に変えると、価格が動いていなくても Discord に `🚀 BITCOIN: +14900.00%` が届く。
- **原因**: 前回値がドル建てのまま、円建ての新しい値と比べられる。
- **対応案**: 設定変更で値の単位が変わる銘柄は、前回値をリセットする。単位を比較キーに含めてもよい。

### B-6 API が 1 つ失敗すると全銘柄の更新が止まる ✅ 高

- **原因**: `fetch_all()`（`providers.py:180-194`）は全部成功か全部失敗のどちらか。例外は poller の 1 か所でまとめて捕まえている（`poller.py:83-88`）。
- **確認結果**: 為替だけが 429 を返すスタブで、`fetch_all` 全体が例外になった。
- **影響**:
  - 為替 API（open.er-api.com）は制限にかかると **20 分間 429** を返す仕様なので、最大 20 分、BTC や HYPE も含めた画面全体が止まり得る。
  - CoinGecko の ID を 1 つ打ち間違えただけでも同じことが起きる。
- **対応案**: API ごと・銘柄ごとに例外を切り分け、最後に取れた値は「古い値」と分かる表示で残す。

### B-7 ブラウザを開いていると停止・再起動が終わらない ✅ 中

- **現象**: SSE 接続中に SIGTERM を送っても、30 秒以上プロセスが終了しない。systemd の既定では 90 秒後に強制終了されるまで待たされる。
- **原因**:
  - uvicorn 0.54.0 は `timeout_graceful_shutdown` が未指定（`None`）だと、接続が閉じるまで無期限に待つ。
  - アプリの終了処理（lifespan の `ctx.stop`）は接続がすべて閉じた後に走るので、SSE のループは停止要求を受け取れない。
- **対応案**: `monitor.py:81` で
  ```python
  uvicorn.run(app, host=..., port=..., timeout_graceful_shutdown=3)
  ```

### B-8 価格がとても小さい銘柄が「$0」と表示される ✅ 中

- **原因**: `money()` は 1 未満の値を小数 6 桁で切っている（`format.py:29-31`）。
- **確認結果**: `money(1.234e-07, "usd")` は `$0`。`money(1.234e-05, "usd")` は `$0.000012` で、有効桁が 2 桁しか残らない。
- **対応案**: 1 未満の値は、小数の桁数ではなく有効桁数（例: 4 桁）で整形する。

### B-9 監視対象が 26 件以上になると定期レポートが送れない 📖 中

- **原因**: Discord の embed はフィールドが 25 個まで。メッセージ全体の embed の文字数も合計 6,000 字まで。レポートは 1 銘柄 1 フィールドで、すべて 1 つの embed に入れている（`notify.py:18-41`）。
- **対応案**: 25 件ごとに embed を分ける。1 メッセージあたり embed は 10 個まで。

### B-10 起動のたびに定期レポートがすぐ送られる 📖 低〜中

- **原因**: 起動時は `last_report_at = None`（`poller.py:81`）なので、最初の取得で必ず送信される。
- **影響**: 再起動を繰り返す障害が起きると、Discord に連投される。
- **対応案**: 最後に送った時刻をファイルに保存する。毎時 0 分など時刻に揃えて送る。

### B-11 1 回実行モードでは Discord 送信に失敗すると前回値が更新されない 📖 低

- **原因**: `run()` は送信（`monitor.py:45`）が失敗すると例外で抜けるので、`prices.json` の保存（`monitor.py:49-52`）まで進まない。
- **影響**: 次回は 2 時間前の値と比べることになる。B-9 の上限超過が続くと、ずっと更新されない。

### B-12 監視対象を並べ替えても、ダッシュボードのカードの順番が変わらない 📖 低

- **原因**: カードは新しく作るときだけ `appendChild` していて（`app.js:67`）、既存のカードは並べ替えない。ページを再読み込みするまで古い順番のまま。

### B-13 設定読み込みエラーのバナーが、次の更新ですぐ消える 📖 低

- **原因**: 新しい値を受け取るたびに `render()` が `hideBanner()` を呼ぶ（`app.js:53`）。SSE の受信で、設定まわりのエラー表示まで消えてしまう。

### B-14 `config.json` が壊れていると、UI で保存したときに既定値で上書きされる 📖 低

- **原因**: `read_raw()` は JSON が壊れていると黙って `{}` として扱う（`config.py:203-213`）。
- **影響**: 稼働中に手で編集して JSON を壊すと、UI には既定値が表示される。その状態で保存すると、手で書いた Webhook などが失われる。
- **対応案**: 壊れていたら読み込みも保存もエラーとして返す。

---

## 2. セキュリティ

### S-1 UI で保存すると `config.json` のパーミッションが 600 から 644 になる ✅ 高

- **原因**: `install.sh` は 600 に設定している。しかし `write_raw()`（`config.py:265-272`）は一時ファイルを umask に従って作り、それで置き換えるため 644 になる。
- **影響**: Webhook URL と認証トークンが、コンテナ内の他ユーザーから読める。
- **対応案**: 一時ファイルを `os.open(..., 0o600)` で作る。またはユニットファイルに `UMask=0077` を追加する。

### S-2 トークン未設定だと、設定 API に誰でも書き込める（既定で開いている）📖 高

- **影響**: Cloudflare Access を付け忘れたままトンネルで公開すると、外部から次のことができる。
  - Webhook を任意の URL（LAN 内の機器など）に書き換え、サーバーからそこへ繰り返し POST させる（SSRF の踏み台）。`report_interval: 1` にすれば、最短の取得間隔（5 秒）ごとに送られる。
  - `web.host` / `web.port` も API から変更できる（`config.py:255-257`）。再起動後に待ち受けアドレスが変わる（例: `0.0.0.0`）。
- **対応案**:
  - トークン未設定のときは、書き込み系 API を無効（読み取り専用）にする。
  - Webhook の宛先を `discord.com` / `discordapp.com` に限定する。
  - 待ち受けアドレスは UI からは変更できないようにする。

### S-3 「更新」の連打で上流 API を叩き放題 ✅ 中

- **確認結果**: 取得に 0.3 秒かかるスタブで、`/api/refresh` を 10 秒間に 50 回呼ぶと、上流への取得が 37 回発生した。
- **影響**: トークン未設定なら外部の誰でもでき、CoinGecko や為替 API の IP 制限を引き起こせる。
- **対応案**: サーバー側で最小間隔（例: 10 秒）を設け、それより短い要求はまとめる。

### S-4 README の説明と実際の保護範囲が違う ✅ 中

- **現象**: README には「`web.auth_token` を設定すると UI/設定 API にトークンが必要」とある。実際には `/`・`/api/state`・`/api/stream`・`/docs`・`/openapi.json` が認証なしで 200 を返す。
- **対応案**:
  - README を直すか、これらも保護する。
  - `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` で API ドキュメントを非公開にする。
  - 補足: `EventSource` は独自ヘッダーを送れない。SSE を保護するなら、Cookie かクエリパラメータでトークンを渡す設計が必要。

### S-5 トークン比較の方法と、試行回数の制限 📖 低

- `supplied != token`（`server.py:36`）は `hmac.compare_digest` に置き換える。
- 認証失敗の回数制限がないので、総当たりを止められない。

### S-6 セキュリティヘッダーがない 📖 低

- UI は外部依存のない自前ファイルだけなので、`Content-Security-Policy: default-src 'self'` 程度は簡単に付けられる。`X-Content-Type-Options` なども同様。

### S-7 systemd のハードニングを強化できる 📖 低

- `UMask=0077`、`PrivateDevices=true`、`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`、`SystemCallFilter=@system-service` などを追加する。`systemd-analyze security poteto-monitor-web` で評価を確認する。

---

## 3. 入力検証

### V-1 型が不正な値を送ると 400 ではなく 500 になる ✅

- **確認結果**:
  - `alert_threshold: null` → `TypeError`
  - `poll_interval: "abc"` → `ValueError`
  - `threshold: "high"` → `ValueError`
- **原因**: `PUT /api/config` は `ConfigError` しか捕まえていない（`server.py:106-109`）。

### V-2 `"vs": "usd"`（文字列）が 1 文字ずつに分解される ✅

- **確認結果**: `('u', 's', 'd')` になる（`config.py:73`）。

### V-3 `history_limit: 0` で履歴が無制限に溜まる ✅

- **原因**: `history[-0:]` はリスト全体を返す（`poller.py:72`, `monitor.py:61`）。負の値も意図しない動きになる。

**まとめての対応案**: FastAPI に同梱の **Pydantic** で設定モデルを定義する。手書きの検証（`config.py` の約 120 行）を置き換えられ、フィールドごとのエラーメッセージを 422 で返せる。

---

## 4. 効率化

### E-1 為替は 1 日 1 回しか更新されないのに、毎分取りに行っている（最大の無駄）

- open.er-api.com のデータは **1 日 1 回** の更新。公式は「1 時間に 1 回なら制限されない」としていて、制限にかかると 429 が 20 分続く。
- `config.example.json`（USD と EUR の 2 系統）を 60 秒間隔で使うと **1 日 2,880 回**。
- **対応案**:
  - レスポンスの `time_next_update_unix` までキャッシュする。1 日数回で済む。
  - ドル円が「日次の値」であることと、データの時刻を UI に表示する。「リアルタイム」表記の誤解を避けるため。

### E-2 CoinGecko を毎回 2 回呼んでいる

- `fetch_crypto`（`providers.py:58`）と `fetch_ratio`（`providers.py:147`）が、別々に `simple/price` を呼んでいる。1 回にまとめられる。
- 利用枠の目安:
  - キーなし: 「毎分 10〜30 回程度で変動、定期ポーリングには不向き」とされている。
  - 無料の Demo キー: 毎分 100 回、ただし **月 10,000 回** の上限がある。
- 60 秒間隔だと、1 回にまとめても月 43,200 回で約 4 倍超える。上限内に収めるには約 260 秒以上の間隔が必要。
- **対応案**: Demo キー（`x-cg-demo-api-key` ヘッダー）に対応し、呼び出しを統合したうえで、E-3 の API ごとの間隔設定を入れる。

### E-3 根本原因は、全 API 共通の `poll_interval` 1 つで回していること

- API ごとに取得間隔（キャッシュの有効期間）を持たせる。
  - Hyperliquid: 数秒ごとで問題ない（`allMids` は重み 2、REST 全体の上限は毎分 1,200）。
  - CoinGecko: 数分ごと。
  - 為替: 1 日 1 回。
- BTC / ETH / SOL など主要銘柄の高頻度表示は Hyperliquid に任せ、CoinGecko はマイナー銘柄用にすると、上限の問題がほぼ消える。
- 注意: Hyperliquid は先物（perp）の板中値なので、現物価格とは少しずれる。

### E-4 HTTP 接続を毎回作り直している

- 各取得関数は `session` 引数を受け取れるのに、poller が渡していない（`poller.py:34`）。そのため毎回 TCP と TLS の接続からやり直している。
- `requests.Session` を `AppContext` に 1 つ持たせて使い回す。

### E-5 取得が順番待ち（直列）

- CoinGecko → 為替（通貨の系統数ぶん）→ Hyperliquid → レート比、と順に実行し、それぞれタイムアウトが 15 秒。最悪の場合、これが積み重なる。
- API ごとに並列化する（`ThreadPoolExecutor`、または `httpx.AsyncClient`）。

### E-6 Discord への送信が画面更新を待たせている

- 送信（`poller.py:51-55`）が画面への配信（`poller.py:58-59`）より先にある。Discord の応答が遅いと、最大 15 秒画面の更新が遅れる。
- 順序を逆にし、通知は別タスクとして実行する。

### E-7 `prices.json` を毎回書いているのに、常駐モードでは読んでいない

- 毎回書き込んでいる（`poller.py:62-64`）が、`state.previous` は空の状態で起動する（`state.py:19`）。
- 起動時に読み込めば、再起動後も前回値との比較を続けられる。読まないのであれば、書く必要もない。

### E-8 履歴の持ち方

- `history.json` は毎回ファイル全体を読み書きしている。常駐モードではレポートを送るときしか記録しない（`poller.py:65-72`）。
- スパークライン（小さな推移グラフ）はブラウザのメモリにしかないため、開いた直後は空。線が出るまで取得 2 回分待つ必要がある。
- **対応案**: 標準ライブラリの **SQLite** に時系列を保存し、`/api/history` から初期表示する。B-4、F-1、F-3、F-4 の土台にもなる。

### E-9 429（レート制限）を受けても再試行の待ち時間を調整していない 📖

- `Retry-After` ヘッダーも段階的な待ち時間（指数バックオフ）もなく、制限中でも毎回取りに行く。
- 制限を受けたら、その API だけ一定時間スキップする。

### E-10 SSE 配信の細かい無駄

- 購読者ごとに `json.dumps` している（`server.py:89`）。配信 1 回につき 1 回だけ変換すればよい。
- キューが満杯のとき、**最新**の値の方を捨てている（`state.py:82-84`）。スナップショットは最新だけ届けば十分なので、最新 1 件を残す方式にする。

### E-11 フロントエンドの細かい点

- canvas が 260×40 固定のまま CSS で引き伸ばしていて、高解像度の画面ではぼやける（`app.js:86`）。
- SSE の再接続のたびに、同じ値がスパークラインに重複して追加される（`app.js:65`）。

---

## 5. 足りない機能

| ID | 機能 | 内容 |
|---|---|---|
| F-1 | 時間窓アラート・クールダウン・価格ライン通知 | 1 時間前比・24 時間前比での判定、連発防止、「ドル円が 150 を割ったら」のような価格ラインでの通知。現状は % 変化しかない |
| F-2 | 取得失敗・復旧の通知と `/healthz` | 今は API が止まっても誰も気付けない。Uptime Kuma などの外部監視にも使える |
| F-3 | 24 時間変化率の表示 | CoinGecko は `include_24hr_change`、Hyperliquid は `metaAndAssetCtxs` の `prevDayPx`（24 時間前の価格）で取れる |
| F-4 | サーバー側の履歴と 24 時間・7 日チャート | E-8 の SQLite が前提 |
| F-5 | 派生アセット（レート比の一般化） | 今は CoinGecko 同士に限られる。分子・分母に任意の監視キーを指定できれば、`hl:HYPE × forex:USDJPY`（HYPE の円建て）などを API 呼び出しを増やさずに作れる |
| F-6 | Hyperliquid の WebSocket | `wss://api.hyperliquid.xyz/ws` で `allMids` を購読する。README の「リアルタイム」に最も近い。無通信だと切断されるため ping が必要 |
| F-7 | UI の改善 | Webhook・トークンの削除操作（空欄は「変更なし」扱いで消せない）、監視対象の並べ替え、Esc で未保存の変更を捨てる前の確認、環境変数で上書き中の項目の明示、入力エラーの表示 |
| F-8 | タイムゾーン設定 | Discord の通知は UTC 固定（`notify.py:40`）。日本向けなら JST を選べた方がよい |
| F-9 | CI と、UI・poller のテスト | GitHub Actions で pytest と ruff を回す。B-1 と B-2 は Playwright のテスト 1 本で検出できた。poller のテストは現在ゼロ |
| F-10 | 更新手順 | `install.sh` を再実行しても、稼働中のサービスは `enable --now` では再起動されない（`install.sh:50`）。再起動の処理と、README への更新手順の追記が必要。なお `pip` は同じバージョンでも再インストールされ、コードは更新されることを確認済み |

※ 前回の報告では B-9 と B-10 を「足りない機能」に入れていましたが、不具合なので「1. バグ」に移しました。

---

## 6. 細かい指摘

| ID | 内容 | 場所 |
|---|---|---|
| N-1 | `except (ProviderError, Exception)` が冗長 | `monitor.py:112` |
| N-2 | `logging.basicConfig` が import 時に実行される | `monitor.py:16` |
| N-3 | バージョン番号を二重に管理している。`importlib.metadata.version()` に一本化する | `pyproject.toml:7`, `__init__.py:7` |
| N-4 | README は既定の監視対象を「BTC/ETH/ドル円」としているが、実際は HYPE も含む | `config.py:30-35` |
| N-5 | `Reading.type` のコメントが crypto / forex だけで古い | `models.py:45` |
| N-6 | `market` は読み込まれるが使われていない | `config.py:112` |
| N-7 | `requirements.txt` に Web 機能の依存が書かれていない | `requirements.txt` |
| N-8 | テストに使っていない import がある（`json`, `config_mod`） | `tests/test_monitor.py:5, 9` |
| N-9 | pytest 実行時に Starlette の警告が出る（`httpx` を使う TestClient は非推奨、`httpx2` を案内） | 開発用の依存 |
| N-10 | `project.license` を TOML テーブルで書くのは非推奨。setuptools が「2027-02-18 までに修正が必要」と警告する。`license = "MIT"` に変える ✅ | `pyproject.toml:11` |
| N-11 | README は Debian 12 が前提。Debian 13 は 2025-08-09 にリリースされ、Proxmox VE 9 も Debian 13 ベース。記載の更新を推奨する（この環境の Python 3.13 で既存テストが通ることは確認済み） | README |
| N-12 | 定期レポートの色が、1 銘柄でも下がると赤になる | `notify.py:21-27` |
| N-13 | 傾向の絵文字のしきい値が ±5% 固定。為替だと 5% は大きすぎて、ほぼ 📈 / 📉 しか出ない | `format.py:65-76` |
| N-14 | 価格カードの領域が `aria-live="polite"` のため、毎分スクリーンリーダーが読み上げてしまう | `index.html:31` |

---

## 7. 未検証・要確認

### U-1 Hyperliquid の銘柄名を大文字に変換している ❓

- `coin` を `.upper()` で大文字にしている（`config.py:109`）。Hyperliquid に小文字を含む銘柄名（1000 単位の `kPEPE` など）があれば、一致しなくなる可能性がある。
- 検索結果には `kPEPE` と `KPEPE` の両方の表記があり、API が大文字・小文字を区別するかは確認できなかった。この環境からは API に直接アクセスできなかった。
- 確認方法: `allMids` の応答のキーに小文字を含むものがあるか調べる。

---

## 8. 対応の進め方（提案）

| 段階 | 対象 | 狙い |
|---|---|---|
| 1. まず直す | B-1, B-2, B-3, B-5, B-7, B-8, S-1, S-3, V-1〜V-3 | どれも数行〜数十行で直せる。再現テストも一緒に追加する |
| 2. API 制限への対策 | B-6, E-1〜E-7, E-9 | 為替のキャッシュ、CoinGecko 呼び出しの統合、接続の再利用、API ごとの取得間隔、エラーの切り分け |
| 3. 履歴の土台 | E-8 → B-4 / F-1, F-2, F-3, F-4 | SQLite の時系列の上に、時間窓アラート・死活通知・24 時間変化率・チャートを載せる |
| 4. 拡張 | F-5, F-6, F-7〜F-10, S-2, S-4〜S-7, 残りの B / N | WebSocket・派生アセット・UI・CI・ハードニング |

---

## 付録 A: 検証の記録

実行環境: Python 3.13.16、fastapi 0.142.2、starlette 1.7.0、uvicorn 0.54.0、requests 2.34.2、Playwright 1.56.1（Chromium）

| 項目 | 方法 | 結果 |
|---|---|---|
| 既存テスト | `pytest` | 23 件すべて通過（Starlette の警告 1 件） |
| B-1 | 1280px と 390px で、各要素の中心の最前面にある要素を調べる | 保存ボタン・入力欄・✕ のすべてで `drawer-scrim`。入力欄をクリックするとドロワーが閉じた |
| B-2 | `pair` 形式の為替・レート比と、`key` 付きの暗号資産の 3 件で、何も変えずに保存 | 3 件 → 1 件。`key` も消えた |
| B-3 | 値が 100 → 110 と変わるスタブで取得を 2 回 | 配信時は `[None, 10.0]`、直後のスナップショットは `0.0`。実画面でも読み込み直後は全カード「+0.00%」 |
| B-5 | 価格が変わらないスタブで、基準通貨を usd → jpy | `🚀 BITCOIN: +14900.00%` が送信された |
| B-6 | 為替だけが例外を投げるスタブ | `fetch_all` 全体が `RuntimeError` になった |
| B-7 | SSE に接続したまま SIGTERM を送る | 30 秒以上終了しなかった |
| B-8 | `money(1.234e-07, "usd")` | `$0` |
| S-1 | パーミッション 600 の `config.json` に対して `write_raw` | `0o644` になった |
| S-3 | `/api/refresh` を 10 秒間に 50 回（取得に 0.3 秒かかるスタブ） | 上流への取得が 37 回 |
| S-4 | `curl /docs` と `curl /openapi.json` | どちらも 200 |
| V-1 | `parse_config` に不正な型を渡す | `TypeError` / `ValueError`（`ConfigError` ではないので 500 になる） |
| V-2 | `"vs": "usd"` | `('u', 's', 'd')` |
| V-3 | `[1, 2, 3][-0:]` | `[1, 2, 3]`（全件） |
| N-10 | `pip wheel -v` | `SetuptoolsDeprecationWarning`（期限 2027-02-18） |
| F-10 補足 | 同じバージョンのまま `pip install` を再実行 | 更新したコードが反映された（問題なし） |

---

## 付録 B: 参考資料

外部 API の数値は、公式ドキュメントの検索結果から取っています。この環境では公式ページを直接開けなかったため、導入前に最新の値を確認してください。

- [CoinGecko: Keyless Public API](https://docs.coingecko.com/docs/keyless-public-api.md) — キーなしの利用枠（毎分 10〜30 回程度で変動）、定期ポーリングには不向き
- [CoinGecko: Plans & Pricing](https://coingecko.com/api/pricing) — Demo プランは毎分 100 回・月 10,000 回
- [CoinGecko: データの更新頻度](https://support.coingecko.com/hc/en-us/articles/4538807536665-How-often-does-data-get-updated-or-refreshed)
- [ExchangeRate-API: Open Access Endpoint](https://www.exchangerate-api.com/docs/free) — 1 日 1 回の更新、1 時間に 1 回なら制限されない、制限時は 429 が 20 分
- [Hyperliquid: Rate limits and user limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits) — REST の合計上限は毎分 1,200、`allMids` の重みは 2
- [Hyperliquid: WebSocket](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)
- [metaAndAssetCtxs（GoldRush docs）](https://goldrush.dev/docs/api-reference/hyperliquid/meta-and-asset-ctxs/) — `prevDayPx` / `midPx` / `markPx`
- [Discord Embed Limits（Python Discord）](https://www.pythondiscord.com/pages/guides/python-guides/discord-embed-limits/) — フィールドは 25 個まで、合計 6,000 字まで、1 メッセージあたり embed 10 個まで
- [Debian "trixie" リリース情報](https://www.debian.org/releases/trixie/index.ko.html) / [Proxmox VE 9.0 は Debian 13 ベース](https://www.deskmodder.de/blog/?p=201501)
