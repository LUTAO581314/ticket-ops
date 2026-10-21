#!/usr/bin/env python3
"""
ticket-ops — 手机端自动化作业平台（抢票是第一个场景）

    python main.py serve                 起调度器 + 看板（日常用这个）
    python main.py dry                   无设备干跑：用 fixture 模拟全流程自测（默认大麦）
    python main.py dry --fixture fixture-maoyan.json    干跑猫眼场景
    python main.py run -t task.json      单任务直接跑（需真机插着）
    python main.py devices               列出 ADB 设备并登记
    python main.py ntp                   只校时，看时钟质量

零第三方依赖。只要 Python 3.10+ 和 platform-tools。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文和符号会炸。统一 UTF-8 + 不因编码错误中断。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.clock import Clock, FakeClock  # noqa: E402
from core.device import (  # noqa: E402
    AdbDevice,
    DeviceError,
    MockDevice,
    MockScript,
    find_adb,
    list_adb_devices,
)
from core.observer import Observer  # noqa: E402
from platform.store import DeviceRow, Store, TaskRow  # noqa: E402
from platform.worker import Worker  # noqa: E402

VAR = ROOT / "var"
CFG = ROOT / "config"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def parse_when(s) -> float:
    """'2026-06-01T12:00:00+08:00' / 1780000000 / '+20s' → unix 秒"""
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if s.startswith("+") and s.endswith("s"):
        return time.time() + float(s[1:-1])
    try:
        return float(s)
    except ValueError:
        pass
    return datetime.fromisoformat(s).timestamp()


def load_task_file(path: Path) -> dict:
    data = json.loads(Path(path).read_text("utf-8"))
    data["sale_at"] = parse_when(data.get("sale_at", "+20s"))
    return data


def banner() -> None:
    print("ticket-ops - 手机端自动化作业平台")
    print(f"  工作目录 {ROOT}")
    adb = find_adb()
    print(f"  adb      {adb or '[!] 未找到（去 config 里配 adb_path）'}")
    print(f"  python   {sys.version.split()[0]}")
    print()


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #

def cmd_serve(args) -> int:
    from platform.scheduler import Scheduler
    from platform.server import serve

    banner()
    store = Store(VAR / "ticket.db")
    clock = Clock()
    print("校时中…")
    rep = clock.sync(rounds=int(args.ntp_rounds))
    print(rep.summary())
    if not clock.quality_ok():
        print("[!] 时钟抖动偏大（服务器之间不一致），开售前建议重跑一次校时\n")

    sched = Scheduler(store=store, clock=clock, adb_path=args.adb,
                      observe_root=VAR / "runs", console=True)

    found = sched.discover()
    print(f"ADB 设备 {len(found)} 台: {[d.serial for d in found] or '无'}")
    online = [d for d in found if d.state == "idle"]
    for d in online:
        print(f"  [ok] {d.serial}  {d.model} Android {d.android}")
    if not online:
        print("  （没有在线设备。看板还能用，可以先 dry 跑一遍验证逻辑）")
    print()

    sched.start()

    # 启动时若有 --task，自动入队一个
    if args.task:
        t = load_task_file(Path(args.task))
        task = sched.submit(TaskRow(
            id="", scene=t.get("scene", "damai"), config=t.get("config", {}),
            sale_at=t["sale_at"], lead_ms=float(t.get("lead_ms", 55)),
            device_filter=t.get("device_filter", []), replicas=int(t.get("replicas", 1)),
            priority=int(t.get("priority", 5)),
        ))
        print(f"已入队 {task.id}  sale_at={datetime.fromtimestamp(task.sale_at)}")

    serve(sched, store, host=args.host, port=args.port)
    return 0


# --------------------------------------------------------------------------- #
# dry —— 干跑自测
# --------------------------------------------------------------------------- #

def cmd_dry(args) -> int:
    """用 MockDevice + fixture 跑完整时序，不需要手机。"""
    banner()
    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = CFG / fixture
    if not fixture.is_file():
        print(f"[!] fixture 不存在: {fixture}")
        return 2

    script = MockScript.from_file(fixture)
    start = script.frames[0].at if script.frames else time.time()
    clock = FakeClock(start=start - 5.0)
    dev = MockDevice(script, serial="mock-01", clock=clock)

    # 场景从 fixture 文件名推断：fixture-maoyan.json → maoyan；否则默认 damai
    scene = args.scene
    if not scene:
        stem = fixture.stem  # fixture-maoyan
        scene = stem.split("-", 1)[1] if stem.startswith("fixture-") and "-" in stem else "damai"

    task_cfg = {
        "item_id": "716435462268", "session": "06-01", "sku": "看台580", "qty": 2,
        "viewers": ["张三"], "entry_xy": [540, 2100], "burst": 12, "burst_gap_ms": 8,
    }
    if scene == "maoyan":
        task_cfg = {
            "演出": "巡回演唱会", "session": "05-18", "price": 580, "qty": 2,
            "viewers": ["张三"], "entry_xy": [540, 2100], "burst": 12, "burst_gap_ms": 8,
        }
    if args.task:
        task_cfg.update(json.loads(Path(args.task).read_text("utf-8")).get("config", {}))

    sale_at = start          # fixture 第一帧就是开售时刻
    # id 必须带毫秒：只到秒的话连着跑两次 dry 会撞 UNIQUE constraint
    tid = f"dry-{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
    task = TaskRow(id=tid, scene=scene, config=task_cfg,
                   sale_at=sale_at, lead_ms=float(args.lead), replicas=1)

    store = Store(VAR / "dry.db")
    store.upsert_device(DeviceRow(serial=dev.serial, kind="mock", state="idle", tags=["mock"]))
    store.create_task(task)
    obs = Observer(VAR / "runs", task.id, console=True)

    print(f"fixture {fixture.name}: {len(script.frames)} 帧  场景 {scene}  "
          f"{[f.package or f'#{i}' for i, f in enumerate(script.frames)]}")
    print(f"任务 {task.id}  开售 {datetime.fromtimestamp(sale_at)}  提前 {task.lead_ms}ms")
    print("-" * 70)

    t0 = time.monotonic()
    w = Worker(task=task, device=dev, clock=clock, store=store, observer=obs,
               stop_event=threading.Event(), dry_run=True)
    out = w.run()
    wall = (time.monotonic() - t0) * 1000

    print("-" * 70)
    print(f"结果: {out.outcome} — {out.detail}")
    print(f"动作 {out.actions} 次  虚拟耗时 {(out.finished_at - out.started_at):.3f}s  真实耗时 {wall:.0f}ms")
    print(f"状态命中: {out.states}")
    print()
    print(dev.report())
    print()
    print(obs.report())
    obs.close()
    return 0 if out.outcome == "success" else 1


# --------------------------------------------------------------------------- #
# run —— 单任务（真机）
# --------------------------------------------------------------------------- #

def cmd_run(args) -> int:
    banner()
    serial = args.serial
    if not serial:
        devs = [s for s, st in list_adb_devices(args.adb) if st == "device"]
        if not devs:
            print("[!] 没有在线设备。插手机、开 USB 调试，或先跑 python main.py dry")
            return 2
        serial = devs[0]
    print(f"设备 {serial}")

    clock = Clock()
    rep = clock.sync(rounds=int(args.ntp_rounds))
    print(rep.summary())

    try:
        dev = AdbDevice(serial, args.adb)
    except DeviceError as e:
        print(f"[!] {e}")
        return 2

    data = load_task_file(Path(args.task)) if args.task else {
        "scene": "damai", "config": {}, "sale_at": time.time() + 20, "lead_ms": 55,
    }
    scene = data.get("scene", "damai")
    tid = f"run-{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
    task = TaskRow(id=tid, scene=scene,
                   config=data.get("config", {}), sale_at=data["sale_at"],
                   lead_ms=float(data.get("lead_ms", 55)), replicas=1)

    store = Store(VAR / "ticket.db")
    store.upsert_device(DeviceRow(serial=serial, kind="adb", state="idle",
                                  tags=["adb"], adb_path=args.adb or ""))
    store.create_task(task)
    obs = Observer(VAR / "runs", task.id, console=True)

    print(f"任务 {task.id}  开售 {datetime.fromtimestamp(task.sale_at)}  "
          f"提前 {task.lead_ms}ms")
    print("-" * 70)
    w = Worker(task=task, device=dev, clock=clock, store=store, observer=obs,
               stop_event=threading.Event())
    out = w.run()
    print("-" * 70)
    print(f"结果: {out.outcome} — {out.detail}")
    print(obs.report())
    obs.close()
    dev.close()
    return 0 if out.outcome == "success" else 1


# --------------------------------------------------------------------------- #
# devices / ntp
# --------------------------------------------------------------------------- #

def cmd_devices(args) -> int:
    banner()
    adb = find_adb(args.adb)
    if not adb:
        print("[!] 找不到 adb。下载 platform-tools 解压后，用 --adb 指定路径：")
        print("   python main.py devices --adb C:/platform-tools/adb.exe")
        return 2
    rows = list_adb_devices(adb)
    if not rows:
        print("没有设备。检查：USB 线 / 开发者选项 / USB 调试授权弹窗。")
        return 1
    store = Store(VAR / "ticket.db")
    for serial, state in rows:
        print(f"  {serial:<24} {state}")
        if state == "device":
            try:
                info = AdbDevice(serial, adb).info()
                print(f"    {info.get('model')}  Android {info.get('android')}  屏幕 {info.get('size')}")
                store.upsert_device(DeviceRow(serial=serial, kind="adb", adb_path=adb,
                                              model=info.get("model", ""),
                                              android=info.get("android", ""),
                                              state="idle", tags=["adb"], info=info))
            except DeviceError as e:
                print(f"    [!] {e}")
    return 0


def cmd_ntp(args) -> int:
    c = Clock()
    print("多源采样中（每源 3 轮）…")
    rep = c.sync(rounds=3)
    print(rep.summary())
    print()
    ok = c.quality_ok()
    print(f"质量: {'[ok] 可用于抢票（抖动 ≤40ms）' if ok else '[!] 抖动偏大，建议换网络/换源重试'}")
    if rep.spread * 1000 > 20:
        print("  提示：抖动大通常是本地网络不稳，或某些源不可达 —— 看上面标 ✗ 的源。")
    return 0 if ok else 1


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(prog="ticket-ops", description="手机端自动化作业平台")
    p.add_argument("--adb", help="adb 可执行文件路径（默认自动找）")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="起调度器 + Web 看板")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("-t", "--task", help="启动时入队的任务配置 JSON")
    s.add_argument("--ntp-rounds", type=int, default=2)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("dry", help="无设备干跑（fixture 自测）")
    s.add_argument("--fixture", default="fixture-damai.json")
    s.add_argument("--scene", help="场景名，默认从 fixture 文件名推断（fixture-maoyan.json → maoyan）")
    s.add_argument("-t", "--task", help="覆盖任务参数")
    s.add_argument("--lead", type=float, default=55)
    s.set_defaults(func=cmd_dry)

    s = sub.add_parser("run", help="单任务真机执行")
    s.add_argument("-t", "--task", required=False)
    s.add_argument("-s", "--serial")
    s.add_argument("--ntp-rounds", type=int, default=2)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("devices", help="列出并登记 ADB 设备")
    s.set_defaults(func=cmd_devices)

    s = sub.add_parser("ntp", help="校时质量检查")
    s.set_defaults(func=cmd_ntp)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
