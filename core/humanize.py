"""
拟人化 — 平时像人，抢票窗口里像机器。

风控看的不是"你有没有点得很快"，而是"你的行为分布像不像真人"：
  - 间隔是否是常数（真人间隔服从长尾分布，机器是等间隔）
  - 点击坐标是否每次都精确落同一个像素（真人有 ±5px 抖动）
  - 无障碍服务是否 24 小时常开（真人不会）
  - 同一个 IP 下挂了几个设备指纹

所以策略是分级的：
  WARMUP / IDLE   → 全量拟人化（人看得见的时段）
  WINDOW          → 关掉所有装饰，只保留最小抖动（±2px，避免"像素级完美"这一条最廉价的特征）
  0.5s 内不重复进同一页面（防"回退连点"这种典型机器行为）
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class Profile:
    """一套节律参数。"""

    name: str = "default"
    tap_gap: tuple[float, float] = (0.090, 0.190)  # 两次点击间隔（秒，闭区间）
    jitter_px: int = 6  # 坐标抖动 ±px
    think_pause: tuple[float, float] = (0.015, 0.055)  # 动作间的"思考"停顿
    scroll_steps: tuple[int, int] = (3, 7)  # 滑动分段数（一次滑到底 = 机器）
    scroll_gap: tuple[float, float] = (0.012, 0.030)

    @staticmethod
    def window() -> "Profile":
        """抢票窗口：快通道。只留最小抖动。"""
        return Profile(
            name="window",
            tap_gap=(0.0, 0.012),
            jitter_px=2,
            think_pause=(0.0, 0.004),
            scroll_steps=(1, 1),
            scroll_gap=(0.0, 0.004),
        )

    @staticmethod
    def warmup() -> "Profile":
        """预热/校验阶段：慢一点，像真人在翻详情页。"""
        return Profile(
            name="warmup",
            tap_gap=(0.220, 0.520),
            jitter_px=9,
            think_pause=(0.060, 0.180),
        )


class Humanizer:
    """
    统一的节律发生器。所有"多等一会儿"都从这里出发，
    这样调风控的时候只改一个地方。
    """

    def __init__(self, profile: Profile | None = None, rng: random.Random | None = None) -> None:
        self.profile = profile or Profile()
        self.rng = rng or random.Random()

    def use(self, profile: Profile) -> None:
        self.profile = profile

    # ---- 间隔 ----

    def gap(self) -> float:
        """两次点击之间等多久。用三角分布而非均匀：真人更常落在中间偏短。"""
        lo, hi = self.profile.tap_gap
        if hi <= lo:
            return max(0.0, lo)
        return self.rng.triangular(lo, hi, lo + (hi - lo) * 0.35)

    def pause(self) -> float:
        lo, hi = self.profile.think_pause
        if hi <= lo:
            return max(0.0, lo)
        return self.rng.uniform(lo, hi)

    # ---- 坐标 ----

    def offset(self, x: int, y: int, bounds: tuple[int, int, int, int] | None = None) -> tuple[int, int]:
        """
        在节点范围内随机落点，而不是死点中心。
        若给了 bounds，抖动量不超过节点自身的 30%（避免点到隔壁按钮）。
        """
        j = self.profile.jitter_px
        if j <= 0:
            return x, y
        dx = self.rng.randint(-j, j)
        dy = self.rng.randint(-j, j)
        if bounds and bounds != (0, 0, 0, 0):
            l, t, r, b = bounds
            max_dx = max(1, int((r - l) * 0.30))
            max_dy = max(1, int((b - t) * 0.30))
            dx = max(-max_dx, min(max_dx, dx))
            dy = max(-max_dy, min(max_dy, dy))
            nx, ny = x + dx, y + dy
            return max(l + 1, min(r - 1, nx)), max(t + 1, min(b - 1, ny))
        return x + dx, y + dy

    # ---- 滑动 ----

    def scroll_plan(self, x: int, y: int, dy: int) -> list[tuple[int, int, int, int, int]]:
        """把"滑一段"拆成若干小段，速度不均匀，模拟手指惯性"""
        steps = self.rng.randint(*self.profile.scroll_steps)
        plan = []
        cur_y = y
        remain = dy
        for i in range(steps):
            frac = remain / (steps - i) * self.rng.uniform(0.7, 1.3)
            nxt = int(cur_y + frac)
            dur = int(self.rng.uniform(*self.profile.scroll_gap) * 1000) or 8
            plan.append((x, cur_y, x, nxt, dur))
            remain -= frac
            cur_y = nxt
        return plan

    # ---- 数值工具 ----

    @staticmethod
    def skew(mean: float, spread: float) -> float:
        """长尾延迟：大部分时候贴近 mean，偶尔拖很久（摸鱼）"""
        return abs(random.gauss(mean, spread)) if spread > 0 else mean

    def __repr__(self) -> str:
        p = self.profile
        return f"<Humanizer {p.name} gap={p.tap_gap} jitter={p.jitter_px}px>"
