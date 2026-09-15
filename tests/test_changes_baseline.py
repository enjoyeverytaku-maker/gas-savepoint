"""変更検知の比較基準（gas/changes.py::get_baseline）の回帰テスト。

2026-09-15、萬年環境で「ロールバック直後に、戻した行為そのものが新しい変更として
検知され、未セーブの変更として残る」不具合が発生した。原因は、比較の基準を
「変更があれば無条件にそちら」としており、復元後の状態を基準にできなかったこと。

基準の選び方を誤ると、(a)実際には変更が無いのに変更ありと表示される、
(b)本当の変更を見落とす、のどちらかが起きる。どちらも商品の信頼性に直結するため固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gas import changes as ch  # noqa: E402

OLD = [{"name": "コード", "type": "server_js", "source": "古い"}]
NEW = [{"name": "コード", "type": "server_js", "source": "新しい"}]


def stub(monkeypatch, *, latest_change=None, latest_savepoint=None):
    monkeypatch.setattr(ch, "_get_latest_change", lambda pid: latest_change)
    monkeypatch.setattr("gas.savepoints.get_latest_savepoint", lambda pid: latest_savepoint)


def change(at, files, h):
    return {"detected_at": at, "source_files": files, "source_hash": h}


def savepoint(at, files, h):
    return {"created_at": at, "source_files": files, "source_hash": h}


class TestGetBaseline:
    def test_どちらも無ければ空(self, monkeypatch):
        stub(monkeypatch)
        assert ch.get_baseline("p1") == ([], None)

    def test_セーブポイントだけならそれを使う(self, monkeypatch):
        stub(monkeypatch, latest_savepoint=savepoint("2026-09-15T01:00:00Z", OLD, "h-old"))
        files, h = ch.get_baseline("p1")
        assert (files, h) == (OLD, "h-old")

    def test_変更だけならそれを使う(self, monkeypatch):
        stub(monkeypatch, latest_change=change("2026-09-15T01:00:00Z", NEW, "h-new"))
        files, h = ch.get_baseline("p1")
        assert (files, h) == (NEW, "h-new")

    def test_変更の方が新しければ変更を使う(self, monkeypatch):
        """通常の運用。セーブ後にGASが編集された状態。"""
        stub(
            monkeypatch,
            latest_change=change("2026-09-15T05:00:00Z", NEW, "h-new"),
            latest_savepoint=savepoint("2026-09-15T01:00:00Z", OLD, "h-old"),
        )
        _, h = ch.get_baseline("p1")
        assert h == "h-new"

    def test_セーブポイントの方が新しければそちらを使う(self, monkeypatch):
        """ロールバック直後。復元後の状態が最新のセーブポイントとして記録されている。
        ここで古い変更を基準にすると、復元が新しい変更として誤検知される。"""
        stub(
            monkeypatch,
            latest_change=change("2026-09-15T05:00:00Z", NEW, "h-new"),
            latest_savepoint=savepoint("2026-09-15T07:00:00Z", OLD, "h-restored"),
        )
        files, h = ch.get_baseline("p1")
        assert h == "h-restored", "復元後の状態を基準にできず、復元が変更として検知される"
        assert files == OLD


class TestDisplayStatus:
    def test_未セーブ(self):
        assert ch.display_status({"release_status": "unreleased"}) == "unreleased"

    def test_セーブ済み(self):
        assert ch.display_status({"release_status": "released"}) == "released"

    def test_復元により取消は未セーブ扱いにしない(self):
        """未セーブのままだと「対応が必要な変更」として画面に出続けてしまう。"""
        assert ch.display_status({"release_status": "rolled_back"}) == "rolled_back"

    def test_状態が無ければ未セーブ扱い(self):
        assert ch.display_status({}) == "unreleased"
