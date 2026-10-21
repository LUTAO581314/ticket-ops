r"""
猫眼演出场景插件。

和大麦的根本差异（这决定了两份插件不能共用一个实现）：

| 维度 | 大麦 | 猫眼 |
|---|---|---|
| 控件树 | 详情页/选座页大量自绘，dump 拿不到按钮 | **标准原生控件**，`android.widget.TextView` / `Button` 都在 |
| 入口文案 | 立即购买 / 立即抢购 | 立即购票 / 立即预订 / 特惠购票（无票时变"缺货登记"） |
| 票档 | `id=tv_price_name` + 文本"无票" | `¥数字` 文本节点，**缺货标记在行的子树里**，不在票档节点自己身上 |
| 数量 | "数量" + 数字 | `\d+份`，`+` 是数量那一行最右边的子节点 |
| 确认 | 确定 / 下一步 | **确认**（不是"确定"） |
| 观演人 | 点姓名 | **勾选框是 Image 节点，勾选状态编码在 text 里（base64 图片哈希）** |
| 支付 | 去支付 / 订单提交成功 | `android.widget.Button` + 收银台"微信支付" |
| 流程顺序 | 票档 → 数量 → 观演人 → 确定 → 提交 | 票档 → 数量 → 确认 → **订单页勾观演人** → 提交 |

最后一行是设计上最容易踩的坑：猫眼的观演人勾选发生在"确认"之后的订单页，和大麦相反。
所以状态机上 VIEWER 必须排在 SUBMIT 前面，并且用"还有没勾的就不放过"来驱动降级，
而不是靠页面标题判断。

数据来源：luode0320/ticket-autox-app 的 MaoYanGo.js / MaoYanGoNew.js / MaoYanMonitor.js
（三个版本互相印证的选择器与文案），已落在 refs/ 下。勾选框的两个 base64 哈希直接取自
MaoYanMonitor.js 的 clickViewerCheckBox()。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from core.fsm import Action, Machine, Node, Snapshot, State, StepResult
from core.tree import container, descendants, subtree_text

# --------------------------------------------------------------------------- #
# 关键词表
# --------------------------------------------------------------------------- #

KW: dict[str, tuple[str, ...]] = {
    # 详情页入口，按优先级排列（原脚本按 立即预订 > 立即购票 > 特惠购票 的顺序试）
    "entry": ("立即预订", "立即购票", "特惠购票"),
    "sold_out_entry": ("缺货登记",),
    # 收银台 — 只有这些出现才叫"抢到了"
    "pay": ("微信支付", "支付宝支付", "支付成功", "订单提交成功", "订单已提交", "收银台"),
    "submit": ("提交订单", "确认订单", "订单确认", "立即支付", "去支付"),
    "picker": ("请选择票档", "选择票档", "数量"),
    "qty": ("数量",),
    "confirm": ("确认", "确定"),
    "viewer": ("选择观演人", "观演人", "实名信息", "购票人"),
    "session": ("场次", "选择场次"),
    "refresh": ("刷新",),
    "queue": ("排队", "人数过多", "稍后再试", "被挤爆", "小主", "系统繁忙", "请稍后"),
    "captcha": ("滑块", "验证码", "拖动", "按住", "请完成验证"),
    "risk": ("环境异常", "验证失败", "操作频繁", "账号异常", "请求异常", "安全验证", "设备异常"),
    "sold_out": ("缺货", "无票", "已售罄", "暂无余票", "已结束", "售完"),
    "dialog": ("我知道了", "我知道啦", "取消", "关闭"),
}

TERMINALS = {"PAY", "CAPTCHA", "RISK"}

# 猫眼票档的价格文本形如 "¥380" / "￥1280"
_PRICE_RE = re.compile(r"[¥￥]\s*(\d{2,6})")
# 数量文本形如 "2份"
_QTY_RE = re.compile(r"(\d+)\s*份")

# MaoYanMonitor.js clickViewerCheckBox() 里的两个 base64 图片哈希。
# 未选中 / 已选中，是猫眼用 Image 的 content 哈希来表达勾选态（无障碍读不到 checked 属性）。
IMG_UNCHECKED = "wc0GRRGh2f2pQAAAABJRU5ErkJggg=="
IMG_CHECKED = "B85bZ04Z1b5tAAAAAElFTkSuQmCC"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

@dataclass
class MaoyanConfig:
    演出: str = ""            # 演出名关键字，仅用于日志
    session: str = ""          # 场次关键字，如 "05-18" / "周六 19:30"
    price: int = 0             # 目标票价；0 = 不限，取最低有票档
    max_price: int = 0         # 票价上限；0 = 不限
    qty: int = 1
    viewers: list[str] = field(default_factory=list)
    entry_xy: tuple[int, int] = (540, 2100)
    confirm_xy: tuple[int, int] = (878, 2263)   # 原脚本默认，1080 宽屏
    burst: int = 12
    burst_gap_ms: float = 8.0
    # 勾选框哈希可覆盖：猫眼换皮肤/换版本会改图片，认不出时退化成点姓名
    img_unchecked: str = IMG_UNCHECKED
    img_checked: str = IMG_CHECKED
    allow_price_fallback: bool = True   # 目标价没票时，允许退到其它有票档
    prebooked: bool = False             # 已经预约过（观演人已填），可跳过勾选

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MaoyanConfig":
        c = cls()
        for k, v in (d or {}).items():
            if not hasattr(c, k):
                continue
            cur = getattr(c, k)
            if k in ("entry_xy", "confirm_xy"):
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    setattr(c, k, (int(v[0]), int(v[1])))
                elif isinstance(v, str) and "," in v:
                    x, y = v.split(",")[:2]
                    setattr(c, k, (int(x), int(y)))
                continue
            if isinstance(cur, bool):
                setattr(c, k, bool(v))
            elif isinstance(cur, int):
                setattr(c, k, int(v))
            elif isinstance(cur, float):
                setattr(c, k, float(v))
            elif isinstance(cur, list):
                setattr(c, k, [str(x) for x in (v or [])])
            else:
                setattr(c, k, v if v is not None else "")
        return c


# --------------------------------------------------------------------------- #
# 票档
# --------------------------------------------------------------------------- #

@dataclass
class Tier:
    node: Node
    price: int
    sold_out: bool
    label: str = ""

    @property
    def name(self) -> str:
        return f"¥{self.price}"


def tiers(snap: Snapshot) -> list[Tier]:
    """
    扫出所有票档。

    缺货判定的关键：缺货标记可能在票档节点自身文本里（部分机型），也可能是同一格里
    的兄弟节点。所以必须查它所在容器（最小包含祖先）的整棵子树 — 只查自己会漏。
    """
    out: list[Tier] = []
    seen: set[tuple[int, tuple[int, int, int, int]]] = set()
    for n in snap.nodes:
        m = _PRICE_RE.search(n.text or "")
        if not m:
            continue
        price = int(m.group(1))
        key = (price, n.bounds)
        if key in seen:
            continue
        seen.add(key)

        box = container(snap, n)
        scope = subtree_text(snap, box if box is not None else n)
        sold = any(w in scope for w in KW["sold_out"])
        label = (n.text or "").strip() or f"¥{price}"
        out.append(Tier(node=n, price=price, sold_out=sold, label=label))
    return out


def available_tiers(snap: Snapshot) -> list[Tier]:
    return [t for t in tiers(snap) if not t.sold_out]


def pick_tier(snap: Snapshot, cfg: MaoyanConfig, tried: set[int] | None = None) -> Tier | None:
    """
    选档策略：目标价 → 同价最近的档 → 最低价。全程只挑**有票**且**没试过**的档。

    不采用原脚本"遍历所有符合价格的档逐个冲"的做法：那会在一轮里连开多张订单，
    并且大概率买到最贵的那个。这里一次只打一档，失败了换下一档。
    """
    tried = tried or set()
    pool = [t for t in available_tiers(snap) if t.price not in tried]
    if not pool:
        return None
    if cfg.max_price > 0:
        capped = [t for t in pool if t.price <= cfg.max_price]
        if capped:
            pool = capped
    if cfg.price > 0:
        exact = [t for t in pool if t.price == cfg.price]
        if exact:
            return exact[0]
        if not cfg.allow_price_fallback:
            return None
        # 就近挑：价差小的优先，同价差取便宜的
        pool.sort(key=lambda t: (abs(t.price - cfg.price), t.price))
        return pool[0]
    pool.sort(key=lambda t: t.price)
    return pool[0]


# --------------------------------------------------------------------------- #
# 数量
# --------------------------------------------------------------------------- #

def qty_value(snap: Snapshot) -> int | None:
    for n in snap.nodes:
        m = _QTY_RE.search(n.text or "")
        if m:
            return int(m.group(1))
    return None


def qty_node(snap: Snapshot) -> Node | None:
    for n in snap.nodes:
        if _QTY_RE.search(n.text or ""):
            return n
    return None


def plus_button(snap: Snapshot) -> Node | None:
    """
    找"+"。

    原脚本走的是 parent.children()[childCount-1] —— 数量那一行的最后一个子节点。
    扁平快照下等价于：同一行里、在数量文本右侧、x 最大的那个节点。
    优先找字面 "+"，找不到再用几何推断。
    """
    q = qty_node(snap)
    cy = q.center[1] if q else None

    for n in snap.nodes:
        if (n.text or "").strip() in ("+", "＋"):
            if cy is None or abs(n.center[1] - cy) <= 80:
                return n

    if q is None:
        return None
    right = q.bounds[2]
    cands = [
        n for n in snap.nodes
        if n.bounds != (0, 0, 0, 0)
        and abs(n.center[1] - cy) <= 45
        and n.bounds[0] >= right - 8
    ]
    if not cands:
        return None
    return max(cands, key=lambda n: (n.center[0], -abs(n.center[1] - cy)))


# --------------------------------------------------------------------------- #
# 观演人
# --------------------------------------------------------------------------- #

def _viewer_node(snap: Snapshot, name: str) -> Node | None:
    """精确匹配姓名，退化为包含匹配。避免"张三"命中"张三丰"。"""
    exact = [n for n in snap.nodes if (n.text or "").strip() == name]
    if exact:
        return exact[0]
    loose = [n for n in snap.nodes if name in (n.text or "")]
    return loose[0] if loose else None


def _check_state(snap: Snapshot, name: str, cfg: MaoyanConfig) -> tuple[Node | None, Node | None, str]:
    """
    返回 (姓名节点, 勾选 Image 节点, 状态)。

    状态：checked / unchecked / unknown
    勾选框位置：姓名节点所在容器（最小包含祖先）的子树里的 Image 节点。
    """
    vn = _viewer_node(snap, name)
    if vn is None:
        return None, None, "missing"

    box = container(snap, vn)
    scope = descendants(snap, box) if box is not None else descendants(snap, vn)
    imgs = [n for n in scope if "Image" in (n.cls or "")] or [
        n for n in scope if (n.text or "") in (cfg.img_checked, cfg.img_unchecked)
    ]
    for im in imgs:
        t = (im.text or "").strip()
        if t == cfg.img_checked:
            return vn, im, "checked"
        if t == cfg.img_unchecked:
            return vn, im, "unchecked"
    # 有图但认不出哈希：状态未知，退化成点姓名整行
    return vn, (imgs[0] if imgs else None), "unknown"


class _Run:
    """
    一次抢票的全部私有运行态。

    为什么不挂在 ctx 上：ctx 是平台层的通用对象，场景往里塞私有字段是抽象泄漏；
    更实际的问题是 detect() 的签名只有 snapshot，拿不到 ctx，只能靠全局中转 ——
    而 scheduler 是"每台设备一个线程"，全局中转必然串味。
    这里由 build_machine() 每台新建一份，闭包捕获，天然隔离且无需加锁。
    """

    __slots__ = ("cfg", "stage", "tried", "attempts", "skip", "qty_base", "qty_bumped")

    def __init__(self, cfg: MaoyanConfig) -> None:
        self.cfg = cfg
        self.stage = 0
        self.tried: set[int] = set()
        self.attempts: dict[str, int] = {}
        self.skip: set[str] = set()
        # 数量控件点"+"之后，屏幕上那个"1份"不一定立刻刷新：真机上是异步重绘，
        # Mock 下更是完全不变。所以不能只信 qty_value()，要记住自己点了几次。
        self.qty_base: int | None = None
        self.qty_bumped = 0


def pending_viewers(snap: Snapshot, run: _Run) -> list[str]:
    """还没勾上的观演人。已经试过两次还勾不上的一律跳过 —— 宁可少勾也不要卡死整轮。"""
    out: list[str] = []
    for name in run.cfg.viewers:
        if name in run.skip:
            continue
        _, _, state = _check_state(snap, name, run.cfg)
        if state in ("checked", "missing"):
            # checked = 已好；missing = 名字不在这屏（可能还没滚到，也可能本页无勾选区）
            continue
        out.append(name)
    return out


def _bump_attempt(run: _Run, name: str, limit: int = 2) -> bool:
    """记一次尝试；超过 limit 就拉黑并返回 True（表示"该放弃了"）。"""
    run.attempts[name] = run.attempts.get(name, 0) + 1
    if run.attempts[name] > limit:
        run.skip.add(name)
        return True
    return False


# --------------------------------------------------------------------------- #
# 动作辅助
# --------------------------------------------------------------------------- #

def _tap_node(ctx: Any, node: Node, *, why: str, jitter: bool = False) -> StepResult:
    """按已定位到的节点精确点击（用坐标，避免二次匹配到别的同名节点）。"""
    x, y = node.center
    if jitter:
        x, y = ctx.humanizer.offset(x, y, bounds=node.bounds)
    act = Action("tap_xy", f"{x},{y}", note=why)
    return ctx.do(act)


def _find_text(snap: Snapshot, words: tuple[str, ...], *, exact_first: bool = True) -> Node | None:
    if exact_first:
        for w in words:
            for n in snap.nodes:
                if (n.text or "").strip() == w and n.bounds != (0, 0, 0, 0):
                    return n
    for w in words:
        for n in snap.nodes:
            if w in (n.text or "") and n.bounds != (0, 0, 0, 0):
                return n
    return None


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

def s_picker(run: _Run, ctx: Any, snap: Snapshot) -> StepResult:
    """
    选座面板，三阶段，一次 tick 只推进一步。

    为什么必须分阶段：一次 tick 里连点三下（票档→加数量→确认），中间屏幕已经变了，
    后两下会点在旧坐标上。分阶段让每一步之间都重新感知一次。
    """
    cfg = run.cfg

    # ---- stage 0：选票档 ----
    if run.stage == 0:
        t = pick_tier(snap, cfg, run.tried)
        if t is None:
            if not available_tiers(snap):
                return StepResult(False, Action("none", note="wait_restock"),
                                  "全部票档缺货（猫眼有余票回流，等下一轮）")
            run.tried.clear()
            return StepResult(False, Action("none", note="price_exhausted"),
                              f"目标价 {cfg.price or '不限'} 的档都试过了，重置重试")
        run.tried.add(t.price)
        run.stage = 1
        return _tap_node(ctx, t.node, why=f"tier:{t.label}", jitter=True)

    # ---- stage 1：数量 ----
    if run.stage == 1:
        cur = qty_value(snap)
        if cur is None:
            run.stage = 2
            return StepResult(True, Action("none", note="no_qty_widget"), "没有数量控件，跳过加数量")
        if run.qty_base is None:
            run.qty_base = cur
        # 屏幕上读到的值可能还没跟上已经点过的"+"，所以两个来源取大的
        effective = max(cur, run.qty_base + run.qty_bumped)
        if effective >= max(1, cfg.qty):
            run.stage = 2
            return StepResult(True, Action("none", note="qty_ready"), f"数量已是 {effective} 份")
        p = plus_button(snap)
        if p is None:
            run.stage = 2
            return StepResult(False, Action("none", note="no_plus"), "找不到 + 按钮，按当前数量继续")
        run.qty_bumped += 1
        return _tap_node(ctx, p, why=f"qty:{effective}->{effective + 1}")

    # ---- stage 2：确认 ----
    ok = _find_text(snap, KW["confirm"])
    run.stage = 0
    if ok is None:
        # 兜底：原脚本在 1080 宽屏上用的坐标
        act = Action("tap_xy", f"{cfg.confirm_xy[0]},{cfg.confirm_xy[1]}", note="confirm_xy")
        return ctx.do(act)
    return _tap_node(ctx, ok, why="confirm", jitter=True)


def s_viewer(run: _Run, ctx: Any, snap: Snapshot) -> StepResult:
    """勾观演人。一 tick 勾一个，勾完自动降级到 SUBMIT。"""
    cfg = run.cfg
    pend = pending_viewers(snap, run)
    if not pend:
        return StepResult(True, Action("none", note="viewers_ready"), "观演人已就绪")

    name = pend[0]
    vn, box, state = _check_state(snap, name, cfg)

    if _bump_attempt(run, name):
        ctx.log(f"[MAOYAN] 观演人 {name} 勾选失败超过 2 次，跳过（抢到后手动补）", level="warn")
        return StepResult(False, Action("none", note="viewer_skip"), f"{name} 勾不上，跳过")

    if vn is None:
        return StepResult(False, Action("none", note="viewer_missing"), f"屏幕上找不到 {name}")

    if state == "unchecked" and box is not None:
        return _tap_node(ctx, box, why=f"viewer:{name}")

    if state == "unknown":
        # 认不出勾选框：点姓名整行（多数版本整行可点），点两次不行就跳过
        ctx.log(f"[MAOYAN] {name} 的勾选框哈希认不出，改为点姓名行", level="warn")
        return _tap_node(ctx, vn, why=f"viewer_row:{name}", jitter=True)

    # 理论上到不了这里（pending 里不会有 checked）
    return StepResult(True, Action("none", note="viewer_noop"), f"{name} 状态 {state}")


def s_submit(ctx: Any, snap: Snapshot) -> StepResult:
    """订单页提交。优先点 android.widget.Button（猫眼的提交/支付按钮就是这个类）。"""
    btn = None
    for n in snap.nodes:
        if "Button" in (n.cls or "") and (n.text or "").strip() and n.bounds != (0, 0, 0, 0):
            btn = n
            break
    if btn is None:
        btn = _find_text(snap, KW["submit"])
    if btn is None:
        return StepResult(False, Action("none", note="no_submit_btn"), "订单页没找到提交按钮")
    return _tap_node(ctx, btn, why="submit", jitter=True)


def s_session(run: _Run, ctx: Any, snap: Snapshot) -> StepResult:
    cfg = run.cfg
    if cfg.session:
        n = _find_text(snap, (cfg.session,))
        if n is not None:
            return _tap_node(ctx, n, why=f"session:{cfg.session}", jitter=True)
    return StepResult(False, Action("none", note="session_not_found"), f"找不到场次 {cfg.session}")


def s_detail(run: _Run, ctx: Any, snap: Snapshot) -> StepResult:
    """还在详情页：点入口。"""
    cfg = run.cfg
    n = _find_text(snap, KW["entry"])
    if n is not None:
        return _tap_node(ctx, n, why="entry", jitter=True)
    if snap.has_any(KW["sold_out_entry"]):
        return StepResult(False, Action("none", note="sold_out"), "详情页只剩缺货登记")
    # 认不出入口就按配置坐标点（盲点）
    return ctx.do(Action("tap_xy", f"{cfg.entry_xy[0]},{cfg.entry_xy[1]}", note="entry_xy"))


def s_queue(ctx: Any, snap: Snapshot) -> StepResult:
    """排队页：等，不要连点（连点只会被判定为脚本）。"""
    ctx.sleep(0.25)
    return StepResult(True, Action("none", note="queue"), "排队中，等待放行")


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #

def _is_pay(snap: Snapshot) -> bool:
    """
    只有收银台特征才算抢到。

    注意不能把"立即支付"算进来 —— 猫眼订单确认页上的按钮就叫"立即支付"，
    把它当成功会导致"还没提交就宣布胜利"，整轮提前收摊。
    """
    return snap.has_any(KW["pay"])


def _is_picker(snap: Snapshot) -> bool:
    """
    必须同时满足"有选座面板标记"和"有票档"，否则详情页的"¥380起"会被误判成选座页。
    """
    if not snap.has_any(KW["picker"]):
        return False
    return bool(tiers(snap))


def _is_viewer(snap: Snapshot, run: _Run) -> bool:
    cfg = run.cfg
    if cfg.prebooked or not cfg.viewers:
        return False
    if not snap.has_any(KW["viewer"]) and not snap.has_any(KW["submit"]):
        # 观演人区可能在订单页底部弹层里，页面标题不一定带"观演人"
        if not any((n.text or "").strip() == v for v in cfg.viewers for n in snap.nodes):
            return False
    return bool(pending_viewers(snap, run))


def build_states(cfg: MaoyanConfig) -> list[State]:
    """
    顺序 = 优先级。猫眼的 VIEWER 必须排在 SUBMIT 之前：
    确认之后的订单页同时有"观演人勾选区"和"提交按钮"，先勾人再提交。
    """
    run = _Run(cfg)

    def _picker(ctx: Any, snap: Snapshot) -> StepResult:
        return s_picker(run, ctx, snap)

    def _viewer(ctx: Any, snap: Snapshot) -> StepResult:
        return s_viewer(run, ctx, snap)

    def _session(ctx: Any, snap: Snapshot) -> StepResult:
        return s_session(run, ctx, snap)

    def _detail(ctx: Any, snap: Snapshot) -> StepResult:
        return s_detail(run, ctx, snap)

    return [
        State("PAY", _is_pay, None, max_hits=1, allow_repeat=True,
              note="收银台已出现 = 抢到"),
        State("QUEUE", lambda s: s.has_any(KW["queue"]), s_queue, max_hits=40,
              note="排队/限流页"),
        State("CAPTCHA", lambda s: s.has_any(KW["captcha"]), None, max_hits=1, allow_repeat=True,
              note="滑块/验证码，必须人工"),
        State("RISK", lambda s: s.has_any(KW["risk"]), None, max_hits=1, allow_repeat=True,
              note="设备或账号被风控"),
        State("VIEWER", lambda s: _is_viewer(s, run), _viewer, max_hits=10,
              note="订单页勾观演人"),
        State("SUBMIT", lambda s: s.has_any(KW["submit"]) or _has_button(s), s_submit, max_hits=8,
              note="订单页提交"),
        State("PICKER", _is_picker, _picker, max_hits=16,
              note="选座面板：票档/数量/确认"),
        State("SESSION", lambda s: s.has_any(KW["session"]) and not _is_picker(s), _session,
              max_hits=4, note="场次选择"),
        State("DETAIL", lambda s: s.has_any(KW["entry"]) or s.has_any(KW["sold_out_entry"]),
              _detail, max_hits=3, note="演出详情页"),
    ]


def _has_button(snap: Snapshot) -> bool:
    return any("Button" in (n.cls or "") and (n.text or "").strip() for n in snap.nodes)


# 插件契约第 1 条：平台层只认 config_class，不认具体类名
config_class = MaoyanConfig


def build_machine(cfg: MaoyanConfig) -> Machine:
    return Machine(build_states(cfg), TERMINALS)


# --------------------------------------------------------------------------- #
# 快通道：盲点连点入口
# --------------------------------------------------------------------------- #

def blind_entry_burst(ctx: Any, cfg: MaoyanConfig) -> StepResult:
    """
    开售瞬间不做感知，直接按坐标连点入口。

    猫眼的入口按钮是原生 TextView，理论上 dump 得动 —— 但 dump 一次往返 300~800ms，
    开售那一秒根本来不及，所以仍然走盲点。点了没反应说明还没放票，继续点。
    """
    dev = ctx.device
    x, y = cfg.entry_xy
    n = max(1, int(cfg.burst))
    gap = max(0.0, cfg.burst_gap_ms) / 1000.0

    probe0 = None
    try:
        probe0 = dev.cheap_probe()
    except Exception:
        probe0 = None

    fired = 0
    for i in range(n):
        if ctx.stop_event.is_set():
            return StepResult(True, Action("tap_xy", f"{x},{y}", note="burst"),
                              f"被其它设备抢占，第 {fired} 次后停手")
        try:
            dev.tap(x, y)
            fired += 1
        except Exception as e:
            return StepResult(False, Action("tap_xy", f"{x},{y}", note="burst"),
                              f"第 {i + 1} 次点击失败: {type(e).__name__}: {e}")
        if probe0 is not None and (i + 1) % 3 == 0:
            try:
                now_probe = dev.cheap_probe()
            except Exception:
                now_probe = probe0
            if now_probe != probe0:
                ctx.action_count = getattr(ctx, "action_count", 0) + fired
                return StepResult(True, Action("tap_xy", f"{x},{y}", note="burst"),
                                  f"blind hit at #{i + 1}（屏幕已变 {probe0}→{now_probe}）")
        if gap:
            time.sleep(gap)

    ctx.action_count = getattr(ctx, "action_count", 0) + fired
    tail = "无探针，交状态机接管" if probe0 is None else "屏幕未变"
    return StepResult(True, Action("tap_xy", f"{x},{y}", note="burst"),
                      f"burst {fired} 次打完（{tail}）")


# --------------------------------------------------------------------------- #
# 预检 / 描述
# --------------------------------------------------------------------------- #

def preflight_check(dev: Any, cfg: MaoyanConfig, observer: Any) -> tuple[bool, str]:
    """
    开售前 30 秒的体检：还在详情页吗？有没有弹窗盖着？是不是已经被限流了？
    任何一条不过都意味着"发射的时候点的是错的坐标"，所以要提前发现。
    """
    try:
        snap = dev.dump()
    except Exception as e:
        return False, f"dump 失败: {type(e).__name__}: {e}"

    if snap.cost_ms > 250 and observer is not None:
        observer.note(f"[MAOYAN] 感知耗时 {snap.cost_ms:.0f}ms，已退化成主机侧 dump", level="warn")

    if snap.has_any(KW["queue"]):
        return False, "已经在排队/限流页"
    if snap.has_any(KW["captcha"]):
        return False, "已经在验证页"

    # 弹窗遮挡：有"我知道了"且没有入口按钮 → 被盖住了
    if snap.has("我知道了") and not snap.has_any(KW["entry"]):
        return False, "有弹窗遮挡（我知道了）"

    if snap.has_any(KW["entry"]):
        return True, "详情页，入口可见"

    if snap.has_any(KW["sold_out_entry"]):
        return False, "详情页但只有缺货登记（未放票或无票）"

    if snap.has_any(("数量", "选择观演人")):
        return False, "已经进到选座/订单页，需要先退回详情页"

    if snap.has_any(("场次",)):
        return False, "在场次选择页，需要先退回详情页"

    head = " / ".join((snap.texts() or [])[:8])
    return False, f"不在演出详情页；当前屏幕头部文本: {head[:120]}"


def describe(cfg: MaoyanConfig) -> str:
    parts = [f"猫眼: {cfg.演出 or '-'}", f"场次 {cfg.session or '-'}"]
    parts.append(f"票价 {cfg.price or '最低'}" + (f"(≤{cfg.max_price})" if cfg.max_price else ""))
    parts.append(f"{cfg.qty} 份")
    if cfg.viewers:
        parts.append("观演人 " + "/".join(cfg.viewers))
    if cfg.prebooked:
        parts.append("已预约(跳过勾观演人)")
    parts.append(f"入口@{cfg.entry_xy[0]},{cfg.entry_xy[1]} burst={cfg.burst}")
    return " | ".join(parts)
