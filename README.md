# SavePoint

GAS（Google Apps Script）を業務利用する非エンジニア企業向けの変更管理システム。
変更履歴の確認・過去状態への復元（ロールバック）・セーブ時のAI影響レビュー・権限管理・監査ログ・台帳管理をブラウザだけで行える。

- 技術スタック: FastAPI / Firestore / Secret Manager / Cloud Run / Google OAuth (Internal) / Apps Script API / Vertex AI (Gemini)
- 仕様・タスク管理は非公開の別リポジトリ（社内SSOT）側で行う。このリポジトリはアプリケーション本体のみを保持する

## セットアップ

```bash
pip install -r requirements.txt
export GCP_PROJECT=<デプロイ先のGCPプロジェクトID>
export VERTEX_LOCATION=asia-northeast1  # 省略時はus-central1。使用モデル(gas/vertex_client.py::GEMINI_MODEL)が
                                          # 利用可能なリージョンを指定すること（モデルごとに提供リージョンが異なる。
                                          # 2026-09-13時点、gemini-3.5-flashはasia-northeast1では動作するがus-central1では404）
export OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback  # 本番はCloud RunのURLに変更
export OAUTHLIB_INSECURE_TRANSPORT=1  # ローカル(http://localhost)のみ。google-auth-oauthlibはデフォルトでHTTPS必須のため。本番(Cloud Run=https)では絶対に設定しない
uvicorn main:app --reload
```

### Google OAuth接続の事前準備（GCPコンソールでの手動作業）

コードだけでは代替できない、GCPコンソール側での設定が必要です。

1. 対象のGCPプロジェクトでOAuth同意画面を **Internal** ユーザータイプで作成する
2. OAuth 2.0クライアントID（ウェブアプリケーション）を作成し、リダイレクトURIに `OAUTH_REDIRECT_URI` の値（例: `https://<Cloud RunのURL>/oauth/callback`）を登録する
3. 発行されたクライアントID・シークレットをSecret Managerへ登録する

```bash
echo -n "<クライアントID>" | gcloud secrets create savepoint-oauth-client-id --data-file=- --project=$GCP_PROJECT
echo -n "<クライアントシークレット>" | gcloud secrets create savepoint-oauth-client-secret --data-file=- --project=$GCP_PROJECT
```

4. アプリを起動し、ログイン中の本人が `/oauth/connect` にアクセスすると同意画面へリダイレクトされ、
   許可後は自動的にそのアカウント専用のSecret Managerシークレット（`savepoint-oauth-token-<メールのハッシュ>`）
   へRefresh Tokenが保存される（2026-09-14、GASを作った担当者それぞれが自分のGoogleアカウントで個別に
   接続する設計へ変更。パートナーズ版`multiuser_oauth.py`を参考にした。自分の接続状態は
   `GET /api/oauth/my-connection` で確認できる。`GET /api/oauth/status` は開発時のRBACユーザー切り替え
   専用に用途が分かれている、後述）

## テスト

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

テストは2種類ある。

**1. 単体テスト**（Javaなしで動く）
外部サービスに接続せず、協調する関数を差し替えて「呼び出しの順序と条件分岐」を固定する。

| 対象 | 何を守っているか |
|------|----------------|
| `tests/test_diff.py` | 差分計算（`gas/diff.py`） |
| `tests/test_users_rbac.py` | 権限判定（`auth/users.py`）・初回管理者登録での自己ロックアウト防止 |
| `tests/test_rollback.py` | 競合時に書き込みを止めるか・復元前バックアップ・失敗時の記録（`gas/rollback.py`） |
| `tests/test_api_permissions.py` | API層の権限チェックと監査ログの実行者詐称防止（`gas/routes.py`） |
| `tests/test_changes_baseline.py` | 変更検知の比較基準（`gas/changes.py`） |

**2. 結合テスト**（Firestoreエミュレータを使う / `tests/test_firestore_integration.py`）
登録の一意性・バージョン採番・権限の一括設定・削除の消し残しなど、Firestoreの実挙動に
依存する部分を検証する。同時実行（登録ボタン連打・同時保存）も含む。

エミュレータはJava製のため、以下が必要。**未導入の環境では結合テストは自動的にスキップされる**
（単体テストだけが実行される）。

```bash
brew install openjdk                                      # macOS
gcloud components install cloud-firestore-emulator beta
```

Homebrew版OpenJDKはPATHに入らないが、`tests/conftest.py` が既定の場所
（`/opt/homebrew/opt/openjdk/bin`）を自動で探すため、PATHの設定は不要。
すでに起動済みのエミュレータを使いたい場合は `FIRESTORE_EMULATOR_HOST` を設定しておく。

Apps Script API・Vertex AIを伴う経路は自動テストの対象外。ローカル開発環境
（`SAVEPOINT_DEV_MODE=1`）で実際のGCPプロジェクトに対して手動で確認する。

## 権限管理（RBAC、F6）

SavePoint画面自体へのログイン認証は、本番ではCloud Run + IAP（Identity-Aware Proxy）が担う想定。
IAPは認証済みユーザーのメールアドレスを `X-Goog-Authenticated-User-Email` ヘッダーでアプリへ渡すため、
`auth/users.py` はそのヘッダーを信頼してユーザーを特定する（Cloud Run側でのIAP設定はT17のデプロイ時に別途行う）。

`users` コレクションが1件も無い間（初回導入時）は、認証済みの誰でも `admin` 扱いになる（ブートストラップ）。
**最初にやること**: 管理者が `POST /api/users` で自分自身を `admin` として登録する。それ以降、未登録のユーザーは
安全側のデフォルトとして `viewer` 扱いになる。

ロール: `admin`（ユーザー管理・GAS登録・承認/却下含む全操作） / `editor`（差分確認・セーブポイント・ロールバック・リリース申請） / `viewer`（閲覧のみ）。

### ローカル開発時

IAPが無いローカル環境では、`SAVEPOINT_DEV_MODE=1` を設定した場合のみ `X-Debug-User-Email` ヘッダーで
ユーザーを指定できる（画面側は `SAVEPOINT_DEV_USER_EMAIL` の値を `GET /api/oauth/status` 経由で
受け取り、このヘッダーを自動付与する。`?debug_email=` クエリパラメータでも同じ値を渡せる。
`<a href>` での素のページ遷移（`/oauth/connect` 等）はJSの `fetch()` と違いヘッダーを付けられないため）。
**本番では絶対に設定しないこと**（`OAUTHLIB_INSECURE_TRANSPORT` と同種の開発専用の抜け穴）。

```bash
export SAVEPOINT_DEV_MODE=1                        # ローカル開発のみ。本番では設定しない
export SAVEPOINT_DEV_USER_EMAIL=you@example.com     # 画面側でシミュレートするユーザー（RBACのロール切り替え用。GAS接続とは別概念）
curl -X POST localhost:8080/api/users -H "X-Debug-User-Email: you@example.com" -H "Content-Type: application/json" -d '{"email":"you@example.com","role":"admin"}'
```

RBACのユーザー（上記）と、GASを操作するために接続するGoogleアカウント（`/oauth/connect`）は別概念。
ローカルでGAS操作を試す場合は、admin登録後に画面から改めて `/oauth/connect` を踏んでGoogleの同意画面を
通し、そのユーザー自身のGoogleアカウントを接続する必要がある。

## 自動検知（F11、Cloud Scheduler）

登録済み全GASプロジェクトの変更有無を定期的にチェックする。人間のRBACとは別に、
Secret Manager管理の共有シークレットトークンで保護する（Cloud SchedulerはIAP配下の
「ユーザー」ではないため）。

```bash
# トークンをSecret Managerへ登録（ランダムな値。以後Cloud Scheduler側の設定にも使う）
openssl rand -hex 32 | gcloud secrets create savepoint-scheduler-token --data-file=- --project=$GCP_PROJECT
```

デプロイ後、Cloud Schedulerジョブを作成する（spec.md §6: 1日数回程度を想定。TODO要確定）:

```bash
gcloud scheduler jobs create http savepoint-sync-check \
  --project=$GCP_PROJECT --location=<リージョン> \
  --schedule="0 */4 * * *" \
  --uri="https://<Cloud RunのURL>/api/sync/check-all" \
  --http-method=POST \
  --headers="X-Scheduler-Token=<savepoint-scheduler-tokenの値>"
```

## AI機能（F12 README自動生成・セーブ時の影響レビュー、Vertex AI経由のGemini API）

GASプロジェクト登録時・セーブポイント作成時のREADME自動生成、およびセーブ実行時の
AI影響レビュー生成は、いずれもVertex AI経由でGemini（`gemini-2.5-flash`）を呼び出す
（`gas/vertex_client.py`）。このアプリは既にGCP（`gas-savepoint`）上で動作しており
Application Default Credentials（ローカル: `gcloud auth application-default login`、
Cloud Run: サービスアカウント）で認証できるため、**Google AI Studio発行の別建て
APIキーは不要**（2026-09-13、会長指摘を受けAPIキー方式から切り替え）。

事前に以下を満たしていること:
```bash
gcloud services enable aiplatform.googleapis.com --project=$GCP_PROJECT
```
Cloud Run本番環境では、サービスアカウントに`roles/aiplatform.user`ロールが必要。

いずれもベストエフォート運用（失敗してもプロジェクト登録・セーブポイント作成自体は
必ず成功する）。

## セキュリティ（spec.md §14）

- 秘密情報（OAuth Client Secret・Refresh Token・Cloud Scheduler用トークン）は
  すべてSecret Manager経由（`auth/secrets.py`）で管理し、コードやFirestoreへ平文保存しない
- HTTPSはCloud Run標準機能を利用する（アプリ側での追加実装は不要）
- `SAVEPOINT_DEV_MODE` / `OAUTHLIB_INSECURE_TRANSPORT` はローカル開発専用の抜け穴。
  Cloud Run上（`K_SERVICE` 環境変数が常に設定される）でこれらが有効な場合、
  `main.py` が起動時に `RuntimeError` を送出してアプリの起動を拒否する
  （デプロイ時の設定ミスで本番にローカル用の抜け穴が漏れ出ることを防ぐ安全装置、2026-09-10追加）
- git履歴・コード全体に秘密情報のハードコードが無いことを確認済み（2026-09-10監査）

## デプロイ

```bash
gcloud run deploy savepoint --project=<デプロイ先のGCPプロジェクトID> --source .
```
