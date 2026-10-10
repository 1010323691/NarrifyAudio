# 性能监测台图表：样式、实现与克隆迁移指南

本文整理 Strata「Monitor」页（`serve/web/index.html` 的 `#view-monitor`，由 `serve/web/app.js` 驱动）里所有图表的做法，
目的是让你以后在别的项目里复刻同样的监测图。所有内容都来自当前仓库代码，文件和行号可直接对照。

## 0. 先看结论

- **零依赖**：没有 Chart.js / ECharts / D3，也没有 canvas。全部是**手写 inline SVG + CSS**，加起来约 60 行 JS、约 40 行 CSS。
- **只有三类图形**：
  1. 迷你折线面积图（sparkline），8 张指标卡各一个；
  2. 270° 环形仪表（context fill）；
  3. 横向进度条（约 8 处：状态进度、VRAM/RAM/温度、会话缓存）。
- **没有饼图**。页面里不存在饼图或环形占比图。第 8 节给了一个沿用同一套风格的饼/环图写法，是新写的，不是仓库里已有的。
- **数据流**：服务端每秒采样一次，存在 60 点的环形缓冲里；前端每秒轮询 `GET /metrics`，整页重画。没有 WebSocket，没有动画库。
- **「Requests」页**（`serve/web/monitor.html` + `monitor.js`，API Monitor）没有任何图表，只有卡片、列表和文本，不用看。

---

## 1. 文件地图

| 作用 | 文件 | 要点 |
|---|---|---|
| 设计令牌（颜色、字号、间距、圆角、阴影，亮/暗两套） | `serve/web/tokens.css` | 复刻时**必须带上**，图表颜色全部来自 `--st-*` 变量 |
| 基础组件样式（卡片、指标、进度条、仪表、表格、徽标） | `serve/web/components.css` | 第 29–46 行是图表相关 |
| Monitor 页布局与细节样式 | `serve/web/app.css` | 第 83–122 行、第 165–170 行（响应式） |
| 页面骨架（状态卡、仪表、进度条、请求表） | `serve/web/index.html` 第 74–140 行 | 8 张指标卡由 JS 生成 |
| 图表渲染逻辑 | `serve/web/app.js` 第 111–153 行（指标卡与折线）、第 240–325 行（仪表、进度条） | |
| 图标 | `serve/web/sprite.svg` | `<symbol id="i-gpu">` 等，用 `<use href>` 引用 |
| 采样与历史缓冲 | `serve/telemetry.py` 第 21 行 `HISTORY = 60`、第 266–365 行 `Telemetry` | 每秒一次，`deque(maxlen=60)` |
| `/metrics` 的组装 | `serve/server.py` 第 2845–2894 行 `metrics()` | |
| 字体 | `serve/web/fonts/`（Outfit，OFL 协议） | 可换成任何无衬线字体，只要数字用等宽数字特性 |
| Grafana / Prometheus 版 | `docs/monitoring/grafana-strata.json` 等 | 另一条路线，见第 9 节 |

---

## 2. 设计令牌（样式的根）

所有图表只用变量，不写死颜色。亮色是默认，暗色靠 `<html data-theme="dark">` 切换（`tokens.css`）。

```css
:root{
  --st-bg:#f9fafb;  --st-surface:#fff;  --st-surface-2:#f7f7f7;
  --st-line:#dee5ee; --st-line-soft:#eee;
  --st-ink:#221e1f;  --st-ink-soft:#303030; --st-ink-muted:#6f6f6f;
  --st-accent:#10b981;                       /* 主色：绿，折线/进度条/仪表默认色 */
  --st-info:#0f97ff;                         /* 蓝：读取/PCIe/磁盘 */
  --st-warn:#e39b0b;                         /* 橙：温度 */
  --st-danger:#e0283f;                       /* 红：RAM > 92% */
  --st-accent-tint:#ecfdf5; --st-info-tint:#e8f4ff; --st-warn-tint:#fff6e0; --st-danger-tint:#fdecee;
  --st-r-pill:9999px; --st-r-lg:20px;
  --st-ease:cubic-bezier(.2,.7,.2,1); --st-dur:160ms;
  --st-sh-card:0 18px 7px rgba(0,0,0,.01),0 10px 6px rgba(0,0,0,.02),0 4px 4px rgba(0,0,0,.03),0 1px 2px rgba(0,0,0,.04);
}
[data-theme="dark"]{
  --st-bg:#0e1113; --st-surface:#161a1d; --st-surface-2:#1d2226;
  --st-line:#2a3036; --st-line-soft:#22272c;
  --st-ink:#eef1f3; --st-ink-muted:#8e979f;
  --st-accent:#34d399; --st-info:#3aa9ff; --st-warn:#f5b53d; --st-danger:#ff5a6e;
  --st-sh-card:0 1px 2px rgba(0,0,0,.4);
}
```

配色规则（图表语义色）：

| 语义 | 变量 | 用在 |
|---|---|---|
| 默认/正常 | `--st-accent` | Speed、GPU load、VRAM、CPU 折线；进度条；仪表填充 |
| 信息/吞吐类 | `--st-info` | PCIe、Disk 折线；「Reading prompt」进度条 |
| 警示 | `--st-warn` | GPU 温度折线、温度进度条 |
| 危险 | `--st-danger` | RAM 超过 92% 时进度条变红 |
| 轨道/底色 | `--st-surface-2` + `--st-line-soft` | 进度条槽、仪表轨道 |
| 数字 | `font-variant-numeric: tabular-nums` | **所有**会跳动的数字，防止抖动 |

> 迁移要点：换主题色只改 `--st-accent` 等几个变量，不用动图表代码。暗色模式靠变量整体替换，图表零改动。

---

## 3. 指标卡 + 迷你折线面积图（sparkline）

### 3.1 外观

每张卡：左上图标 + 标题，下面是大号数字（32px / 900 字重，单位用 14px 灰色小字），再下面一行 12px 灰色副文本，最底部是 32px 高的整宽折线图。
折线 1.6px 圆角线头，下面铺 12% 透明度的同色面积。网格 4 列，≤1000px 变 2 列。

### 3.2 DOM（由 JS 模板生成，`app.js` 第 112–136 行）

```html
<div class="st-card metric-card"><div class="st-metric">
  <span class="st-metric__label"><svg class="st-icon st-icon--sm"><use href="web/sprite.svg#i-gpu"/></svg>GPU load</span>
  <span class="st-metric__value" id="mv-gpu">–</span>
  <span class="st-metric__sub"   id="ms-gpu"></span>
  <svg class="st-metric__spark" id="sp-gpu" viewBox="0 0 100 32" preserveAspectRatio="none" data-tone="warn">
    <path class="area" fill="currentColor" opacity=".12"/>
    <path class="line" fill="none" stroke="currentColor" stroke-width="1.6"
          stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
  </svg>
</div></div>
```

关键属性，缺一个就会变形：

- `viewBox="0 0 100 32"` + `preserveAspectRatio="none"`：用固定的 100×32 逻辑坐标系，被拉伸到任意宽度。
- `vector-effect="non-scaling-stroke"`：拉伸后线宽仍是 1.6px，不会被压扁。
- `fill/stroke="currentColor"`：颜色由 CSS `color` 决定，所以换色只需改 `color`。

### 3.3 样式

```css
.st-metric{display:flex;flex-direction:column;gap:10px}
.st-metric__label{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:500;color:var(--st-ink-muted)}
.st-metric__value{font-weight:900;font-size:32px;line-height:1;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.st-metric__value small{font-size:14px;font-weight:500;color:var(--st-ink-muted);margin-left:4px}
.st-metric__sub{font-size:12px;color:var(--st-ink-muted);font-variant-numeric:tabular-nums;min-height:1em}
.st-metric__spark{display:block;overflow:visible;width:100%;height:32px;color:var(--st-accent)}
.st-metric__spark[data-tone="warn"]{color:var(--st-warn)}
.st-metric__spark[data-tone="info"]{color:var(--st-info)}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.metric-card{padding:16px 20px}
@media (max-width:1000px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}
```

`min-height:1em` 的副文本行是为了防止有无副文本时卡片高度跳动。

### 3.4 画线函数（完整，`app.js` 第 138–147 行）

```js
function spark(id, values, max) {
  const svg = $(id);
  const v = (values || []).map((x) => (x == null ? 0 : x));          // 缺失点按 0 画
  if (v.length < 2) { svg.querySelector(".line").setAttribute("d", ""); svg.querySelector(".area").setAttribute("d", ""); return; }
  const top = Math.max(max || 0, ...v, 1e-9);                         // Y 轴上限：给定固定上限与数据最大值取大
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  svg.querySelector(".line").setAttribute("d", line);
  svg.querySelector(".area").setAttribute("d", `${line}L100,32L0,32Z`);   // 折线 + 回到底边 = 面积
}
```

画法细节：

- **X**：点均匀分布在 0–100；60 个点 = 最近 60 秒，最新点在最右。
- **Y**：`30 - x/top*26`，即值域映射到 y∈[4, 30]，上下各留 2~4 单位的边距，线头不会被裁。
- **零基线**：Y 轴总是从 0 开始，不做自动缩放下限。
- **上限 `max` 的取法**（每个指标不同，在 `renderMonitor` 里）：

| 指标 | `max` | 含义 |
|---|---|---|
| Speed / Prefill | 不给 | 按窗口内最大值自适应 |
| GPU load、CPU | `100` | 固定百分比刻度 |
| VRAM | `hw.gpu_mem_total` | 满格 = 显存总量 |
| 温度 | `90` | 固定 90 °C 为顶 |
| 功率 | `hw.gpu_power_limit` | 满格 = 功率墙 |
| PCIe、Disk | 不给 | 自适应 |

- **没有坐标轴、网格、tooltip、图例**。这是刻意的极简风格：数字在上面，折线只表达趋势。

### 3.5 Speed 卡的双线变体

Speed 卡一个 SVG 里画两条线：Decode（主色）和 Prefill（深一档的主色），数字左右并排。

```css
.speed-values{display:flex;justify-content:space-between;gap:12px}
.speed-values>div{display:flex;flex-direction:column;gap:10px;min-width:0}
.speed-values>div:last-child{text-align:right}
.speed-values .st-metric__value{font-size:clamp(20px,2.2vw,32px)}
.st-metric__spark .speed-prefill{color:color-mix(in srgb,var(--st-accent) 65%,black)}
```

第二条线放在 `<g id="sp-prefill" class="speed-prefill">` 里，同样含 `.area` 与 `.line`；`spark("sp-prefill", ...)` 也是同一个函数。
`color-mix(... 65%, black)` 要求较新的浏览器（Chrome 111+、Safari 16.2+、Firefox 113+）。

### 3.6 数字格式化（直接复制）

```js
const fmt  = (n, d = 0) => (n == null || Number.isNaN(n) ? "–" : Number(n).toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d}));
const kfmt = (n) => (n == null ? "–" : n >= 1000 ? `${fmt(n / 1000, n >= 10000 ? 0 : 1)}k` : fmt(n));
const gb   = (b, d = 1) => (b == null ? "–" : fmt(b / 1073741824, d));      // 二进制 GB，和 Windows 任务管理器一致
function setMetric(key, value, unit, sub) {
  $(`mv-${key}`).innerHTML = value == null ? "–" : `${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ""}`;
  $(`ms-${key}`).textContent = sub || "";
}
```

无数据统一显示 `–`（不是 0）。动态文本进 `innerHTML` 前一律过 `esc()`。

### 3.7 8 张卡的配置表（数据驱动，`METRICS` 数组）

```js
const METRICS = [
  {key:"speed", label:"Speed",    icon:"gauge",       unit:"t/s",  series:"tok_s"},
  {key:"gpu",   label:"GPU load", icon:"gpu",         unit:"%",    series:"gpu_util", max:100},
  {key:"vram",  label:"VRAM",     icon:"layers",      unit:"GB",   series:"gpu_mem_used"},
  {key:"temp",  label:"GPU temp", icon:"thermometer", unit:"°C",   series:"gpu_temp", tone:"warn"},
  {key:"power", label:"Power",    icon:"bolt",        unit:"W",    series:"gpu_power"},
  {key:"pcie",  label:"PCIe",     icon:"link",        unit:"",     series:"gpu_pcie_rx_mb", tone:"info"},
  {key:"cpu",   label:"CPU",      icon:"cpu",         unit:"%",    series:"cpu", max:100},
  {key:"disk",  label:"Disk read",icon:"disk",        unit:"MB/s", series:"disk_read_mb", tone:"info"},
];
```

复刻时只改这个数组就能增删指标。注意：实际渲染时 `series`/`max`/`unit` 并不是自动读取的，`renderMonitor` 里对每张卡手写了
`setMetric(...)` 和 `spark(...)` 两行（因为每张卡的副文本、上限逻辑不同）。如果想完全数据驱动，需要自己把这些差异也放进配置。

---

## 4. 环形仪表（Context fill）

### 4.1 外观

150×150 的 270° 开口圆环（缺口朝下），圆头线帽，线宽 10；中间叠一个大号百分比 + 一行灰字（`12.3k / 32K`）。
填充弧过渡 400ms。

### 4.2 SVG（`index.html` 第 99–105 行）

```html
<div class="ctx-gauge">
  <svg class="st-gauge" viewBox="0 0 120 120" aria-hidden="true">
    <circle class="st-gauge__track" cx="60" cy="60" r="50" fill="none" stroke-width="10" stroke-linecap="round"
            stroke-dasharray="235.6 314.2" transform="rotate(135 60 60)"/>
    <circle class="st-gauge__fill" id="ctx-fill" cx="60" cy="60" r="50" fill="none" stroke-width="10"
            stroke-linecap="round" stroke-dasharray="0 314.2" transform="rotate(135 60 60)"/>
  </svg>
  <div class="ctx-gauge__label"><span class="ctx-gauge__pct" id="ctx-pct">0%</span><span class="muted" id="ctx-sub">–</span></div>
</div>
```

数学：

- 半径 50 → 周长 `2π·50 = 314.2`；270° 弧长 = `314.2 × 0.75 = 235.6`。
- `stroke-dasharray="<可见长度> <空白长度>"`：轨道固定 `235.6 314.2`，填充按比例 `235.6×frac 314.2`。
- `transform="rotate(135 60 60)"`：圆默认从 3 点钟方向开始，转 135° 后起点落在左下角，终点在右下角，缺口朝下。
- 想改开口角度 θ：弧长 `L = 314.2 × θ/360`，旋转角 `= 90 + (360-θ)/2`。

### 4.3 样式

```css
.st-gauge{position:relative;width:120px;height:120px}
.st-gauge__track{stroke:var(--st-surface-2)}
.st-gauge__fill{stroke:var(--st-accent)}
.ctx-gauge{position:relative;width:150px;height:150px;margin:8px auto 12px}
.ctx-gauge .st-gauge{width:150px;height:150px}
.ctx-gauge .st-gauge__fill{transition:stroke-dasharray 400ms var(--st-ease)}
.ctx-gauge__label{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-size:12px}
.ctx-gauge__pct{font-weight:900;font-size:24px;line-height:1.1;font-variant-numeric:tabular-nums}
```

### 4.4 更新（`app.js` 第 311–319 行）

```js
const frac = ctx ? Math.min(1, used / ctx) : 0;
$("ctx-fill").setAttribute("stroke-dasharray", `${(235.6 * frac).toFixed(1)} 314.2`);
$("ctx-fill").style.opacity = 235.6 * frac >= 3 ? "1" : "0";     // 弧长接近 0 时只会画出一个圆点帽，所以直接隐藏
$("ctx-pct").textContent = `${Math.round(frac * 100)}%`;
$("ctx-sub").textContent = ctx ? `${kfmt(used)} / ${ctxfmt(ctx)}` : "–";
```

坑：`stroke-linecap="round"` 在弧长为 0 时仍会画一个点，所以代码在弧长 < 3 时把透明度置 0。

---

## 5. 横向进度条

### 5.1 样式（`components.css` 第 38–42 行）

```css
.st-progress{height:8px;border-radius:9999px;background:var(--st-surface-2);border:1px solid var(--st-line-soft);overflow:hidden}
.st-progress__bar{height:100%;border-radius:inherit;background:var(--st-accent);transition:width 160ms var(--st-ease)}
.st-progress[data-tone="info"]  .st-progress__bar{background:var(--st-info)}
.st-progress[data-tone="warn"]  .st-progress__bar{background:var(--st-warn)}
.st-progress[data-tone="danger"] .st-progress__bar{background:var(--st-danger)}
.bar-row{display:flex;justify-content:space-between;font-size:13px;font-weight:500;margin-top:8px;font-variant-numeric:tabular-nums}
```

### 5.2 结构与更新

```html
<div class="bar-row"><span>System RAM</span><span class="muted" id="ram-text">–</span></div>
<div class="st-progress" id="ram-progress"><div class="st-progress__bar" id="ram-bar" style="width:0%"></div></div>
```

```js
const ramPct = hw.ram_total ? (100 * hw.ram_used) / hw.ram_total : 0;
$("ram-bar").style.width = `${ramPct}%`;
if (ramPct > 92) $("ram-progress").dataset.tone = "danger"; else delete $("ram-progress").dataset.tone;   // 阈值变色
```

用到进度条的地方：

| 位置 | 含义 | 色调 |
|---|---|---|
| Model state | 读 prompt 进度 `read/total`，或生成进度 `generated/max_tokens` | 读取时 `info`，生成时默认绿 |
| Experts in VRAM | 专家缓存占显存 | 绿 |
| System RAM | 已用/总量 | 绿，>92% 红 |
| GPU temperature | 温度 °C 直接当百分比（0–100） | `warn` |
| Conversation cache | 已停放会话数、占用内存 | 绿 |

宽度变化靠 CSS `transition: width`，JS 不做任何动画。

---

## 6. 状态徽标（图表的「图例」）

Model state 卡顶部 5 个徽标：Idle / Reading / Generating / Queued / Error。当前状态**不透明，其他 38% 透明**。

```css
.badges .st-badge{opacity:.38;transition:opacity 160ms}
.badges .st-badge.on{opacity:1}
.st-badge{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:9999px;font-size:12px;font-weight:700;
          background:var(--st-surface-2);color:var(--st-ink-muted);border:1px solid var(--st-line)}
.st-badge::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.st-badge--reading{background:var(--st-info-tint);color:var(--st-info-text);border-color:transparent}
.st-badge--generating{background:var(--st-accent-tint);color:var(--st-accent-text);border-color:transparent}
```

```js
for (const b of document.querySelectorAll("#state-badges .st-badge"))
  b.classList.toggle("on", b.dataset.s === on || b.dataset.s === live.state);
```

---

## 7. 数据层：怎么喂这些图

### 7.1 服务端采样（`serve/telemetry.py`）

- 一个守护线程，每 1 秒调用 `sample()`，把读数写入 `self.now`，并把 10 个序列 append 到 `deque(maxlen=60)`：
  `gpu_util, gpu_mem_used, gpu_temp, gpu_power, gpu_pcie_rx_mb, cpu, ram_used, disk_read_mb, tok_s, prefill_tok_s_mean`。
- GPU 读数：NVIDIA 用 NVML（`ctypes` 直接加载，不依赖 pynvml），AMD 读 sysfs；CPU/RAM/磁盘用 `psutil`，没有时退化为自带的 `_CpuRamFallback`。
- 磁盘速率靠相邻两次 `disk_io_counters()` 的差除以时间。
- 多卡时 `gpu_*` 是汇总值（显存/功率求和，利用率求均值，温度取最高），另有 `gpus[]` 逐卡明细。
- 业务指标（tok/s）通过 `extra()` 回调注入，所以采样器与业务解耦。

### 7.2 `/metrics` 的 JSON 形状（前端消费的全部字段）

```jsonc
{
  "engine":   {"model":"...", "max_context":32768, "expert_slots":..., "expert_cache_mib":...},
  "live":     {"state":"idle|reading|generating|unloaded", "queued":0, "tok_s":..., "prefill_tok_s_mean":...,
               "prompt_read":..., "prompt_total":..., "generated":..., "max_tokens":..., "phase":"..."},
  "hardware": {"gpu_util":..,"gpu_mem_used":..,"gpu_mem_total":..,"gpu_temp":..,"gpu_power":..,"gpu_power_limit":..,
               "gpu_pcie_rx_mb":..,"cpu":..,"ram_used":..,"ram_total":..,"disk_read_mb":..,"disk_write_mb":..,"gpus":[...]},
  "hardware_static": {"gpu_name":"...","cpu_name":"...","cores":8,"threads":16,"psutil":true},
  "history":  {"tok_s":[60个数],"gpu_util":[...],"gpu_mem_used":[...], "...":"..."},
  "requests": [最近 12 条], "totals": {...}, "conversation_cache": {...}
}
```

内存单位是**字节**，速率单位是 MB/s，温度 °C，功率 W。

### 7.3 前端轮询（`app.js` 第 155–178 行）

```js
async function poll() {
  try {
    const r = await fetch("metrics", {headers: headers()});
    if (r.ok) { lastMetrics = await r.json(); render(lastMetrics); }
  } catch (e) { /* 连续失败 3 次才提示「Server not reachable」 */ }
  setTimeout(poll, 1000);          // 上一次完成后再排下一次，不会请求堆积
}
```

- 用 `setTimeout` 递归而不是 `setInterval`：慢响应不会叠加。
- 只在 Monitor 标签页可见时才渲染（`if (tab === "monitor") renderMonitor(...)`），后台标签只更新顶部状态胶囊。
- 每次整页重算 `d` 属性与 `style.width`，没有 diff。数据只有 60 点 × 10 条，开销可以忽略。

### 7.4 窗口

固定 60 秒滚动窗口，没有时间缩放、没有历史存盘。要更长的曲线，请把 `HISTORY` 改大（同时注意前端点数多了折线会更密，`stroke-width` 可能要调细），
或者走第 9 节的 Prometheus + Grafana。

---

## 8. 克隆迁移步骤

### 8.1 最小可用清单（照抄即可得到同样的外观）

1. 拷贝 `tokens.css` + `components.css` 中第 3–5 节所列规则（或整文件）。字体 Outfit 可选。
2. 拷贝 `sprite.svg` 中需要的图标，或换成你自己的图标库。
3. 拷贝 `spark()`、`setMetric()`、`fmt()`、`esc()` 和 `METRICS` 卡片模板。
4. 提供一个 JSON 接口，字段形状见 7.2；`history.<key>` 为按时间升序的数字数组（最旧在前）。
5. 在页面里每秒 `fetch` 并调用 `spark(id, history[key], max)`。

### 8.2 一个最小可运行示例（单文件，不依赖本仓库）

```html
<style>
  :root{--accent:#10b981;--warn:#e39b0b;--muted:#6f6f6f;--surface:#fff;--line:#dee5ee}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:20px;padding:16px 20px;width:240px;
        display:flex;flex-direction:column;gap:10px;font-family:system-ui}
  .v{font-weight:900;font-size:32px;line-height:1;font-variant-numeric:tabular-nums}
  .v small{font-size:14px;font-weight:500;color:var(--muted);margin-left:4px}
  svg.spark{display:block;overflow:visible;width:100%;height:32px;color:var(--accent)}
  svg.spark[data-tone=warn]{color:var(--warn)}
</style>
<div class="card"><span style="color:var(--muted);font-size:13px">GPU load</span>
  <span class="v" id="v">–</span>
  <svg class="spark" id="sp" viewBox="0 0 100 32" preserveAspectRatio="none">
    <path class="area" fill="currentColor" opacity=".12"/>
    <path class="line" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"
          stroke-linecap="round" vector-effect="non-scaling-stroke"/>
  </svg></div>
<script>
const data = [];
function spark(svg, values, max) {
  const v = values.map(x => x ?? 0), top = Math.max(max || 0, ...v, 1e-9);
  const pts = v.map((x, i) => [(i / (v.length - 1)) * 100, 30 - (x / top) * 26]);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join("");
  svg.querySelector(".line").setAttribute("d", line);
  svg.querySelector(".area").setAttribute("d", `${line}L100,32L0,32Z`);
}
setInterval(() => {                                   // 用随机数模拟 /metrics 的 history
  data.push(40 + Math.random() * 50); if (data.length > 60) data.shift();
  document.getElementById("v").innerHTML = `${data.at(-1).toFixed(0)}<small>%</small>`;
  if (data.length > 1) spark(document.getElementById("sp"), data, 100);
}, 1000);
</script>
```

### 8.3 迁移到框架时的写法

- **React / Vue**：把 `spark()` 改成纯函数 `sparkPath(values, max) -> {line, area}`，在组件里 `<path d={line}/>`。样式和 SVG 属性原样保留。
- **React 注意**：属性写 `vectorEffect="non-scaling-stroke"`、`strokeLinejoin`、`strokeLinecap`、`preserveAspectRatio`（驼峰）。
- **后端换技术栈**：只要给出 `{ now, history, static }` 三块即可。采样线程 + 定长环形缓冲的模式在任何语言里都一样：每秒读一次硬件，`append` 到长度 60 的队列。
- **不想轮询**：把 `poll()` 换成 SSE / WebSocket，渲染函数 `render(m)` 不用改。
- **要 tooltip / 十字线**：本实现没有。最简单的做法是在 SVG 上加一个透明 `<rect>` 监听 `pointermove`，用 `x/width * (n-1)` 取最近点下标，再画一条竖线和一个浮层。

### 8.4 迁移检查清单

- [ ] 引入 `tokens.css`，并设置 `<html data-theme="light|dark">`。
- [ ] 数字元素都加了 `font-variant-numeric: tabular-nums`。
- [ ] 折线 SVG 同时具备 `viewBox`、`preserveAspectRatio="none"`、`vector-effect="non-scaling-stroke"`。
- [ ] 折线颜色用 `currentColor`，由容器的 `color` 控制；用 `data-tone` 切换语义色。
- [ ] 仪表的 `r`、`dasharray`、`rotate` 三者互相匹配（见 4.2 的公式）。
- [ ] 弧长接近 0 时隐藏圆头帽。
- [ ] 无数据显示 `–`，副文本保留 `min-height:1em`。
- [ ] 窄屏：指标网格 4 列 → 2 列，下方两栏布局改单列（`app.css` 第 165–170 行）。
- [ ] 所有来自接口的字符串写入 `innerHTML` 前过 `esc()`。

---

## 9. 补充一：如果你要饼图/环形占比图（新写的，非仓库已有）

仓库里没有饼图。下面是沿用同样风格（SVG 圆环 + dasharray + 令牌配色）的写法，可直接用于例如「prompt 复用 vs 新读取」「各状态耗时占比」。

```html
<svg viewBox="0 0 120 120" width="150" height="150" id="donut">
  <circle cx="60" cy="60" r="50" fill="none" stroke="var(--st-surface-2)" stroke-width="14"/>
  <!-- 每个扇区一个 circle，由 JS 填 -->
</svg>
```

```js
function donut(svg, parts) {                      // parts: [{value, color}]
  const R = 50, C = 2 * Math.PI * R, total = parts.reduce((s, p) => s + p.value, 0) || 1;
  svg.querySelectorAll(".seg").forEach((e) => e.remove());
  let offset = 0;
  for (const p of parts) {
    const len = (p.value / total) * C;
    const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    c.setAttribute("class", "seg");
    c.setAttribute("cx", 60); c.setAttribute("cy", 60); c.setAttribute("r", R);
    c.setAttribute("fill", "none"); c.setAttribute("stroke", p.color); c.setAttribute("stroke-width", 14);
    c.setAttribute("stroke-dasharray", `${len} ${C - len}`);
    c.setAttribute("stroke-dashoffset", -offset);           // 负偏移把扇区顺时针推到上一个之后
    c.setAttribute("transform", "rotate(-90 60 60)");       // 从 12 点钟开始
    svg.appendChild(c);
    offset += len;
  }
}
donut(document.getElementById("donut"), [
  {value: 62, color: "var(--st-accent)"},
  {value: 28, color: "var(--st-info)"},
  {value: 10, color: "var(--st-warn)"},
]);
```

要做实心饼图，把 `r` 设为 25、`stroke-width` 设为 50 即可（描边宽度等于半径的两倍时变成实心）。
这里不用 `stroke-linecap="round"`，否则相邻扇区会互相盖住。

## 10. 补充二：Grafana / Prometheus 路线

Monitor 页之外，仓库还带一套现成的 Prometheus 抓取与 Grafana 面板：

- `docs/monitoring/grafana-strata.json`：导入即用，含 17 个 `timeseries` 面板（请求数、tok/s、首 token 延迟 p50/p95、
  上下文占用、专家缓存命中率、逐卡 GPU 利用率/显存、主机 RAM 等），分两个 row：vLLM 通用名与 `strata:` 引擎名。
- `docs/monitoring/prometheus.yml`、`servicemonitor.yaml`：抓取配置与 Kubernetes ServiceMonitor。
- 服务端出口：`serve/prometheus.py`（把 `/metrics` JSON 的键转成 `strata:` 前缀的 Prometheus 文本）。

适用场景对比：

| | 内置 Monitor 页 | Grafana 路线 |
|---|---|---|
| 依赖 | 无 | Prometheus + Grafana |
| 历史 | 最近 60 秒 | 任意长度，可回看 |
| 图表类型 | sparkline / 仪表 / 进度条 | 完整时序图、分位数、告警 |
| 适合 | 本机一眼看当前状态 | 长期观测、多实例 |

想复刻「长时间曲线」就走这条；想复刻「卡片式一眼看」的风格就按前面 2–8 节做。

---

## 11. 已知限制

- 只有 60 秒窗口，刷新页面后折线从服务端缓冲重新取，所以**不丢**，但服务端重启会清空。
- 折线没有坐标轴与悬停提示，不适合精读数值。
- 当某个序列里出现 `null`，会被按 0 画，曲线会掉到底。
- `color-mix()` 只影响 Prefill 第二条线的颜色；低版本浏览器会退化为继承主色（两条线同色）。
- 本文数字（60 点、1 秒、阈值 92%、上限 90 °C 等）均来自代码常量，不是性能测量结果；未在真机上做渲染截图核对。
