"""差分計算（gas/diff.py）の回帰テスト。

2026-09-15に、行数集計をdifflib.ndiff（最悪O(n^2)、表示用のunified_diffと二重計算）から
SequenceMatcherベースへ置き換えたため、集計値・表示用データの整合性をここで固定する。
差分はロールバック判断の根拠になるので、数字がずれると利用者の判断を誤らせる。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gas.diff import calculate_diff, calculate_source_hash  # noqa: E402


def _file(name: str, source: str, file_type: str = "server_js") -> dict:
    return {"name": name, "type": file_type, "source": source}


class TestCalculateSourceHash:
    def test_同じ内容なら同じハッシュになる(self):
        files = [_file("コード", "function a() {}")]
        assert calculate_source_hash(files) == calculate_source_hash(list(files))

    def test_ファイルの並び順が違っても同じハッシュになる(self):
        a = _file("a", "1")
        b = _file("b", "2")
        assert calculate_source_hash([a, b]) == calculate_source_hash([b, a])

    def test_中身が変われば違うハッシュになる(self):
        assert calculate_source_hash([_file("a", "1")]) != calculate_source_hash([_file("a", "2")])

    def test_ファイル名が変われば違うハッシュになる(self):
        assert calculate_source_hash([_file("a", "1")]) != calculate_source_hash([_file("b", "1")])


class TestCalculateDiff:
    def test_新規ファイルはAddedになり行数が全行数になる(self):
        diff = calculate_diff([_file("new", "line1\nline2\nline3")], [])
        assert len(diff) == 1
        assert diff[0]["status"] == "Added"
        assert diff[0]["added_lines"] == 3
        assert diff[0]["deleted_lines"] == 0

    def test_削除されたファイルはDeletedになる(self):
        diff = calculate_diff([], [_file("gone", "a\nb")])
        assert len(diff) == 1
        assert diff[0]["status"] == "Deleted"
        assert diff[0]["added_lines"] == 0
        assert diff[0]["deleted_lines"] == 2

    def test_変更なしのファイルは差分に出ない(self):
        same = [_file("same", "unchanged")]
        assert calculate_diff(same, list(same)) == []

    def test_1行の置き換えは追加1削除1になる(self):
        previous = [_file("f", "a\nb\nc")]
        current = [_file("f", "a\nB\nc")]
        diff = calculate_diff(current, previous)
        assert len(diff) == 1
        assert diff[0]["status"] == "Modified"
        assert diff[0]["added_lines"] == 1
        assert diff[0]["deleted_lines"] == 1

    def test_行の追加のみなら削除はゼロになる(self):
        diff = calculate_diff([_file("f", "a\nb\nc")], [_file("f", "a\nc")])
        assert diff[0]["added_lines"] == 1
        assert diff[0]["deleted_lines"] == 0

    def test_ファイル名は同じでも種別が違えば別ファイル扱いになる(self):
        previous = [_file("appsscript", "{}", file_type="json")]
        current = [_file("appsscript", "{}", file_type="server_js")]
        diff = calculate_diff(current, previous)
        statuses = sorted(entry["status"] for entry in diff)
        assert statuses == ["Added", "Deleted"]


class TestIncludeLines:
    def test_既定では行単位の差分が含まれる(self):
        diff = calculate_diff([_file("f", "a\nB")], [_file("f", "a\nb")])
        assert diff[0]["lines"], "表示用の行差分が空になっている"

    def test_include_lines_Falseなら行単位の差分を持たない(self):
        diff = calculate_diff([_file("f", "a\nB")], [_file("f", "a\nb")], include_lines=False)
        assert diff[0]["lines"] == []

    def test_行差分を省いても増減行数は同じになる(self):
        previous = [_file("f", "\n".join(f"line {i}" for i in range(50)))]
        current = [_file("f", "\n".join(f"line {i}" for i in range(0, 50, 2)))]
        with_lines = calculate_diff(current, previous)[0]
        without_lines = calculate_diff(current, previous, include_lines=False)[0]
        assert (with_lines["added_lines"], with_lines["deleted_lines"]) == (
            without_lines["added_lines"],
            without_lines["deleted_lines"],
        )

    def test_表示用の行差分と集計値が一致する(self):
        previous = [_file("f", "a\nb\nc\nd")]
        current = [_file("f", "a\nX\nc\nY\nZ")]
        entry = calculate_diff(current, previous)[0]
        inserts = sum(1 for line in entry["lines"] if line["type"] == "insert")
        deletes = sum(1 for line in entry["lines"] if line["type"] == "delete")
        assert entry["added_lines"] == inserts
        assert entry["deleted_lines"] == deletes


class TestDiffLines:
    def test_行番号が変更前後で別々に振られる(self):
        entry = calculate_diff([_file("f", "a\nX\nc")], [_file("f", "a\nb\nc")])[0]
        inserted = [line for line in entry["lines"] if line["type"] == "insert"]
        deleted = [line for line in entry["lines"] if line["type"] == "delete"]
        assert inserted[0]["text"] == "X"
        assert inserted[0]["b"] == 2
        assert inserted[0]["a"] is None
        assert deleted[0]["text"] == "b"
        assert deleted[0]["a"] == 2
        assert deleted[0]["b"] is None
