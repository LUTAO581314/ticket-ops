"""
时序引擎 — 抢票成败的第一要素。

三件事：
1. NTP 多服务器 × 多采样 → 取中位数，压掉单源 ±30ms 抖动
2. 用 time.monotonic() 做时间基准，避免系统时钟被 NTP 校时"跳变"打断倒计时
3. T-lead 忙等发射（粗等到 T-200ms，剩下 1ms 精度自旋）

用法：
    clk = Clock()
    clk.sync(["ntp.aliyun.com", "ntp.tencent.com", "cn.ntp.org.cn"])
    print(clk.status())          # offset / delay / 服务器一致性
    clk.fire_at(target_unix, lead_ms=55, on_fire=lambda: ...)
"""
from __future__ import annotations

import socket
import statistics
import struct
import time
from dataclasses import dataclass, field

NTP_EPOCH_OFFSET = 2_208_988_800  # 1900-01-01 → 1970-01-01
NTP_PORT = 123
NTP_PACKET_SIZE = 48

DEFAULT_SERVERS = (
    "ntp.aliyun.com",
    "ntp.tencent.com",
    "cn.ntp.org.cn",
    "time.windows.com",
    "pool.ntp.org",
)


@dataclass
class Sample:
    server: str
    offset: float  # 本地时间 - 服务器时间（秒），要减掉才是服务器时间
    delay: float  # 往返时延（秒）
    stratum: int
    error: str = ""


@dataclass
class SyncReport:
    samples: list[Sample] = field(default_factory=list)
    offset: float = 0.0
    delay: float = 0.0
    spread: float = 0.0
    ok: int = 0
    failed: int = 0

    def summary(self) -> str:
        lines = [
            f"NTP 校时: ok={self.ok} failed={self.failed} "
            f"offset={self.offset * 1000:+.1f}ms delay={self.delay * 1000:.1f}ms "
            f"spread={self.spread * 1000:.1f}ms"
        ]
        for s in self.samples:
            if s.error:
                lines.append(f"  ✗ {s.server:<22} {s.error}")
            else:
                lines.append(
                    f"  [ok] {s.server:<22} offset={s.offset * 1000:+8.1f}ms "
                    f"delay={s.delay * 1000:6.1f}ms stratum={s.stratum}"
                )
        return "\n".join(lines)


def _read_ntp_ts(buf: bytes, off: int) -> float:
    """NTP 时间戳（32 位秒 + 32 位小数）→ unix 秒"""
    seconds, fraction = struct.unpack("!II", buf[off : off + 8])
    return (seconds - NTP_EPOCH_OFFSET) + fraction / 2**32


def _query(server: str, timeout: float = 1.5) -> Sample:
    """单次 SNTP 查询，返回 offset/delay"""
    pkt = b"\x1b" + 47 * b"\x00"  # LI=0 VN=3 Mode=3(client)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t1 = time.time()
        sock.sendto(pkt, (server, NTP_PORT))
        data, _ = sock.recvfrom(1024)
        t4 = time.time()
    except socket.timeout:
        return Sample(server, 0.0, 0.0, 0, "timeout")
    except OSError as e:
        return Sample(server, 0.0, 0.0, 0, f"socket: {e}")
    finally:
        sock.close()

    if len(data) < NTP_PACKET_SIZE:
        return Sample(server, 0.0, 0.0, 0, "short packet")

    stratum = data[1]
    # 分层防御：stratum 0 表示 kiss-o'-death，不能信
    if stratum == 0 or stratum > 15:
        return Sample(server, 0.0, 0.0, stratum, f"bad stratum={stratum}")

    t3 = _read_ntp_ts(data, 40)  # transmit timestamp
    if t3 <= 0:
        return Sample(server, 0.0, 0.0, stratum, "zero timestamp")

    # 标准 SNTP 公式（t2 服务器收包时刻无从得知时，用中值近似）
    offset = t3 - (t1 + t4) / 2
    delay = t4 - t1
    return Sample(server, offset, delay, stratum)


class Clock:
    """以 monotonic 为基准的"服务器时间"时钟。"""

    def __init__(self) -> None:
        self._base_mono = time.monotonic()
        self._base_wall = time.time()  # 未校时时等于本机时间
        self._calibrated = False
        self.report: SyncReport | None = None

    # ---------- 校时 ----------

    def sync(
        self,
        servers: tuple[str, ...] | list[str] = DEFAULT_SERVERS,
        rounds: int = 3,
        timeout: float = 1.5,
        per_round_gap: float = 0.2,
    ) -> SyncReport:
        """多服务器 × 多轮采样，取 offset 中位数（剔除 delay 明显偏离的样本）"""
        samples: list[Sample] = []
        for r in range(rounds):
            for srv in servers:
                samples.append(_query(srv, timeout))
            if r < rounds - 1:
                time.sleep(per_round_gap)

        good = [s for s in samples if not s.error]
        rep = SyncReport(samples=samples, ok=len(good), failed=len(samples) - len(good))

        if not good:
            self.report = rep
            return rep

        # 用 delay 中位数 * 2 做离群过滤：延迟异常的样本 offset 不可信
        d_med = statistics.median(s.delay for s in good)
        filtered = [s for s in good if s.delay <= max(d_med * 3, d_med + 0.15)]
        if not filtered:
            filtered = good

        offsets = sorted(s.offset for s in filtered)
        offset = statistics.median(offsets)

        # 服务器间一致性：中位数绝对偏差
        spread = statistics.median([abs(o - offset) for o in offsets]) if len(offsets) > 1 else 0.0

        self._base_mono = time.monotonic()
        self._base_wall = time.time() - offset
        self._calibrated = True

        rep.offset = offset
        rep.delay = statistics.median(s.delay for s in filtered)
        rep.spread = spread
        self.report = rep
        return rep

    # ---------- 读数 ----------

    def now(self) -> float:
        """当前"服务器时间"（unix 秒，float）"""
        return self._base_wall + (time.monotonic() - self._base_mono)

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def status(self) -> str:
        if not self.report:
            return "NTP: 未校时（使用本机时钟）"
        return self.report.summary()

    def quality_ok(self, max_spread_ms: float = 40.0) -> bool:
        """spread 太大说明服务器之间不一致，抢票前必须报警"""
        if not self.report or not self.report.ok:
            return False
        return self.report.spread * 1000 <= max_spread_ms

    # ---------- 发射 ----------

    def wait_until(
        self,
        target_unix: float,
        lead_ms: float = 0.0,
        coarse_ms: float = 200.0,
        on_tick=None,
    ) -> float:
        """
        精确等到 (target_unix - lead_ms)，返回实际到达时刻。

        coarse_ms 之前用 sleep（省 CPU），之后 1ms 级自旋。
        lead_ms=55 表示提前 55ms 发射，补偿"点击→服务端收到"的链路延迟。
        """
        fire_at = target_unix - lead_ms / 1000.0
        coarse = coarse_ms / 1000.0

        # 阶段 1：粗等
        while True:
            remain = fire_at - self.now()
            if remain <= coarse:
                break
            sleep_for = min(remain - coarse, 0.5)
            if sleep_for > 0:
                time.sleep(sleep_for)

        # 阶段 2：自旋（禁止 sleep，否则精度退化到调度粒度 ~15ms）
        while True:
            remain = fire_at - self.now()
            if remain <= 0:
                break
            if on_tick and remain < 0.02:
                on_tick(remain)
        return self.now()


class FakeClock(Clock):
    """干跑用：时间由外部推进，不依赖真实时钟。"""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        super().__init__()
        self._fake = start

    def now(self) -> float:
        return self._fake

    def advance(self, seconds: float) -> None:
        self._fake += seconds

    def wait_until(self, target_unix: float, lead_ms: float = 0.0, coarse_ms: float = 200.0, on_tick=None) -> float:
        fire_at = target_unix - lead_ms / 1000.0
        if self._fake < fire_at:
            self._fake = fire_at
        return self._fake


if __name__ == "__main__":
    c = Clock()
    r = c.sync(rounds=2)
    print(r.summary())
    print(f"quality_ok={c.quality_ok()}")
