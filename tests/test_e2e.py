"""
端到端自测：不开真机、不起 Web，直接把整条链路跑一遍。

    python -m unittest discover -s tests -v

覆盖：
  1. Worker + MockDevice 全流程（时序 / 状态机 / 分阶段选座 / 落库）
  2. Scheduler + Store 全链路（派发 / 执行 / FIRST_COMPLETED / 结果入库）
  3. 状态机优先级（订单页不能被误判成选座页）
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.clock import Clock, FakeClock                      # noqa: E402
from core.device import MockDevice, MockScript               # noqa: E402
from core.observer import Observer                           # noqa: E402
from platform.scheduler import Scheduler                     # noqa: E402
from platform.store import DeviceRow, Store, TaskRow         # noqa: E402
from platform.worker import Worker                           # noqa: E402
from plugins import damai                                    # noqa: E402

FIXTURE = ROOT / "config" / "fixture-damai.json"
CFG = {"item_id": "716435462268", "session": "06-01", "sku": "看台580", "qty": 2,
       "viewers": ["张三"], "entry_xy": [540, 2100], "burst": 12, "burst_gap_ms": 0}


def make_mock(tmpdir: Path, serial="mock-01"):
    script = MockScript.from_file(FIXTURE)
    start = script.frames[0].at
    clock = FakeClock(start=start - 5.0)
    return MockDevice(script, serial=serial, clock=clock), clock, start


class TestWorkerDry(unittest.TestCase):
    def test_full_flow_success(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            dev, clock, start = make_mock(tmp)
            store = Store(tmp / "t.db")
            store.upsert_device(DeviceRow(serial=dev.serial, kind="mock", tags=["damai"]))
            task = TaskRow(id="t1", scene="damai", config=CFG, sale_at=start, lead_ms=55)
            store.create_task(task)
            obs = Observer(tmp / "runs", "t1"); self.addCleanup(obs.close)

            out = Worker(task=task, device=dev, clock=clock, store=store,
                         observer=obs, stop_event=threading.Event()).run()

            self.assertEqual(out.outcome, "success", out.detail)
            # 全流程必须走到订单成功页，且中间经过了选座面板与提交
            self.assertEqual(set(out.states), {"PICKER", "SUBMIT", "PAY"}, out.states)
            # 结果落库（看板 & 报告靠这个）
            res = store.results("t1")
            self.assertTrue(any(r["outcome"] == "success" for r in res), res)
            # 盲点连点必须"跳转就停手"，不能把 burst 打完
            self.assertLessEqual(len(dev.taps), 12)
            store.close()

    def test_viewer_actually_clicked(self):
        """分阶段选座的核心断言：观演人那一屏真的被点了。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            dev, clock, start = make_mock(tmp)
            task = TaskRow(id="t2", scene="damai", config=CFG, sale_at=start, lead_ms=55)
            obs = Observer(tmp / "runs", "t2"); self.addCleanup(obs.close)
            Worker(task=task, device=dev, clock=clock, store=None,
                   observer=obs, stop_event=threading.Event()).run()

            clicked = [t[3] for t in dev.taps]
            self.assertIn("张三", clicked, clicked)
            self.assertIn("看台580", clicked, clicked)
            self.assertIn("提交订单", clicked, clicked)
            # 数量 + 与观演人勾选都不换页，但必须被点到
            self.assertIn("+", clicked, clicked)


class TestMachinePriority(unittest.TestCase):
    def test_order_page_not_misread_as_picker(self):
        """订单页上也有票档文本（"看台580 × 2"），不能被 PICKER 抢走。"""
        cfg = damai.DamaiConfig.from_dict(CFG)
        m = damai.build_machine(cfg)
        snap = MockScript.from_file(FIXTURE).frames[4]  # SUBMIT 帧
        matched = None
        for st in m.states:
            if st.detect(snap):
                matched = st.name
                break
        self.assertEqual(matched, "SUBMIT")

    def test_pay_is_terminal(self):
        cfg = damai.DamaiConfig.from_dict(CFG)
        m = damai.build_machine(cfg)
        snap = MockScript.from_file(FIXTURE).frames[5]  # PAY 帧
        matched = [st.name for st in m.states if st.detect(snap)]
        self.assertEqual(matched[0], "PAY")
        self.assertIn("PAY", m.terminals)


class TestSchedulerChain(unittest.TestCase):
    def test_dispatch_and_collect(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            dev, _clock, _start = make_mock(tmp)
            store = Store(tmp / "t.db")
            clock = Clock()          # 真时钟：调度线程按真实时间推进
            sched = Scheduler(store=store, clock=clock, adb_path=None,
                              observe_root=tmp / "runs", console=False)
            sched.register_mock(dev, tags=["damai"])

            task = sched.submit(TaskRow(
                id="", scene="damai", config=CFG,
                sale_at=clock.now() + 0.6, lead_ms=55,
                device_filter=["damai"], replicas=1,
            ))
            sched.start()
            try:
                done = sched.wait(task.id, timeout=60)
            finally:
                sched.stop()
                store.close()

            self.assertIsNotNone(done, "任务没在超时内结算")
            self.assertEqual(done.status, "success", done)
            self.assertEqual(done.outcome, "success", done.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
