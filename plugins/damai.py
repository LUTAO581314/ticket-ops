"""
大麦场景插件 — 把"人怎么抢票"翻译成状态机。

这里的状态顺序 = 优先级 = 策略。顺序不是随便排的：
  先认终态（抢到了就立刻停手，别多点一下把订单取消掉）
  再认拦截（被限流了要退让，硬刚只会把 IP 关小黑屋）
  再认快态（提交订单/观演人/票档/场次，从流程末端往前端排，避免"提交订单"页被误判成详情页）
  最后才是入口（立即购买）

实测控件（Android 大麦 v8.x，不同版本会有变动，全部集中在 KEYWORDS 里好改）：
    id("tv_price_name")          票档名
    同级 child(1).child(0)        余票状态，"无票" 表示该档售罄
    text("立即购买") / "立即抢购" / "缺货登记" / "立即预订" / "特惠抢票"
    text("提交订单")              选座/确认页
    text("去支付") / "立即支付"    下单成功页
    text("哎呦喂，被挤爆啦")       排队限流（不是失败！等它自己散）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.fsm import (
    Action,
    Context,
    Machine,
    Node,
    Snapshot,
    State,
    StepResult,
    detect_any,
)

# --------------------------------------------------------------------------- #
# 关键词表 —— 改版本就改这里
# --------------------------------------------------------------------------- #

KW = {
    "buy": ("立即购买", "立即抢购", "特惠抢票", "立即预订", "预约抢票"),
    "sold_out_entry": ("缺货登记", "已售罄", "暂无余票"),
    "submit": ("提交订单", "确认订单", "立即提交"),
    "pay": ("去支付", "立即支付", "确认支付", "订单提交成功", "支付订单"),
    "viewer": ("观演人", "选择观演人", "实名信息", "购票人"),
    "session": ("选择场次", "场次", "选择日期"),
    "sku": ("选择票档", "票档", "选择价格", "票档信息"),
    "qty": ("购买数量", "数量", "+"),
    "queue": ("被挤爆", "人数过多", "排队中", "系统繁忙", "稍后再试", "小主", "努力加载"),
    "captcha": ("滑块", "拖动", "验证码", "请完成验证", "按住"),
    "risk": ("环境异常", "操作频繁", "账号异常", "设备异常", "请求异常"),
    "answer": ("请回答", "答题", "以下哪个", "请选择正确"),
    "price_name_id": "tv_price_name",
    "no_ticket": "无票",
}

DEFAULT_ENTRY_XY = (540, 2100)  # 详情页底部"立即购买"大致位置；务必按自己机型改


@dataclass
class DamaiConfig:
    """一个抢票任务在大麦侧的参数。"""

    item_id: str = ""
    # 目标场次：匹配页面文本（如 "06-01" "19:30" "2026-06-01"）；留空=第一个可选
    session: str = ""
    # 目标票档：按名称子串匹配（如 "看台580" "内场"）；留空=第一个有票的
    sku: str = ""
    qty: int = 1
    # 观演人姓名（在观演人页按名字勾选）；留空=页面默认
    viewers: list[str] = field(default_factory=list)
    # 盲点入口坐标（x,y）。填了就走"零感知快通道"：开售瞬间连点这里，不做 UI dump
    entry_xy: tuple[int, int] = DEFAULT_ENTRY_XY
    # 盲点连点次数与间隔
    burst: int = 12
    burst_gap_ms: float = 8.0
    # 允许在"无票"的情况下继续尝试别的档（大麦会有人退票，捡漏靠这个）
    allow_fallback_sku: bool = True
    # 排队限流时最多重试几次
    queue_retries: int = 6

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DamaiConfig":
        d = dict(d or {})
        if "entry_xy" in d and isinstance(d["entry_xy"], (list, tuple)):
            d["entry_xy"] = tuple(d["entry_xy"])  # type: ignore[assignment]
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------- #
# 动作实现
# --------------------------------------------------------------------------- #

def _click_text(ctx: Context, snap: Snapshot, keywords: tuple[str, ...], *, prefer_clickable: bool = True) -> StepResult:
    """在快照里找第一个命中关键词的节点并点击。返回 StepResult。"""
    for kw in keywords:
        cands = snap.find(kw, clickable_only=prefer_clickable) or snap.find(kw)
        for n in cands:
            if n.bounds == (0, 0, 0, 0):
                continue
            act = Action(kind="tap_node", target=kw, note=n.text[:24])
            return ctx.do(act)
    return StepResult(ok=False, action=Action(kind="tap_node", target=keywords[0]), detail="未找到可点节点")


def _sku_rows(snap: Snapshot) -> list[tuple[Node, str, bool]]:
    """
    解析票档列表。返回 [(票档节点, 名称, 是否售罄)]。
    两种布局都兼容：
      A) 节点本身 text 是票档名（"看台580"），兄弟节点里有"无票"
      B) 节点 res_id 命中 tv_price_name，名字在 text，状态在 children_text
    """
    rows: list[tuple[Node, str, bool]] = []
    for n in snap.nodes:
        name = (n.text or "").strip()
        is_price_node = KW["price_name_id"] in n.res_id or "price" in n.res_id.lower()
        if not (is_price_node or ("元" in name or "看台" in name or "内场" in name or "票" in name)):
            continue
        if not name or n.bounds == (0, 0, 0, 0):
            continue
        hay = n.haystack
        sold = KW["no_ticket"] in hay or "缺货" in hay or "售罄" in hay
        rows.append((n, name, sold))
    return rows


def _pick_sku(snap: Snapshot, want: str, allow_fallback: bool) -> tuple[Node, str] | None:
    rows = _sku_rows(snap)
    if not rows:
        return None
    if want:
        hit = [(n, name) for n, name, sold in rows if want in name and not sold]
        if hit:
            return hit[0]
        if allow_fallback:
            # 指定档没票 → 同价位段里找有票的（"580" 这类数字子串匹配）
            import re

            digits = re.findall(r"\d{2,4}", want)
            if digits:
                for n, name, sold in rows:
                    if not sold and any(d in name for d in digits):
                        return n, name
        return None
    for n, name, sold in rows:
        if not sold:
            return n, name
    return None


# --------------------------------------------------------------------------- #
# 状态定义
# --------------------------------------------------------------------------- #

def build_states(cfg: DamaiConfig) -> list[State]:
    """按优先级构造状态表。"""

    def s_pay(ctx: Context, snap: Snapshot) -> StepResult:
        # 走到这里说明订单已经提交成功，等人付款。不再做任何点击（多点会把订单搞没）
        ctx.result("success", "订单已提交，等待支付")
        return StepResult(ok=True, action=Action(kind="none", note="wait_pay"), detail="订单已提交")

    def s_queue(ctx: Context, snap: Snapshot) -> StepResult:
        ctx.log("[queue] 命中排队限流，退让重试", level="warn")
        return StepResult(ok=True, action=Action(kind="wait", note="queue_backoff"), detail="排队中")

    def s_captcha(ctx: Context, snap: Snapshot) -> StepResult:
        ctx.result("captcha", "需要人工过滑块/验证码")
        return StepResult(ok=False, action=Action(kind="none", note="captcha"), detail="等待人工验证")

    def s_risk(ctx: Context, snap: Snapshot) -> StepResult:
        ctx.result("risk_blocked", "设备/账号被判风险")
        return StepResult(ok=False, action=Action(kind="none", note="risk"), detail="风控拦截")

    def s_answer(ctx: Context, snap: Snapshot) -> StepResult:
        ctx.result("answer_required", "出现答题")
        return StepResult(ok=False, action=Action(kind="none", note="answer"), detail="答题")

    def s_submit(ctx: Context, snap: Snapshot) -> StepResult:
        res = _click_text(ctx, snap, KW["submit"])
        if res.detail.startswith("未找到"):
            # 提交订单按钮常常在屏幕底部且不可点（被遮挡），退化成坐标点底栏
            return ctx.do(Action(kind="tap_xy", target="540,2280", note="submit_fallback"))
        return res

    def s_picker(ctx: Context, snap: Snapshot) -> StepResult:
        """
        选座面板（票档 → 数量 → 观演人 → 确定）。

        刻意做成"一次 tick 只推进一步"：每步之间 Machine 会重新 dump 一次。
        开售瞬间的毫秒之争在"详情页 → 选座页"那一下，已经由 blind_entry_burst 解决；
        进了选座页之后是几百 ms 级的活，值得为每一步付一次感知成本，换"绝不点错"。
        阶段记在 ctx 上（`_dm_stage` 等），页面真的换了就由 detect 自然失配而重置。
        """
        stage = getattr(ctx, "_dm_stage", 0)

        if stage == 0:  # 选票档
            setattr(ctx, "_dm_stage", 1)
            setattr(ctx, "_dm_qty", 1)
            picked = _pick_sku(snap, cfg.sku, cfg.allow_fallback_sku)
            if not picked:
                return StepResult(ok=True, action=Action(kind="none", note="no_sku_rows"),
                                  detail="本屏无票档行，跳到数量")
            node, name = picked
            return ctx.do(Action(kind="tap_node", target=name, note=f"选票档 {name}"))

        if stage == 1:  # 数量
            want = max(1, cfg.qty)
            got = getattr(ctx, "_dm_qty", 1)
            if got < want:
                plus = snap.first("+", contains=False)
                if plus and plus.bounds != (0, 0, 0, 0):
                    setattr(ctx, "_dm_qty", got + 1)
                    return ctx.do(Action(kind="tap_node", target="+", note=f"加数量 {got + 1}/{want}"))
            setattr(ctx, "_dm_stage", 2)
            return StepResult(ok=True, action=Action(kind="none", note="qty_ready"),
                              detail=f"数量 {got}/{want}")

        if stage == 2:  # 观演人（一 tick 勾一个，逐个确认）
            done = getattr(ctx, "_dm_viewers", set())
            for name in cfg.viewers:
                if name in done:
                    continue
                for n in snap.find(name, contains=True):
                    if n.clickable and n.bounds != (0, 0, 0, 0):
                        setattr(ctx, "_dm_viewers", done | {name})
                        return ctx.do(Action(kind="tap_node", target=name, note=f"勾观演人 {name}"))
            setattr(ctx, "_dm_stage", 3)
            return StepResult(ok=True, action=Action(kind="none", note="viewer_ready"),
                              detail=f"观演人就绪 {len(done)}/{len(cfg.viewers)}")

        # stage >= 3：确定。点到了就重置阶段，因为后面可能还有第二层面板（选座图）
        res = _click_text(ctx, snap, ("确定", "下一步", "选好了", "确认", "去结算"))
        setattr(ctx, "_dm_stage", 0)
        setattr(ctx, "_dm_viewers", set())
        if not res.ok:
            return StepResult(ok=False, action=Action(kind="none", note="no_confirm"),
                              detail="找不到确定按钮")
        return res

    def s_session(ctx: Context, snap: Snapshot) -> StepResult:
        if cfg.session:
            for n in snap.find(cfg.session):
                if n.clickable and n.bounds != (0, 0, 0, 0):
                    ctx.do(Action(kind="tap_node", target=cfg.session, note="选场次"))
                    return _click_text(ctx, snap, ("确定", "下一步", "选好了"))
        # 没指定/没匹配到 → 点第一个可点的场次
        return _click_text(ctx, snap, ("确定", "下一步", "选好了", "立即购买"))

    def s_detail(ctx: Context, snap: Snapshot) -> StepResult:
        # 详情页：判断是"可买"还是"缺货登记"
        if snap.has_any(KW["buy"]):
            res = _click_text(ctx, snap, KW["buy"])
            if res.detail.startswith("未找到") and cfg.entry_xy:
                return ctx.do(Action(kind="tap_xy", target=f"{cfg.entry_xy[0]},{cfg.entry_xy[1]}", note="blind_entry"))
            return res
        ctx.result("sold_out", "详情页无购买入口（缺货登记/已售罄）")
        return StepResult(ok=False, action=Action(kind="none", note="sold_out"), detail="缺货登记")

    return [
        # ---- 终态 ----
        State("PAY", detect_any(*KW["pay"]), s_pay, max_hits=99, note="订单已提交"),
        # ---- 拦截 ----
        State("QUEUE", detect_any(*KW["queue"]), s_queue, max_hits=cfg.queue_retries + 2, timeout_s=0),
        State("CAPTCHA", detect_any(*KW["captcha"]), s_captcha, max_hits=1),
        State("RISK", detect_any(*KW["risk"]), s_risk, max_hits=1),
        State("ANSWER", detect_any(*KW["answer"]), s_answer, max_hits=1),
        # ---- 流程（末端 → 前端）----
        State("SUBMIT", detect_any(*KW["submit"]), s_submit, max_hits=4),
        State(
            "PICKER",
            lambda s: bool(_sku_rows(s)) or s.has_any(("选择观演人", "观演人", "实名信息")),
            s_picker,
            max_hits=22,
            note="选座面板：票档/数量/观演人/确定",
        ),
        State("SESSION", detect_any(*KW["session"]), s_session, max_hits=3),
        # ---- 入口 ----
        State(
            "DETAIL",
            lambda s: s.has_any(KW["buy"]) or s.has_any(KW["sold_out_entry"]),
            s_detail,
            max_hits=6,
        ),
    ]


TERMINALS = {"PAY", "CAPTCHA", "RISK", "ANSWER"}

# 插件契约第 1 条：平台层只认 config_class，不认具体类名
config_class = DamaiConfig


def build_machine(cfg: DamaiConfig, ctx_log=None) -> Machine:
    return Machine(build_states(cfg), terminals=TERMINALS)


# --------------------------------------------------------------------------- #
# 快通道：开售瞬间的盲点连点
# --------------------------------------------------------------------------- #

def blind_entry_burst(ctx: Context, cfg: DamaiConfig) -> StepResult:
    """
    零感知快通道。开售 T-55ms 起，对着"立即购买"的位置连点 burst 次。

    为什么不做 UI dump：主机侧 dump 一次 300~800ms，开售那 200ms 里一次都来不及。
    为什么盲点也行：详情页布局在开售前后不变，"立即购买"按钮位置是固定的。
    什么时候不该用：页面有弹窗（开售提醒/更新提示/优惠券）时盲点会点到弹窗上 → 先做一次
    预检（preflight_check），确认在详情页且无弹窗再进快通道。
    """
    x, y = cfg.entry_xy
    dev = getattr(ctx, "device", None)
    probe0 = dev.cheap_probe() if dev is not None and hasattr(dev, "cheap_probe") else None
    worst = 0.0
    for i in range(cfg.burst):
        r = ctx.do(Action(kind="tap_xy", target=f"{x},{y}", note=f"burst#{i + 1}"))
        worst = max(worst, r.cost_ms)
        # 只在设备给得出廉价探针时才判断跳转（MockDevice 能，真机 ADB 不能）。
        # 真机上这里必然拿不到信号 → 就老实把 burst 打完，然后立刻交棒给状态机；
        # 状态机的第一次 dump 才是"探路"，那一次成本必须付。
        if probe0 is not None and dev is not None and (i + 1) % 3 == 0:
            now_probe = dev.cheap_probe()
            if now_probe and now_probe != probe0:
                return StepResult(
                    ok=True, action=Action(kind="tap_xy"),
                    detail=f"blind hit at #{i + 1}（屏幕已变 {probe0}→{now_probe}）",
                )
        if cfg.burst_gap_ms > 0:
            ctx.sleep(cfg.burst_gap_ms / 1000.0)  # type: ignore[attr-defined]
    return StepResult(ok=False, action=Action(kind="tap_xy"),
                      detail=f"burst {cfg.burst} 次打完（无探针，交状态机接管）", cost_ms=worst)


def preflight_check(dev, cfg: DamaiConfig, observer=None) -> tuple[bool, str]:
    """
    开售前的预检：必须停在详情页、没有弹窗、票档可见。
    这一步用慢速 uiautomator 也没关系——它在开售前完成。
    """
    try:
        snap = dev.dump()
    except Exception as e:
        return False, f"dump 失败: {e!r}"

    if not snap.nodes:
        return False, "页面为空（未打开大麦详情页？）"

    text = "\n".join(snap.texts())
    if any(k in text for k in ("立即更新", "新版本", "用户协议", "我知道了", "允许")):
        return False, "有弹窗遮挡，先手动关掉"
    if any(k in text for k in KW["queue"]):
        return False, "已经在排队/限流状态，说明不在详情页或已被拦截"
    if not (any(k in text for k in KW["buy"]) or any(k in text for k in KW["sold_out_entry"])):
        return False, "不在详情页（找不到购买/缺货入口）"
    if cfg.item_id and cfg.item_id not in text:
        # item_id 不一定会出现在屏幕文本里，只做提示不拦
        if observer:
            observer.note(f"提示：页面文本里没看到 item_id={cfg.item_id}，确认打开的是正确演出")
    return True, "预检通过"


def describe(cfg: DamaiConfig) -> str:
    return (
        f"大麦 item={cfg.item_id or '(未设)'} 场次={cfg.session or '第一个'} "
        f"票档={cfg.sku or '第一个有票'} ×{cfg.qty} 观演人={cfg.viewers or '默认'} "
        f"盲点={cfg.entry_xy} burst={cfg.burst}×{cfg.burst_gap_ms}ms"
    )
