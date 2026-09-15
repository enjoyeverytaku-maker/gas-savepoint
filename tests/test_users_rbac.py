"""権限判定（auth/users.py）の回帰テスト。

2026-09-15に、メールアドレスを正規化せずFirestoreのドキュメントIDに使っていた不具合を
修正した。Firestoreのドキュメントは大文字小文字を区別するため、管理者が
「Taro.Yamada@mannen.jp」で登録したユーザーにIAPが「taro.yamada@mannen.jp」を渡すと、
ロールが引けず静かに権限なし扱いに落ちていた。ここで正規化の一貫性を固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth.users import (  # noqa: E402
    get_current_user_email,
    get_project_role,
    get_user_role,
    normalize_email,
    upsert_user,
)


class TestNormalizeEmail:
    def test_小文字に揃える(self):
        assert normalize_email("Taro.Yamada@Mannen.JP") == "taro.yamada@mannen.jp"

    def test_前後の空白を落とす(self):
        assert normalize_email("  user@example.com \n") == "user@example.com"

    def test_空やNoneは空文字になる(self):
        assert normalize_email("") == ""
        assert normalize_email(None) == ""  # type: ignore[arg-type]


class _FakeRequest:
    """get_current_user_emailが参照する部分だけを持つ最小のリクエスト。"""

    def __init__(self, headers: dict | None = None, query_params: dict | None = None):
        self.headers = headers or {}
        self.query_params = query_params or {}


class TestGetCurrentUserEmail:
    def test_IAPヘッダーの接頭辞を取り除いて小文字化する(self):
        request = _FakeRequest({"X-Goog-Authenticated-User-Email": "accounts.google.com:Taro@Mannen.JP"})
        assert get_current_user_email(request) == "taro@mannen.jp"

    def test_未認証ならNoneを返す(self):
        assert get_current_user_email(_FakeRequest()) is None

    def test_開発モードでないときはデバッグヘッダーを信用しない(self, monkeypatch):
        monkeypatch.delenv("SAVEPOINT_DEV_MODE", raising=False)
        request = _FakeRequest({"X-Debug-User-Email": "attacker@example.com"})
        assert get_current_user_email(request) is None

    def test_開発モードならデバッグヘッダーを使う(self, monkeypatch):
        monkeypatch.setenv("SAVEPOINT_DEV_MODE", "1")
        request = _FakeRequest({"X-Debug-User-Email": "Dev@Example.com"})
        assert get_current_user_email(request) == "dev@example.com"

    def test_開発モードならクエリパラメータも使える(self, monkeypatch):
        monkeypatch.setenv("SAVEPOINT_DEV_MODE", "1")
        request = _FakeRequest(query_params={"debug_email": "Dev@Example.com"})
        assert get_current_user_email(request) == "dev@example.com"


def _fake_db_with_user(role: str | None):
    """usersコレクションの1件取得だけを模したFirestoreクライアント。"""
    client = MagicMock()
    snapshot = MagicMock()
    snapshot.exists = role is not None
    snapshot.to_dict.return_value = {"role": role} if role else {}
    client.collection.return_value.document.return_value.get.return_value = snapshot
    return client


class TestGetUserRole:
    def test_大文字で登録されていても小文字のIDで引きにいく(self):
        client = _fake_db_with_user("admin")
        with patch("auth.users.db", return_value=client):
            assert get_user_role("Taro@Mannen.JP") == "admin"
        client.collection.assert_called_with("users")
        client.collection.return_value.document.assert_called_with("taro@mannen.jp")

    def test_未登録ユーザーはmemberになる(self):
        client = _fake_db_with_user(None)
        # usersコレクションに他の利用者がいる（＝ブートストラップ状態ではない）状況を作る
        client.collection.return_value.limit.return_value.stream.return_value = iter([MagicMock()])
        with patch("auth.users.db", return_value=client):
            assert get_user_role("unknown@example.com") == "member"

    def test_誰も登録されていない初回はadmin扱いになる(self):
        client = _fake_db_with_user(None)
        client.collection.return_value.limit.return_value.stream.return_value = iter([])
        with patch("auth.users.db", return_value=client):
            assert get_user_role("first@example.com") == "admin"


class TestGetProjectRole:
    def test_グローバルadminは常にownerとして扱われる(self):
        with patch("auth.users.get_user_role", return_value="admin"):
            assert get_project_role("proj1", "admin@example.com") == "owner"

    def test_メンバーは個別付与が無ければNone(self):
        client = MagicMock()
        snapshot = MagicMock()
        snapshot.exists = False
        client.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = snapshot
        with patch("auth.users.get_user_role", return_value="member"), patch("auth.users.db", return_value=client):
            assert get_project_role("proj1", "member@example.com") is None

    def test_メンバー権限の参照も小文字のIDで行う(self):
        client = MagicMock()
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"role": "developer"}
        members = client.collection.return_value.document.return_value.collection.return_value
        members.document.return_value.get.return_value = snapshot
        with patch("auth.users.get_user_role", return_value="member"), patch("auth.users.db", return_value=client):
            assert get_project_role("proj1", "Taro@Mannen.JP") == "developer"
        members.document.assert_called_with("taro@mannen.jp")


class TestUpsertUserBootstrapGuard:
    """初回登録時に操作者が締め出されないことを固定する（2026-09-15、萬年環境で実際に発生）。"""

    def _client(self, bootstrap: bool):
        """ドキュメントIDごとに別々のモックを返すFirestoreクライアント。

        document()が常に同じモックを返すと「どのIDに書いたか」を区別できないため、
        IDごとに保持する（読み取りのget()と書き込みのset()も分けて数えられる）。
        """
        docs: dict[str, MagicMock] = {}

        def document(doc_id: str) -> MagicMock:
            if doc_id not in docs:
                doc = MagicMock()
                snapshot = MagicMock()
                snapshot.exists = True
                snapshot.to_dict.return_value = {"role": "admin"}
                doc.get.return_value = snapshot
                docs[doc_id] = doc
            return docs[doc_id]

        collection = MagicMock()
        collection.document.side_effect = document
        # _is_bootstrap_state(): usersが0件ならブートストラップ状態
        collection.limit.return_value.stream.return_value = (
            iter([]) if bootstrap else iter([MagicMock()])
        )
        client = MagicMock()
        client.collection.return_value = collection
        client._docs = docs
        return client

    def _written(self, client) -> dict[str, int]:
        """実際に書き込み(set)が行われたドキュメントIDと回数を返す。"""
        return {doc_id: doc.set.call_count for doc_id, doc in client._docs.items() if doc.set.call_count}

    def test_初回登録では操作者もadminとして記録される(self):
        client = self._client(bootstrap=True)
        with patch("auth.users.db", return_value=client):
            upsert_user(email="other@example.com", role="admin", updated_by="me@example.com")
        written = self._written(client)
        assert "me@example.com" in written, "操作者が記録されず、登録直後に締め出される"
        assert "other@example.com" in written

    def test_すでに利用者がいる場合は操作者を勝手に登録しない(self):
        client = self._client(bootstrap=False)
        with patch("auth.users.db", return_value=client):
            upsert_user(email="other@example.com", role="member", updated_by="me@example.com")
        assert self._written(client) == {"other@example.com": 1}

    def test_自分自身を初回登録する場合は二重書き込みしない(self):
        client = self._client(bootstrap=True)
        with patch("auth.users.db", return_value=client):
            upsert_user(email="me@example.com", role="admin", updated_by="me@example.com")
        assert self._written(client) == {"me@example.com": 1}

    def test_操作者の判定も大文字小文字を無視する(self):
        client = self._client(bootstrap=True)
        with patch("auth.users.db", return_value=client):
            upsert_user(email="Other@Example.com", role="admin", updated_by="Me@Example.COM")
        assert set(self._written(client)) == {"other@example.com", "me@example.com"}
