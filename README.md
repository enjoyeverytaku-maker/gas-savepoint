# SavePoint

GAS（Google Apps Script）を業務利用する非エンジニア企業向けの変更管理システム。
変更履歴の確認・過去状態への復元（ロールバック）・変更のレビュー承認・権限管理・監査ログ・台帳管理をブラウザだけで行える。

- 技術スタック: FastAPI / Firestore / Secret Manager / Cloud Run / Google OAuth (Internal) / Apps Script API / Gemini API
- 仕様・タスク管理は非公開の別リポジトリ（社内SSOT）側で行う。このリポジトリはアプリケーション本体のみを保持する

## セットアップ

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## デプロイ

```bash
gcloud run deploy savepoint --project=<デプロイ先のGCPプロジェクトID> --source .
```
