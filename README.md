# SavePoint

GAS（Google Apps Script）を業務利用する非エンジニア企業向けの変更管理システム。
変更履歴の確認・過去状態への復元（ロールバック）・変更のレビュー承認・権限管理・監査ログ・台帳管理をブラウザだけで行える。

- 技術スタック: FastAPI / Firestore / Secret Manager / Cloud Run / Google OAuth (Internal) / Apps Script API / Gemini API
- 仕様・タスク管理は非公開の別リポジトリ（社内SSOT）側で行う。このリポジトリはアプリケーション本体のみを保持する

## セットアップ

```bash
pip install -r requirements.txt
export GCP_PROJECT=<デプロイ先のGCPプロジェクトID>
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

4. アプリを起動し `/oauth/connect` にアクセスすると同意画面へリダイレクトされ、許可後は自動的に `savepoint-oauth-refresh-token` シークレットへRefresh Tokenが保存される（`/api/oauth/status` で接続状態を確認できる）

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
ユーザーを指定できる（ダッシュボードもこのヘッダーを自動付与する）。**本番では絶対に設定しないこと**
（`OAUTHLIB_INSECURE_TRANSPORT` と同種の開発専用の抜け穴）。

```bash
export SAVEPOINT_DEV_MODE=1  # ローカル開発のみ。本番では設定しない
curl -X POST localhost:8080/api/users -H "X-Debug-User-Email: you@example.com" -H "Content-Type: application/json" -d '{"email":"you@example.com","role":"admin"}'
```

## デプロイ

```bash
gcloud run deploy savepoint --project=<デプロイ先のGCPプロジェクトID> --source .
```
