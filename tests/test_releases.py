"""セーブ申請・承認・却下（gas/releases.py）の回帰テスト。

2026-09-16、萬年様向け見積項目3「変更内容のレビュー・承認フロー」対応として、
セーブ申請（request_release）→ Maintainer以上の承認（approve_release）でのみ
実際にセーブポイントが作られる2段階フローを復元した。ここでは
「承認するまでセーブポイントが作られないこと」「却下すると変更が未セーブのまま
再申請可能な状態に戻ること」「承認待ち以外への承認・却下は拒否されること」を、
本物のFirestoreエミュレータ上で検証する（releases.py自身がdb()を直接操作するため、
差し替え方式ではFirestoreの実際の書き込み・削除を検証できない）。

Apps Script APIとAIレビュー生成は外部サービスのため差し替える。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gas import projects as pj  # noqa: E402
from gas import releases as rel  # noqa: E402
from gas import savepoints as sp  # noqa: E402
from gas.changes import get_unreleased_changes  # noqa: E402

FILES_V1 = [{"name": "コード", "type": "server_js", "source": "function a() {}"}]
FILES_V2 = [{"name": "コード", "type": "server_js", "source": "function b() {}"}]


@pytest.fixture(autouse=True)
def stub_external(monkeypatch):
    """Apps Script APIとAIレビュー生成（いずれも外部サービス）を固定する。"""
    monkeypatch.setattr(rel, "get_credentials_for", lambda email: object())
    monkeypatch.setattr(rel, "generate_change_review", lambda *a, **k: {"risk_level": "low", "summary": "テスト", "sections": []})


def new_project(fs, **extra):
    return pj.create_project(
        {
            "project_name": "テスト用GAS",
            "script_id": "script-1",
            "google_account": "owner@example.com",
            **extra,
        }
    )


def request(monkeypatch, project_id, source_files, requested_by="dev@example.com", comment=""):
    monkeypatch.setattr(rel, "fetch_source_files", lambda script_id, creds: source_files)
    return rel.request_release(project_id, requested_by=requested_by, comment=comment)


class TestRequestRelease:
    """申請時点ではセーブポイントを作らないこと。"""

    def test_申請してもセーブポイントは作られない(self, fs, monkeypatch):
        project = new_project(fs)
        result = request(monkeypatch, project["id"], FILES_V1)
        assert result["release"]["status"] == "pending"
        assert result["release"]["resulting_version_no"] is None
        assert sp.list_savepoints(project["id"]) == []

    def test_未セーブの変更が無ければ拒否する(self, fs, monkeypatch):
        project = new_project(fs)
        # 初回申請でFILES_V1が「変更」として検知される前提を崩さないよう、
        # 先に同一内容で一度セーブポイントを作った状態を作る（差分ゼロ）。
        sp.create_savepoint(project["id"], FILES_V1, comment="初期", created_by="owner@example.com")
        with pytest.raises(rel.NoChangesToReleaseError):
            request(monkeypatch, project["id"], FILES_V1)

    def test_存在しないプロジェクトは拒否する(self, fs, monkeypatch):
        with pytest.raises(rel.ReleaseNotFoundError):
            request(monkeypatch, "no-such-project", FILES_V1)

    def test_申請者が記録される(self, fs, monkeypatch):
        project = new_project(fs)
        result = request(monkeypatch, project["id"], FILES_V1, requested_by="dev@example.com")
        assert result["release"]["requested_by"] == "dev@example.com"

    def test_AIレビューが添付される(self, fs, monkeypatch):
        project = new_project(fs)
        result = request(monkeypatch, project["id"], FILES_V1)
        assert result["release"]["ai_review"]["summary"] == "テスト"

    def test_AIレビュー生成に失敗しても申請自体は成功する(self, fs, monkeypatch):
        monkeypatch.setattr(rel, "generate_change_review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("落ちた")))
        project = new_project(fs)
        result = request(monkeypatch, project["id"], FILES_V1)
        assert result["release"]["status"] == "pending"
        assert result["release"]["ai_review"] is None


class TestApproveRelease:
    """承認して初めてセーブポイントが作られること。"""

    def test_承認するとセーブポイントが作られる(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1, comment="初回申請")["release"]

        result = rel.approve_release(release["id"], approved_by="lead@example.com")

        assert result["release"]["status"] == "approved"
        assert result["release"]["approved_by"] == "lead@example.com"
        savepoints = sp.list_savepoints(project["id"])
        assert len(savepoints) == 1
        assert savepoints[0]["version_no"] == result["release"]["resulting_version_no"]

    def test_承認後はsource_filesが本体に残らない(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]
        rel.approve_release(release["id"], approved_by="lead@example.com")

        raw = fs.collection(rel.COLLECTION).document(release["id"]).get().to_dict()
        assert "source_files" not in raw, "承認後もsource_filesを二重に保持している"

    def test_対象の変更がリリース済みになる(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]
        assert get_unreleased_changes(project["id"]), "申請時点ではまだ未セーブのはず"

        rel.approve_release(release["id"], approved_by="lead@example.com")

        assert get_unreleased_changes(project["id"]) == [], "承認後も未セーブの変更として残っている"

    def test_承認待ち以外を承認できない(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]
        rel.approve_release(release["id"], approved_by="lead@example.com")

        with pytest.raises(rel.ReleaseNotPendingError):
            rel.approve_release(release["id"], approved_by="lead@example.com")

    def test_存在しない申請は承認できない(self, fs, monkeypatch):
        with pytest.raises(rel.ReleaseNotFoundError):
            rel.approve_release("no-such-release", approved_by="lead@example.com")


class TestRejectRelease:
    """却下してもセーブポイントは作られず、変更は再申請可能なまま残ること。"""

    def test_却下してもセーブポイントは作られない(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]

        result = rel.reject_release(release["id"], rejected_by="lead@example.com", reason="動作確認が不十分")

        assert result["status"] == "rejected"
        assert result["rejection_reason"] == "動作確認が不十分"
        assert sp.list_savepoints(project["id"]) == []

    def test_却下後も変更は未セーブのまま残る(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]

        rel.reject_release(release["id"], rejected_by="lead@example.com")

        assert get_unreleased_changes(project["id"]), "却下したのに変更が未セーブでなくなっている（再申請できない）"

    def test_却下後はsource_filesが残らない(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]
        rel.reject_release(release["id"], rejected_by="lead@example.com")

        raw = fs.collection(rel.COLLECTION).document(release["id"]).get().to_dict()
        assert "source_files" not in raw

    def test_承認待ち以外を却下できない(self, fs, monkeypatch):
        project = new_project(fs)
        release = request(monkeypatch, project["id"], FILES_V1)["release"]
        rel.reject_release(release["id"], rejected_by="lead@example.com")

        with pytest.raises(rel.ReleaseNotPendingError):
            rel.reject_release(release["id"], rejected_by="lead@example.com")

    def test_存在しない申請は却下できない(self, fs, monkeypatch):
        with pytest.raises(rel.ReleaseNotFoundError):
            rel.reject_release("no-such-release", rejected_by="lead@example.com")


class TestSerializeBackwardCompat:
    """2026-09-16より前に作られた（statusフィールドを持たない）本番データとの互換性。"""

    def test_旧データはapproved扱いになる(self, fs):
        doc_ref = fs.collection(rel.COLLECTION).document()
        doc_ref.set({"project_id": "p1", "resulting_version_no": 3, "requested_by": "owner@example.com"})
        release = rel.get_release(doc_ref.id)
        assert release["status"] == "approved"
