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

## デプロイ

```bash
gcloud run deploy savepoint --project=<デプロイ先のGCPプロジェクトID> --source .
```
