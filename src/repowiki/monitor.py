"""Rate-limit sentinel: symptom ledger + adaptive dispatch cap.

The CLI never sees agent-session failures (stream interrupts, hung runs,
cancels) — the driving session observes them and reports here via
``repowiki monitor --report``. The monitor keeps a sliding-window ledger in
``state/monitor.json`` and, when symptoms cross a threshold, caps how many
live claims ``next --claim`` may create. Recovery is probe-based: after a
cooldown exactly one worker gets through (the probe); its first successful
task starts a linear cap ramp back to the fleet size.

State machine (all transitions lazy — they happen inside whatever command
touches the monitor: next / check / status / monitor; there is no daemon):

  normal ---threshold hit---> throttled (cap=1, cooldown T)
  throttled ---T elapsed---> probing (cap=1; the one worker is the probe)
  probing ---task done---> recovering (cap=2)
  recovering ---each task done---> cap+1, until ceiling -> normal
  probing ---T elapsed, no success---> throttled (fresh cooldown)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

from .errors import StateError, UsageError
from .paths import WikiPaths
from .state import _exclusive_lock, now_iso

STREAM_STREAK_LIMIT = 5        # consecutive stream interrupts in window
DEFAULT_WINDOW_SECONDS = 10 * 60
DEFAULT_COOLDOWN_SECONDS = 30 * 60
DEFAULT_FLEET = 4              # ramp ceiling when fleet size was never declared

STATE_NORMAL = "normal"
STATE_THROTTLED = "throttled"
STATE_PROBING = "probing"
STATE_RECOVERING = "recovering"

SYMPTOM_TYPES = ("stream_error", "timeout", "cancel", "ok")


def window_seconds() -> int:
    return _env_seconds("REPOWIKI_MONITOR_WINDOW", DEFAULT_WINDOW_SECONDS)


def cooldown_seconds() -> int:
    return _env_seconds("REPOWIKI_MONITOR_COOLDOWN", DEFAULT_COOLDOWN_SECONDS)


def _env_seconds(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _ts(iso: str) -> float:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return 0.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_state() -> dict:
    return {
        "state": STATE_NORMAL,
        "max_workers": 0,          # 0 = unlimited (driver never declared a fleet)
        "cap": None,               # effective cap while not normal
        "symptoms": [],            # sliding-window ledger of reported symptoms
        "cooldown_until": None,
        "probing_since": None,
        "throttle_reason": None,
        "updated_at": now_iso(),
    }


class Monitor:
    def __init__(self, paths: WikiPaths):
        self.paths = paths

    # --- persistence (same flock + atomic replace discipline as index.json) ---

    def _file(self):
        return self.paths.monitor_file

    def load(self) -> dict:
        f = self._file()
        if not f.exists():
            return _default_state()
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise StateError(
                f"state/monitor.json 损坏（{e}）。文件已原样保留；"
                "可手工修复，或删除该文件以重置限流监视器"
            ) from e
        base = _default_state()
        base.update({k: v for k, v in data.items() if k in base})
        return base

    def _save(self, data: dict) -> None:
        f = self._file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)

    def _transaction(self, mutate):
        """Serialize monitor read-modify-write across processes (index lock)."""
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.paths.state_dir / ".index.lock", "a+")
        try:
            _exclusive_lock(fh)
            data = self.load()
            changed = mutate(data)
            if changed:
                data["updated_at"] = now_iso()
                self._save(data)
            return data
        finally:
            fh.close()

    # --- lazy state transitions ---

    def _refresh(self, data: dict, now: float | None = None) -> bool:
        """Apply time-based transitions in place; return True if changed."""
        now = time.time() if now is None else now
        cooldown = cooldown_seconds()
        if data["state"] == STATE_THROTTLED:
            if data["cooldown_until"] and now >= _ts(data["cooldown_until"]):
                data["state"] = STATE_PROBING
                data["probing_since"] = _iso(now)
                return True
        elif data["state"] == STATE_PROBING:
            since = _ts(data["probing_since"] or "")
            if now - since >= cooldown:
                data["state"] = STATE_THROTTLED
                data["throttle_reason"] = "probe_timeout"
                data["cooldown_until"] = _iso(now + cooldown)
                return True
        return False

    def snapshot(self) -> dict:
        def apply(data: dict) -> bool:
            return self._refresh(data)

        return self._transaction(apply)

    # --- public operations ---

    def record(self, symptom: str, worker: str | None = None) -> dict:
        """Append a reported symptom and re-evaluate throttle thresholds."""
        if symptom not in SYMPTOM_TYPES:
            raise UsageError(
                f"未知症状类型 {symptom!r}（可选：{'/'.join(SYMPTOM_TYPES)}）"
            )
        window = window_seconds()

        def apply(data: dict) -> bool:
            now = time.time()
            self._refresh(data, now)
            data["symptoms"].append(
                {"type": symptom, "at": _iso(now), "worker": worker or ""}
            )
            data["symptoms"] = [
                s for s in data["symptoms"] if now - _ts(s["at"]) <= window
            ]
            if self._threshold_hit(data["symptoms"]):
                self._throttle(data, now, reason=self._threshold_reason(data["symptoms"]))
            elif data["state"] == STATE_THROTTLED and symptom != "ok":
                # still symptomatic while throttled: push the cooldown out
                data["cooldown_until"] = _iso(now + cooldown_seconds())
            return True

        return self._transaction(apply)

    @staticmethod
    def _threshold_hit(symptoms: list[dict]) -> bool:
        hard = [s for s in symptoms if s["type"] in ("timeout", "cancel")]
        if hard:
            return True
        streak = 0
        for s in reversed(symptoms):
            if s["type"] == "stream_error":
                streak += 1
            else:
                break
        return streak >= STREAM_STREAK_LIMIT

    @staticmethod
    def _threshold_reason(symptoms: list[dict]) -> str:
        for s in symptoms:
            if s["type"] in ("timeout", "cancel"):
                return s["type"]
        return "stream_error_streak"

    @staticmethod
    def _throttle(data: dict, now: float, reason: str) -> None:
        data["state"] = STATE_THROTTLED
        data["cap"] = 1
        data["cooldown_until"] = _iso(now + cooldown_seconds())
        data["throttle_reason"] = reason
        data["probing_since"] = None

    def set_workers(self, n: int) -> dict:
        def apply(data: dict) -> bool:
            data["max_workers"] = max(0, n)
            return True

        return self._transaction(apply)

    def on_task_success(self) -> dict:
        """A task flipped to done: probe succeeds, or the ramp advances."""

        def apply(data: dict) -> bool:
            now = time.time()
            self._refresh(data, now)
            if data["state"] == STATE_PROBING:
                data["state"] = STATE_RECOVERING
                data["cap"] = 2
                return True
            if data["state"] == STATE_RECOVERING:
                ceiling = data["max_workers"] if data["max_workers"] > 0 else DEFAULT_FLEET
                data["cap"] = (data["cap"] or 1) + 1
                if data["cap"] >= ceiling:
                    data["state"] = STATE_NORMAL
                    data["cap"] = None
                    data["throttle_reason"] = None
                return True
            return False

        return self._transaction(apply)

    def effective_cap(self) -> int | None:
        """Max live claims `next --claim` may create right now; None = unlimited."""
        data = self.snapshot()
        state = data["state"]
        if state == STATE_NORMAL:
            return data["max_workers"] or None
        if state in (STATE_THROTTLED, STATE_PROBING):
            return 1
        return data["cap"]

    def status_payload(self) -> dict:
        data = self.snapshot()
        now = time.time()
        window = window_seconds()
        recent = [s for s in data["symptoms"] if now - _ts(s["at"]) <= window]
        streak = 0
        for s in reversed(recent):
            if s["type"] == "stream_error":
                streak += 1
            else:
                break
        payload = dict(data)
        payload["window_seconds"] = window
        payload["cooldown_seconds"] = cooldown_seconds()
        payload["stream_streak"] = streak
        payload["recent_symptom_count"] = len(recent)
        if data["state"] == STATE_THROTTLED and data["cooldown_until"]:
            payload["cooldown_remaining_seconds"] = max(
                0, int(_ts(data["cooldown_until"]) - now)
            )
        else:
            payload["cooldown_remaining_seconds"] = 0
        return payload


def run_monitor(paths: WikiPaths, report: str | None, worker: str | None,
                workers: int | None, as_json: bool) -> int:
    mon = Monitor(paths)
    if workers is not None:
        mon.set_workers(workers)
    if report is not None:
        mon.record(report, worker)
    payload = mon.status_payload()

    if as_json:
        print(json.dumps({"ok": True, **payload}, ensure_ascii=False, indent=2))
    else:
        state = payload["state"]
        cap = payload["cap"] if state != STATE_NORMAL else (payload["max_workers"] or "不限")
        lines = [f"限流监视器：{state}（发放上限 {cap}）"]
        if payload["throttle_reason"]:
            lines.append(f"  触发原因：{payload['throttle_reason']}")
        if payload["cooldown_remaining_seconds"]:
            minutes = (payload["cooldown_remaining_seconds"] + 59) // 60
            lines.append(f"  距下次状态切换约 {minutes} 分钟")
        lines.append(
            f"  最近症状：窗口 {payload['window_seconds'] // 60} 分钟内 "
            f"{payload['recent_symptom_count']} 条（连续流中断 {payload['stream_streak']}）"
        )
        print("\n".join(lines))
    return 0
