"""``repowiki restore``: rebuild wiki pages from tool-call records.

Every wiki page is written through a tool call (``Write``/``Edit``) or a
Bash heredoc (``cat > file <<DELIM``), and those calls — with their full
input payloads — are persisted in a sqlite ``part`` table. When the wiki
output is lost (e.g. an interrupted replan), this command replays those
records to regenerate the pages.

Data sources, in priority order:
  1. ``<repo>/.repowiki/db/recovery.db`` — the self-contained snapshot that
     every plan/replan refreshes (see plan.py); the manifest
     (``recovery-manifest.json``) sitting next to the wiki root documents the
     generation's rowid window.
  2. ``$REPOWIKI_RECOVERY_DB`` — an explicitly configured tool-record db,
     filtered by repo path.

Anti-pollution guarantees: records are keyed by the repo root extracted from
the absolute ``file_path`` (the prefix before ``/.repowiki/``); only records
whose repo matches ``--repo`` are restored. Foreign-repo records are counted
and listed, never merged. After restoring, run the claim+check loop (or
``repowiki plan --replan``) so the validator re-applies its deterministic
fixes; semantic errors (e.g. a missing required section) need manual edits.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from .errors import UsageError

_HEREDOC_RE = re.compile(
    r'cat\s*>\s*["\']?([^"\'\n]+?)["\']?\s*<<\s*[\'"]?(\w+)[\'"]?\s*\n(.*?)\n\2(?:\n|\Z)', re.S)
_CONTENT_RE = re.compile(r'^[a-z]{2}(?:[-_][A-Za-z]{2,4})?/(?:content|meta|knowledge)/')

# Incident-specific path normalizations: some tool records contain path
# variants that never existed on disk (fullwidth vs backslash vs spaces).
PATH_NORMALIZATIONS = {
    "PL＼SQL Engine": "PL／SQL Engine",   # c1102 recorded with a backslash separator
}
OBSOLETE_PATHS = {
    # first failed draft of the PL/SQL chapter index (spaces variant, never passed check)
    "en/content/PL SQL Engine/PL SQL Engine.md",
}

_MARKER = "/.repowiki/"


def _repo_key(file_path: str, repo: str):
    """Return (repo_root, rel_path) for records of ``repo``; (root, None) for
    foreign repos; (None, None) when the path is not a wiki output path."""
    i = file_path.find(_MARKER)
    if i < 0:
        return None, None
    root = file_path[:i]
    if os.path.abspath(root) != os.path.abspath(repo):
        return root, None          # foreign repo — keep key, never merge
    rel = file_path[i + len(_MARKER):]
    for bad, good in PATH_NORMALIZATIONS.items():
        rel = rel.replace(bad, good)
    return root, rel


def _default_db(repo: str) -> Path | None:
    """Snapshot inside the wiki root first, else the machine-wide tool db."""
    snap = Path(repo) / ".repowiki" / "db" / "recovery.db"
    if snap.exists():
        return snap
    p = os.environ.get("REPOWIKI_RECOVERY_DB")
    if not p:
        return None
    p = Path(p)
    return p if p.exists() else None


def _manifest_rowid_min(repo: str) -> int | None:
    try:
        m = json.loads((Path(repo) / ".repowiki" / "recovery-manifest.json")
                       .read_text(encoding="utf-8"))
        v = m.get("part_rowid_min")
        return int(v) if v is not None else None
    except Exception:
        return None


def _extract_ops(db_path: Path, repo: str, rowid_min: int | None):
    """Replay tool records into {rel_path: [content, session, ts]} + edits."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    q = ("SELECT rowid, time_created, session_id, data FROM part "
         "WHERE data LIKE '%.repowiki/%'")
    if rowid_min is not None:
        q += f" AND rowid > {int(rowid_min)}"
    q += " ORDER BY time_created, rowid"
    writes: dict[str, list] = {}
    edits: dict[str, list] = {}
    foreign: list[str] = []
    try:
        for rowid, ts, sess, data in conn.execute(q):
            try:
                obj = json.loads(data)
            except Exception:
                continue
            tool = obj.get("tool")
            if tool not in ("Write", "Edit", "Bash"):
                continue
            state = obj.get("state") or {}
            if state.get("status") not in ("completed", None):
                continue
            inp = state.get("input") or {}
            if tool in ("Write", "Edit"):
                fp = (inp.get("file_path") or "").replace("&amp;", "&")
                rkey, rel = _repo_key(fp, repo)
                if rkey and rel is None:
                    foreign.append(rkey)
                    continue
                if not rel or not rel.endswith(".md") or not _CONTENT_RE.match(rel):
                    continue
                if rel in OBSOLETE_PATHS:
                    continue
                if tool == "Write":
                    writes[rel] = [inp.get("content") or "", sess, ts]
                else:
                    edits.setdefault(rel, []).append(
                        (inp.get("old_string") or "", inp.get("new_string") or "", sess, ts))
            else:  # Bash: only heredoc writes are recoverable
                for m in _HEREDOC_RE.finditer(inp.get("command") or ""):
                    fp = m.group(1).strip().replace("&amp;", "&")
                    rkey, rel = _repo_key(fp, repo)
                    if rkey and rel is None:
                        foreign.append(rkey)
                        continue
                    if not rel or not rel.endswith(".md") or not _CONTENT_RE.match(rel):
                        continue
                    if rel in OBSOLETE_PATHS:
                        continue
                    writes[rel] = [m.group(3) + "\n", sess, ts]
    finally:
        conn.close()
    return writes, edits, foreign


def run_restore(repo: str, db: str | None = None, locale: str | None = None,
                rowid_min: int | None = None, out: str | None = None,
                dry_run: bool = False, as_json: bool = False) -> int:
    repo_root = os.path.abspath(repo)
    db_path = Path(db).expanduser() if db else _default_db(repo_root)
    if db_path is None or not db_path.exists():
        raise UsageError(
            "找不到恢复数据源：没有 .repowiki/db/recovery.db 快照，"
            "也没有 $REPOWIKI_RECOVERY_DB 指定的数据源"
        )
    if rowid_min is None:
        rowid_min = _manifest_rowid_min(repo_root)
    out_root = Path(out).expanduser() if out else Path(repo_root) / ".repowiki"

    writes, edits, foreign = _extract_ops(db_path, repo_root, rowid_min)
    if locale:
        writes = {k: v for k, v in writes.items() if k.split("/", 1)[0] == locale}
        edits = {k: v for k, v in edits.items() if k.split("/", 1)[0] == locale}

    applied, unapplied = 0, []
    for rel, elist in edits.items():
        if rel not in writes:
            unapplied.append((rel, "no base Write"))
            continue
        text = writes[rel][0]
        for old, new, _sess, ts in elist:
            if old in text:
                text = text.replace(old, new, 1)
                applied += 1
            else:
                unapplied.append((rel, f"old_string not found @{ts}"))
        writes[rel][0] = text

    result = {
        "repo": repo_root,
        "db": str(db_path),
        "rowid_min": rowid_min,
        "files": len(writes),
        "files_with_edits": len(edits),
        "edits_applied": applied,
        "foreign_repos_skipped": sorted(set(foreign)),
        "dry_run": dry_run,
        "would_write": {rel: len(c[0]) for rel, c in sorted(writes.items())} if dry_run else None,
    }
    if not dry_run:
        n = 0
        for rel, (content, _sess, _ts) in sorted(writes.items()):
            dest = out_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            n += 1
        result["restored"] = n

    from .output import emit
    emit(result, _restore_human, as_json)
    return 0


def _restore_human(r: dict) -> None:
    src = "snapshot" if r["db"].endswith("recovery.db") else "tool db"
    print(f"恢复源: {r['db']}（{src}）  rowid_min: {r['rowid_min']}")
    print(f"页面: {r['files']} 个；带 Edit 记录: {r['files_with_edits']}；应用 Edit: {r['edits_applied']}")
    if r["foreign_repos_skipped"]:
        print(f"已跳过外来仓库记录: {', '.join(r['foreign_repos_skipped'])}")
    if r["dry_run"]:
        for rel, size in r["would_write"].items():
            print(f"  将写入 {size:6d}B  {rel}")
    else:
        print(f"已恢复 {r['restored']} 个文件")
    print("提示: 恢复后运行 claim+check 循环让校验器重套确定性修复；语义类错误需手工修复")
