"""
猫眼插件的自测。

    python -m unittest discover -s tests -v

比大麦那套多测三件事，因为猫眼的坑和它不重合：
  1. "缺货"标记在票档所在行的**兄弟节点**里，不在票档自己身上 —— 只查自己会漏
  2. 勾选状态编码在 Image 节点的 text 里（base64 图片哈希），不是 checked 属性
  3. 订单页上的"立即支付"按钮不能算抢到

这里同时验证插件契约：平台层拿 plugins.maoyan 就能跑起来，
不需要 worker 里写任何 if scene == "maoyan"。
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.clock import FakeClock                              # noqa: E402
from core.device import MockDevice, MockScript                 # noqa: E402
from core.fsm import Node, Snapshot                            # noqa: E402
from core.observer import Observer                             # noqa: E402
from platform.store import DeviceRow, Store, TaskRow           # noqa: E402
from platform.worker import Worker, load_scene                 # noqa: E402
from plugins import maoyan                                     # noqa: E402

FIXTURE = ROOT / "config" / "fixture-maoyan.json"
CFG = {"演出": "巡回演唱会", "session": "05-18", "price": 580, "qty": 2,
       "viewers": ["张三"], "entry_xy": [540, 2100], "burst": 12, "burst_gap_ms": 0}

# 与 fixture 对齐的帧下标
F_DETAIL, F_PICKER, F_ORDER_A, F_ORDER_B, F_PAY = 0, 1, 2, 3, 4


def frames() -> list[Snapshot]:
    return MockScript.from_file(FIXTURE).frames


def make_mock(serial="mock-01"):
    script = MockScript.from_file(FIXTURE)
    start = script.frames[0].at
    clock = FakeClock(start=start - 5.0)
    return MockDevice(script, serial=serial, clock=clock), clock, start


class TestPluginContract(unittest.TestCase):
    """场景是插件：平台层只按名字加载，且契约五件套齐备。"""

    def test_load_by_name(self):
        m = load_scene("maoyan")
        self.assertIs(m, maoyan)
        # 契约五件套
        self.assertTrue(hasattr(m, "config_class"))
        for fn in ("describe", "preflight_check", "blind_entry_burst", "build_machine"):
            self.assertTrue(callable(getattr(m, fn)), fn)

    def test_config_from_dict(self):
        cfg = maoyan.config_class.from_dict(CFG)
        self.assertEqual(cfg.price, 580)
        self.assertEqual(cfg.qty, 2)
        self.assertEqual(cfg.viewers, ["张三"])
        self.assertEqual(cfg.entry_xy, (540, 2100))
        # 勾选框哈希有默认值，且能覆盖
        self.assertEqual(cfg.img_unchecked, maoyan.IMG_UNCHECKED)
        self.assertEqual(maoyan.config_class.from_dict({"img_checked": "x"}).img_checked, "x")

    def test_unknown_scene_raises(self):
        with self.assertRaises(RuntimeError):
            load_scene("no_such_platform")


class TestTierSoldOut(unittest.TestCase):
    """缺货标记在行的子树里，不在票档节点自己身上。"""

    def test_sold_out_detected_from_sibling(self):
        snap = frames()[F_PICKER]
        all_tiers = {t.price: t for t in maoyan.tiers(snap)}
        self.assertEqual(set(all_tiers), {580, 1280}, list(all_tiers))
        # ¥1280 自己的文本里没有"缺货"，缺货是同一行的兄弟节点
        self.assertNotIn("缺货", all_tiers[1280].node.text or "")
        self.assertTrue(all_tiers[1280].sold_out, "没从行容器子树里认出缺货")
        self.assertFalse(all_tiers[580].sold_out)

    def test_pick_only_available(self):
        snap = frames()[F_PICKER]
        cfg = maoyan.config_class.from_dict({"price": 1280, "allow_price_fallback": False})
        # 目标价 1280 缺货且不允许回退 → 一个都选不出
        self.assertIsNone(maoyan.pick_tier(snap, cfg))
        # 允许回退 → 退到有票的 580
        cfg2 = maoyan.config_class.from_dict({"price": 1280, "allow_price_fallback": True})
        t = maoyan.pick_tier(snap, cfg2)
        self.assertIsNotNone(t)
        self.assertEqual(t.price, 580)

    def test_detail_page_price_not_a_tier_page(self):
        """详情页有"¥580 起"，但它不是选座面板。"""
        snap = frames()[F_DETAIL]
        self.assertFalse(maoyan._is_picker(snap))
        self.assertTrue(maoyan.tiers(snap), "详情页的参考价应当被 tiers 扫到（用来验证误判防护）")


class TestViewerCheckbox(unittest.TestCase):
    """勾选态 = Image 节点 text 里的 base64 哈希。"""

    def test_unchecked_and_checked(self):
        cfg = maoyan.config_class.from_dict(CFG)
        _, _, st_a = maoyan._check_state(frames()[F_ORDER_A], "张三", cfg)
        self.assertEqual(st_a, "unchecked")
        _, _, st_b = maoyan._check_state(frames()[F_ORDER_B], "张三", cfg)
        self.assertEqual(st_b, "checked")

    def test_unknown_hash_degrades_to_unknown(self):
        cfg = maoyan.config_class.from_dict({"viewers": ["张三"]})
        cfg.img_unchecked = "changed-by-a-new-app-version"
        vn, box, st = maoyan._check_state(frames()[F_ORDER_A], "张三", cfg)
        self.assertIsNotNone(vn)
        self.assertEqual(st, "unknown")
        self.assertIsNotNone(box, "认不出哈希时也要拿到那个 Image，好在它上面点")

    def test_exact_name_match(self):
        """张三丰不能被当成张三勾上。"""
        snap = Snapshot(nodes=[
            Node(text="张三丰", bounds=(200, 100, 500, 160), clickable=True),
            Node(text="张三", bounds=(200, 300, 500, 360), clickable=True),
        ])
        n = maoyan._viewer_node(snap, "张三")
        self.assertEqual(n.text, "张三")


class TestStatePriority(unittest.TestCase):
    def _first_match(self, idx, cfg=None):
        cfg = maoyan.config_class.from_dict(cfg or CFG)
        m = maoyan.build_machine(cfg)
        snap = frames()[idx]
        return next((st.name for st in m.states if st.detect(snap)), None)

    def test_order_page_is_viewer_then_submit(self):
        # 还没勾人 → VIEWER 优先（同一屏上也有提交按钮）
        self.assertEqual(self._first_match(F_ORDER_A), "VIEWER")
        # 勾完了 → 落到 SUBMIT
        self.assertEqual(self._first_match(F_ORDER_B), "SUBMIT")

    def test_picker_and_pay(self):
        self.assertEqual(self._first_match(F_PICKER), "PICKER")
        self.assertEqual(self._first_match(F_PAY), "PAY")

    def test_immediate_pay_button_is_not_success(self):
        """只有"立即支付"按钮的页面不是成功页。"""
        snap = Snapshot(nodes=[Node(text="立即支付", bounds=(700, 2200, 1000, 2300), clickable=True)])
        self.assertFalse(maoyan._is_pay(snap))
        self.assertTrue(maoyan._is_pay(Snapshot(nodes=[Node(text="微信支付", bounds=(0, 0, 10, 10))])))

    def test_prebooked_skips_viewer(self):
        cfg = maoyan.config_class.from_dict({**CFG, "prebooked": True})
        m = maoyan.build_machine(cfg)
        # 已预约过：即使张三没勾，也不该停在 VIEWER
        self.assertFalse(any(st.detect(frames()[F_ORDER_A]) for st in m.states if st.name == "VIEWER"))


class TestMaoyanFlow(unittest.TestCase):
    def test_full_flow_success(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            dev, clock, start = make_mock()
            store = Store(tmp / "m.db")
            store.upsert_device(DeviceRow(serial=dev.serial, kind="mock", tags=["maoyan"]))
            task = TaskRow(id="m1", scene="maoyan", config=CFG, sale_at=start, lead_ms=55)
            store.create_task(task)
            obs = Observer(tmp / "runs", "m1"); self.addCleanup(obs.close)

            out = Worker(task=task, device=dev, clock=clock, store=store,
                         observer=obs, stop_event=threading.Event()).run()

            self.assertEqual(out.outcome, "success", out.detail)
            self.assertEqual(set(out.states), {"PICKER", "VIEWER", "SUBMIT", "PAY"}, out.states)
            self.assertTrue(any(r["outcome"] == "success" for r in store.results("m1")))
            store.close()

    def test_actually_clicked_the_right_things(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            dev, clock, start = make_mock()
            task = TaskRow(id="m2", scene="maoyan", config=CFG, sale_at=start, lead_ms=55)
            obs = Observer(tmp / "runs", "m2"); self.addCleanup(obs.close)
            Worker(task=task, device=dev, clock=clock, store=None,
                   observer=obs, stop_event=threading.Event()).run()

            clicked = [t[3] for t in dev.taps]
            self.assertIn("看台 ¥580", clicked, clicked)      # 选了有票的档
            self.assertIn("+", clicked, clicked)              # 加了数量
            self.assertNotIn("内场 ¥1280", clicked, clicked)   # 缺货的档一次都没点
            # 勾的是 Image（哈希），不是姓名行
            self.assertIn(maoyan.IMG_UNCHECKED, clicked, clicked)
            self.assertNotIn("张三", clicked, clicked)
            self.assertIn("提交订单", clicked, clicked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
