"""``repowiki site``: render the finished wiki into one self-contained HTML file.

Reads every generated page plus the overview, embeds the source lines behind
each ``file://`` reference, and inlines the vendored markdown/mermaid JS.
The result (``<locale>/wiki.html``) opens in any browser with zero network
and zero server — double-click, or share the single file.

Rendering is progressive: while tasks are still in flight (e.g. module-scoped
generation), a draft metadata is written and only finished pages render;
once every task is done, ``site`` runs the real finalize and renders the
full-resolution wiki — same command, one shot, no extra steps.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import webbrowser
from pathlib import Path

from . import templates
from .catalog import FlatNode, flatten
from .errors import UsageError
from .i18n import strings
from .output import emit
from .paths import WikiPaths
from .state import TaskStore, now_iso
from .validate import extract_refs

MAX_SNIPPET_LINES = 20_000  # larger spans are skipped rather than bloating the file


# --- progressive metadata (draft ↔ full) ---

def _load_metadata(paths: WikiPaths) -> dict | None:
    """Parsed metadata, or None when missing / corrupt / still a draft."""
    if not paths.metadata_file.is_file():
        return None
    try:
        return json.loads(paths.metadata_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _has_content(paths: WikiPaths) -> bool:
    return paths.content_dir.is_dir() and any(paths.content_dir.rglob("*.md"))


def _all_tasks_done(paths: WikiPaths) -> bool:
    """True when the manifest includes the overview task and everything is done.
    An absent overview task means finalize would still expand the manifest
    (first-run exit 3), so the wiki is not at full resolution yet."""
    try:
        tasks = TaskStore(paths).load().get("tasks") or {}
    except Exception:
        return False
    if not tasks or "overview" not in tasks:
        return False
    return all(t.get("status") == "done" for t in tasks.values())


def _write_draft_metadata(paths: WikiPaths) -> dict:
    """Minimal placeholder letting `site` render a partial wiki. The real
    `finalize` atomically replaces it once every task is done."""
    draft = {
        "wiki_repo": {"name": paths.repo_root.name},
        "wiki_overview": "",
        "generatedAt": now_iso(),
        "_draft": True,
        "draft_note": "Draft metadata for progressive rendering (partial completion); "
                      "`repowiki finalize` replaces it with the full document.",
    }
    paths.meta_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.metadata_file.with_name(f".{paths.metadata_file.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, paths.metadata_file)
    return draft


def _ensure_metadata(paths: WikiPaths, has_pages: bool) -> tuple[dict, bool, bool]:
    """Progressive gate: return (metadata, is_draft, finalized_now).

    - Full metadata present → render as-is.
    - Missing, corrupt, or draft metadata with every task done → run the real
      finalize (silently; the site summary reports it) and render the
      full-resolution wiki.
    - Partial progress (module/scoped generation) → write the draft metadata
      and render only the finished pages.
    """
    meta = _load_metadata(paths)
    if meta is not None and not meta.get("_draft"):
        return meta, False, False
    from .metadata import run_finalize  # lazy: mirrors cli.py's command wiring

    if has_pages and _all_tasks_done(paths):
        # keep stdout clean: the site command owns the output contract
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run_finalize(paths, as_json=False)
        if rc == 0:
            meta = _load_metadata(paths)
            if meta is not None:
                return meta, False, True
    if not has_pages:
        raise UsageError(
            f"未找到任何已生成的 wiki 页面（{paths.content_dir} 为空）："
            "请先运行 `repowiki plan` 并生成页面；若已全部完成，运行 `repowiki finalize <repo>`"
        )
    return _write_draft_metadata(paths), True, False


def run_site(paths: WikiPaths, open_browser: bool, as_json: bool) -> int:
    metadata, is_draft, finalized_now = _ensure_metadata(paths, _has_content(paths))

    nodes = _ordered_nodes(paths)
    pages = _collect_pages(paths, metadata, nodes)
    if not pages:
        raise UsageError(f"未找到任何已生成的 wiki 页面（{paths.content_dir} 为空）")
    nav = _build_nav(paths, pages, nodes)
    snippets = _collect_snippets(paths.repo_root, pages)

    payload = {
        "repo": _repo_name(paths, metadata),
        "locale": paths.locale,
        "generatedAt": now_iso(),
        "ui": strings(paths.locale)["site"],
        "nav": nav,
        "pages": pages,
        "snippets": snippets,
    }
    html = _render_html(payload, paths.locale)

    out = paths.site_file
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(html, encoding="utf-8")
    os.replace(tmp, out)
    if open_browser:
        webbrowser.open(out.as_uri())

    summary = {
        "ok": True,
        "site": str(out),
        "draft": is_draft,
        "finalized": finalized_now,
        "pages": len(pages),
        "snippets": len(snippets),
        "size_mb": round(out.stat().st_size / 1024 / 1024, 2),
    }
    emit(summary, _site_human, as_json)
    return 0


def _site_human(r: dict) -> str:
    lines = [
        f"✓ 站点已生成: {r['site']}",
        f"  页面 {r['pages']} · 源码片段 {r['snippets']} · 体积 {r['size_mb']} MB",
    ]
    if r.get("draft"):
        lines.append(
            "  草稿模式（渐进式）：仅含已完成页面；全部任务完成后重跑 `repowiki site`"
            " 将自动 finalize 并升级为完整站点"
        )
    elif r.get("finalized"):
        lines.append("  已自动 finalize：检测到全部任务完成，全量 metadata 已生成，完整站点一次渲染")
    lines.append("  单文件离线可用：浏览器直接打开即可（--open 自动打开）")
    return "\n".join(lines)


# --- content assembly ---

def _repo_name(paths: WikiPaths, metadata: dict) -> str:
    return (metadata.get("wiki_repo") or {}).get("name") or paths.repo_root.name


def _collect_pages(paths: WikiPaths, metadata: dict, nodes: list[FlatNode]) -> list[dict]:
    site = strings(paths.locale)["site"]
    pages: list[dict] = []

    overview = metadata.get("wiki_overview") or ""
    if not overview and paths.overview_file.is_file():
        overview = paths.overview_file.read_text(encoding="utf-8")
    if overview and overview != "No overview yet.":
        pages.append({
            "id": "overview",
            "title": site["overview_label"],
            "path": paths.overview_file.relative_to(paths.root).as_posix(),
            "md": overview,
        })

    for node in nodes:
        f = paths.root / node.output
        if not f.is_file():
            continue
        pages.append({
            "id": node.id,
            "title": node.title,
            "path": node.output,
            "md": f.read_text(encoding="utf-8"),
        })
    return pages


def _ordered_nodes(paths: WikiPaths) -> list[FlatNode]:
    """Plan order from state/catalog.json; after `repowiki clean`, degrade to
    the on-disk directory layout so the site stays buildable."""
    if paths.catalog_file.is_file():
        try:
            catalog = json.loads(paths.catalog_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            catalog = None
        if catalog is not None:
            return flatten(catalog, paths.locale)
    return _nodes_from_disk(paths)


def _nodes_from_disk(paths: WikiPaths) -> list[FlatNode]:
    """Chapter per sub-directory, page per .md file — plan order is lost,
    but the site stays buildable after `repowiki clean`."""
    nodes: list[FlatNode] = []
    content = paths.content_dir
    if not content.is_dir():
        return nodes

    def page_node(f: Path, parent_id: str | None) -> FlatNode:
        return FlatNode(
            id=f.stem, title=f.stem, slug=f.stem, summary="", kind="page",
            dependent_files=[], page_brief="", parent_id=parent_id, depth=2,
            output=f.relative_to(paths.root).as_posix(),
        )

    for entry in sorted(content.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and any(entry.glob("*.md")):
            nodes.append(FlatNode(
                id=entry.name, title=entry.name, slug=entry.name, summary="", kind="chapter",
                dependent_files=[], page_brief="", parent_id=None, depth=1,
                output=f"{entry.relative_to(paths.root).as_posix()}/__chapter__.md",
            ))
            for f in sorted(entry.glob("*.md")):
                nodes.append(page_node(f, entry.name))
        elif entry.is_file() and entry.suffix == ".md":
            nodes.append(page_node(entry, None))
    return nodes


def _build_nav(paths: WikiPaths, pages: list[dict], nodes: list[FlatNode]) -> list[dict]:
    site = strings(paths.locale)["site"]
    by_output = {p["path"]: i for i, p in enumerate(pages)}
    children_of: dict[str | None, list[FlatNode]] = {}
    for n in nodes:
        children_of.setdefault(n.parent_id, []).append(n)

    def descendants(node_id: str) -> list[FlatNode]:
        out: list[FlatNode] = []
        for k in children_of.get(node_id, []):
            out.append(k)
            out.extend(descendants(k.id))
        return out

    entries: list[dict] = []
    for i, p in enumerate(pages):
        if p["id"] == "overview":
            entries.append({"title": site["overview_label"], "page": i})
            break

    for top in children_of.get(None, []):
        own = [{"title": top.title, "page": by_output[top.output]}] if top.output in by_output else []
        kids = [
            {"title": k.title, "page": by_output[k.output]}
            for k in descendants(top.id) if k.output in by_output
        ]
        if not kids:
            if own:  # standalone top-level page (with or without a chapter wrapper)
                entries.append({"title": top.title, "page": own[0]["page"]})
            continue
        entries.append({"title": top.title, "children": own + kids})
    return entries


# --- source snippet extraction ---

def _collect_snippets(repo_root: Path, pages: list[dict]) -> dict:
    snippets: dict = {}
    for p in pages:
        for path, start, end in extract_refs(p["md"]):
            if start is None:
                key, s, e = path, 1, None
            else:
                key, s, e = f"{path}#L{start}-L{end}", start, end
            if key in snippets:
                continue
            lines = _read_lines(repo_root / path)
            if lines is None:
                snippets[key] = {"path": path, "missing": True}
                continue
            e = min(e or len(lines), len(lines))
            s = max(1, min(s, e))
            if e - s + 1 > MAX_SNIPPET_LINES:
                snippets[key] = {"path": path, "missing": True}
                continue
            snippets[key] = {"path": path, "start": s, "end": e, "lines": lines[s - 1:e]}
    return snippets


def _read_lines(p: Path) -> list[str] | None:
    try:
        if not p.is_file():
            return None
        data = p.read_bytes()
        if b"\0" in data[:8192]:  # binary file
            return None
        return data.decode("utf-8", "replace").splitlines()
    except OSError:
        return None


# --- HTML assembly ---

def _render_html(payload: dict, locale: str) -> str:
    payload_js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    shell = (templates.TEMPLATE_DIR / "site.html").read_text(encoding="utf-8")
    app_js = _script_safe((templates.TEMPLATE_DIR / "site" / "app.js").read_text(encoding="utf-8"))
    return templates.render(
        shell,
        SITE_PAYLOAD=payload_js,
        MARKED_JS=_vendor("marked.min.js"),
        MERMAID_JS=_vendor("mermaid.min.js"),
        APP_JS=app_js,
    )


def _vendor(name: str) -> str:
    js = (Path(__file__).parent / "vendor" / name).read_text(encoding="utf-8")
    return _script_safe(js)


def _script_safe(js: str) -> str:
    # Inside an inline <script> the literal `</script` would close the tag;
    # `<\/script` is an equivalent escape inside JS strings and regexes.
    return js.replace("</script", "<\\/script")
