"""API層の権限チェック（gas/routes.py・main.py）の回帰テスト。

「誰がどのGASに何をできるか」はこの商品が内部統制として売っている中核だが、
2026-09-15時点でgas/routes.pyのカバレッジは0%で、本番でも管理者2名しか使っていないため
権限を絞った状態が一度も検証されていなかった。権限判定を誤ると、
他人のGASを操作できてしまう／正当な担当者が締め出されるという直接的な事故になる。

FastAPIのTestClientでHTTPとして呼び、認証と保存層だけを差し替えて判定ロジックを固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from gas import routes  # noqa: E402

PROJECT = {
    "id": "proj-1",
    "project_name": "テスト用GAS",
    "script_id": "script-1",
    "google_account": "owner@example.com",
    "status": "active",
}


@pytest.fixture
def client(monkeypatch):
    """認証済みユーザーを差し替えられるTestClient。"""
    state = {"email": "member@example.com", "global_role": "member", "project_role": None}

    monkeypatch.setattr("auth.users.get_current_user_email", lambda request: state["email"])
    monkeypatch.setattr("auth.users.get_user_role", lambda email: state["global_role"])
    monkeypatch.setattr("auth.users.get_project_role", lambda pid, email: "owner" if state["global_role"] == "admin" else state["project_role"])
    # routes側は import 済みの参照を持つため、そちらも差し替える
    monkeypatch.setattr(routes, "get_user_role", lambda email: state["global_role"])
    monkeypatch.setattr(routes, "get_project", lambda pid: PROJECT if pid == PROJECT["id"] else None)
    monkeypatch.setattr(routes, "list_projects", lambda: [PROJECT])
    monkeypatch.setattr(routes, "list_projects_for_user", lambda email, is_admin: [PROJECT] if is_admin else [])
    monkeypatch.setattr(routes, "list_members", lambda pid: [])
    monkeypatch.setattr(routes, "list_operations", lambda: [])
    monkeypatch.setattr(routes, "log_operation", lambda **kwargs: None)

    test_client = TestClient(main.app)
    test_client.state = state
    return test_client


def as_user(client, email, global_role, project_role=None):
    client.state.update({"email": email, "global_role": global_role, "project_role": project_role})


class TestUnauthenticated:
    def test_未認証は401(self, client, monkeypatch):
        monkeypatch.setattr("auth.users.get_current_user_email", lambda request: None)
        assert client.get("/api/projects").status_code == 401


class TestGlobalRole:
    """管理者限定の操作を、一般メンバーが実行できないこと。"""

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/audit-logs"),
            ("GET", "/api/users"),
            ("DELETE", "/api/projects/proj-1"),
            ("GET", "/api/members/by-user/someone@example.com"),
            ("PUT", "/api/members/bulk"),
        ],
    )
    def test_メンバーは管理者限定APIを実行できない(self, client, method, path):
        as_user(client, "member@example.com", "member")
        res = client.request(method, path, json={"email": "x@example.com", "assignments": {}})
        assert res.status_code == 403, f"{method} {path} がメンバーに許可されている"

    def test_管理者は監査ログを見られる(self, client):
        as_user(client, "admin@example.com", "admin")
        assert client.get("/api/audit-logs").status_code == 200


class TestProjectRole:
    """GASごとの権限。アクセス権のないGASを操作できないこと。"""

    def test_アクセス権がなければ詳細を見られない(self, client):
        as_user(client, "member@example.com", "member", project_role=None)
        assert client.get("/api/projects/proj-1").status_code == 403

    def test_閲覧者は見られるが復元はできない(self, client):
        as_user(client, "member@example.com", "member", project_role="viewer")
        assert client.get("/api/projects/proj-1").status_code == 200
        res = client.post("/api/projects/proj-1/rollback", json={"target_version_no": 1})
        assert res.status_code == 403, "閲覧者がGASを上書きできてしまう"

    def test_閲覧者はメンバーを追加できない(self, client):
        as_user(client, "member@example.com", "member", project_role="viewer")
        res = client.post("/api/projects/proj-1/members", json={"email": "x@example.com", "role": "viewer"})
        assert res.status_code == 403

    def test_開発者はメンバー管理できないが復元はできる(self, client, monkeypatch):
        as_user(client, "member@example.com", "member", project_role="developer")
        res = client.post("/api/projects/proj-1/members", json={"email": "x@example.com", "role": "viewer"})
        assert res.status_code == 403, "開発者がメンバー管理できてしまう"

        monkeypatch.setattr(routes, "rollback_to_version", lambda **kwargs: {"restored_version_no": 1, "auto_backup_version_no": 2})
        assert client.post("/api/projects/proj-1/rollback", json={"target_version_no": 1}).status_code == 200

    def test_所有者はメンバーを追加できる(self, client, monkeypatch):
        as_user(client, "member@example.com", "member", project_role="owner")
        monkeypatch.setattr(routes, "upsert_member", lambda *a, **k: {"email": "x@example.com", "role": "viewer"})
        res = client.post("/api/projects/proj-1/members", json={"email": "x@example.com", "role": "viewer"})
        assert res.status_code == 200

    def test_管理者は個別付与なしで操作できる(self, client):
        as_user(client, "admin@example.com", "admin", project_role=None)
        assert client.get("/api/projects/proj-1").status_code == 200


class TestProjectRegistration:
    """登録時に、他人の接続アカウントを騙れないこと。"""

    def test_メンバーは他人のアカウントで登録できない(self, client, monkeypatch):
        as_user(client, "member@example.com", "member")
        monkeypatch.setattr(routes, "is_account_connected", lambda email: True)
        res = client.post(
            "/api/projects",
            json={"project_name": "x", "script_id": "s1", "google_account": "someone-else@example.com"},
        )
        assert res.status_code == 403, "他人の接続アカウントで登録できてしまう"

    def test_未接続のアカウントでは登録できない(self, client, monkeypatch):
        as_user(client, "member@example.com", "member")
        monkeypatch.setattr(routes, "is_account_connected", lambda email: False)
        res = client.post(
            "/api/projects",
            json={"project_name": "x", "script_id": "s1", "google_account": "member@example.com"},
        )
        assert res.status_code == 400

    def test_重複登録は409で拒否される(self, client, monkeypatch):
        as_user(client, "member@example.com", "member")
        monkeypatch.setattr(routes, "is_account_connected", lambda email: True)

        def duplicate(_data):
            raise routes.DuplicateScriptIdError(script_id="s1")

        monkeypatch.setattr(routes, "create_project", duplicate)
        res = client.post(
            "/api/projects",
            json={"project_name": "x", "script_id": "s1", "google_account": "member@example.com"},
        )
        assert res.status_code == 409


class TestAuditIdentity:
    """監査ログの実行者を、クライアントが詐称できないこと（2026-09-15修正の再発防止）。"""

    def test_復元の実行者はリクエスト本文で上書きできない(self, client, monkeypatch):
        as_user(client, "member@example.com", "member", project_role="developer")
        captured = {}

        def fake_rollback(**kwargs):
            captured.update(kwargs)
            return {"restored_version_no": 1, "auto_backup_version_no": 2}

        monkeypatch.setattr(routes, "rollback_to_version", fake_rollback)
        client.post(
            "/api/projects/proj-1/rollback",
            json={"target_version_no": 1, "performed_by": "別人@example.com"},
        )
        assert captured["performed_by"] == "member@example.com", "監査ログの実行者を詐称できてしまう"

    def test_セーブの申請者もリクエスト本文で上書きできない(self, client, monkeypatch):
        as_user(client, "member@example.com", "member", project_role="developer")
        captured = {}

        def fake_release(**kwargs):
            captured.update(kwargs)
            return {"release": {}}

        monkeypatch.setattr(routes, "request_release", fake_release)
        client.post(
            "/api/projects/proj-1/releases",
            json={"comment": "test", "requested_by": "別人@example.com"},
        )
        assert captured["requested_by"] == "member@example.com"
