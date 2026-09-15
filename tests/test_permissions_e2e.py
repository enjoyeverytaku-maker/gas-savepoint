"""権限制限の通し確認（実アプリ + 実Firestore）。

tests/test_api_permissions.py は権限判定を差し替えて「API層が判定結果をどう扱うか」を固定して
いるが、「Firestoreに入っている利用者情報とメンバー情報から権限が正しく解決されるか」までは
見ていない。萬年本番は2026-09-15時点で登録4名が全員管理者のため、権限を絞った状態が一度も
実行されていない。ここでは差し替えを一切行わず、エミュレータに実際の利用者・メンバーを作って
HTTPで叩く。

GAS本体へアクセスする処理はダミーのスクリプトIDのため実際には成功しないが、権限チェックは
その手前で行われるため、403（権限で弾かれた）とそれ以外（権限は通った）を区別できる。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ADMIN = "admin@example.com"
OWNER = "owner@example.com"
DEVELOPER = "developer@example.com"
VIEWER = "viewer@example.com"
OUTSIDER = "outsider@example.com"

LABELS = {ADMIN: "管理者", OWNER: "所有者", DEVELOPER: "開発者", VIEWER: "閲覧者", OUTSIDER: "権限なし"}

ALLOWED = "許可"      # 200が返る
DENIED = "拒否"       # 403で弾かれる
PASSED = "権限は通過"  # 権限では弾かれない（後段の処理結果は問わない）


@pytest.fixture
def app_client(fs, monkeypatch):
    """利用者・GAS・メンバー権限を実際に作り、それを見るアプリのクライアントを返す。"""
    # 開発モードの?debug_email=で利用者を名乗る（IAPが無いローカルでの唯一の手段。
    # 本番Cloud Runではmain.py::_refuse_dev_mode_on_cloud_runが起動を拒否する）。
    monkeypatch.setenv("SAVEPOINT_DEV_MODE", "1")

    for email, role in ((ADMIN, "admin"), (OWNER, "member"), (DEVELOPER, "member"),
                        (VIEWER, "member"), (OUTSIDER, "member")):
        fs.collection("users").document(email).set({"email": email, "role": role})
    fs.collection("google_accounts").document(ADMIN).set({"email": ADMIN})

    from gas.members import upsert_member
    from gas.projects import create_project

    project = create_project({
        "project_name": "権限確認用GAS", "script_id": "dummy-script-id",
        "google_account": ADMIN, "description": "検証用",
    })
    upsert_member(project["id"], OWNER, "owner", ADMIN)
    upsert_member(project["id"], DEVELOPER, "developer", ADMIN)
    upsert_member(project["id"], VIEWER, "viewer", ADMIN)

    import main

    client = TestClient(main.app)
    client.project_id = project["id"]
    return client


def request_as(client, method: str, path: str, actor: str, body=None):
    separator = "&" if "?" in path else "?"
    return client.request(method, f"{path}{separator}debug_email={actor}", json=body)


def check(client, method: str, path: str, actor: str, expected: str, body=None):
    status = request_as(client, method, path, actor, body).status_code
    if expected == DENIED:
        assert status == 403, f"{LABELS[actor]} が {method} {path} を実行できてしまう (HTTP {status})"
    elif expected == ALLOWED:
        assert status == 200, f"{LABELS[actor]} が {method} {path} を実行できない (HTTP {status})"
    else:
        assert status not in (401, 403), f"{LABELS[actor]} が {method} {path} で権限に弾かれた (HTTP {status})"


class TestGlobalRole:
    """管理者限定の画面・操作。"""

    @pytest.mark.parametrize("actor,expected", [(ADMIN, ALLOWED), (OWNER, DENIED), (VIEWER, DENIED)])
    @pytest.mark.parametrize("path", ["/api/audit-logs", "/api/users"])
    def test_管理者限定の閲覧(self, app_client, path, actor, expected):
        check(app_client, "GET", path, actor, expected)

    @pytest.mark.parametrize("actor,expected", [(ADMIN, ALLOWED), (OWNER, DENIED), (DEVELOPER, DENIED)])
    def test_権限の一括設定は管理者限定(self, app_client, actor, expected):
        body = {"email": VIEWER, "assignments": {app_client.project_id: "viewer"}}
        check(app_client, "PUT", "/api/members/bulk", actor, expected, body)

    @pytest.mark.parametrize("actor,expected", [(ADMIN, ALLOWED), (OWNER, DENIED)])
    def test_他人のアクセス権の閲覧は管理者限定(self, app_client, actor, expected):
        check(app_client, "GET", f"/api/members/by-user/{VIEWER}", actor, expected)

    @pytest.mark.parametrize("actor,expected", [(OWNER, DENIED), (DEVELOPER, DENIED), (VIEWER, DENIED)])
    def test_GASの削除は管理者限定(self, app_client, actor, expected):
        """所有者であっても台帳からの削除はできない（消えるデータが大きいため）。"""
        check(app_client, "DELETE", f"/api/projects/{app_client.project_id}", actor, expected)


class TestProjectRole:
    """GASごとに付与した4段階の役割。"""

    @pytest.mark.parametrize("actor,expected", [
        (ADMIN, ALLOWED), (OWNER, ALLOWED), (DEVELOPER, ALLOWED), (VIEWER, ALLOWED), (OUTSIDER, DENIED)])
    def test_アクセス権のあるGASだけ詳細を見られる(self, app_client, actor, expected):
        check(app_client, "GET", f"/api/projects/{app_client.project_id}", actor, expected)

    @pytest.mark.parametrize("path", ["savepoints", "changes"])
    @pytest.mark.parametrize("actor,expected", [(VIEWER, ALLOWED), (OUTSIDER, DENIED)])
    def test_履歴の閲覧は閲覧者から(self, app_client, path, actor, expected):
        check(app_client, "GET", f"/api/projects/{app_client.project_id}/{path}", actor, expected)

    @pytest.mark.parametrize("actor,expected", [
        (OWNER, PASSED), (DEVELOPER, PASSED), (VIEWER, DENIED), (OUTSIDER, DENIED)])
    def test_復元は開発者から(self, app_client, actor, expected):
        check(app_client, "POST", f"/api/projects/{app_client.project_id}/rollback",
              actor, expected, {"target_version_no": 1})

    @pytest.mark.parametrize("actor,expected", [
        (OWNER, PASSED), (DEVELOPER, PASSED), (VIEWER, DENIED), (OUTSIDER, DENIED)])
    def test_変更検知は開発者から(self, app_client, actor, expected):
        check(app_client, "POST", f"/api/projects/{app_client.project_id}/sync-check", actor, expected)

    @pytest.mark.parametrize("actor,expected", [
        (DEVELOPER, PASSED), (VIEWER, DENIED), (OUTSIDER, DENIED)])
    def test_セーブは開発者から(self, app_client, actor, expected):
        check(app_client, "POST", f"/api/projects/{app_client.project_id}/releases",
              actor, expected, {"comment": "テスト"})

    @pytest.mark.parametrize("actor,expected", [
        (ADMIN, ALLOWED), (OWNER, ALLOWED), (DEVELOPER, DENIED), (VIEWER, DENIED)])
    def test_メンバー管理は所有者から(self, app_client, actor, expected):
        check(app_client, "POST", f"/api/projects/{app_client.project_id}/members",
              actor, expected, {"email": "someone@example.com", "role": "viewer"})

    @pytest.mark.parametrize("actor,expected", [
        (OWNER, ALLOWED), (DEVELOPER, DENIED), (VIEWER, DENIED), (OUTSIDER, DENIED)])
    def test_台帳項目の編集は所有者から(self, app_client, actor, expected):
        check(app_client, "PATCH", f"/api/projects/{app_client.project_id}",
              actor, expected, {"description": "書き換え"})

    def test_権限なしでも一覧は見られるが中身は空(self, app_client):
        """一覧自体は403にしない（サイドバーが表示できなくなるため）。中身が漏れないことが要点。"""
        response = request_as(app_client, "GET", "/api/projects", OUTSIDER)
        assert response.status_code == 200
        assert response.json() == [], "アクセス権の無いGASが一覧に出ている"


class TestUnauthenticated:
    """利用者を名乗らない経路。"""

    def test_未認証は401(self, app_client):
        assert app_client.get("/api/projects").status_code == 401

    def test_トークン無しの定期実行は401(self, app_client):
        """Cloud Scheduler用の経路は人間のRBACとは別に共有トークンで守る。"""
        assert app_client.post("/api/sync/check-all").status_code == 401
