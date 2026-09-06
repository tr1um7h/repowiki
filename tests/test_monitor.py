"""Monitor tests: symptom thresholds, throttle/probe/recovery state machine,
and the dispatch cap enforced by `next --claim`."""

from __future__ import annotations

import json
import time

import pytest

from conftest import valid_page
from repowiki.dispatch import run_check, run_next, run_status
from repowiki.monitor import (
    DEFAULT_FLEET,
    STATE_NORMAL,
    STATE_PROBING,
    STATE_RECOVERING,
    STATE_THROTTLED,
    Monitor,
)
from repowiki.state import TaskStore, new_task


@pytest.fixture
def mon(paths):
    paths.ensure()
    return Monitor(paths)


def _backdate(mon: Monitor, field: str, seconds_ago: int) -> None:
    """Rewrite a timestamp field in monitor.json to the past (time travel)."""
    data = mon.load()
    data[field] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago)
    )
    mon._save(data)


def _seed_throttled(mon: Monitor) -> None:
    mon.record("timeout", "w1")
    assert mon.load()["state"] == STATE_THROTTLED


# --- thresholds ---

def test_normal_by_default(mon):
    assert mon.load()["state"] == STATE_NORMAL
    assert mon.effective_cap() is None


def test_stream_streak_throttles_at_five(mon):
    for i in range(4):
        mon.record("stream_error", f"w{i}")
    assert mon.effective_cap() is None  # 4 连击不触发
    mon.record("stream_error", "w4")
    assert mon.load()["state"] == STATE_THROTTLED
    assert mon.effective_cap() == 1
    assert mon.load()["throttle_reason"] == "stream_error_streak"


def test_ok_resets_stream_streak(mon):
    for i in range(4):
        mon.record("stream_error", "w1")
    mon.record("ok", "w1")
    mon.record("stream_error", "w2")
    assert mon.load()["state"] == STATE_NORMAL


def test_timeout_and_cancel_throttle_immediately(mon):
    mon.record("timeout", "w1")
    assert mon.effective_cap() == 1
    mon2 = Monitor(mon.paths)
    mon2.set_workers(3)  # 不影响已限流状态
    assert mon2.effective_cap() == 1


def test_single_cancel_throttles(mon):
    mon.record("cancel", "w1")
    assert mon.load()["state"] == STATE_THROTTLED
    assert mon.load()["throttle_reason"] == "cancel"


def test_window_prunes_old_symptoms(mon):
    for i in range(5):
        mon.record("stream_error", "w1")
    assert mon.load()["state"] == STATE_THROTTLED
    # 回到 normal 并把全部症状改成窗口之外 → 旧症状不再参与连击统计
    data = mon.load()
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    for s in data["symptoms"]:
        s["at"] = old
    data["state"] = STATE_NORMAL
    data["cap"] = None
    data["cooldown_until"] = None
    mon._save(data)
    payload = mon.status_payload()
    assert payload["stream_streak"] == 0
    assert payload["recent_symptom_count"] == 0
    mon.record("stream_error", "w1")  # 窗口内只有这 1 条
    assert mon.load()["state"] == STATE_NORMAL


def test_throttled_symptom_extends_cooldown(mon):
    mon.record("timeout", "w1")
    first = mon.load()["cooldown_until"]
    time.sleep(1.1)
    mon.record("stream_error", "w2")
    assert mon.load()["cooldown_until"] > first


def test_corrupt_monitor_file_raises(paths):
    paths.ensure()
    paths.monitor_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception):
        Monitor(paths).effective_cap()
    assert paths.monitor_file.read_text(encoding="utf-8") == "{not json"  # 现场保留


# --- state machine: cooldown → probe → recover ---

def test_cooldown_expiry_enters_probing(mon):
    _seed_throttled(mon)
    _backdate(mon, "cooldown_until", 1)  # 冷却已过期
    assert mon.effective_cap() == 1
    assert mon.load()["state"] == STATE_PROBING


def test_probe_success_starts_ramp(mon):
    _seed_throttled(mon)
    _backdate(mon, "cooldown_until", 1)
    mon.effective_cap()  # 触发惰性转换 → probing
    mon.on_task_success()
    assert mon.load()["state"] == STATE_RECOVERING
    assert mon.load()["cap"] == 2
    assert mon.effective_cap() == 2


def test_ramp_until_declared_fleet(mon):
    mon.set_workers(3)
    _seed_throttled(mon)
    _backdate(mon, "cooldown_until", 1)
    mon.effective_cap()
    mon.on_task_success()  # probing → recovering cap=2
    mon.on_task_success()  # cap=3 == ceiling → normal
    assert mon.load()["state"] == STATE_NORMAL
    assert mon.effective_cap() == 3  # normal 态下 cap = max_workers


def test_ramp_default_ceiling_when_fleet_unknown(mon):
    _seed_throttled(mon)
    _backdate(mon, "cooldown_until", 1)
    mon.effective_cap()
    for _ in range(3):  # 2 → 3 → 4（DEFAULT_FLEET）→ normal
        mon.on_task_success()
    assert mon.load()["state"] == STATE_NORMAL
    assert mon.effective_cap() is None
    assert DEFAULT_FLEET == 4


def test_probe_timeout_returns_to_throttled(mon):
    _seed_throttled(mon)
    _backdate(mon, "cooldown_until", 7200)  # 冷却早过期
    mon.effective_cap()  # → probing
    _backdate(mon, "probing_since", 7200)  # 探针卡了 2 小时
    assert mon.snapshot()["state"] == STATE_THROTTLED
    assert mon.load()["throttle_reason"] == "probe_timeout"


def test_success_during_throttle_does_not_lift(mon):
    _seed_throttled(mon)
    mon.on_task_success()
    assert mon.load()["state"] == STATE_THROTTLED  # 必须等冷却+探针


# --- dispatch integration ---

def _plan_two_tasks(paths):
    store = TaskStore(paths)
    store.add_tasks([
        new_task("t01", "page", 2, "页一", "zh/content/p1.md"),
        new_task("t02", "page", 2, "页二", "zh/content/p2.md"),
    ])
    return store


def test_next_refuses_at_cap(paths):
    paths.ensure()
    store = _plan_two_tasks(paths)
    _seed_throttled(Monitor(paths))

    r1 = json.loads(_capture_next(paths))  # 第一个 worker 领到（探针）
    assert len(r1["tasks"]) == 1
    assert r1["throttle"] is None

    r2 = json.loads(_capture_next(paths))  # 已有 1 个存活认领 == cap 1 → 拒绝
    assert r2["tasks"] == []
    assert r2["throttle"]["active"] is True
    assert r2["throttle"]["cap"] == 1
    assert r2["busy"] == 1


def test_next_unlimited_without_throttle(paths):
    paths.ensure()
    store = _plan_two_tasks(paths)
    r1 = json.loads(_capture_next(paths))
    assert len(r1["tasks"]) == 1
    r2 = json.loads(_capture_next(paths))
    assert len(r2["tasks"]) == 1  # 无限流时第二个认领正常发放
    assert r2["throttle"] is None


def _capture_next(paths) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        run_next(paths, claim=True, worker="w-test", as_json=True)
    return buf.getvalue()


# --- check hook drives the ramp ---

def test_check_done_advances_monitor(paths):
    paths.ensure()
    out = paths.root / "zh" / "content" / "探针页.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(valid_page("探针页"), encoding="utf-8")
    store = TaskStore(paths)
    store.add_tasks([new_task("probe", "page", 2, "探针页", "zh/content/探针页.md")])
    store.claim("probe", "w-probe")

    _seed_throttled(Monitor(paths))
    _backdate(Monitor(paths), "cooldown_until", 1)
    Monitor(paths).effective_cap()  # → probing

    assert run_check(paths, "probe", as_json=True) == 0
    data = Monitor(paths).load()
    assert data["state"] == STATE_RECOVERING
    assert data["cap"] == 2
    assert store.get("probe")["status"] == "done"


def test_status_includes_monitor_section(paths):
    paths.ensure()
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        run_status(paths, as_json=True)
    out = json.loads(buf.getvalue())
    assert out["monitor"]["state"] == STATE_NORMAL
    assert "cooldown_remaining_seconds" in out["monitor"]


def test_monitor_set_workers_persists(paths):
    paths.ensure()
    mon = Monitor(paths)
    mon.set_workers(6)
    assert Monitor(paths).status_payload()["max_workers"] == 6
    assert Monitor(paths).effective_cap() == 6  # normal 态按声明上限发放
