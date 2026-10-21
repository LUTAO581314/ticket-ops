"""
设备层 — 抽象"一台手机"。

三条实现：
- AdbDevice   主机侧 ADB 驱动。持久 shell（长连 stdin/stdout），点击 16ms 而非 fork 41ms。
- LocalDevice 设备内高速执行器（AutoX.js）通过 HTTP 回报，主机只发指令不等 UI dump。
- MockDevice  干跑。读 fixture 脚本模拟状态推进，用于开发与 CI。

为什么持久 shell 重要（实测数据）：
    adb shell input tap x y   每次 fork 进程 ≈ 41ms
    持久 shell 里写一行命令       ≈ 16ms
    抢票全流程 6~8 次点击 → 差 150~200ms，这就是抢到与抢不到的距离。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from core.fsm import Node, Snapshot


class DeviceError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 基类
# --------------------------------------------------------------------------- #

class BaseDevice:
    serial: str = "unknown"

    # -- 必须实现 --

    def tap(self, x: int, y: int) -> None:
        raise NotImplementedError

    def dump(self) -> Snapshot:
        raise NotImplementedError

    # -- 可选实现 --

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        raise NotImplementedError

    def back(self) -> None:
        self.key("KEYCODE_BACK")

    def key(self, code: str) -> None:
        raise NotImplementedError

    def text(self, s: str) -> None:
        """输入文本（ASCII 安全；中文需走 clipbd+broadcast 或设备内输入法）"""
        raise NotImplementedError

    def screenshot(self, path: Path) -> bool:
        return False

    def shell(self, cmd: str, timeout: float = 5.0) -> str:
        raise NotImplementedError

    def info(self) -> dict:
        return {"serial": self.serial, "kind": type(self).__name__}

    def cheap_probe(self) -> str | None:
        """
        廉价探测：不 dump 也能知道"还停在不在原来那一屏"。

        真机 ADB 上做不到（只能 dump，300~800ms，快通道里绝不能调）→ 返回 None，
        调用方就退化成"纯连点、交给状态机感知"。
        MockDevice 能免费返回帧号，所以干跑时能验证"跳转后就停手"这条逻辑。
        """
        return None

    def close(self) -> None:
        pass

    # -- 便捷 --

    def tap_node(self, node: Node) -> None:
        x, y = node.center
        self.tap(x, y)


# --------------------------------------------------------------------------- #
# ADB
# --------------------------------------------------------------------------- #

def find_adb(explicit: str | None = None) -> str | None:
    """按配置 → PATH → 常见安装位置找 adb"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        if p.is_dir() and (p / "adb.exe").is_file():
            return str(p / "adb.exe")
        raise DeviceError(f"配置里 adb_path 指向的文件不存在: {explicit}")

    found = shutil.which("adb")
    if found:
        return found

    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
        r"C:\Android\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        "/usr/bin/adb",
        "/usr/local/bin/adb",
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_NODE_RE = re.compile(r"<node\b[^>]*/?>", re.S)
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_ui_xml(xml: str, at: float = 0.0, cost_ms: float = 0.0) -> Snapshot:
    """把 uiautomator dump 的 XML 解成 Snapshot（零依赖，正则够用）"""
    nodes: list[Node] = []
    for m in _NODE_RE.finditer(xml):
        attrs = dict(_ATTR_RE.findall(m.group(0)))
        b = _BOUNDS_RE.search(attrs.get("bounds", ""))
        bounds = tuple(int(x) for x in b.groups()) if b else (0, 0, 0, 0)
        nodes.append(
            Node(
                text=attrs.get("text", ""),
                desc=attrs.get("content-desc", ""),
                res_id=attrs.get("resource-id", ""),
                cls=attrs.get("class", ""),
                bounds=bounds,  # type: ignore[arg-type]
                clickable=attrs.get("clickable") == "true",
                enabled=attrs.get("enabled") != "false",
            )
        )
    return Snapshot(nodes=nodes, at=at, cost_ms=cost_ms)


class AdbDevice(BaseDevice):
    """
    ADB 设备。默认持久 shell（关键性能项）。

    注意：Windows 上 adb shell 的 stdin 是一行行吃的，用 echo 哨兵确认命令执行完毕，
    否则读超时不可控（表现就是"点了没反应"其实点了，或者卡死 4 秒）。
    """

    SENTINEL = "__TICKETOK__"

    def __init__(
        self,
        serial: str,
        adb_path: str | None = None,
        *,
        persistent: bool = True,
        default_timeout: float = 4.0,
    ) -> None:
        self.serial = serial
        adb = find_adb(adb_path)
        if not adb:
            raise DeviceError(
                "找不到 adb。把 platform-tools 解压到任意目录，然后在 config 里写 "
                '"adb_path": "C:/platform-tools/adb.exe"'
            )
        self.adb = adb
        self.timeout = default_timeout
        self.persistent = persistent
        self._proc: subprocess.Popen | None = None
        self._buf: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._alive = False
        if persistent:
            self._start_shell()

    # ---------- 持久 shell ----------

    def _start_shell(self) -> None:
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000  # CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [self.adb, "-s", self.serial, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creationflags,
        )
        self._alive = True
        threading.Thread(target=self._reader, name=f"adb-{self.serial}", daemon=True).start()
        # 唤醒 + 探活
        self._write("echo " + self.SENTINEL)
        if not self._read_until_sentinel(timeout=6.0):
            raise DeviceError(f"adb shell 无响应（设备 {self.serial} 未授权/离线？）")

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        buf = b""
        while self._alive:
            chunk = self._proc.stdout.read(1)
            if not chunk:
                break
            if chunk == b"\n":
                self._buf.put(buf.decode("utf-8", "replace").rstrip("\r"))
                buf = b""
            else:
                buf += chunk

    def _write(self, line: str) -> None:
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _read_until_sentinel(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._buf.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return False
            if self.SENTINEL in line:
                return True
        return False

    # ---------- 命令执行 ----------

    def shell(self, cmd: str, timeout: float | None = None) -> str:
        """执行 shell 命令并返回 __SENTINEL__ 之前的输出"""
        timeout = timeout or self.timeout
        if not self.persistent:
            r = subprocess.run(
                [self.adb, "-s", self.serial, "shell", cmd],
                capture_output=True, timeout=timeout + 2,
            )
            return r.stdout.decode("utf-8", "replace")

        with self._lock:
            # 排掉上一次残留
            while not self._buf.empty():
                self._buf.get_nowait()
            self._write(f"{cmd}; echo {self.SENTINEL}")
            lines: list[str] = []
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = self._buf.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if self.SENTINEL in line:
                    return "\n".join(lines)
                lines.append(line)
            raise DeviceError(f"shell 超时 {timeout}s: {cmd[:60]}")

    # ---------- 基本动作 ----------

    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {int(x)} {int(y)}", timeout=2.0)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {ms}", timeout=3.0)

    def key(self, code: str) -> None:
        self.shell(f"input keyevent {code}", timeout=2.0)

    def text(self, s: str) -> None:
        safe = s.replace(" ", "%s")
        self.shell(f"input text '{safe}'", timeout=3.0)

    # ---------- 感知 ----------

    def dump(self) -> Snapshot:
        """
        uiautomator dump。[!] 主机侧往返 300~800ms，只能用于"慢态"（选场次/选观演人）。
        抢票窗口内的快态必须用设备内 LocalDevice。
        """
        t0 = time.monotonic()
        self.shell("uiautomator dump /sdcard/_uidump.xml >/dev/null 2>&1", timeout=6.0)
        xml = self.shell("cat /sdcard/_uidump.xml", timeout=6.0)
        cost = (time.monotonic() - t0) * 1000
        return parse_ui_xml(xml, at=time.time(), cost_ms=cost)

    def screenshot(self, path: Path) -> bool:
        try:
            self.shell("screencap -p /sdcard/_shot.png", timeout=6.0)
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [self.adb, "-s", self.serial, "pull", "/sdcard/_shot.png", str(path)],
                capture_output=True, timeout=15,
            )
            return path.is_file()
        except Exception:
            return False

    # ---------- 元信息 ----------

    def info(self) -> dict:
        d: dict = {"serial": self.serial, "kind": "adb", "persistent": self.persistent}
        try:
            d["model"] = self.shell("getprop ro.product.model", timeout=3.0).strip()
            d["android"] = self.shell("getprop ro.build.version.release", timeout=3.0).strip()
            d["abi"] = self.shell("getprop ro.product.cpu.abi", timeout=3.0).strip()
            d["size"] = self.shell("wm size", timeout=3.0).strip().replace("Physical size: ", "")
            # 判断是否被大麦的设备可信检测拦过（页面会出现"环境异常"）
            d["root"] = "1" in self.shell("getprop ro.build.type", timeout=3.0)
        except DeviceError:
            pass
        return d

    def close(self) -> None:
        self._alive = False
        if self._proc:
            try:
                self._proc.stdin and self._proc.stdin.close()
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


def list_adb_devices(adb_path: str | None = None) -> list[tuple[str, str]]:
    """返回 [(serial, state)]，state ∈ device / offline / unauthorized"""
    adb = find_adb(adb_path)
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, timeout=10).stdout.decode("utf-8", "replace")
    except Exception:
        return []
    out_list = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out_list.append((parts[0], parts[1]))
    return out_list


# --------------------------------------------------------------------------- #
# 设备内执行器（AutoX.js 跑在手机上，主机通过 HTTP 调）
# --------------------------------------------------------------------------- #

class LocalDevice(BaseDevice):
    """
    设备内执行器代理。语义上仍是"一台设备"，但感知与点击都发生在手机内部：
      - snapshot: AutoX.js 侧 dump 控件树（单帧级，几毫秒）
      - tap: 直接 performAction，不走 ADB
    主机侧只做 HTTP 往返（同 WiFi 2~5ms），这是 200ms 全流程的关键。
    """

    def __init__(self, serial: str, base_url: str, token: str = ""):
        self.serial = serial
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _post(self, path: str, payload: dict, timeout: float = 3.0) -> dict:
        import urllib.request

        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **({"X-Token": self.token} if self.token else {})},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def tap(self, x: int, y: int) -> None:
        self._post("/tap", {"x": x, "y": y}, timeout=1.5)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._post("/swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms})

    def key(self, code: str) -> None:
        self._post("/key", {"code": code})

    def dump(self) -> Snapshot:
        t0 = time.monotonic()
        d = self._post("/dump", {}, timeout=2.0)
        nodes = [
            Node(
                text=n.get("text", ""),
                desc=n.get("desc", ""),
                res_id=n.get("id", ""),
                cls=n.get("cls", ""),
                bounds=tuple(n.get("bounds", (0, 0, 0, 0))),  # type: ignore[arg-type]
                clickable=bool(n.get("clickable")),
                children_text=n.get("children", ""),
            )
            for n in d.get("nodes", [])
        ]
        return Snapshot(
            nodes=nodes,
            at=time.time(),
            cost_ms=(time.monotonic() - t0) * 1000,
            package=d.get("pkg", ""),
            activity=d.get("act", ""),
        )

    def info(self) -> dict:
        return {"serial": self.serial, "kind": "local", "url": self.base_url}


# --------------------------------------------------------------------------- #
# 干跑设备
# --------------------------------------------------------------------------- #

@dataclass
class MockScript:
    """一份干跑脚本：按顺序回放的屏幕。"""

    frames: list[Snapshot] = field(default_factory=list)
    # 每帧里"点了会换页"的节点包围盒；不在集合里的可点节点点了只变状态、不换页
    # （比如"+"加数量、勾选观演人 —— 真机上这两下屏幕是不变的）
    nav: list[set] = field(default_factory=list)
    taps: list[tuple[int, int, float]] = field(default_factory=list)  # (x,y,at)
    _i: int = 0

    @classmethod
    def from_file(cls, path: Path) -> "MockScript":
        data = json.loads(Path(path).read_text("utf-8"))
        frames: list[Snapshot] = []
        nav: list[set] = []
        t = data.get("start_at", 1_800_000_000.0)
        for f in data["frames"]:
            nodes = [
                Node(
                    text=n.get("text", ""),
                    desc=n.get("desc", ""),
                    res_id=n.get("id", res_id_from(n)),
                    cls=n.get("cls", ""),
                    bounds=tuple(n.get("bounds", (0, 0, 0, 0))),
                    clickable=bool(n.get("clickable", True)),
                    children_text=n.get("children", ""),
                )
                for n in f.get("nodes", [])
            ]
            nav.append({tuple(n.get("bounds", (0, 0, 0, 0))) for n in f.get("nodes", [])
                        if n.get("nav", True)})
            frames.append(Snapshot(nodes=nodes, at=t + f.get("t", 0.0)))
        return cls(frames=frames, nav=nav)

    @property
    def idx(self) -> int:
        return min(self._i, max(0, len(self.frames) - 1))

    def current(self) -> Snapshot:
        return self.frames[self.idx] if self.frames else Snapshot()

    def is_nav(self, bounds) -> bool:
        if not self.nav:
            return True
        return tuple(bounds) in self.nav[self.idx]

    def advance(self) -> None:
        self._i += 1


def res_id_from(n: dict) -> str:
    return n.get("res_id", "")


class MockDevice(BaseDevice):
    """
    干跑设备。每被点击一次推进一帧——所以状态机的每次准确点击都会被"兑现"为屏幕变化，
    点错了则原地不动（和真机一样），从而真实暴露状态机的逻辑错误。
    """

    def __init__(self, script: MockScript, serial: str = "mock-0", clock=None):
        self.script = script
        self.serial = serial
        self.clock = clock
        self.taps: list[tuple[float, int, int, str]] = []
        self.frame_ticks = 0

    def _now(self) -> float:
        return self.clock.now() if self.clock else time.time()

    def cheap_probe(self) -> str | None:
        return f"frame#{self.script._i}"

    def tap(self, x: int, y: int) -> None:
        snap = self.script.current()
        hit = ""
        hit_bounds = None
        cands = []
        for n in snap.visible_bounds():
            l, t, r, b = n.bounds
            if l <= x <= r and t <= y <= b:
                cands.append(n)
        if cands:
            # 真机的触摸派发落到最深（面积最小）的节点，容器通常不接收点击。
            # 这里必须先按 clickable 过滤再取最小 —— 否则 fixture 里的行容器
            # （包着勾选框和姓名）会抢在真正的勾选框前面被命中。
            clickable = [n for n in cands if n.clickable]
            pool = clickable or cands
            n = min(pool, key=lambda c: (c.bounds[2] - c.bounds[0]) * (c.bounds[3] - c.bounds[1]))
            hit = n.text or n.desc or n.res_id
            hit_bounds = n.bounds
        self.taps.append((self._now(), x, y, hit))
        # 只有点中"会换页"的节点才推进；"+"/勾选观演人这类点了屏幕不变的，不推进
        if hit and hit_bounds is not None and self.script.is_nav(hit_bounds):
            self.script.advance()

    def swipe(self, x1, y1, x2, y2, ms=300) -> None:
        self.taps.append((self._now(), x2, y2, f"swipe({x1},{y1})"))

    def key(self, code: str) -> None:
        self.taps.append((self._now(), 0, 0, code))
        if code == "KEYCODE_BACK":
            self.script.advance()

    def dump(self) -> Snapshot:
        self.frame_ticks += 1
        s = self.script.current()
        # 复制一份，避免状态机修改污染 fixture
        return Snapshot(
            nodes=list(s.nodes),
            at=self._now(),
            cost_ms=1.0,
            package=s.package or "cn.damai",
            activity=s.activity or "com.damai.MainActivity",
        )

    def screenshot(self, path: Path) -> bool:
        return False

    def info(self) -> dict:
        return {"serial": self.serial, "kind": "mock", "frames": len(self.script.frames), "taps": len(self.taps)}

    def report(self) -> str:
        lines = [f"Mock {self.serial}: frames={len(self.script.frames)} taps={len(self.taps)}"]
        for at, x, y, hit in self.taps:
            lines.append(f"  t={at:.3f} tap({x},{y}) → {hit or 'MISS'}")
        return "\n".join(lines)
