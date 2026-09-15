FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# --proxy-headers / --forwarded-allow-ips について（2026-09-15）:
# Cloud RunはTLSをフロントエンドで終端し、コンテナへはHTTPで転送したうえで
# X-Forwarded-Proto: https を付与する。uvicornはproxy_headers自体は既定で有効だが、
# forwarded_allow_ipsの既定値が127.0.0.1のため、Googleのフロントエンドから来た
# このヘッダーを信用せず、アプリ側はリクエストのスキームをhttpと認識してしまう。
# その結果 request.url が http:// となり、oauthlibが
# 「(insecure_transport) OAuth 2 MUST utilize https.」を送出して
# /oauth/callback（Googleアカウント接続）が必ず失敗していた（萬年環境で実際に発生）。
# 招待メールに載せるURL(main.py::_app_base_url)も同じ理由でhttp://になっていた。
# Cloud Runではコンテナに直接到達できるのはGoogleのフロントエンド経由のみ
# （本サービスはさらにIAPのサービスエージェントにしかrun.invokerを与えていない）ため、
# 転送ヘッダーを信頼して差し支えない。
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
