"""
Web 看板 — 零依赖（http.server + SSE），`python main.py serve` 就能开。

为什么不引 FastAPI：
    这套东西要能在一台只装了 Python 和 platform-tools 的机器上直接跑起来。
    看板的需求只有"看状态 + 建任务"，标准库够；装一个 web 框架反而让部署变成门槛。

接口：
    GET  /                      看板
    GET  /api/state             设备 + 任务 + 统计（一次拉全）
    GET  /api/events?task=&since=  事件流
    GET  /api/stream            SSE，0.5s 一帧
    GET  /api/devices           扫描 ADB 并返回设备
    POST /api/task              建任务 {scene,config,sale_at,lead_ms,replicas,...}
    POST /api/task/cancel       取消 {id}
"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from platform.scheduler import Scheduler
from platform.store import Store, TaskRow

STATIC_DIR = Path(__file__).with_name("static")

# 设备内执行器（autox-speed.js）的调试指令队列与回执
_AGENT_CMDS: dict[str, list] = {}
_AGENT_LOCK = threading.Lock()
_cmd_results: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    scheduler: Scheduler = None  # type: ignore[assignment]
    store: Store = None  # type: ignore[assignment]
    quiet = True

    # ---------------- 基础设施 ----------------

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        if not self.quiet:
            print(f"[web] {self.address_string()} {fmt % args}")

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg: str, status: int = 400) -> None:
        self._json({"error": msg}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------------- GET ----------------

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)

        if path in ("/", "/index.html"):
            return self._static("dashboard.html")
        if path == "/api/state":
            return self._json(self._state())
        if path == "/api/events":
            task = (qs.get("task") or [""])[0]
            since = int((qs.get("since") or ["0"])[0])
            limit = int((qs.get("limit") or ["200"])[0])
            return self._json(self.store.events(task or None, since, limit))
        if path == "/api/devices":
            found = self.scheduler.discover()
            return self._json([self._dev_dict(d) for d in self.store.devices()])
        if path == "/api/stream":
            return self._stream()
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])

        # ---- 设备内执行器（autox-speed.js）回调 ----
        if path == "/agent/config":
            serial = (qs.get("serial") or [""])[0]
            return self._json(self._agent_config(serial))
        if path == "/agent/poll":
            serial = (qs.get("serial") or [""])[0]
            with _AGENT_LOCK:
                cmds = _AGENT_CMDS.get(serial) or []
                if cmds:
                    return self._json({"cmd": cmds.pop(0)})
            return self._json({})

        return self._err("not found", 404)

    def _agent_config(self, serial: str) -> dict:
        """这台设备现在该跑哪个任务？只看 armed/running 且未结束的派发。"""
        for status in ("running", "armed"):
            for t in self.store.tasks(limit=20, status=status):
                for a in self.store.assignments(t.id):
                    if a["serial"] == serial and not a["ended_at"]:
                        return {
                            "task": {
                                "id": t.id, "scene": t.scene, "config": t.config,
                                "sale_at": t.sale_at, "lead_ms": t.lead_ms,
                            },
                            "now": self.scheduler.clock.now(),
                        }
        return {"task": None, "now": self.scheduler.clock.now()}

    def _static(self, name: str) -> None:
        p = (STATIC_DIR / name).resolve()
        if not p.is_file() or STATIC_DIR.resolve() not in p.parents:
            return self._err("not found", 404)
        data = p.read_bytes()
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dev_dict(self, d) -> dict:
        return {
            "serial": d.serial, "kind": d.kind, "model": d.model, "android": d.android,
            "account": d.account, "tags": d.tags, "state": d.state,
            "busy_task": self.scheduler.status()["busy"].get(d.serial, ""),
            "last_seen": d.last_seen,
        }

    def _state(self) -> dict:
        st = self.scheduler.status()
        tasks = [
            {
                "id": t.id, "scene": t.scene, "status": t.status, "outcome": t.outcome,
                "detail": t.detail, "sale_at": t.sale_at, "lead_ms": t.lead_ms,
                "replicas": t.replicas, "config": t.config,
                "countdown_ms": max(0.0, (t.sale_at - self.scheduler.clock.now()) * 1000),
                "devices": [a["serial"] for a in self.store.assignments(t.id)],
            }
            for t in self.store.tasks(limit=40)
        ]
        return {
            "now": self.scheduler.clock.now(),
            "clock": {
                "calibrated": self.scheduler.clock.calibrated,
                "offset_ms": round((self.scheduler.clock.report.offset * 1000), 1) if self.scheduler.clock.report else None,
                "spread_ms": round((self.scheduler.clock.report.spread * 1000), 1) if self.scheduler.clock.report else None,
            },
            "devices": [self._dev_dict(d) for d in self.store.devices()],
            "tasks": tasks,
            "busy": st["busy"],
            "groups": st["groups"],
            "stats": st["stats"],
            "results": self.store.results()[:20],
        }

    # ---------------- SSE ----------------

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_event_id = 0
        try:
            while True:
                state = self._state()
                evs = self.store.events(None, last_event_id, 60)
                if evs:
                    last_event_id = max(e["id"] for e in evs)
                payload = json.dumps(
                    {"state": state, "events": list(reversed(evs))}, ensure_ascii=False, default=str
                )
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---------------- POST ----------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()

        if path == "/api/task":
            try:
                sale_at = float(body.get("sale_at") or (self.scheduler.clock.now() + 20))
            except (TypeError, ValueError):
                return self._err("sale_at 必须是 unix 秒")
            scene = body.get("scene", "damai")
            # 建任务时就校验场景能不能加载，不要等开售前 30 秒预检才报"未知场景"
            try:
                from platform.worker import load_scene
                load_scene(scene)
            except RuntimeError as e:
                return self._err(str(e))
            task = TaskRow(
                id="",
                scene=scene,
                config=body.get("config", {}) or {},
                sale_at=sale_at,
                lead_ms=float(body.get("lead_ms", 55)),
                device_filter=body.get("device_filter", []) or [],
                replicas=int(body.get("replicas", 1)),
                priority=int(body.get("priority", 5)),
            )
            t = self.scheduler.submit(task)
            return self._json({"id": t.id, "sale_at": t.sale_at})

        if path == "/api/task/cancel":
            tid = body.get("id", "")
            return self._json({"ok": self.scheduler.cancel(tid)})

        if path == "/api/clock/sync":
            rep = self.scheduler.clock.sync(rounds=2)
            return self._json({"offset_ms": rep.offset * 1000, "spread_ms": rep.spread * 1000,
                               "ok": rep.ok, "failed": rep.failed})

        # ---- 设备内执行器上报 ----
        if path == "/agent/event":
            return self._agent_event(body)

        if path == "/agent/cmd":
            # 给某台设备塞一条调试指令
            serial = body.get("serial", "")
            with _AGENT_LOCK:
                _AGENT_CMDS.setdefault(serial, []).append(body.get("cmd", "state"))
            return self._json({"ok": True})

        return self._err("not found", 404)

    def _agent_event(self, body: dict) -> None:
        """把设备内执行器的事件写进同一张 events 表，看板上就能看到手机里的进展。"""
        serial = body.get("serial", "?")
        kind = body.get("kind", "note")
        detail = body.get("detail", "")
        state = body.get("state", "")
        if kind == "result":
            outcome = body.get("outcome", "unknown")
            # 找这台设备当前的任务
            tid = ""
            for t in self.store.tasks(limit=20, status="running") + self.store.tasks(limit=20, status="armed"):
                if any(a["serial"] == serial and not a["ended_at"] for a in self.store.assignments(t.id)):
                    tid = t.id
                    break
            self.store.add_result(tid, serial, outcome, reason=detail)
            self.store.add_event(tid, serial, "result", ok=(outcome == "success"), detail=f"{outcome}: {detail}")
            if outcome == "success" and tid:
                self.scheduler.cancel(tid)  # 一台成功，叫停其余
        elif kind == "state":
            tid = self._tid_for(serial)
            self.store.add_event(tid, serial, "state", state=state, detail=detail)
        elif kind == "cmd_result":
            if body.get("nodes") is not None:
                self._cmd_results.append({"kind": "dump", "serial": serial, "nodes": body["nodes"]})
            elif body.get("state") is not None:
                self._cmd_results.append({"kind": "state", "serial": serial, "state": body["state"]})
        else:
            tid = self._tid_for(serial)
            self.store.add_event(tid, serial, "note", detail=detail,
                                 ok=body.get("error") is None)
        return self._json({"ok": True})

    def _tid_for(self, serial: str) -> str:
        for t in self.store.tasks(limit=20, status="running") + self.store.tasks(limit=20, status="armed"):
            if any(a["serial"] == serial and not a["ended_at"] for a in self.store.assignments(t.id)):
                return t.id
        return ""


def serve(scheduler: Scheduler, store: Store, host: str = "127.0.0.1", port: int = 8770) -> None:
    Handler.scheduler = scheduler
    Handler.store = store
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"看板: http://{host}:{port}")
    print("Ctrl+C 停止")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n收工")
    finally:
        httpd.server_close()


def serve_background(scheduler: Scheduler, store: Store, host: str, port: int) -> ThreadingHTTPServer:
    Handler.scheduler = scheduler
    Handler.store = store
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="web", daemon=True).start()
    return httpd
