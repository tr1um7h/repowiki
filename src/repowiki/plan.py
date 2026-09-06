"""``repowiki plan``: scan the repo and lay down the task manifest."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .errors import UsageError
from .i18n import SUPPORTED, detect_locale, strings
from .output import emit
from .paths import WikiPaths
from . import tasks
from .catalog import validate_catalog, flatten
from .scanner import scan
from .state import TaskStore

MIN_CODE_FILES = 10


def _load_catalog(paths: WikiPaths) -> dict | None:
    if not paths.catalog_file.exists():
        return None
    try:
        return json.loads(paths.catalog_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _resolve_locale(paths: WikiPaths, locale_flag: str, inv) -> str:
    """Explicit --locale wins; else a persisted state/locale; else detect."""
    if locale_flag != "auto":
        return locale_flag
    if (paths.state_dir / "locale").exists():
        return paths.locale  # lazy property reads the persisted value
    return detect_locale(paths.repo_root, [f.path for f in inv.files if f.is_code])


def _confirm_replan(force: bool) -> None:
    """replan destroys task state; require explicit confirmation."""
    import sys

    if force:
        return
    if not sys.stdin.isatty():
        raise UsageError(
            "replan 会重置任务状态（整个 .repowiki 会原地重命名为 .repowiki-<最后更新时间>/ 保留）；"
            "非交互环境必须显式加 --force 确认"
        )
    ans = input(
        "replan 将重置任务状态，并把整个 .repowiki 原地重命名为 .repowiki-<最后更新时间>/ 保留，输入 yes 确认: "
    )
    if ans.strip().lower() not in ("y", "yes"):
        raise UsageError("已取消 replan")


def _latest_mtime(root: Path) -> float:
    """Newest mtime anywhere under root (falls back to root's own mtime)."""
    latest = root.stat().st_mtime
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                latest = max(latest, os.stat(os.path.join(dirpath, fn)).st_mtime)
            except OSError:
                continue
    return latest


RECOVERY_DB_ENV = "REPOWIKI_RECOVERY_DB"
RECOVERY_SNAPSHOT_DIR = "db"
RECOVERY_SNAPSHOT_NAME = "recovery.db"


def _recovery_db_path() -> Path | None:
    """Source tool-record db (ZCode); overridable via REPOWIKI_RECOVERY_DB."""
    p = Path(os.environ.get(
        RECOVERY_DB_ENV,
        ".repowiki/db/recovery.db",
    ))
    return p if p.exists() else None


def _part_rowid_max(db_path: Path | None) -> int | None:
    """Max rowid of the tool-record part table — read-only probe, never fatal."""
    if db_path is None:
        return None
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(rowid) FROM part").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception:
        return None


def _snapshot_part_rows(paths: WikiPaths, rowid_min: int, rowid_max: int) -> Path | None:
    """Copy this generation's part rows into <repo>/.repowiki/db/recovery.db.

    The snapshot makes the wiki generation self-contained: the rows (bounded
    by the generation's rowid window and filtered to this repo's records) live
    inside .repowiki and travel with it when it is archived, so recovery never
    depends on — or risks pollution from — the machine-wide tool db.
    """
    src = _recovery_db_path()
    if src is None:
        return None
    try:
        import sqlite3

        sconn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            rows = sconn.execute(
                "SELECT rowid, time_created, session_id, data FROM part "
                "WHERE rowid > ? AND rowid <= ? AND instr(data, ?) > 0",
                (rowid_min, rowid_max, f"{paths.repo_root}/.repowiki/"),
            ).fetchall()
        finally:
            sconn.close()
        dest_dir = paths.root / RECOVERY_SNAPSHOT_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / RECOVERY_SNAPSHOT_NAME
        dconn = sqlite3.connect(dest)
        try:
            dconn.execute(
                "CREATE TABLE IF NOT EXISTS part ("
                "rowid INTEGER PRIMARY KEY, time_created INTEGER, "
                "session_id TEXT, data TEXT)")
            dconn.execute("DELETE FROM part")
            dconn.executemany(
                "INSERT INTO part (rowid, time_created, session_id, data) "
                "VALUES (?, ?, ?, ?)", rows)
            dconn.commit()
        finally:
            dconn.close()
        return dest
    except Exception:
        return None


def _read_manifest(paths: WikiPaths) -> dict:
    try:
        return json.loads((paths.root / "recovery-manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_manifest(paths: WikiPaths, rowid_min: int | None) -> None:
    """Record how to restore this generation's content, and snapshot its rows.

    ``part_rowid_min`` is the max rowid recorded when the previous generation
    was archived (or the value at first plan); ``part_rowid_max`` is the max
    rowid at write time. The half-open window (min, max] bounds the sqlite
    part rows that produced this wiki generation; those rows are snapshotted
    into ``.repowiki/db/recovery.db`` so the manifest's ``recovery_db`` stays
    valid even after this directory is renamed or moved to another machine.
    """
    db = _recovery_db_path()
    cur_max = _part_rowid_max(db)
    snap = None
    if cur_max is not None:
        snap = _snapshot_part_rows(paths, rowid_min or 0, cur_max)
    manifest = {
        "repo_root": str(paths.repo_root),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "entries": sorted(c.name for c in paths.root.iterdir()),
        "part_rowid_min": rowid_min,
        "part_rowid_max": cur_max,
        "recovery_db": (
            f"{RECOVERY_SNAPSHOT_DIR}/{RECOVERY_SNAPSHOT_NAME}" if snap else None
        ),
        "recovery_hint": (
            "python3 repowiki-restore.py --repo <repo_root> "
            "--db <wiki_root>/db/recovery.db  "
            "(the snapshot already contains exactly this generation's rows; "
            "part_rowid_min/max document the source-db window)"
        ),
    }
    (paths.root / "recovery-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _archive_repo_wiki(paths: WikiPaths) -> str:
    """Rename the whole .repowiki to .repowiki-<lastUpdatedTime>, in place.

    Nothing is deleted or moved elsewhere: the archived directory stays next
    to the new one. Before the rename the existing recovery manifest and its
    db/recovery.db snapshot are refreshed with the current part rowid max, so
    the archived folder is fully self-describing and self-contained; the new
    .repowiki starts a fresh snapshot window for the upcoming generation.
    """
    prev = _read_manifest(paths)
    rowid_min = prev.get("part_rowid_max") or 0
    # refresh manifest + snapshot of the outgoing generation, then archive it
    _write_manifest(paths, rowid_min)
    cur_max = _part_rowid_max(_recovery_db_path())

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(_latest_mtime(paths.root)))
    target = paths.root.with_name(f"{paths.root.name}-{stamp}")
    n = 1
    while target.exists():
        target = paths.root.with_name(f"{paths.root.name}-{stamp}-{n}")
        n += 1
    paths.root.rename(target)
    # state/ went with the archived dir; recreate a bare state dir for the new plan
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    # start a fresh snapshot window for the upcoming generation
    _write_manifest(paths, cur_max if cur_max is not None else rowid_min)
    return target.name


def run_plan(paths: WikiPaths, replan: bool = False, max_pages: int | None = None,
             knowledge: bool = False, force: bool = False, as_json: bool = False,
             locale: str = "auto") -> int:
    archived: str | None = None
    pending_warnings: list[str] = []
    if replan and paths.root.exists():
        busy = _busy_tasks(paths)
        if busy is None and not force:
            raise UsageError(
                "state/index.json 无法解析，无法确认是否有任务在执行；"
                "replan 会删除现有状态与规格，确认放弃恢复请加 --force"
            )
        if busy and not force:
            raise UsageError(
                f"检测到 {len(busy)} 个 in_progress 任务（{', '.join(busy[:5])}），"
                "replan 会删除正在被写入的产物；确认请加 --force"
            )
        if busy and force:
            pending_warnings.append(
                f"检测到 {len(busy)} 个 in_progress 任务但已 --force 继续："
                "这些 worker 的后续写入可能会与新的 plan 冲突，"
                "建议先 release 或等待 worker 退出后再 replan"
            )
        _confirm_replan(force)
        archived = _archive_repo_wiki(paths)
    store = TaskStore(paths)
    inv = scan(paths.repo_root)

    if inv.code_file_count < MIN_CODE_FILES:
        raise UsageError(
            f"代码文件数 {inv.code_file_count} < {MIN_CODE_FILES}，仓库太小，不适合生成 RepoWiki"
        )

    # locale must be resolved before ensure(): the locale decides which
    # <locale>/content and <locale>/meta directories get created
    resolved = _resolve_locale(paths, locale, inv)
    persisted = (paths.state_dir / "locale").exists()
    if not persisted or locale != "auto":
        paths.persist_locale(resolved)
    paths.ensure()
    if not (paths.root / "recovery-manifest.json").exists():
        _db = _recovery_db_path()
        _write_manifest(paths, (_part_rowid_max(_db) or 0) if _db else 0)

    warnings: list[str] = []
    warnings.extend(pending_warnings)
    if archived:
        warnings.append(
            f"replan 已将原 .repowiki 原地重命名保留为 {archived}/"
            "（含 recovery-manifest.json，记录对应的 sqlite part 行号范围）；如需清理请手动删除"
        )
    added: list[str] = []
    catalog = _load_catalog(paths)
    if catalog is not None:
        errors, warns = validate_catalog(catalog, inv.known_paths())
        warnings.extend(warns)
        if errors:
            warnings.append("已有 catalog.json 校验失败，将重新规划: " + "; ".join(errors[:5]))
            catalog = None

    if catalog is None:
        added += store.add_tasks([tasks.build_catalog_task(paths, inv)])
    else:
        nodes = flatten(catalog, resolved)
        added += store.add_tasks(tasks.build_page_tasks(paths, nodes, inv, max_pages))
        warnings.extend(_warn_dangling_outputs(paths, nodes))

    if knowledge:
        added += store.add_tasks([tasks.build_knowledge_plan_task(paths, inv)])

    stats = store.stats()
    result = {
        "repo": str(paths.repo_root),
        "locale": resolved,
        "code_file_count": inv.code_file_count,
        "tasks_added": added,
        "tasks_total": stats["total"],
        "by_status": stats["by_status"],
        "current_phase": stats["current_phase"],
        "warnings": warnings,
        "next": "执行 `repowiki next <repo> --claim` 领取任务",
    }
    emit(result, _plan_human, as_json)
    return 0


def _plan_human(r: dict) -> str:
    lines = [
        f"仓库：{r['repo']}（代码文件 {r['code_file_count']} 个，产出语言：{strings(r['locale'])['name']}）",
        f"新增任务 {len(r['tasks_added'])} 个，总任务 {r['tasks_total']} 个，当前阶段 {r['current_phase']}",
        *[f"  ⚠ {w}" for w in r["warnings"]],
        r["next"],
    ]
    return "\n".join(lines)


def _busy_tasks(paths: WikiPaths) -> list[str] | None:
    """Ids of in_progress tasks; None when the index exists but is unreadable."""
    if not paths.index_file.exists():
        return []
    try:
        data = json.loads(paths.index_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return [tid for tid, t in data.get("tasks", {}).items() if t.get("status") == "in_progress"]


def _warn_dangling_outputs(paths: WikiPaths, nodes: list[dict]) -> list[str]:
    """Pages already on disk but no longer in the catalog (stale leftovers)."""
    expected = {n.output for n in nodes}
    dangling: list[str] = []
    content = paths.content_dir
    if not content.exists():
        return dangling
    for p in content.rglob("*.md"):
        rel = f"{paths.locale}/content/" + p.relative_to(content).as_posix()
        if rel not in expected:
            dangling.append(rel)
    if dangling:
        return [f"{len(dangling)} 个已生成页面不在当前 catalog 中（可能为旧残留）: " + ", ".join(dangling[:5])]
    return []
