"""ロールバック（gas/rollback.py）の回帰テスト。

このシステムで最も事故が起きやすい箇所。Apps Script APIのupdateContentは差分パッチではなく
全体置換のため、判断を1つ誤るとGAS本体の内容を失わせる。にもかかわらず2026-09-15時点で
自動テストが1件も無く（カバレッジ0%）、実環境でも実行実績が1回しかなかったため追加した。

Firestoreやネットワークには触れず、協調する関数を差し替えて「どういう順序で何を呼ぶか」
「危ない条件のときに本当に書き込みを止めるか」を固定する。今日見つかった不具合は
いずれもこの層（順序・条件分岐・状態遷移）にあった。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gas import rollback as rb  # noqa: E402
from gas.apps_script import AppsScriptAPIError  # noqa: E402
from gas.diff import calculate_source_hash  # noqa: E402

OLD_FILES = [{"name": "コード", "type": "server_js", "source": "古い内容"}]
NEW_FILES = [{"name": "コード", "type": "server_js", "source": "新しい内容"}]


class Harness:
    """rollbackが呼び出す協調先を記録するための差し替え一式。"""

    def __init__(self, monkeypatch, *, current_files, savepoints, update_error=None, fetch_error=None):
        self.current_files = current_files
        self.savepoints = savepoints  # {version_no: source_files}
        self.update_error = update_error
        self.fetch_error = fetch_error
        self.updated_with = None          # GAS本体へ書き込まれた内容
        self.created_savepoints = []      # (comment, source_files)
        self.logs = []                    # (result, details)
        self.superseded = []              # 取消扱いにした変更の対象バージョン
        self._next_version = max(savepoints) + 1 if savepoints else 1

        monkeypatch.setattr(rb, "get_project", lambda pid: {"script_id": "script-1", "google_account": "user@example.com"})
        monkeypatch.setattr(rb, "get_credentials_for", lambda email: object())
        monkeypatch.setattr(rb, "fetch_source_files", self._fetch)
        monkeypatch.setattr(rb, "update_content", self._update)
        monkeypatch.setattr(rb, "get_savepoint_by_version", self._get_savepoint)
        monkeypatch.setattr(rb, "list_savepoints", self._list_savepoints)
        monkeypatch.setattr(rb, "create_savepoint", self._create_savepoint)
        monkeypatch.setattr(rb, "log_operation", self._log)
        monkeypatch.setattr(rb, "mark_changes_superseded_by_rollback", self._supersede)

    def _fetch(self, script_id, creds):
        if self.fetch_error:
            raise self.fetch_error
        return self.current_files

    def _update(self, script_id, source_files, creds):
        if self.update_error:
            raise self.update_error
        self.updated_with = source_files

    def _get_savepoint(self, project_id, version_no):
        files = self.savepoints.get(version_no)
        if files is None:
            return None
        return {"version_no": version_no, "source_files": files, "source_hash": calculate_source_hash(files)}

    def _list_savepoints(self, project_id, include_source=False):
        return [
            {"version_no": no, "source_hash": calculate_source_hash(files)}
            for no, files in sorted(self.savepoints.items(), reverse=True)
        ]

    def _create_savepoint(self, project_id, source_files, comment, created_by):
        no = self._next_version
        self._next_version += 1
        self.savepoints[no] = source_files
        self.created_savepoints.append((comment, source_files))
        return {"version_no": no, "source_files": source_files}

    def _log(self, **kwargs):
        self.logs.append((kwargs.get("result"), kwargs.get("details") or {}))

    def _supersede(self, project_id, target_version_no):
        self.superseded.append(target_version_no)
        return 1


class TestOptimisticLock:
    """第三者がGASを直接編集していた場合に、上書きを止められるか。"""

    def test_想定と実際の状態が違えばGASに書き込まない(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        with pytest.raises(rb.RollbackConflictError):
            rb.rollback_to_version("p1", 1, "user@example.com", expected_current_hash="ちがうハッシュ")
        assert h.updated_with is None, "競合しているのにGASを上書きしてしまった"
        assert h.created_savepoints == [], "競合時にセーブポイントを作ってしまっている"
        assert h.logs[-1][0] == "conflict"

    def test_想定と一致すれば実行される(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        result = rb.rollback_to_version(
            "p1", 1, "user@example.com", expected_current_hash=calculate_source_hash(NEW_FILES)
        )
        assert h.updated_with == OLD_FILES
        assert result["restored_version_no"] == 1

    def test_ハッシュ未指定でも実行できる(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        rb.rollback_to_version("p1", 1, "user@example.com")
        assert h.updated_with == OLD_FILES


class TestBackupBeforeWrite:
    """復元前に現状を必ず退避できているか（ロールバック自体を取り消せること）。"""

    def test_書き込み前に現状がバックアップされる(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        result = rb.rollback_to_version("p1", 1, "user@example.com")
        backup = [c for c in h.created_savepoints if "自動バックアップ" in c[0]]
        assert backup, "復元前の自動バックアップが作られていない"
        assert backup[0][1] == NEW_FILES, "バックアップの内容が復元前の状態と違う"
        assert h.savepoints[result["auto_backup_version_no"]] == NEW_FILES

    def test_現状が既存セーブポイントと同じなら重複して作らない(self, monkeypatch):
        # 直前にセーブしたばかりの状態から戻す場合。v2が現状と一致する
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES, 2: NEW_FILES})
        result = rb.rollback_to_version("p1", 1, "user@example.com")
        backup = [c for c in h.created_savepoints if "自動バックアップ" in c[0]]
        assert backup == [], "同じ内容のバックアップを重複して作っている"
        assert result["auto_backup_version_no"] == 2, "既存のv2を取り消し地点として返すべき"

    def test_書き込みに失敗してもバックアップは残り失敗が記録される(self, monkeypatch):
        error = AppsScriptAPIError(status_code=403, message="権限がありません")
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES}, update_error=error)
        with pytest.raises(AppsScriptAPIError):
            rb.rollback_to_version("p1", 1, "user@example.com")
        assert [c for c in h.created_savepoints if "自動バックアップ" in c[0]], "失敗時にバックアップが残っていない"
        assert h.logs[-1][0] == "failed_update"
        assert "auto_backup_version_no" in h.logs[-1][1], "戻り先が監査ログに残っていない"


class TestAfterRestore:
    """復元後の状態を正しく記録できているか（2026-09-15の不具合の再発防止）。"""

    def test_復元後の状態がセーブポイントとして記録される(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        rb.rollback_to_version("p1", 1, "user@example.com")
        restored = [c for c in h.created_savepoints if "復元した状態" in c[0]]
        assert restored, "復元後の状態が記録されていない（次回の変更検知で復元が変更扱いになる）"
        assert restored[0][1] == OLD_FILES

    def test_未セーブの変更が取消扱いになる(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        rb.rollback_to_version("p1", 1, "user@example.com")
        assert h.superseded == [1], "復元で消えた変更が未セーブのまま残ってしまう"

    def test_記録に失敗しても復元自体は成功扱いにする(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})

        def boom(*args, **kwargs):
            raise RuntimeError("Firestore障害")

        monkeypatch.setattr(rb, "mark_changes_superseded_by_rollback", boom)
        result = rb.rollback_to_version("p1", 1, "user@example.com")
        assert result["restored_version_no"] == 1
        assert h.logs[-1][0] == "success", "付随処理の失敗で復元まで失敗扱いになっている"


class TestGuards:
    """前提条件が崩れているときにGASへ触れないか。"""

    def test_存在しないバージョンへの復元は拒否する(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        with pytest.raises(rb.RollbackTargetNotFoundError):
            rb.rollback_to_version("p1", 99, "user@example.com")
        assert h.updated_with is None

    def test_存在しないプロジェクトは拒否する(self, monkeypatch):
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES})
        monkeypatch.setattr(rb, "get_project", lambda pid: None)
        with pytest.raises(rb.RollbackTargetNotFoundError):
            rb.rollback_to_version("p1", 1, "user@example.com")
        assert h.updated_with is None

    def test_現状取得に失敗したらGASに書き込まない(self, monkeypatch):
        error = AppsScriptAPIError(status_code=404, message="見つかりません")
        h = Harness(monkeypatch, current_files=NEW_FILES, savepoints={1: OLD_FILES}, fetch_error=error)
        with pytest.raises(AppsScriptAPIError):
            rb.rollback_to_version("p1", 1, "user@example.com")
        assert h.updated_with is None, "現状が分からないのに上書きしてしまった"
        assert h.logs[-1][0] == "failed_fetch_current"
