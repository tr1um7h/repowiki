"""Tests for the restore command (tool-record replay recovery)."""

import json
import sqlite3

import pytest

from repowiki.restore import run_restore
from repowiki.errors import UsageError


def _make_tool_db(tmp_path):
    """A fake machine-wide tool db with records from this repo and a foreign one."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "README.md").write_text("# t\n")
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE part (rowid INTEGER PRIMARY KEY, time_created INTEGER,"
        " session_id TEXT, data TEXT)")
    now = 1700000000000
    rows = [
        # foreign repo record — must never be restored
        json.dumps({"tool": "Write", "state": {"status": "completed", "input": {
            "file_path": "/elsewhere/other/.repowiki/en/content/F.md", "content": "foreign"}}}),
        # Write record
        json.dumps({"tool": "Write", "state": {"status": "completed", "input": {
            "file_path": f"{repo}/.repowiki/en/content/A.md", "content": "A body\n"}}}),
        # Bash heredoc record
        json.dumps({"tool": "Bash", "state": {"status": "completed", "input": {
            "command": f"cat > \"{repo}/.repowiki/en/content/B.md\" <<'EOF'\nB body\nEOF"}}}),
        # failed Write — must be skipped
        json.dumps({"tool": "Write", "state": {"status": "error", "input": {
            "file_path": f"{repo}/.repowiki/en/content/Bad.md", "content": "nope"}}}),
        # Edit record against A.md
        json.dumps({"tool": "Edit", "state": {"status": "completed", "input": {
            "file_path": f"{repo}/.repowiki/en/content/A.md",
            "old_string": "A body", "new_string": "A edited"}}}),
        # record outside the rowid window
        json.dumps({"tool": "Write", "state": {"status": "completed", "input": {
            "file_path": f"{repo}/.repowiki/en/content/C.md", "content": "later"}}}),
    ]
    conn.executemany(
        "INSERT INTO part (time_created, session_id, data) VALUES (?, 's', ?)",
        [(now + i, r) for i, r in enumerate(rows)])
    conn.commit()
    conn.close()
    return repo, db


@pytest.fixture()
def tool_db(tmp_path):
    return _make_tool_db(tmp_path)


def test_restore_replay_all_mechanisms(tmp_path, capsys):
    repo, db = _make_tool_db(tmp_path)
    out = repo / ".repowiki"
    run_restore(str(repo), db=str(db), rowid_min=0, out=str(out))
    text = capsys.readouterr().out
    assert "已恢复 3 个文件" in text                      # A.md + B.md, Bad.md skipped
    assert "/elsewhere/other" in text                    # foreign repo reported
    assert (out / "en/content/A.md").read_text() == "A edited\n"   # Edit applied
    assert (out / "en/content/B.md").read_text() == "B body\n"     # heredoc replayed
    assert not (out / "en/content/Bad.md").exists()                 # failed call skipped
    assert not (out / "en/content/F.md").exists()                   # foreign never merged


def test_restore_rowid_window(tool_db, capsys):
    repo, db = tool_db
    out = repo / "out2"
    run_restore(str(repo), db=str(db), rowid_min=5, out=str(out))
    capsys.readouterr()
    assert (out / "en/content/C.md").exists()          # only the later record
    assert not (out / "en/content/A.md").exists()


def test_restore_dry_run_writes_nothing(tool_db, capsys):
    repo, db = tool_db
    out = repo / "out3"
    run_restore(str(repo), db=str(db), rowid_min=0, out=str(out), dry_run=True)
    text = capsys.readouterr().out
    assert "将写入" in text
    assert not out.exists()


def test_restore_missing_source_raises(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    with pytest.raises(UsageError):
        run_restore(str(repo), db=str(tmp_path / "nope.sqlite"))


def test_restore_snapshot_preferred(tmp_path, capsys):
    """When .repowiki/db/recovery.db exists it wins over the machine-wide db."""
    repo, db = _make_tool_db(tmp_path)
    snap_dir = repo / ".repowiki" / "db"
    snap_dir.mkdir(parents=True)
    snap = snap_dir / "recovery.db"
    conn = sqlite3.connect(snap)
    conn.execute(
        "CREATE TABLE part (rowid INTEGER PRIMARY KEY, time_created INTEGER,"
        " session_id TEXT, data TEXT)")
    conn.execute("INSERT INTO part (time_created, session_id, data) VALUES (?, 's', ?)",
                 (1, json.dumps({"tool": "Write", "state": {"status": "completed", "input": {
                     "file_path": f"{repo}/.repowiki/en/content/FromSnap.md",
                     "content": "snap"}}})))
    conn.commit()
    conn.close()
    out = repo / "out4"
    run_restore(str(repo), out=str(out))               # no --db: snapshot wins
    capsys.readouterr()
    assert (out / "en/content/FromSnap.md").exists()
    assert not (out / "en/content/A.md").exists()      # machine-wide db not consulted


def test_restore_no_fallback_without_env(tmp_path, monkeypatch):
    """No snapshot + no REPOWIKI_RECOVERY_DB must raise, never guess a default."""
    repo = tmp_path / "bare"
    repo.mkdir()
    monkeypatch.delenv("REPOWIKI_RECOVERY_DB", raising=False)
    with pytest.raises(UsageError):
        run_restore(str(repo))
