/**
 * autox-speed.js — 设备内高速执行器（AutoX.js）
 *
 * 为什么要有这一份：主机侧 `uiautomator dump` 往返 300~800ms，
 * 而开售那 200ms 里一次 dump 都来不及。所以"看"这件事必须在手机内部完成——
 * AutoX.js 走 AccessibilityService，拿控件信息是单帧级（几毫秒）。
 *
 * 职责划分：
 *   主机（Python）  = 决策、调度、校时、看板、失败归因
 *   本脚本（手机内） = 感知 + 点击 + 毫秒级倒计时 + 上报
 *
 * 用法：
 *   1. 手机装 AutoX.js，把本文件放进脚本目录
 *   2. 主机起 `python main.py serve`（默认 8770）
 *   3. 手机上编辑 CONFIG.host 为你的电脑内网 IP，然后运行本脚本
 *   4. 看板上会看到这台设备变 idle，然后就可以派任务了
 *
 * [!] 无障碍服务在抢票窗口前 10 秒才开（CONFIG.openA11yBeforeSec），
 *   长期开着无障碍是风控最廉价的特征之一。
 */

var CONFIG = {
    host: "192.168.1.100:8770",   // ← 改成你的电脑 IP
    serial: device.serial || "autox-1",
    heartbeatMs: 1000,
    a11yWarmupSec: 10,            // 开售前多少秒打开无障碍
    burstGapMs: 8,                // 盲点连点间隔
};

var http_ = require("http");
var BASE = "http://" + CONFIG.host;

// ------------------------------------------------------------------ //
// 通信
// ------------------------------------------------------------------ //

function post(path, obj) {
    try {
        var r = http_.postJson(BASE + path, obj, { timeout: 2500 });
        return r && r.statusCode === 200 ? r.body.json() : null;
    } catch (e) {
        return null;
    }
}

function get(path) {
    try {
        var r = http_.get(BASE + path, { timeout: 2500 });
        return r && r.statusCode === 200 ? r.body.json() : null;
    } catch (e) {
        return null;
    }
}

function report(kind, extra) {
    var o = { serial: CONFIG.serial, kind: kind, ts: Date.now() / 1000 };
    if (extra) for (var k in extra) o[k] = extra[k];
    post("/agent/event", o);
}

// ------------------------------------------------------------------ //
// 感知（设备内，单帧级）
// ------------------------------------------------------------------ //

/** 拿当前屏幕所有可见节点的文本 + 位置，几十毫秒 */
function snapTexts() {
    var out = [];
    var cls = ["android.widget.TextView", "android.widget.Button", "android.view.View", "android.widget.ImageView"];
    for (var i = 0; i < cls.length; i++) {
        var nodes = className(cls[i]).find();
        for (var j = 0; j < nodes.length; j++) {
            var n = nodes[j];
            var t = n.text() || n.desc() || "";
            if (!t) continue;
            var b = n.bounds();
            if (!b || b.width() <= 0) continue;
            out.push({ t: t, x: b.centerX(), y: b.centerY(), w: b.width(), h: b.height() });
        }
    }
    return out;
}

function hasAny(keywords) {
    var ts = snapTexts();
    for (var i = 0; i < ts.length; i++) {
        for (var j = 0; j < keywords.length; j++) {
            if (ts[i].t.indexOf(keywords[j]) >= 0) return ts[i];
        }
    }
    return null;
}

function clickText(kw, timeoutMs) {
    var o = textContains(kw).findOne(timeoutMs || 300);
    if (o) { var b = o.bounds(); return click(b.centerX(), b.centerY()); }
    return false;
}

function clickId(idName, timeoutMs) {
    var o = id(idName).findOne(timeoutMs || 300);
    if (o) { var b = o.bounds(); return click(b.centerX(), b.centerY()); }
    return false;
}

// ------------------------------------------------------------------ //
// 状态判定（与大麦插件的 KW 表保持一致）
// ------------------------------------------------------------------ //

var KW = {
    pay: ["去支付", "立即支付", "确认支付", "订单提交成功"],
    submit: ["提交订单", "确认订单"],
    viewer: ["观演人", "选择观演人", "实名信息"],
    sku: ["选择票档", "票档", "选择价格"],
    session: ["选择场次", "场次"],
    buy: ["立即购买", "立即抢购", "特惠抢票", "立即预订"],
    soldout: ["缺货登记", "已售罄", "暂无余票"],
    queue: ["被挤爆", "人数过多", "排队中", "系统繁忙", "稍后再试", "努力加载"],
    captcha: ["滑块", "拖动", "验证码", "请完成验证"],
    risk: ["环境异常", "操作频繁", "账号异常", "设备异常"],
    answer: ["请回答", "答题", "以下哪个"],
};

function classify() {
    var t = snapTexts();
    var joined = "";
    for (var i = 0; i < t.length; i++) joined += t[i].t + "\n";

    function has(list) {
        for (var i = 0; i < list.length; i++) if (joined.indexOf(list[i]) >= 0) return list[i];
        return null;
    }
    if (has(KW.pay)) return { state: "PAY", hit: has(KW.pay) };
    if (has(KW.queue)) return { state: "QUEUE", hit: has(KW.queue) };
    if (has(KW.captcha)) return { state: "CAPTCHA", hit: has(KW.captcha) };
    if (has(KW.risk)) return { state: "RISK", hit: has(KW.risk) };
    if (has(KW.answer)) return { state: "ANSWER", hit: has(KW.answer) };
    if (has(KW.submit)) return { state: "SUBMIT", hit: has(KW.submit) };
    if (has(KW.viewer)) return { state: "VIEWER", hit: has(KW.viewer) };
    if (has(KW.sku)) return { state: "SKU", hit: has(KW.sku) };
    if (has(KW.session)) return { state: "SESSION", hit: has(KW.session) };
    if (has(KW.buy)) return { state: "DETAIL", hit: has(KW.buy) };
    if (has(KW.soldout)) return { state: "SOLD_OUT", hit: has(KW.soldout) };
    return { state: "UNKNOWN", hit: null };
}

// ------------------------------------------------------------------ //
// 抢票主流程
// ------------------------------------------------------------------ //

function fireBurst(cfg) {
    var xy = cfg.entry_xy || [540, 2100];
    var n = cfg.burst || 12;
    var gap = cfg.burst_gap_ms != null ? cfg.burst_gap_ms : CONFIG.burstGapMs;
    report("note", { detail: "盲点发射 x" + n + " @" + xy.join(",") });
    for (var i = 0; i < n; i++) {
        click(xy[0], xy[1]);
        if (gap > 0) sleep(gap);
        // 每 3 次探一次是否已跳进选座页（探一次 ~5ms，值得）
        if (i % 3 === 2) {
            var st = classify();
            if (st.state !== "DETAIL" && st.state !== "SOLD_OUT") {
                report("note", { detail: "第 " + (i + 1) + " 次点击已跳转 " + st.state });
                return true;
            }
        }
    }
    return false;
}

function runGrab(cfg) {
    report("note", { detail: "开始执行 item=" + (cfg.item_id || "?") });

    // ---- 等待开售（用主机给的时间戳换算成本地时钟）----
    var saleAt = cfg.sale_at;             // unix 秒
    var leadMs = cfg.lead_ms != null ? cfg.lead_ms : 55;
    if (saleAt) {
        var now = Date.now() / 1000;
        var waitMs = (saleAt - now) * 1000 - leadMs;
        if (waitMs > 0) {
            report("note", { detail: "蹲守 " + (waitMs / 1000).toFixed(2) + "s" });
            // 粗等（sleep 精度 ~15ms），留 200ms 给自旋
            if (waitMs > 400) sleep(Math.floor(waitMs - 200));
            while (Date.now() / 1000 < saleAt - leadMs / 1000) { /* 自旋 */ }
        }
    }

    fireBurst(cfg);

    // ---- 状态机推进 ----
    var deadline = Date.now() + 180000;
    var lastState = "";
    var stall = 0;

    while (Date.now() < deadline) {
        var st = classify();
        if (st.state !== lastState) {
            report("state", { state: st.state, detail: st.hit || "" });
            lastState = st.state;
            stall = 0;
        } else {
            stall++;
        }

        if (st.state === "PAY") {
            report("result", { outcome: "success", detail: "订单已提交" });
            return "success";
        }
        if (st.state === "CAPTCHA" || st.state === "RISK" || st.state === "ANSWER") {
            report("result", { outcome: "blocked", detail: st.state + ":" + (st.hit || "") });
            return "blocked";
        }
        if (st.state === "SOLD_OUT") {
            report("result", { outcome: "sold_out", detail: "无票" });
            return "sold_out";
        }
        if (stall > 40) {
            report("result", { outcome: "timeout", detail: "卡在 " + lastState });
            return "timeout";
        }

        // 各状态的推进动作
        if (st.state === "SUBMIT") {
            if (!clickText("提交订单", 200)) clickText("确认订单", 200);
            sleep(120);
        } else if (st.state === "VIEWER") {
            if (cfg.viewers) {
                for (var i = 0; i < cfg.viewers.length; i++) clickText(cfg.viewers[i], 150);
            }
            clickText("确定", 150) || clickText("下一步", 150);
            sleep(150);
        } else if (st.state === "SKU") {
            var picked = false;
            if (cfg.sku) picked = clickText(cfg.sku, 200);
            if (!picked) {
                // 点第一个非"无票"的票档
                var rows = id("tv_price_name").find();
                for (var r = 0; r < rows.length; r++) {
                    var b = rows[r].bounds();
                    var parentTxt = rows[r].parent() ? rows[r].parent().text() : "";
                    if (parentTxt.indexOf("无票") < 0 && parentTxt.indexOf("缺货") < 0) {
                        click(b.centerX(), b.centerY());
                        picked = true;
                        break;
                    }
                }
            }
            sleep(80);
            clickText("确定", 150) || clickText("下一步", 150) || clickText("选好了", 150);
            sleep(120);
        } else if (st.state === "SESSION") {
            if (cfg.session) clickText(cfg.session, 200);
            sleep(80);
            clickText("确定", 150) || clickText("下一步", 150) || clickText("立即购买", 150);
            sleep(120);
        } else if (st.state === "DETAIL") {
            clickText("立即购买", 200) || clickText("立即抢购", 200);
            sleep(150);
        } else if (st.state === "QUEUE") {
            // 限流不要狂点，退让
            sleep(600);
        } else {
            sleep(60);
        }
    }
    report("result", { outcome: "timeout", detail: "180s 硬截止" });
    return "timeout";
}

// ------------------------------------------------------------------ //
// 主循环：注册 → 心跳 → 拿任务 → 执行
// ------------------------------------------------------------------ //

function main() {
    report("note", { detail: "agent 上线 " + device.model + " / " + device.serial });
    toastLog("ticket-ops agent 已连接 " + CONFIG.host);

    while (true) {
        var job = get("/agent/config?serial=" + encodeURIComponent(CONFIG.serial));
        if (job && job.task && job.task.sale_at) {
            var cfg = job.task.config || {};
            cfg.sale_at = job.task.sale_at;
            cfg.lead_ms = job.task.lead_ms;

            // 抢票窗口才开无障碍
            var untilSale = cfg.sale_at - Date.now() / 1000;
            if (untilSale > CONFIG.a11yWarmupSec) {
                report("note", { detail: "距离开售 " + untilSale.toFixed(0) + "s，暂不启动无障碍" });
                sleep(Math.min(untilSale - CONFIG.a11yWarmupSec, 5) * 1000);
                continue;
            }
            report("note", { detail: "启动无障碍与执行" });
            if (!auto.service) { auto.waitFor(); }
            runGrab(cfg);
            sleep(1500);
            continue;
        }
        sleep(CONFIG.heartbeatMs);
    }
}

// 允许被主机以指令方式点（调试用）：/agent/poll
threads.start(function () {
    while (true) {
        var cmd = get("/agent/poll?serial=" + encodeURIComponent(CONFIG.serial));
        if (cmd && cmd.cmd) {
            var r = { serial: CONFIG.serial, kind: "cmd_result", cmd: cmd.cmd };
            try {
                if (cmd.cmd === "dump") {
                    r.nodes = snapTexts();
                } else if (cmd.cmd === "tap") {
                    click(cmd.x, cmd.y);
                } else if (cmd.cmd === "state") {
                    r.state = classify();
                }
            } catch (e) { r.error = String(e); }
            post("/agent/event", r);
        }
        sleep(60);
    }
});

main();
