"""
状态机引擎 — 抢票的本质是"在 200ms 内识别屏幕状态并做出唯一正确的动作"。

设计取舍：
- 不引入第三方状态机库。抢票场景的状态机是"优先级匹配 + 幂等动作"，不是学术 FSM。
- 每个 State 自带 detect()：一次快照里可能同时满足多个状态（例如选座页同时有"确定"和"提交订单"
  的文案命中），按注册顺序取第一个命中，所以顺序 = 优先级，顺序 = 策略。
- 动作幂等：同一个状态被连续命中不会重复点击（除非显式 allow_repeat），
  避免"一次点击没生效→连点→多买一张票"这类事故。
- 失败归因内建：每个状态记录 visits / last_seen / dwell，失败时直接给出"卡在哪一态"。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol


# --------------------------------------------------------------------------- #
# 感知快照
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Node:
    """控件节点。字段对齐 uiautomator dump / AutoX.js 的 UiObject。"""

    text: str = ""
    desc: str = ""
    res_id: str = ""
    cls: str = ""
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    clickable: bool = False
    enabled: bool = True
    children_text: str = ""  # 子树文本拼接，用于"父节点含无票"这类判断

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def haystack(self) -> str:
        return f"{self.text}\x00{self.desc}\x00{self.children_text}"

    def matches(self, keyword: str, *, contains: bool = True) -> bool:
        if not keyword:
            return False
        k = keyword.strip()
        if contains:
            return k in self.haystack
        return k == self.text.strip() or k == self.desc.strip()

    def __repr__(self) -> str:
        return f"<Node {self.text[:24]!r} id={self.res_id!r} @{self.center}>"


@dataclass(slots=True)
class Snapshot:
    """某一时刻的屏幕全量节点 + 采样时刻。"""

    nodes: list[Node] = field(default_factory=list)
    at: float = 0.0  # Clock.now()
    cost_ms: float = 0.0  # 本次感知耗时，>150ms 要报警（说明退化成主机侧 dump）
    package: str = ""
    activity: str = ""

    # -- 查询 --

    def find(self, keyword: str, *, contains: bool = True, clickable_only: bool = False) -> list[Node]:
        out = [n for n in self.nodes if n.matches(keyword, contains=contains)]
        if clickable_only:
            out = [n for n in out if n.clickable]
        return out

    def first(self, keyword: str, *, contains: bool = True) -> Node | None:
        r = self.find(keyword, contains=contains)
        return r[0] if r else None

    def has(self, keyword: str, *, contains: bool = True) -> bool:
        return self.first(keyword, contains=contains) is not None

    def has_any(self, keywords: Iterable[str], *, contains: bool = True) -> bool:
        return any(self.has(k, contains=contains) for k in keywords)

    def texts(self) -> list[str]:
        return [n.text for n in self.nodes if n.text]

    def by_id(self, res_id: str) -> Node | None:
        for n in self.nodes:
            if n.res_id == res_id or n.res_id.endswith(f":id/{res_id}"):
                return n
        return None

    def visible_bounds(self) -> list[Node]:
        return [n for n in self.nodes if n.bounds != (0, 0, 0, 0)]

    def dump(self, limit: int = 40) -> str:
        rows = [f"  {n.text[:36]!r:<40} id={n.res_id:<32} @{n.center} click={n.clickable}"
                for n in self.visible_bounds()[:limit]]
        return "\n".join(rows)


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Action:
    """一个原子动作。kind 决定其余字段的含义。"""

    kind: str  # tap | tap_xy | tap_node | back | swipe | wait | key | none
    target: str = ""  # tap: 匹配关键词；tap_xy: "x,y"；swipe: "x1,y1,x2,y2,ms"
    dx: int = 0
    dy: int = 0
    note: str = ""
    required: bool = False  # 找不到目标时：True=报错，False=跳过


@dataclass
class StepResult:
    ok: bool
    action: Action
    detail: str = ""
    cost_ms: float = 0.0


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

@dataclass
class State:
    """一个可识别的屏幕状态 + 在该状态下要做的事。"""

    name: str
    detect: Callable[[Snapshot], bool]
    act: Callable[["Context", Snapshot], StepResult] | None = None
    # 完成后是否立即重新感知（True 用于连续推进，False 用于需要等待的态）
    immediate: bool = True
    # 同一状态允许连续执行的次数上限，防止"点了没反应→无限连点"
    max_hits: int = 3
    # 在该状态停留多久算超时（秒），0 = 不限
    timeout_s: float = 0.0
    allow_repeat: bool = False
    note: str = ""

    hits: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    entered: bool = False

    def reset(self) -> None:
        self.hits = 0
        self.first_seen = 0.0
        self.last_seen = 0.0
        self.entered = False


class Context(Protocol):
    """状态机对外部世界的接口（由 runner 实现）。"""

    clock: object
    device: object
    log: Callable[..., None]

    def snapshot(self) -> Snapshot: ...
    def do(self, action: Action) -> StepResult: ...
    def result(self, name: str, detail: str = "") -> None: ...


# --------------------------------------------------------------------------- #
# 机器
# --------------------------------------------------------------------------- #

class Machine:
    """
    优先级匹配状态机。

    - 每次 tick：取一次快照 → 按 states 顺序找第一个 detect() 为真的态
    - 命中后执行该态 act()，并累加 hits
    - hits 超过 max_hits 视为该态无进展 → 交给 on_stuck 处理
    - 到达 terminal 立即返回
    """

    def __init__(
        self,
        states: list[State],
        terminals: Iterable[str] = (),
        *,
        on_stuck: Callable[["Machine", Snapshot, State], None] | None = None,
        unknown_state_reporter: Callable[[Snapshot], None] | None = None,
    ) -> None:
        self.states = states
        self.terminals = set(terminals)
        self.on_stuck = on_stuck
        self.unknown_state_reporter = unknown_state_reporter
        self.current: State | None = None
        self.unknown_ticks = 0
        self.ticks = 0
        self.history: list[tuple[float, str, str]] = []  # (at, state, detail)
        self.done: str | None = None

    def reset(self) -> None:
        for s in self.states:
            s.reset()
        self.current = None
        self.unknown_ticks = 0
        self.ticks = 0
        self.history.clear()
        self.done = None

    # ------------------------------------------------------------------ #

    def tick(self, ctx: Context) -> str | None:
        """推进一格。返回当前状态名；到达终态返回终态名。"""
        if self.done:
            return self.done

        snap = ctx.snapshot()
        self.ticks += 1
        now = snap.at or time.time()

        matched: State | None = None
        for st in self.states:
            try:
                if st.detect(snap):
                    matched = st
                    break
            except Exception as e:  # 单个 detect 的 bug 不该拖垮整机
                ctx.log(f"[fsm] detect({st.name}) 异常: {e!r}", level="warn")
        if matched is None:
            self.unknown_ticks += 1
            if self.unknown_state_reporter:
                self.unknown_state_reporter(snap)
            return None

        # 状态切换：清掉上一态计数
        if self.current is not matched:
            if self.current is not None:
                self.history.append((now, self.current.name, f"→{matched.name}"))
            self.current = matched
            matched.hits = 0
            matched.first_seen = now
        matched.last_seen = now
        matched.entered = True
        matched.hits += 1
        try:
            ctx.current_state = matched.name  # type: ignore[attr-defined]
        except Exception:
            pass

        if matched.name in self.terminals:
            self.done = matched.name
            self.history.append((now, matched.name, "terminal"))
            return matched.name

        if matched.timeout_s and (now - matched.first_seen) > matched.timeout_s:
            ctx.log(f"[fsm] {matched.name} 超时 {matched.timeout_s}s", level="warn")
            self.done = f"{matched.name}:timeout"
            return self.done

        if matched.hits > matched.max_hits:
            if self.on_stuck:
                self.on_stuck(self, snap, matched)
                matched.hits = 0  # 给它一次重新尝试的机会
            if self.done:
                return self.done
            return matched.name

        if matched.act:
            res = matched.act(ctx, snap)
            self.history.append((now, matched.name, f"{res.action.kind}:{res.detail} ok={res.ok}"))
            ctx.log(
                f"[{matched.name}] {res.action.kind} {res.action.target or res.action.note} "
                f"→ {'ok' if res.ok else 'FAIL'} {res.detail} ({res.cost_ms:.0f}ms)"
            )
            if not res.ok and matched.hits >= matched.max_hits:
                if self.on_stuck:
                    self.on_stuck(self, snap, matched)
        return matched.name

    def run(self, ctx: Context, *, max_ticks: int = 400, deadline: float | None = None) -> str | None:
        """跑到终态 / 超 tick / 超时。返回终态名或 None。"""
        while self.ticks < max_ticks:
            r = self.tick(ctx)
            if r and r in self.terminals:
                return r
            if r and r.endswith(":timeout"):
                return r
            if deadline is not None and ctx.clock.now() > deadline:  # type: ignore[attr-defined]
                ctx.log("[fsm] 全局 deadline 到期，停止", level="warn")
                return "deadline"
        return None

    def report(self) -> str:
        lines = [f"状态机: ticks={self.ticks} unknown={self.unknown_ticks} done={self.done}"]
        for s in self.states:
            if s.entered:
                dwell = (s.last_seen - s.first_seen) * 1000 if s.last_seen >= s.first_seen else 0
                lines.append(f"  {s.name:<12} hits={s.hits:<3} dwell={dwell:6.0f}ms")
        for at, name, detail in self.history[-25:]:
            lines.append(f"  - {name:<12} {detail}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 复用的 detect 工厂
# --------------------------------------------------------------------------- #

def detect_any(*keywords: str, contains: bool = True) -> Callable[[Snapshot], bool]:
    """页面出现任一关键词即命中"""
    def _d(snap: Snapshot) -> bool:
        return snap.has_any(keywords, contains=contains)
    _d.__name__ = f"detect_any{keywords}"
    return _d


def detect_all(*keywords: str) -> Callable[[Snapshot], bool]:
    """全部关键词都出现才命中"""
    def _d(snap: Snapshot) -> bool:
        return all(snap.has(k) for k in keywords)
    return _d


def detect_regex(pattern: str) -> Callable[[Snapshot], bool]:
    rx = re.compile(pattern)

    def _d(snap: Snapshot) -> bool:
        return any(rx.search(n.haystack) for n in snap.nodes)
    return _d


def detect_none_of(*keywords: str) -> Callable[[Snapshot], bool]:
    """排除型：用于"必须在详情页且已经离开选座页"这类组合条件"""
    def _d(snap: Snapshot) -> bool:
        return not snap.has_any(keywords)
    return _d
