"""``repowiki plan``: scan the repo and lay down the task manifest."""

from __future__ import annotations

import json
import shutil

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
            "replan 会重置任务状态（现有各语言产物会先备份到 .repowiki-backup-<时间戳>/）；"
            "非交互环境必须显式加 --force 确认"
        )
    ans = input(
        "replan 将重置任务状态，并把现有 wiki 产物移入 .repowiki-backup-<时间戳>/，输入 yes 确认: "
    )
    if ans.strip().lower() not in ("y", "yes"):
        raise UsageError("已取消 replan")


def _backup_outputs(paths: WikiPaths) -> list[str]:
    """Move every non-state child of .repowiki into a timestamped backup dir.

    Prevents replan from destroying finished wiki output (e.g. other locales
    or a completed site) — state/ alone is enough to rebuild the rest.
    """
    import time

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest_root = paths.repo_root / f".repowiki-backup-{stamp}"
    moved: list[str] = []
    for child in sorted(paths.root.iterdir()):
        if child.name == "state":
            continue
        dest = dest_root / child.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(child), str(dest))
        moved.append(child.name)
    return moved


def run_plan(paths: WikiPaths, replan: bool = False, max_pages: int | None = None,
             knowledge: bool = False, force: bool = False, as_json: bool = False,
             locale: str = "auto") -> int:
    backed_up: list[str] = []
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
        backed_up = _backup_outputs(paths)
        shutil.rmtree(paths.root)
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

    warnings: list[str] = []
    warnings.extend(pending_warnings)
    if backed_up:
        warnings.append(
            "replan 已将现有产物备份到 .repowiki-backup-*/（" + ", ".join(backed_up) + "）"
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
