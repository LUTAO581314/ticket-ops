"""
观测 — 抢票系统里最被低估的部分。

抢一次票只有三种结局：抢到 / 没抢到 / 不知道为什么没抢到。
前两种你不需要观测，第三种没有观测就永远调不出成功率。

所以每台设备的每次动作都落一条结构化事件：
    {ts, task, device, state, action, ok, ms, detail, shot}
以及逐态截图。事后能回答三个问题：
    1. 卡在哪一态？（状态停留时长分布）
    2. 每个动作花了多少 ms？（延迟预算超了哪一环）
    3. 是被风控挡了还是票没了？（关键词归因）
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# 大麦/猫眼常见的拦截文案 → 归因标签
BLOCK_PATTERNS: dict[str, tuple[str, ...]] = {
    "queue": ("被挤爆", "人数过多", "排队中", "请稍后", "系统繁忙", "稍后再试", "小主"),
    "sold_out": ("无票", "缺货", "已售罄", "暂无余票", "已结束", "缺货登记"),
    "risk": ("环境异常", "验证失败", "操作频繁", "账号异常", "请求异常", "安全验证", "设备异常"),
    "captcha": ("滑块", "验证码", "拖动", "按住", "请完成验证"),
    "login": ("登录", "请先登录", "账号已过期"),
    "net": ("网络", "连接失败", "超时", "重试"),
}


@dataclass
class Event:
    ts: float
    kind: str  # state | action | note | result | block
    task: str = ""
    device: str = ""
    state: str = ""
    action: str = ""
    ok: bool = True
    ms: float = 0.0
    detail: str = ""
    shot: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        base = f"[{self.device or '-'}]"
        if self.state:
            base += f"[{self.state}]"
        if self.action:
            base += f" {self.action}"
        if self.detail:
            base += f" — {self.detail}"
        if self.ms:
            base += f" ({self.ms:.0f}ms)"
        return base


class Observer:
    """
    一个任务一个 Observer。线程安全（多台设备写同一个任务）。
    落盘：<root>/<task_id>/events.jsonl + <root>/<task_id>/<device>/*.png
    """

    def __init__(self, root: Path, task_id: str, *, console: bool = True, max_events: int = 4000) -> None:
        self.root = Path(root) / task_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self.console = console
        self._lock = threading.Lock()
        self._jsonl = (self.root / "events.jsonl").open("a", encoding="utf-8")
        self.events: list[Event] = []
        self.max_events = max_events
        self._markers: Counter[str] = Counter()

    # ---------- 写 ----------

    def log(self, kind: str, **kw) -> Event:
        ev = Event(ts=time.time(), kind=kind, task=self.task_id, **kw)
        with self._lock:
            self._jsonl.write(json.dumps(asdict(ev), ensure_ascii=False) + "\n")
            self._jsonl.flush()
            self.events.append(ev)
            if len(self.events) > self.max_events:
                del self.events[: len(self.events) - self.max_events]
        if self.console:
            print(("  " if kind == "action" else "") + ev.line(), flush=True)
        return ev

    def note(self, msg: str, **kw) -> Event:
        return self.log("note", detail=msg, **kw)

    def state(self, device: str, name: str, ms: float = 0.0) -> Event:
        return self.log("state", device=device, state=name, ms=ms)

    def action(self, device: str, state: str, action: str, ok: bool, ms: float, detail: str = "") -> Event:
        return self.log("action", device=device, state=state, action=action, ok=ok, ms=ms, detail=detail)

    def result(self, device: str, outcome: str, detail: str = "") -> Event:
        self._markers[outcome] += 1
        return self.log("result", device=device, ok=(outcome == "success"), detail=detail, extra={"outcome": outcome})

    # ---------- 归因 ----------

    @staticmethod
    def classify(text: str) -> str:
        """把页面文本归因成本质原因。抢不到时这一句比十行日志都有用。"""
        if not text:
            return "unknown"
        for tag, pats in BLOCK_PATTERNS.items():
            if any(p in text for p in pats):
                return tag
        return "normal"

    def block(self, device: str, reason: str, text: str = "") -> Event:
        return self.log("block", device=device, ok=False, detail=reason, extra={"sample": text[:200]})

    # ---------- 截图 ----------

    def shot_dir(self, device: str) -> Path:
        d = self.root / device.replace(":", "_").replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_shot(self, device: str, tag: str, data_or_dump: Callable[[Path], bool] | bytes | None) -> str:
        """调用方给个"把图写到 path"的回调，或者给字节。返回相对路径。"""
        path = self.shot_dir(device) / f"{int(time.time() * 1000)}_{tag}.png"
        ok = False
        if callable(data_or_dump):
            try:
                ok = bool(data_or_dump(path))
            except Exception:
                ok = False
        elif isinstance(data_or_dump, (bytes, bytearray)):
            path.write_bytes(data_or_dump)
            ok = True
        return str(path.relative_to(self.root)) if ok else ""

    # ---------- 汇总 ----------

    def summary(self) -> dict:
        acts = [e for e in self.events if e.kind == "action"]
        states = [e for e in self.events if e.kind == "state"]
        blocks = [e for e in self.events if e.kind == "block"]
        by_action: dict[str, list[float]] = {}
        for e in acts:
            by_action.setdefault(e.action or "?", []).append(e.ms)
        return {
            "task": self.task_id,
            "events": len(self.events),
            "actions": len(acts),
            "failed_actions": sum(1 for e in acts if not e.ok),
            "states": dict(Counter(e.state for e in states)),
            "blocks": dict(Counter(e.detail for e in blocks)),
            "outcomes": dict(self._markers),
            "latency_ms": {k: round(sum(v) / len(v), 1) for k, v in by_action.items() if v},
            "started": self.events[0].ts if self.events else 0,
            "ended": self.events[-1].ts if self.events else 0,
        }

    def report(self) -> str:
        s = self.summary()
        dur = (s["ended"] - s["started"]) * 1000 if s["ended"] > s["started"] else 0
        lines = [
            f"任务 {s['task']} 时长 {dur / 1000:.2f}s  事件 {s['events']}  动作 {s['actions']}"
            f"（失败 {s['failed_actions']}）",
            f"  截获结果: {s['outcomes'] or '无'}",
            f"  拦截归因: {s['blocks'] or '无'}",
            f"  状态分布: {s['states']}",
        ]
        if s["latency_ms"]:
            slow = sorted(s["latency_ms"].items(), key=lambda kv: -kv[1])
            lines.append("  动作耗时: " + "  ".join(f"{k}={v:.0f}ms" for k, v in slow))
        return "\n".join(lines)

    def close(self) -> None:
        try:
            self._jsonl.close()
        except Exception:
            pass
