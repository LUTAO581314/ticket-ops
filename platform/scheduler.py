"""
调度器 — 把"一堆任务 × 一堆设备"排成一条可执行的作业流水线。

三件平台级的事，脚本层面做不到：
1. 一任务多机（replicas）：同一个票档派 3 台设备同时抢，谁先到算谁的
2. FIRST_COMPLETED 抢占：一台成功，立刻叫停其余 —— 避免多买、避免白烧账号
3. 统一时钟：所有设备共用同一个 Clock 实例，时间基准误差 = 0（不是每台各校一次）

设备占用状态由调度器独占管理（内存 + DB 双写）：一台机同时只跑一个任务，
因为真机上页面只有一个，两个任务会互相点掉。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.clock import Clock
from core.device import AdbDevice, BaseDevice, DeviceError, LocalDevice, MockDevice, list_adb_devices
from core.observer import Observer
from platform.store import DeviceRow, Store, TaskRow
from platform.worker import GrabOutcome, Worker

ARM_LEAD_SECONDS = 45.0  # 提前多久把 worker 拉起来（预热 30s + 余量）


@dataclass
class RunGroup:
    """一个任务的一次执行：N 台设备同时跑，共享 stop_event。"""

    task: TaskRow
    stop_event: threading.Event
    threads: list[threading.Thread] = None  # type: ignore[assignment]
    outcomes: list[GrabOutcome] = None  # type: ignore[assignment]
    started_at: float = 0.0

    def __post_init__(self) -> None:
        self.threads = []
        self.outcomes = []


class Scheduler:
    def __init__(
        self,
        *,
        store: Store,
        clock: Clock,
        adb_path: str | None = None,
        observe_root: Path = Path("var/runs"),
        console: bool = True,
    ) -> None:
        self.store = store
        self.clock = clock
        self.adb_path = adb_path
        self.observe_root = Path(observe_root)
        self.console = console

        self._lock = threading.RLock()
        self._busy: dict[str, str] = {}  # serial -> task_id
        self._groups: dict[str, RunGroup] = {}
        self._armed: set[str] = set()
        self._tasks: dict[str, TaskRow] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._device_cache: dict[str, BaseDevice] = {}

    # ------------------------------------------------------------------ #
    # 设备
    # ------------------------------------------------------------------ #

    def discover(self) -> list[DeviceRow]:
        """扫描 ADB 设备并登记。不认识的老设备保留在库里（可能只是没插线）。"""
        found: list[DeviceRow] = []
        for serial, state in list_adb_devices(self.adb_path):
            row = DeviceRow(serial=serial, kind="adb", adb_path=self.adb_path or "",
                            state="idle" if state == "device" else "offline",
                            last_seen=time.time())
            if state == "device":
                try:
                    dev = self._get_device(serial)
                    info = dev.info()
                    row.model = info.get("model", "")
                    row.android = info.get("android", "")
                    row.info = info
                except Exception:
                    row.state = "offline"
            else:
                row.info = {"adb_state": state}
            self.store.upsert_device(row)
            found.append(row)
        return found

    def register_mock(self, device: MockDevice, tags: list[str] | None = None) -> DeviceRow:
        # 默认标签是**设备类型**，不是场景名：同一台手机既能抢大麦也能抢猫眼，
        # 设备标签绑死场景会让"猫眼任务找不到设备"。任务的 device_filter 留空即不限。
        row = DeviceRow(
            serial=device.serial, kind="mock", tags=tags or ["mock"], state="idle",
            info=device.info(), last_seen=time.time(), account="mock",
        )
        self.store.upsert_device(row)
        with self._lock:
            self._device_cache[device.serial] = device
        return row

    def register_local(self, serial: str, endpoint: str, tags: list[str] | None = None) -> DeviceRow:
        row = DeviceRow(serial=serial, kind="local", endpoint=endpoint, tags=tags or ["local"],
                        state="idle", last_seen=time.time())
        self.store.upsert_device(row)
        with self._lock:
            self._device_cache[serial] = LocalDevice(serial, endpoint)
        return row

    def _get_device(self, serial: str) -> BaseDevice:
        with self._lock:
            if serial in self._device_cache:
                return self._device_cache[serial]
            row = next((d for d in self.store.devices() if d.serial == serial), None)
            kind = row.kind if row else "adb"
            if kind == "local" and row:
                dev: BaseDevice = LocalDevice(serial, row.endpoint)
            else:
                dev = AdbDevice(serial, self.adb_path)
            self._device_cache[serial] = dev
            return dev

    def free_devices(self, filt: list[str] | None = None) -> list[DeviceRow]:
        with self._lock:
            busy = set(self._busy)
        return [d for d in self.store.devices() if d.serial not in busy and d.matches(filt or [])]

    # ------------------------------------------------------------------ #
    # 任务
    # ------------------------------------------------------------------ #

    def submit(self, task: TaskRow) -> TaskRow:
        task = self.store.create_task(task)
        with self._lock:
            self._tasks[task.id] = task
        self.store.add_event(task.id, "", "note", detail=f"任务入队 sale_at={task.sale_at:.3f}")
        return task

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            g = self._groups.get(task_id)
        if g:
            g.stop_event.set()
        t = self.store.task(task_id)
        if t and t.status in ("queued", "armed", "running"):
            self.store.update_task(task_id, status="cancelled", finished_at=time.time())
            return True
        return False

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for g in self._groups.values():
                g.stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
            except Exception as e:
                print(f"[scheduler] scan 异常: {e!r}")
            self._stop.wait(0.15)

    def _scan(self) -> None:
        """每 150ms 看一次：有任务该 arm 了吗？"""
        now = self.clock.now()
        for task in self.store.tasks(status="queued"):
            if task.id in self._armed:
                continue
            arm_at = task.sale_at - ARM_LEAD_SECONDS - task.lead_ms / 1000.0
            if now >= arm_at:
                self._armed.add(task.id)
                self._arm(task)

    def _arm(self, task: TaskRow) -> None:
        replicas = max(1, task.replicas)
        devs = self.free_devices(task.device_filter)[:replicas]
        if not devs:
            self.store.update_task(task.id, status="failed", outcome="failed",
                                   detail="没有可用设备（都忙/离线/不匹配 filter）", finished_at=time.time())
            self.store.add_event(task.id, "", "note", ok=False, detail="无可用设备")
            return

        group = RunGroup(task=task, stop_event=threading.Event(), started_at=time.time())
        with self._lock:
            self._groups[task.id] = group
            for d in devs:
                self._busy[d.serial] = task.id
        self.store.update_task(task.id, status="armed", started_at=time.time(),
                               detail=f"派发 {len(devs)} 台: {[d.serial for d in devs]}")
        self.store.add_event(task.id, "", "note", detail=f"arm，派发 {len(devs)} 台")

        observer = Observer(self.observe_root, task.id, console=self.console)

        def success_hook(_tid: str, serial: str) -> None:
            group.stop_event.set()
            self.store.add_event(task.id, serial, "note", detail="★ 抢先成功，叫停其余设备")

        for d in devs:
            aid = self.store.assign(task.id, d.serial)
            th = threading.Thread(
                target=self._run_one,
                args=(task, d, observer, group, aid),
                name=f"w-{d.serial}",
                daemon=True,
            )
            group.threads.append(th)
            th.start()

        threading.Thread(target=self._reap, args=(group, observer, devs), name=f"reap-{task.id}",
                         daemon=True).start()

    def _run_one(self, task: TaskRow, drow: DeviceRow, observer: Observer, group: RunGroup, aid: int) -> None:
        try:
            dev = self._get_device(drow.serial)
            w = Worker(
                task=task, device=dev, clock=self.clock, store=self.store, observer=observer,
                stop_event=group.stop_event,
                shared_success=self._make_success_hook(group),
            )
            outcome = w.run()
        except Exception as e:
            outcome = GrabOutcome(task_id=task.id, device=drow.serial, outcome="error",
                                  detail=f"{type(e).__name__}: {e}")
        with self._lock:
            group.outcomes.append(outcome)
        self.store.end_assign(aid, outcome.outcome, outcome.detail)

    def _make_success_hook(self, group: RunGroup):
        def hook(_tid: str, serial: str) -> None:
            group.stop_event.set()
            self.store.add_event(group.task.id, serial, "note", detail="★ 抢先成功，叫停其余设备")
        return hook

    def _reap(self, group: RunGroup, observer: Observer, devs: list[DeviceRow]) -> None:
        """等所有 worker 结束，汇总结果。"""
        for th in group.threads:
            th.join(timeout=300)
        with self._lock:
            for d in devs:
                self._busy.pop(d.serial, None)
        wins = [o for o in group.outcomes if o.outcome == "success"]
        status = "success" if wins else "failed"
        detail = (
            f"{wins[0].device} 抢到，动 {wins[0].actions} 次，反应 {wins[0].reaction_ms:.0f}ms"
            if wins
            else "; ".join(f"{o.device}:{o.outcome}" for o in group.outcomes) or "无结果"
        )
        self.store.update_task(group.task.id, status=status, outcome=status, detail=detail,
                               finished_at=time.time())
        observer.note(f"=== 任务收官：{status} — {detail} ===")
        observer.note(observer.report())
        if self.console:
            print(f"\n[{group.task.id}] {status}: {detail}")
            print(observer.report())
        observer.close()
        with self._lock:
            self._groups.pop(group.task.id, None)

    # ------------------------------------------------------------------ #

    def wait(self, task_id: str, timeout: float = 400) -> TaskRow | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.store.task(task_id)
            if t and t.status in ("success", "failed", "cancelled"):
                return t
            time.sleep(0.2)
        return self.store.task(task_id)

    def status(self) -> dict:
        with self._lock:
            busy = dict(self._busy)
            groups = {
                tid: {
                    "task": g.task.id,
                    "devices": [o.device for o in g.outcomes] or ["(运行中)"],
                    "stopped": g.stop_event.is_set(),
                }
                for tid, g in self._groups.items()
            }
        return {"busy": busy, "groups": groups, "stats": self.store.stats()}
