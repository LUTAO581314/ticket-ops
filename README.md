# ticket-ops

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Android%208.0%2B-lightgrey)

Android 手机端自动化作业平台：**设备池调度 · 毫秒级时序对齐 · 可插拔场景状态机**。

> A device-side automation runtime for Android: pooled scheduling, multi-source clock alignment,
> and pluggable scene state machines. Ticketing is the first scene, not the system itself.

**抢票是第一个场景插件，不是这个系统本身。**

仓库里最不值钱的就是场景脚本——几百行代码、控件改一行就得改、谁都能抄。
真正稀缺的是另外三样，这个项目围绕它们搭：

1. **能过设备可信检测的真机**（大麦 2025-04 起接入金融级设备可信检测，模拟器/云手机/群控基本进不去）
2. **能过风控的实名账号**（一机一号一实名，账号本身就是资产）
3. **毫秒级对齐开售时刻的调度能力**（多源 NTP、提前量、多设备同时发射、抢到即停）

平台做的是把这三样的**利用率**拉到最高：设备池调度、中心校时、状态机作业、全链路观测。

---

## 1. 三十秒跑起来（不需要手机）

```bash
cd ticket-ops
python main.py dry                        # 大麦
python main.py dry --fixture fixture-maoyan.json   # 猫眼
```

它会用 fixture 模拟一台手机走完整个下单流程：
详情页 → 盲点连点进选座 → 选票档 → 加数量 → 勾观演人 → 确定 → 提交订单 → 订单成功。

输出里能看到每一步的耗时、动作、状态命中、最终结果：

```
[mock-01] - 快通道结果: blind hit at #3（屏幕已变 frame#0→frame#1）
  [mock-01] tap_node:看台580 - @271,860 (4ms)
  [mock-01] tap_node:提交订单 - @851,2252 (1ms)
  [mock-01] - 订单已提交
结果: success - 订单已提交
```

**这条路径能跑通，说明时序、状态机、分阶段选座、落库、观测全是好的。**
真机上出问题，就是"设备/控件/网络"这三个变量，不再有逻辑变量。

### 两个场景，一套引擎

| | 大麦 `plugins/damai.py` | 猫眼 `plugins/maoyan.py` |
|---|---|---|
| 控件树 | 详情页/选座页大量自绘，dump 拿不到按钮 | **标准原生控件**，能一路 dump |
| 票档识别 | `id=tv_price_name` + 同行"无票" | `¥数字`，缺货在**行容器子树**里 |
| 数量 | "数量 1" | `1份`，`+` 在同行最右 |
| 确认 | 确定 / 下一步 | **确认** |
| 观演人 | 点姓名行 | 勾选框是 `Image`，**勾选态编码在 text 的 base64 图片哈希里** |
| 流程 | 票档→数量→观演人→确定→提交 | 票档→数量→确认→**订单页勾观演人**→提交 |

两者共用同一套 clock / fsm / device / humanize / observer / store / scheduler，
平台层唯一认识的字符串是场景名（`load_scene(name)`），没有任何 `if scene == "maoyan"`。
加第三个场景只要放一个 `plugins/<name>.py` 满足五件套契约：

```
config_class                         带 from_dict(dict) 的配置类
describe(cfg) -> str                 一行任务摘要，进日志
preflight_check(dev, cfg, obs)       -> (bool, str)  开售前体检
blind_entry_burst(ctx, cfg)          -> StepResult   开售瞬间的盲点快通道
build_machine(cfg)                   -> Machine      状态机
```

约定：**所有场景的"抢到"终态都叫 `PAY`**，判定逻辑因此只写一次。

---

## 2. 接真机

### 2.1 装 adb

Windows 下载 [platform-tools](https://developer.android.com/studio/releases/platform-tools) 解压，比如 `C:\platform-tools\adb.exe`。

不想加 PATH 就每次带 `--adb`：

```bash
python main.py devices --adb C:/platform-tools/adb.exe
```

### 2.2 手机

1. 开发者选项 → **USB 调试** 打开（还要开"USB 安装"和"USB 调试(安全设置)"，用无障碍点屏幕必须要后者）
2. 插线，手机上点"允许 USB 调试"，勾"一律允许"
3. `python main.py devices` 应该看到 `device` 状态和机型

### 2.3 校时（这步别跳）

```bash
python main.py ntp
```

多源 NTP（阿里/腾讯/cn.ntp.org.cn/…）各采 3 轮，**offset 取中位数、抖动用 MAD**，剔掉离群源。

```
✓ 可用  基准偏差 +12.4ms  抖动 3.2ms  采用 4/5 源
```

抖动 > 40ms 就别抢了，或者先换网络。

---

## 3. 跑起来

```bash
python main.py serve --adb C:/platform-tools/adb.exe
```

浏览器开 <http://127.0.0.1:8770>。看板三栏：**设备池 / 任务列表 / 实时事件流**（SSE 推，不是轮询）。

新建任务填：

| 字段 | 说明 |
|---|---|
| item | 大麦演出 itemId（URL 里 `itemId=` 那串） |
| session | 场次文本（`06-01` / `19:30`），留空 = 第一个可选 |
| sku | 票档名子串（`看台580` / `内场`），留空 = 第一个有票的 |
| qty | 数量 |
| viewers | 观演人姓名，逗号分隔 |
| replicas | 派几台设备并发 |
| sale | 开售时刻（本地时间） |
| lead | 提前量 ms，默认 55 |
| entry_xy | 详情页"立即购买"的屏幕坐标 |

也可以用文件：

```bash
python main.py serve -t config/task.example.json
```

单任务不开看板：

```bash
python main.py run -t config/task.example.json --adb C:/platform-tools/adb.exe
```

---

## 4. 设备内执行器（可选，速度上限在这里）

主机侧控制设备有个天花板：`adb uiautomator dump` 一次往返 **300~800ms**。开售那 200ms 里一次都来不及。

所以有两个速度档：

| 档 | 感知在哪 | 单步延迟 | 适用 |
|---|---|---|---|
| **慢档**（默认） | 主机 dump XML | 300~800ms/感知 | 慢态：选票档、点确定、提交订单 |
| **快档** | 手机内 AccessibilityService | 单帧级（~几十 ms） | 开售瞬间到进选座页 |

快档要在手机上装 [AutoX.js](https://github.com/kkevsekk1/AutoX/releases)，把 `scripts/autox-speed.js` 导入，改顶部：

```js
const CONFIG = {
  host: "192.168.1.100:8770",   // 你电脑的内网 IP:端口
  serial: "你的设备名",
  ...
};
```

然后手机和电脑同一局域网，跑脚本。它会自己去 `/agent/config` 领任务，开售前 10 秒才打开无障碍（**长期开无障碍是最廉价的风控特征**），抢完立刻关。

不装 AutoX.js 也能用，只是全走慢档 —— 慢档的成功率取决于你手速之外的东西，别指望。

---

## 5. 坐标怎么取（entry_xy）

`entry_xy` 是详情页"立即购买"按钮的中心点。**这是唯一必须按你机型手调的参数。**

```bash
# 开发者选项里开"指针位置"，手指按住按钮读屏幕顶部的 X/Y
# 或者截图后量像素（分辨率 1080x2400 的机器，按钮一般在 y≈2050~2150）
```

盲点连点的前提是**页面布局在开售前后不变**。如果你的演出页有开售倒计时浮层，盲点会点到浮层上 —— 所以发射前有 `preflight_check`（检查在详情页、没弹窗、没被限流）。

---

## 6. 目录

```
ticket-ops/
├─ main.py                  入口：serve / dry / run / devices / ntp
├─ core/
│  ├─ clock.py              多源 NTP + 单调时钟基准 + T-55ms 精确等待
│  ├─ fsm.py                优先级匹配状态机（顺序即策略）
│  ├─ tree.py              从扁平 dump 还原行结构（祖先/子树/容器）
│  ├─ device.py             AdbDevice（持久 shell）/ MockDevice（干跑）/ LocalDevice（设备内代理）
│  ├─ humanize.py           拟人化：间隔三角分布、坐标抖动
│  └─ observer.py           事件流 + 逐态截图 + 失败归因（BLOCK_PATTERNS）
├─ platform/
│  ├─ store.py              SQLite（WAL）：devices/tasks/assignments/events/results
│  ├─ scheduler.py          设备池调度、按 replicas 派发、FIRST_COMPLETED 抢到即停
│  ├─ worker.py             单机执行体：预热→预检→蹲守→发射→状态机→判定
│  ├─ server.py             HTTP + SSE 看板后端；/agent/* 给手机内执行器用
│  └─ static/dashboard.html 单页看板
├─ plugins/
│  ├─ damai.py              大麦场景：关键词表 + 10 态状态机 + 盲点快通道
│  └─ maoyan.py             猫眼场景：行容器推断 + 勾选框哈希 + 9 态状态机
├─ scripts/
│  └─ autox-speed.js        AutoX.js 设备内执行器（快档）
├─ tests/
│  ├─ test_e2e.py           干跑全流程 + 状态机优先级 + 调度链路
│  └─ test_maoyan.py        插件契约 / 缺货行推断 / 勾选态 / 状态优先级
├─ docs/
│  └─ platform-research.md  三条技术路线实测对比与风控侧设计约束
└─ config/
   ├─ fixture-damai.json    干跑 fixture（6 帧）
   ├─ fixture-maoyan.json   干跑 fixture（5 帧，含行容器节点）
   ├─ task.example.json     任务配置示例
   └─ task.maoyan.example.json
```

---

## 7. 为什么这么设计

**为什么"抢到即停"（FIRST_COMPLETED）**
多台设备同时抢，一台成功立刻 `stop_event.set()` 叫停其余。多抢一张不是战果：多出来的票要退，退票还会连带影响账号信用，而且并发下单本身就更容易触发风控。

**为什么状态机顺序 = 策略**
`Machine` 按 `states` 数组顺序匹配，第一个命中的生效。所以顺序是：**终态 → 拦截 → 流程末端 → 流程前端 → 入口**。
- 终态放最前：抢到了多点一下可能把订单取消
- 拦截放前面："被挤爆"页面上还有"立即购买"，误判成详情页会一直点空
- 末端在前端前：订单页也有票档文本，会误判成选座页

**为什么选座面板要分阶段、一 tick 一步**
开售瞬间的毫秒之争在"详情页→选座页"那一下，已经由盲点连点解决。进了选座面板就是几百 ms 级的事，值得为每一步付一次感知成本换"绝不点错"。每步之间状态机重新 dump，所以点错了下一帧就能发现。

**为什么 MockDevice 的语义是"点中会换页的节点才推进"**
真机上点"+"加数量、勾选观演人，屏幕是不变的。如果 Mock 里点一下就换页，状态机的分阶段逻辑就测不出来（这正是第一版干跑出现过的 bug：一次 tick 点两下，跳过了观演人页面）。fixture 里用 `"nav": false` 标注这类节点。

**为什么快照要能还原行结构（core/tree.py）**
`uiautomator dump` 出来是扁平的节点列表，但猫眼的"缺货"标记和"勾选框"都不在票档/姓名节点自己身上，而是同一行的兄弟节点。`dump` 的节点顺序本来就是先序遍历，所以"先序 + bounds 包含"足以还原层级，不必重新解析 XML。`container(node)` 返回最小包含祖先＝它所在的那一行，查缺货、找勾选框都靠它。

**为什么勾选态要去读 base64 图片哈希**
猫眼把勾选框做成 `android.widget.Image`，无障碍读不到 `checked` 属性，能读到的只有它的 `text`——那是张内联图片的哈希。未选中 `wc0GRRGh2f2pQAAAABJRU5ErkJggg==`，已选中 `B85bZ04Z1b5tAAAAAElFTkSuQmCC`。认不出哈希（App 换皮肤）时退化为"点姓名整行"，两次点不动就跳过这个人，绝不卡死整轮。

**为什么场景插件不把状态挂在 ctx 上**
`detect()` 的签名只有 snapshot，拿不到 ctx，第一版为此用了一个模块级 `_ctx_holder` 中转——而 scheduler 是**每台设备一个线程**，全局中转必然串味。现在每个 `build_machine(cfg)` 自己 new 一个 `_Run`,所有动作函数通过闭包捕获，天然隔离、无需加锁。

**为什么零第三方依赖**
这套东西要能在任何一台装了 Python + platform-tools 的机器上直接跑。看板用标准库 `ThreadingHTTPServer` + SSE，不用 FastAPI。

**为什么平台侧不碰协议层**
`mtop` 系列接口（`mtop.trade.order.create.h5`）快是真快（~13ms），但 `x-mini-wua` 绑设备、要 Frida 从 App 里把签名函数 RPC 出来，且平台一升级就全废。真机自动化是**唯一不会因为签名算法更新而死**的路。协议层留给"极端场景专用通道"，不进主路径。

---

## 8. 已知限制（不是 bug，是边界）

- **无障碍驱动不了滑块**：大麦偶尔弹滑块验证，这一档只能人工过。状态机识别到 `CAPTCHA` 就停下报信，不硬刚。
- **盲点坐标按机型硬编码**：换机型要重取 `entry_xy`。
- **大麦控件树不稳**：大麦详情页是自绘的，`uiautomator dump` 拿不到控件树，所以详情页只能靠盲点 + 坐标，选座页才用文本匹配。
- **猫眼勾选框哈希会随版本变**：`img_unchecked` / `img_checked` 认不出来时退化为点姓名行；实在点不上会跳过这个人（抢到后手动补），不会卡死整轮。
- **猫眼选座有余票回流**：目标价没票时会一直等回流，`pick_tier` 把试过的价位记在 `_Run.tried` 里，全试过才重置重来，避免在同一档上死循环。
- **一机一号**：同一台手机切账号会触发设备指纹关联，别在一台上来回切。
- **同 IP 别超 2 台**：运营商 NAT 之外的出口 IP 是风控的重要维度。

## 9. 使用边界

代码是通用自动化框架，票务是第一个场景实现。**自用（含家人朋友）没有问题**；但把这类工具作为商品对外销售，在国内已有被认定构成不正当竞争的判例（北京东城区法院，售卖抢票软件案），向不特定公众提供代操作服务同样存在法律风险。本项目的形态是"自己的设备作业平台"。

平台代码不涉及：账号买卖、身份信息伪造、平台接口签名逆向。
