"""Firestoreエミュレータを使った結合テスト（登録の一意性・採番・権限の一括設定・削除）。

差し替え方式のテストでは「Firestoreが実際にどう振る舞うか」に依存する部分を検証できない。
2026-09-15に萬年環境で起きた同一GASの12件重複登録は、まさにその層の不具合だった
（「先に検索して無ければ追加する」方式が、登録ボタン連打による同時リクエストをすり抜けた）。

ここでは本物と同じ挙動をするエミュレータに対して、同時実行を含めて検証する。
Javaが無い環境では conftest.py の firestore_emulator が自動的にスキップする。
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gas import members as mb  # noqa: E402
from gas import projects as pj  # noqa: E402
from gas import savepoints as sp  # noqa: E402

FILES_A = [{"name": "コード", "type": "server_js", "source": "function a() {}"}]
FILES_B = [{"name": "コード", "type": "server_js", "source": "function b() {}"}]


def new_project(script_id="script-1", name="テスト用GAS", **extra):
    return pj.create_project(
        {
            "project_name": name,
            "script_id": script_id,
            "google_account": "owner@example.com",
            **extra,
        }
    )


class TestDuplicateRegistration:
    """同じGASが二重登録されないこと（2026-09-15の12件重複事故の再発防止）。"""

    def test_登録できる(self, fs):
        project = new_project()
        assert project["id"]
        assert project["script_id"] == "script-1"
        assert len(pj.list_projects()) == 1

    def test_同じスクリプトIDは二度目を拒否する(self, fs):
        new_project()
        with pytest.raises(pj.DuplicateScriptIdError):
            new_project(name="別名で登録し直そうとする")
        assert len(pj.list_projects()) == 1

    def test_連打による同時登録でも1件しか通らない(self, fs):
        """事故の再現条件そのもの。5リクエストが同時に来ても登録は1件でなければならない。"""
        def attempt(index):
            try:
                new_project(name=f"試行{index}")
                return "ok"
            except pj.DuplicateScriptIdError:
                return "duplicate"

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(attempt, range(5)))

        assert results.count("ok") == 1, f"同時登録で複数件通ってしまった: {results}"
        assert len(pj.list_projects()) == 1

    def test_索引導入前に登録されたGASも重複を拒否する(self, fs):
        """索引が無い既存データへの移行パス。本体側の存在確認が効いていること。"""
        fs.collection(pj.COLLECTION).add(
            {"project_name": "索引なしの旧データ", "script_id": "script-1", "google_account": "owner@example.com"}
        )
        with pytest.raises(pj.DuplicateScriptIdError):
            new_project()
        # 拒否した際に索引を残すと、そのGASを二度と登録できなくなる
        index_id = pj._index_id("script-1")
        assert not fs.collection(pj.INDEX_COLLECTION).document(index_id).get().exists

    def test_別のスクリプトIDは登録できる(self, fs):
        new_project(script_id="script-1")
        new_project(script_id="script-2", name="別のGAS")
        assert len(pj.list_projects()) == 2


class TestDeleteProject:
    """管理をやめる操作で、関連データが消し残されないこと。"""

    def test_紐づくデータがすべて消える(self, fs):
        project = new_project()
        pid = project["id"]
        sp.create_savepoint(pid, FILES_A, "初期", "owner@example.com")
        mb.upsert_member(pid, "member@example.com", "viewer", "owner@example.com")
        fs.collection("sync_status").document(pid).set({"last_synced_at": "2026-09-15"})

        deleted = pj.delete_project(pid)

        assert deleted["savepoints"] == 1
        assert deleted["members"] == 1
        assert pj.get_project(pid) is None
        assert sp.list_savepoints(pid) == []
        assert mb.list_members(pid) == []
        assert not fs.collection("sync_status").document(pid).get().exists

    def test_削除後に同じGASを登録し直せる(self, fs):
        """索引が消し残ると、そのGASを二度と登録できなくなる。"""
        project = new_project()
        pj.delete_project(project["id"])
        again = new_project(name="登録し直し")
        assert again["id"]

    def test_監査ログは削除しない(self, fs):
        """誰が何をしたかの記録は、管理対象から外した後も残す。"""
        project = new_project()
        sp.create_savepoint(project["id"], FILES_A, "初期", "owner@example.com")
        before = len(list(fs.collection("operation_logs").stream()))
        assert before > 0

        pj.delete_project(project["id"])
        assert len(list(fs.collection("operation_logs").stream())) == before


class TestVersionNumbering:
    """セーブポイントの採番。番号が重複・欠番すると履歴と復元が壊れる。"""

    def test_順番に採番される(self, fs):
        pid = new_project()["id"]
        numbers = [
            sp.create_savepoint(pid, FILES_A, f"v{i}", "owner@example.com")["version_no"]
            for i in range(1, 4)
        ]
        assert numbers == [1, 2, 3]

    def test_同時保存でも番号が重複しない(self, fs):
        """ロールバックの自動バックアップと手動セーブが重なる場面を想定。"""
        pid = new_project()["id"]

        def save(index):
            return sp.create_savepoint(pid, FILES_A, f"同時{index}", "owner@example.com")["version_no"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            numbers = list(pool.map(save, range(5)))

        assert len(set(numbers)) == 5, f"バージョン番号が重複した: {sorted(numbers)}"
        assert sorted(numbers) == [1, 2, 3, 4, 5]

    def test_カウンタ導入前のプロジェクトは履歴の最大値から続ける(self, fs):
        """移行パス。ここを誤ると既存バージョンを上書きする番号を振ってしまう。"""
        pid = new_project()["id"]
        for version_no in (1, 2, 3):
            fs.collection(sp.COLLECTION).add(
                {"project_id": pid, "version_no": version_no, "source_files": FILES_A, "comment": "旧データ"}
            )
        assert sp.create_savepoint(pid, FILES_B, "移行後", "owner@example.com")["version_no"] == 4

    def test_プロジェクトごとに独立して採番される(self, fs):
        pid_a = new_project(script_id="script-1")["id"]
        pid_b = new_project(script_id="script-2", name="GAS-B")["id"]
        sp.create_savepoint(pid_a, FILES_A, "A1", "owner@example.com")
        assert sp.create_savepoint(pid_b, FILES_B, "B1", "owner@example.com")["version_no"] == 1


class TestSavepointRetrieval:
    def test_一覧は新しい順で本文を含まない(self, fs):
        """履歴一覧でソース全文まで取得すると、版が増えるほど画面が重くなる。"""
        pid = new_project()["id"]
        sp.create_savepoint(pid, FILES_A, "v1", "owner@example.com")
        sp.create_savepoint(pid, FILES_B, "v2", "owner@example.com")

        listed = sp.list_savepoints(pid)
        assert [item["version_no"] for item in listed] == [2, 1]
        assert "source_files" not in listed[0], "一覧でソース本文まで取得している"

    def test_最新は本文込みで取得できる(self, fs):
        pid = new_project()["id"]
        sp.create_savepoint(pid, FILES_A, "v1", "owner@example.com")
        sp.create_savepoint(pid, FILES_B, "v2", "owner@example.com")

        latest = sp.get_latest_savepoint(pid)
        assert latest["version_no"] == 2
        assert latest["source_files"] == FILES_B

    def test_バージョン指定で復元対象を取得できる(self, fs):
        pid = new_project()["id"]
        sp.create_savepoint(pid, FILES_A, "v1", "owner@example.com")
        sp.create_savepoint(pid, FILES_B, "v2", "owner@example.com")

        target = sp.get_savepoint_by_version(pid, 1)
        assert target["source_files"] == FILES_A
        assert sp.get_savepoint_by_version(pid, 99) is None

    def test_セーブポイントが無ければNone(self, fs):
        pid = new_project()["id"]
        assert sp.get_latest_savepoint(pid) is None


class TestMemberAccess:
    """GASごとの権限付与。誤ると他人のGASが見えるか、担当者が締め出される。"""

    def test_付与されたGASだけが見える(self, fs):
        pid_a = new_project(script_id="script-1")["id"]
        new_project(script_id="script-2", name="GAS-B")
        mb.upsert_member(pid_a, "member@example.com", "developer", "admin@example.com")

        visible = pj.list_projects_for_user("member@example.com", is_admin=False)
        assert [item["id"] for item in visible] == [pid_a]

    def test_管理者は全件見える(self, fs):
        new_project(script_id="script-1")
        new_project(script_id="script-2", name="GAS-B")
        assert len(pj.list_projects_for_user("admin@example.com", is_admin=True)) == 2

    def test_付与が無ければ何も見えない(self, fs):
        new_project()
        assert pj.list_projects_for_user("nobody@example.com", is_admin=False) == []

    def test_大文字のメールアドレスでも権限が引ける(self, fs):
        """表記ゆれで権限が引けなくなると、担当者が自分のGASを見られなくなる。"""
        pid = new_project()["id"]
        mb.upsert_member(pid, "Member@Example.com", "viewer", "admin@example.com")
        assert [item["id"] for item in pj.list_projects_for_user("member@example.com", False)] == [pid]

    def test_一括設定で複数GASへまとめて付与できる(self, fs):
        pid_a = new_project(script_id="script-1")["id"]
        pid_b = new_project(script_id="script-2", name="GAS-B")["id"]

        result = mb.set_members_bulk(
            "member@example.com", {pid_a: "developer", pid_b: "viewer"}, "admin@example.com"
        )
        assert result == {"assigned": 2, "removed": 0}
        assert mb.list_projects_for_member("member@example.com") == {pid_a: "developer", pid_b: "viewer"}

    def test_一括設定でNoneを渡すと権限を外せる(self, fs):
        pid_a = new_project(script_id="script-1")["id"]
        pid_b = new_project(script_id="script-2", name="GAS-B")["id"]
        mb.set_members_bulk("member@example.com", {pid_a: "developer", pid_b: "viewer"}, "admin@example.com")

        result = mb.set_members_bulk("member@example.com", {pid_a: None}, "admin@example.com")
        assert result == {"assigned": 0, "removed": 1}
        assert mb.list_projects_for_member("member@example.com") == {pid_b: "viewer"}

    def test_不正なロールは一括設定ごと拒否される(self, fs):
        """1件でも不正なら、途中まで適用された中途半端な権限状態を作らない。"""
        pid_a = new_project(script_id="script-1")["id"]
        pid_b = new_project(script_id="script-2", name="GAS-B")["id"]

        with pytest.raises(ValueError):
            mb.set_members_bulk("member@example.com", {pid_a: "developer", pid_b: "社長"}, "admin@example.com")
        assert mb.list_projects_for_member("member@example.com") == {}

    def test_メンバーを外すとアクセス権を失う(self, fs):
        pid = new_project()["id"]
        mb.upsert_member(pid, "member@example.com", "viewer", "admin@example.com")
        mb.remove_member(pid, "member@example.com")
        assert pj.list_projects_for_user("member@example.com", False) == []


class TestReadme:
    """AI生成READMEの反映。利用者の手入力を勝手に上書きしないこと。"""

    def test_用途が空なら要約が入る(self, fs):
        pid = new_project()["id"]
        pj.set_readme(pid, "# README", summary="請求書を集計する")
        assert pj.get_project(pid)["description"] == "請求書を集計する"

    def test_用途が入力済みなら上書きしない(self, fs):
        pid = new_project(description="経理が手で書いた説明")["id"]
        pj.set_readme(pid, "# README", summary="AIが推定した説明")
        assert pj.get_project(pid)["description"] == "経理が手で書いた説明"

    def test_要約なしでもREADMEは保存される(self, fs):
        pid = new_project()["id"]
        pj.set_readme(pid, "# README本文")
        assert pj.get_project(pid)["readme_markdown"] == "# README本文"
