"""
单机执行体 — 一台设备抢一次票的完整时序。

时序骨架（这正是"抢票"和"点点点脚本"的全部差别）：

    T-30s   预热：加载页面、预检、切到快通道节律
    T-3s    最后一次校验（还在详情页吗？有弹窗吗？）
    T-55ms  发射：盲点连点购买入口
    +200ms  进入选座页 → 交状态机
    ...     状态机推进到 PAY
    T+180s  硬截止（过了这个点还没成，基本就是没了）

任何一台设备抢到 → 立刻 set stop_event，其余设备停手，不去抢第二张。
"""
from __future__ import annotations

import importlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.clock import Clock
from core.device import BaseDevice
from core.fsm import Action, Machine, Snapshot, StepResult
from core.humanize import Humanizer, Profile
from core.observer import Observer
from platform.store import Store

# 插件通过字符串注册，避免 worker 依赖具体场景
SCENES: dict[str, Any] = {}


def register_scene(name: str, module: Any) -> None:
    SCENES[name] = module


def load_scene(name: str) -> Any:
    """
    按场景名取插件模块（懒加载：main 启动时不该把全部插件拉起来）。

    插件契约 —— 场景想接进来必须提供这五样，平台层不认识任何具体平台：
        config_class                        带 from_dict(dict) 的配置类
        describe(cfg) -> str                一行人类可读的任务摘要，进日志
        preflight_check(dev, cfg, obs)      -> (bool, str) 开售前体检
        blind_entry_burst(ctx, cfg)         -> StepResult  开售瞬间的盲点快通道
        build_machine(cfg)                  -> Machine     状态机

    约定：所有场景的"抢到"终态都叫 PAY —— 判定逻辑在平台层只写一次。
    """
    if name in SCENES:
        return SCENES[name]
    try:
        mod = importlib.import_module(f"plugins.{name}")
    except ModuleNotFoundError as e:
        raise RuntimeError(f"未知场景 {name}：没有 plugins/{name}.py") from e
    register_scene(name, mod)
    return mod


def known_scenes() -> list[str]:
    return sorted(SCENES)


# --------------------------------------------------------------------------- #
# 执行上下文
# --------------------------------------------------------------------------- #

class WorkerContext:
    """状态机看到的世界：一次感知 + 一次动作。"""

    def __init__(
        self,
        *,
        clock: Clock,
        device: BaseDevice,
        humanizer: Humanizer,
        observer: Observer,
        store: Store | None,
        task_id: str,
        stop_event: threading.Event,
    ) -> None:
        self.clock = clock
        self.device = device
        self.humanizer = humanizer
        self.observer = observer
        self.store = store
        self.task_id = task_id
        self.stop_event = stop_event
        self.last_snap: Snapshot | None = None
        self.current_state: str = ""
        self.results: list[tuple[str, str]] = []
        self.action_count = 0
        self.shot_every_state = True
        self._shot_taken: set[str] = set()

    # ---- Context 协议 ----

    def log(self, msg: str, level: str = "info") -> None:
        self.observer.note(msg, device=self.device.serial)

    def snapshot(self) -> Snapshot:
        snap = self.device.dump()
        self.last_snap = snap
        return snap

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def result(self, name: str, detail: str = "") -> None:
        self.results.append((name, detail))
        self.observer.result(self.device.serial, name, detail)
        if self.store:
            self.store.add_result(self.task_id, self.device.serial, name, reason=detail)

    # ---- 动作执行 ----

    def do(self, action: Action) -> StepResult:
        t0 = time.monotonic()
        dev = self.device
        detail = ""
        ok = True
        try:
            if action.kind == "tap_node":
                node = self._locate(action.target)
                if node is None:
                    ok, detail = False, f"找不到节点[{action.target}]"
                else:
                    x, y = self.humanizer.offset(*node.center, bounds=node.bounds)
                    dev.tap(x, y)
                    detail = f"@{x},{y}"
                    self.sleep(self.humanizer.pause())
            elif action.kind == "tap_xy":
                x, y = (int(v) for v in action.target.split(","))
                x, y = self.humanizer.offset(x, y)
                dev.tap(x, y)
                detail = f"@{x},{y}"
            elif action.kind == "back":
                dev.back()
                detail = "back"
            elif action.kind == "key":
                dev.key(action.target)
                detail = action.target
            elif action.kind == "swipe":
                parts = [int(v) for v in action.target.split(",")]
                dev.swipe(*parts[:5])  # type: ignore[arg-type]
                detail = action.target
            elif action.kind == "wait":
                self.sleep(0.35)
                detail = "wait"
            elif action.kind == "none":
                detail = action.note or "noop"
            else:
                ok, detail = False, f"未知动作 {action.kind}"
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"

        cost = (time.monotonic() - t0) * 1000
        self.action_count += 1
        st = self.current_state
        self.observer.action(dev.serial, st, f"{action.kind}:{action.target}", ok, cost, detail)
        if self.store:
            self.store.add_event(self.task_id, dev.serial, "action", state=st,
                                 action=f"{action.kind}:{action.target}",
                                 ok=ok, ms=cost, detail=detail)
        return StepResult(ok=ok, action=action, detail=detail, cost_ms=cost)

    def _locate(self, keyword: str):
        """在当前快照里找可点节点。找不到返回 None。"""
        snap = self.last_snap
        if snap is None:
            snap = self.snapshot()
        cands = snap.find(keyword, clickable_only=True) or snap.find(keyword)
        for n in cands:
            if n.bounds != (0, 0, 0, 0):
                return n
        return None

    # ---- 截图 ----

    def shot(self, tag: str, once: bool = False) -> str:
        if once and tag in self._shot_taken:
            return ""
        self._shot_taken.add(tag)
        dev = self.device
        return self.observer.save_shot(dev.serial, tag, lambda p: dev.screenshot(p))


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #

@dataclass
class GrabOutcome:
    task_id: str
    device: str
    outcome: str  # success | sold_out | blocked | captcha | preempted | error | timeout
    detail: str = ""
    started_at: float = 0.0
    fired_at: float = 0.0
    finished_at: float = 0.0
    actions: int = 0
    states: dict = field(default_factory=dict)

    @property
    def reaction_ms(self) -> float:
        return (self.fired_at - self.started_at) * 1000 if self.fired_at else 0.0


class Worker:
    """一台设备 × 一个任务。"""

    def __init__(
        self,
        *,
        task: Any,
        device: BaseDevice,
        clock: Clock,
        store: Store | None,
        observer: Observer,
        stop_event: threading.Event,
        shared_success: Callable[[str, str], None] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.task = task
        self.device = device
        self.clock = clock
        self.store = store
        self.observer = observer
        self.stop_event = stop_event
        self.shared_success = shared_success
        self.dry_run = dry_run
        self.t0 = time.monotonic()
        self.log_warn = lambda m: observer.note(m, device=device.serial)

    # ------------------------------------------------------------------ #

    def run(self) -> GrabOutcome:
        serial = self.device.serial
        started = self.clock.now()
        out = GrabOutcome(task_id=self.task.id, device=serial, outcome="error", started_at=started)
        self.observer.note(f"=== 开始：{self.task.scene} ===", device=serial)
        if self.store:
            self.store.set_device_state(serial, "busy")

        try:
            out = self._run_scene(out)
        except Exception as e:
            out.outcome = "error"
            out.detail = f"{type(e).__name__}: {e}"
            self.observer.note(f"异常：{out.detail}", device=serial)
        finally:
            out.finished_at = self.clock.now()
            if self.store:
                self.store.set_device_state(serial, "idle")
            self.observer.note(f"=== 结束：{out.outcome} {out.detail} ===", device=serial)
        return out

    # ------------------------------------------------------------------ #

    def _run_scene(self, out: GrabOutcome) -> GrabOutcome:
        plugin = load_scene(self.task.scene)
        cfg = plugin.config_class.from_dict(self.task.config)
        serial = self.device.serial

        ctx = WorkerContext(
            clock=self.clock,
            device=self.device,
            humanizer=Humanizer(Profile.warmup()),
            observer=self.observer,
            store=self.store,
            task_id=self.task.id,
            stop_event=self.stop_event,
        )

        self.observer.note(plugin.describe(cfg), device=serial)

        # ---- 阶段 1：预热（开售前 30s 起）----
        warm_at = self.task.sale_at - 30.0
        if self.clock.now() < warm_at:
            self.clock.wait_until(warm_at, lead_ms=0, coarse_ms=500)
        if self.stop_event.is_set():
            out.outcome, out.detail = "preempted", "已被其它设备抢占"
            return out

        ok, why = plugin.preflight_check(self.device, cfg, self.observer)
        self.observer.note(f"预检: {'通过' if ok else '未通过'} — {why}", device=serial)
        if not ok:
            self.observer.block(serial, "preflight_failed", why)
            # 预检不过不直接放弃：可能是弹窗，先按返回键清一下再试一次
            try:
                self.device.back()
                self.clock.wait_until(max(self.clock.now() + 0.8, 0), lead_ms=0, coarse_ms=100)
                ok, why = plugin.preflight_check(self.device, cfg, self.observer)
            except Exception:
                pass
            if not ok:
                out.outcome, out.detail = "blocked", f"预检失败: {why}"
                return out

        # ---- 阶段 2：发射前最后 3 秒蹲守 ----
        ctx.humanizer.use(Profile.window())
        fire_target = self.task.sale_at
        remain = fire_target - self.task.lead_ms / 1000.0 - self.clock.now()
        if remain > 0.3:
            self.observer.note(f"距离发射 {remain:.2f}s，进入蹲守", device=serial)
            self.clock.wait_until(fire_target, lead_ms=self.task.lead_ms, coarse_ms=200)

        if self.stop_event.is_set():
            out.outcome, out.detail = "preempted", "已被其它设备抢占"
            return out

        # ---- 阶段 3：盲点快通道 ----
        self.observer.note("发射：盲点连点购买入口", device=serial)
        burst = plugin.blind_entry_burst(ctx, cfg)
        out.fired_at = self.clock.now()
        self.observer.note(f"快通道结果: {burst.detail}", device=serial)

        # ---- 阶段 4：状态机推进 ----
        machine: Machine = plugin.build_machine(cfg)
        deadline = self.clock.now() + 180.0
        ctx.shot_every_state = True
        ticks = 0
        last_state = ""

        while not self.stop_event.is_set():
            ticks += 1
            if ticks > 400:  # 兜底：干跑时 FakeClock 不走时钟，deadline 永不触发
                self.observer.note("状态机迭代 400 次仍未收敛，放弃", device=serial)
                break
            snap_state = machine.tick(ctx)
            cur = machine.current.name if machine.current else ""
            if cur and cur != last_state:
                self.observer.state(serial, cur)
                last_state = cur
            if snap_state and snap_state in machine.terminals:
                break
            if snap_state and snap_state.endswith(":timeout"):
                break
            if self.clock.now() > deadline:
                self.observer.note("硬截止到期（180s）", device=serial)
                break
            # 状态切换时留张截图，事后能看"卡在哪一屏"
            if machine.current and ctx.shot_every_state:
                ctx.shot(machine.current.name, once=True)
            if machine.unknown_ticks > 25:
                self.observer.note("连续 25 帧未识别状态，放弃（页面可能被换成了活动页/登录页）", device=serial)
                break
            time.sleep(0.05)  # 无进展时的让渡，避免空转烧 CPU

        out.actions = ctx.action_count
        out.states = {s.name: s.hits for s in machine.states if s.entered}

        # ---- 阶段 5：判定 ----
        # terminal 态在 Machine.tick 里是"认出即返回、不执行 act"，
        # 所以落库必须在这里显式做一次，不能指望状态自己的 act 回调。
        done = machine.done or ""
        if done == "PAY":
            out.outcome, out.detail = "success", "订单已提交"
            ctx.result("success", "订单已提交")
            if self.shared_success:
                self.shared_success(self.task.id, serial)
        elif done.startswith("CAPTCHA"):
            out.outcome, out.detail = "captcha", "需要人工过验证"
            ctx.result("captcha", "滑块/验证码拦截")
        elif done.startswith("RISK"):
            out.outcome, out.detail = "blocked", "设备/账号风控"
            ctx.result("blocked", "设备或账号被风控")
        elif done.startswith("ANSWER"):
            out.outcome, out.detail = "blocked", "答题门"
            ctx.result("answer", "开售答题拦截")
        elif self.stop_event.is_set():
            out.outcome, out.detail = "preempted", "已被其它设备抢占"
        elif done == "":
            txt = " ".join((ctx.last_snap.texts() if ctx.last_snap else [])[:60])
            reason = self.observer.classify(txt)
            out.outcome = "sold_out" if reason in ("sold_out", "normal", "unknown") else reason
            out.detail = f"未完成，页面归因={reason}；停在 {machine.current.name if machine.current else '未知'}"
        else:
            out.outcome, out.detail = "timeout", f"终止于 {done}"

        self.observer.note(machine.report(), device=serial)
        return out
