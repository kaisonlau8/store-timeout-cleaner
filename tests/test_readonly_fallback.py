"""只读副本回退只针对 unable to open database file，locked 等错误照常抛出。"""

import sqlite3

import pytest

import score_query
import timeout_query
from timeout_store import is_readonly_open_error


def test_readonly_error_detection():
    assert is_readonly_open_error(sqlite3.OperationalError("unable to open database file"))
    assert not is_readonly_open_error(sqlite3.OperationalError("database is locked"))


@pytest.mark.parametrize("module", [score_query, timeout_query])
def test_locked_error_does_not_fall_back_to_immutable(tmp_path, monkeypatch, module):
    db = tmp_path / "timeout.db"
    db.touch()
    fallback_calls = []

    def locked(_path):
        raise sqlite3.OperationalError("database is locked")

    def immutable(_path):
        fallback_calls.append(_path)
        return object()

    monkeypatch.setattr(module, "connect", locked)
    monkeypatch.setattr(module, "connect_ro", immutable)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        module._open(str(tmp_path), db_path=str(db))
    assert fallback_calls == []


@pytest.mark.parametrize("module", [score_query, timeout_query])
def test_readonly_open_error_uses_immutable_fallback(tmp_path, monkeypatch, module):
    db = tmp_path / "timeout.db"
    db.touch()
    fallback_calls = []

    def readonly(_path):
        raise sqlite3.OperationalError("unable to open database file")

    def immutable(path):
        fallback_calls.append(path)
        return "immutable-connection"

    monkeypatch.setattr(module, "connect", readonly)
    monkeypatch.setattr(module, "connect_ro", immutable)

    assert module._open(str(tmp_path), db_path=str(db)) == "immutable-connection"
    assert fallback_calls == [str(db)]
