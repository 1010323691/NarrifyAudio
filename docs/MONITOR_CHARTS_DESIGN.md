# 管理控制台图表设计指南

本文是 NarrifyAudio 管理控制台（`/admin`）图表的样式、实现与扩展规范。所有内容对应本仓库现有代码，文件路径可直接对照。
新增或修改任何图表前先读本文，沿用同一套风格。

> 本文最初借鉴自另一个项目的监测页图表实现（sparkline / 270° 仪表 / 环形图的数学与 SVG 技巧），现已改写为本仓库的实现；
> 凡与旧版不一致处，以本文和代码为准。

## 0. 先看结论

- **零图表库依赖**：没有 ECharts / Chart.js / D3 / canvas。全部是手写 inline SVG + CSS（历史上用过 ECharts，已在 `cdb29d8` 移除，不要再引入）。
- **视图只产出 spec，不写绘图细节**：视图里用 `*Option()` 构造一个纯数据的 `ChartSpec`，交给 `AdminChart.vue` 按 `kind` 分发到对应 SVG 组件。
- **六种图**：`time`（时序折线/面积/柱）、`bar`（分类柱/条）、`donut`（环形占比）、`gauge`（270° 仪表）、`heatmap`（热力图）、`spark`（迷你折线）。
- **颜色只引用 CSS 变量**，亮暗主题随 `.dark` 类自动切换，图表代码里零颜色字面量、零 JS 主题判断。
- **纯几何函数集中在 `scale.ts`**，无 DOM、可单测，由 `npm run test:admin-charts` 钉住。
- **数据来源**：Worker 每 30 秒写入的 `system_metric_samples`（`backend/platform/metrics_sampler.py`，保留 7 天）与任务表 / 额度账本聚合（`backend/services/admin_analytics.py`）。API 延迟序列是 API 进程内存口径，进程重启即清空。

---

## 1. 文件地图

| 作用 | 文件 | 要点 |
|---|---|---|
| 分发入口 | `src/components/admin/charts/AdminChart.vue` | 接收 `option: (tokens) => ChartSpec`、`label`（无障碍名）、`height`；按 `spec.kind` 渲染 |
| spec 类型与构造器 | `charts/options.ts` | `timeSeriesOption` / `barOption` / `donutOption` / `gaugeOption` / `heatmapOption` / `sparklineOption` |
| 颜色令牌 | `charts/tokens.ts` | `chartTokens()`：`series[0..7]`、`status`、`sequential`、`muted`、`axis`；`groupColor()` 给 worker 组固定配色 |
| 纯几何/格式化 | `charts/scale.ts` | `niceTicks`、`sparkPaths`、`gaugeDash`、`donutSlices`、`segments`、`linePath`、`areaPath`、`roundedRect`、`timeLabel`、`truncate` 等 |
| 尺寸监听/指针坐标 | `charts/useChartSize.ts` | `ResizeObserver` 取像素尺寸，让刻度按真实宽度布局；`pointerIn()` 取相对坐标 |
| 悬停提示 | `charts/ChartTooltip.vue` | 超过半宽自动翻到左侧 |
| 六个绘图组件 | `charts/{TimeSeries,Bar,Donut,Gauge,Heatmap}Chart.vue`、`Sparkline.vue` | |
| 卡片外壳 | `src/components/admin/ChartCard.vue` | 标题/副标题/hover 说明/`span-N` 栅格跨度/可切换“数据表”视图（无障碍与精读数值） |
| KPI 卡（带 sparkline） | `src/components/admin/KpiCard.vue` | 数值 + 同比标记 + 迷你折线 |
| 进度条 | `src/components/admin/MeterBar.vue` | 横向进度条，样式 `.meter__*` |
| 数据整形 | `src/views/admin/series.ts` | `series()` / `summed()` / `values()` / `scaleFor()` / `throughputScale()`，缺失样本保持 `null` |
| 样式与变量 | `src/styles/admin.css` | 第 1–30 行变量定义；第 115–139 行 chart-card；第 403 行起 `.vc-*` SVG 图样式 |
| 回归测试 | `scripts/test-admin-charts.mjs` | `npm run test:admin-charts`：刻度、折线断点、仪表弧、环图扇区 |

页面使用方：`src/views/admin/AdminMonitor.vue`（时序/柱/条/环/仪表最全）、`AdminOverview.vue`、`AdminTasks.vue`、`AdminUsers.vue`、`AdminResources.vue`、`AdminLogs.vue`。

---

## 2. 设计令牌（样式的根）

变量定义在 `src/styles/admin.css` 的 `.admin-shell`（亮色）与 `.dark .admin-shell`（暗色）。图表一律经 `charts/tokens.ts` 取值，视图里**不写死颜色**。

| 令牌 | 变量 | 用途 |
|---|---|---|
| 分类色 8 档 | `--viz-1` … `--viz-8` | 多系列区分；顺序是经过验证的参考色板 |
| 状态色 | `--status-good / -warning / -serious / -critical` | 健康度、阈值警示（仪表、进度条、告警点） |
| 同比色 | `--delta-good / --delta-bad` | KPI 同比标记文字 |
| 轨道/底色 | `hsl(var(--muted))`（`.vc-track`） | 仪表、环图的轨道 |
| 网格/坐标轴 | `hsl(var(--border))` / `hsl(var(--input))` | `.vc-grid`；基线用更深的 `is-base` |
| 文字 | `hsl(var(--muted-foreground))` | 刻度、图例、提示标题 |
| 顺序色阶 | `tokens.sequential`（`--viz-1` 与卡片底色的 `color-mix` 梯度） | 热力图、“空闲/剩余”浅色块 |

规则：

1. **分类色按顺序取用，不循环**。系列超过 8 个，把尾部合并成“其他”，不要回头复用颜色。
2. **同一实体在所有图里同一颜色**。worker 服务组用 `groupColor(tokens, group)`（`GROUP_ORDER = llm / tts / audio / system / worker`），不要自己挑色。
3. **颜色不是唯一的信息载体**：状态用颜色 + 文字/图标；图例可点击切换系列；每张图都有 `label`，并尽量配 `ChartCard` 的数据表视图。
4. 换主题色只改 `admin.css` 里的变量，不动图表代码；亮暗两套 `--viz-*` 需要同步维护并保持对比度。
5. **所有会跳动的数字**加 `font-variant-numeric: tabular-nums`（`.vc` / `.vc-plot` 已统一设置）。
6. 无数据显示 `–` 或 `—`，不要显示 0；缺失样本在序列里保持 `null`（见 §3 断线语义）。

---

## 3. 数据流与断线语义

```
metrics_sampler (Worker, 30s) ─► system_metric_samples ─► /api admin 接口 ─► MetricPoint[]
                                                                       │
                              views/admin/series.ts: series(points, key, scale) ─► [time, value|null][]
                                                                       │
                       views/admin/*.vue: timeSeriesOption(tokens, {...}) ─► ChartSpec ─► AdminChart.vue
```

- 视图把后端点列转成 `[isoTime, number | null][]`。**缺失样本必须保持 `null`**：`scale.ts:segments()` 遇到 `null` 会把折线断开，而不是插值或画成 0，避免图表“说谎”。
- 单个孤立样本由 `linePath()` 画成零长度线段（`M x,y h0`），圆头线帽仍会显示成一个点。
- 时间轴粒度用 `scaleFor(step, span)` / `throughputScale(range)` 决定标签格式：`minute`（`HH:mm`）、`hour`（`MM-DD HH:00`）、`day`（`MM-DD`）。
- 窗口由后端接口的 range 参数决定，保留期 7 天（`RETENTION_DAYS`）；前端无本地历史缓存。
- 轮询/竞态/失效由各页的 `useAdminLoader` 承载（见 `scripts/test-admin-console.mjs`），图表组件本身是无状态纯展示。

---

## 4. 在视图里加一张图（标准写法）

```vue
<script setup lang="ts">
import { computed } from 'vue'
import AdminChart from '@/components/admin/charts/AdminChart.vue'
import ChartCard from '@/components/admin/ChartCard.vue'
import { timeSeriesOption } from '@/components/admin/charts/options'
import type { ChartTokens } from '@/components/admin/charts/tokens'
import { series } from './series'

const queueChart = computed(() => (t: ChartTokens) => timeSeriesOption(t, {
  scale: scale.value,                       // 'minute' | 'hour' | 'day'
  format: value => String(Math.round(value)),
  series: [
    { name: 'Stream 长度', color: t.series[0], data: series(points.value, 'queue_length') },
    { name: '待确认 (pending)', color: t.series[1], data: series(points.value, 'queue_pending') },
  ],
}))
</script>

<template>
  <ChartCard title="任务队列积压" subtitle="最近 24 小时" :span="6">
    <AdminChart :option="queueChart" :height="220" label="任务队列积压趋势" />
  </ChartCard>
</template>
```

要点：

- `option` 是 **`(tokens) => spec` 的函数**，包在 `computed` 里；数据变化时 `AdminChart` 重新求值。
- `label` 必填，作为 `role="group"` 的 `aria-label`；SVG 本身 `aria-hidden`。
- 高度由 `AdminChart` 的 `height` 控制：时序 220、条形 200、迷你折线随 KPI 卡。
- `ChartCard` 的 `span` 取 3/4/5/6/7/8/12（12 栅格），窄屏统一变整行（`admin.css` 中 `.chart-grid > * { grid-column: 1 / -1 }`）。
- 想提供精读数值：给 `ChartCard` 传 `columns` + `rows`，右上角出现“数据表/图表”切换。
- 一页内多张同构图，参考 `AdminMonitor.vue` 的 `lines()` 辅助函数（接受“系列定义 + 格式化函数”，统一拼装 `timeSeriesOption`）。

---

## 5. 各类图的规格

### 5.1 时序图 `kind: 'time'`（`TimeSeriesChart.vue`）

- 系列支持 `type: 'line' | 'bar'`、`area`（面积）、`stack`（同 key 堆叠）、`dashed`（虚线，常用于“总容量”参考线）。
- Y 轴默认从 0 起；`max` 固定上限（如百分比 100），`fit: true` 改为贴合数据范围（上下各留 10% 边距，下限不低于 0）；`decimals` 允许小数刻度，否则取整刻度。
- 刻度由 `niceTicks()` 生成（1 / 2 / 2.5 / 5 × 10ⁿ 步长），`fixedMax` 时保留给定上限而不向上取整。
- `markLine: { value, label }` 画一条阈值红线（`--status-critical`），阈值会参与 Y 域计算。
- 悬停：十字线 + 圆点 + `ChartTooltip`，按时间点聚合所有系列；图例按钮可切换系列显隐（`aria-pressed`）。
- 线宽 2px，圆角线头（`.vc-line`）。

### 5.2 分类柱/条 `kind: 'bar'`（`BarChart.vue`）

- `horizontal: true` 为横向条形（阶段耗时、排行）；`labels: true` 在柱端显示数值；`stack` 同 key 堆叠。
- 只圆角“数据端”（`roundedRect()` 独立四角半径），堆叠相邻块用卡片底色描边（`.vc-bar--sep`）分隔。
- 长类别名用 `truncate()` 按宽度截断。

### 5.3 环形占比图 `kind: 'donut'`（`DonutChart.vue`）

- 实现：每个扇区一个 `<circle>`，`stroke-dasharray = "<弧长> <剩余>"`，`stroke-dashoffset = -累计偏移`，整体 `rotate(-90)` 从 12 点钟起顺时针。
- 半径 46、描边 14；多于一个非零扇区时用 `gap = 1.5` 分隔（`donutSlices(values, circumference, gap)`）。
- 中心显示合计（`total` 可覆盖）+ `title`；右侧图例带数值，悬停某扇区/图例项其余项降为 0.35 透明度，提示里显示“值 · 百分比”。
- 不用 `stroke-linecap: round`，否则相邻扇区互相覆盖。全 0 时只画轨道并显示“暂无数据”。
- 要做实心饼图：`r` 取描边宽度的一半、`stroke-width` 取 2r。

### 5.4 仪表 `kind: 'gauge'`（`GaugeChart.vue`）

- 120×120 视窗，`r = 50`，`stroke-width = 10`，圆头线帽；270° 开口朝下。
- 数学（`scale.ts`）：周长 `GAUGE_CIRC = 2π·50 ≈ 314.2`；可见弧 `GAUGE_ARC = 0.75 × 周长 ≈ 235.6`；轨道 `dasharray = "<ARC> <CIRC>"`，填充 `gaugeDash(ratio) = "<ARC×ratio> <CIRC>"`；`transform="rotate(135 60 60)"` 把起点放到左下角。
- 想改开口角度 θ：弧长 `= 周长 × (360-θ)/360`，旋转角 `= 90 + θ/2`（θ 为缺口角度）。
- **严重度着色**：`warn` / `critical` 是占比阈值（0–1），默认 `--viz-1`，≥ warn 变 `--status-serious`，≥ critical 变 `--status-critical`；填充色过渡 160ms，弧长过渡 400ms。
- **坑**：`round` 线帽在弧长为 0 时仍会画一个点，所以弧长 < 3 时把填充透明度置 0。
- 值与标签在圆环内用 SVG `<text>` 绘制；无值显示 `–`。

### 5.5 热力图 `kind: 'heatmap'`（`HeatmapChart.vue`）

- 单元格色阶用 `tokens.sequential` 同源的 `color-mix(--viz-1 N%, 卡片底色)`，图例 5 档（8 / 30 / 52 / 74 / 100%）。
- 悬停单元格描边高亮（`.vc-cell.is-active`）并弹出提示。

### 5.6 迷你折线 `kind: 'spark'`（`Sparkline.vue`）

用在 `KpiCard` 底部，不画坐标轴/网格/图例/提示，仅表达趋势。关键属性缺一不可：

```html
<svg viewBox="0 0 100 32" preserveAspectRatio="none" style="color: …">
  <path d="…area…" fill="currentColor" opacity=".12" />
  <path d="…line…" fill="none" stroke="currentColor" stroke-width="1.6"
        stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
</svg>
```

- `viewBox="0 0 100 32"` + `preserveAspectRatio="none"`：固定逻辑坐标系，被拉伸到任意宽度。
- `vector-effect="non-scaling-stroke"`：拉伸后线宽仍为 1.6px。
- `currentColor`：颜色由容器 `color` 控制（KPI 卡传 `tokens.series[accent]`）。
- 几何（`sparkPaths(values, max)`）：X 在 0–100 均匀分布；Y = `30 - value/top × 26`，即映射到 [4, 30]，上下留边防止线头被裁；`top = max(max, 数据最大值)`；零基线固定，不做自动下限缩放；`null` 断线；面积 = 折线 + 回到 y=32 底边闭合。
- 少于 2 个点不画。

### 5.7 横向进度条 `MeterBar.vue`

- 轨道：6px 高、药丸圆角，底色为 `--viz-1` 14% 混 `--muted`；填充 `--viz-1`，宽度过渡 300ms，**动画只靠 CSS，JS 只改 width**。
- 阈值变色：`.meter__fill.is-warn` → `--status-serious`，`.is-critical` → `--status-critical`。
- 堆叠分段（已用/预留/空闲）用 `.stacked-meter` + `.legend-row`，三态固定映射 `--viz-1`（used）/ `--viz-4`（reserved）/ `--viz-3` 混灰（free）。

### 5.8 状态点与健康横幅

`.status-dot`（8px）与 `.health-banner` 用 `is-positive / is-warning / is-negative` 三态映射 `--status-good / -warning / -critical`；严重度必须同时有文字，不能只靠颜色。

---

## 6. 布局、响应式与无障碍

- `ChartCard` 放进 `.chart-grid`（12 栅格，`gap 16px`）；窄屏（见 `admin.css` 媒体查询）所有卡片变整行，KPI 卡的 sparkline 左右出血边距随之收窄。
- 图表容器 `min-width: 0`，宽度完全由 `useChartSize` 的 `ResizeObserver` 驱动；`width/height ≤ 8` 时不渲染（避免首帧闪烁/负尺寸）。
- 动效：仪表、环图、热力图有短过渡；`admin.css` 在 `prefers-reduced-motion: reduce` 下对 `.admin-shell` 内所有元素全局关闭 transition/animation（并对图表元素另有显式规则）。新增动画不要用内联脚本驱动，保持纯 CSS 以受该开关约束。
- 触控：命中层 `.vc-hit` 设置 `touch-action: pan-y`，竖向滑动仍可滚动页面。
- 无障碍：`AdminChart` 容器 `role="group"` + `aria-label`；SVG `aria-hidden`；图例是真 `<button>` 带 `aria-pressed`；提供数据表切换作为等价文本。

---

## 7. 扩展清单

### 新增一种图（kind）

1. `options.ts`：加 `XxxSpec` 接口、并入 `ChartSpec` 联合类型、加 `xxxOption()` 构造器。
2. `charts/XxxChart.vue`：用 `useChartSize` 取尺寸，颜色只用 spec 里传来的令牌值或 CSS 变量，布局/刻度逻辑抽到 `scale.ts`。
3. `AdminChart.vue`：加一条 `v-else-if="spec.kind === 'xxx'"`。
4. `admin.css` 的 `/* SVG charts */` 段加 `.vc-xxx` 样式（用 `.admin-shell` 前缀与现有变量，动画保持纯 CSS）。
5. 纯函数写进 `scale.ts` 并在 `scripts/test-admin-charts.mjs` 加用例。

### 新增指标

1. 后端采样：`backend/platform/metrics_sampler.py` 写入样本；聚合类走 `backend/services/admin_analytics.py`。
2. API 返回字段 → `src/api/admin.ts` 的 `MetricPoint` 类型。
3. 视图里 `series(points, 'key')` 取序列，按 §4 写 spec。
4. 若是“某实体的一条线”，先确认该实体是否已有固定颜色（`groupColor`），再取 `tokens.series[n]`。

### 提交前检查

- [ ] 颜色全部来自 `tokens` / CSS 变量，亮暗两套都看过。
- [ ] 缺失样本为 `null`，没有被写成 0。
- [ ] 数字元素 `tabular-nums`，无数据显示 `–`。
- [ ] `AdminChart` 传了 `label`；重要图配了 `ChartCard` 数据表。
- [ ] 窄屏（390px）不出现横向滚动，卡片整行。
- [ ] 新增动画为纯 CSS，受 `prefers-reduced-motion` 约束。
- [ ] 改了 `scale.ts` 就补/跑 `npm.cmd run test:admin-charts`；最终 `npm.cmd run typecheck` 与 `npm.cmd run build` 通过。

---

## 8. 已知限制

- 图表无本地历史缓存，范围受后端保留期（7 天）和采样间隔（30 秒）限制；需要更长曲线应改后端保留策略，而不是前端拼接。
- API 延迟序列来自 API 进程内存，重启后清零，且多 API 进程各自独立。
- 手写 SVG 不做虚拟化：单图点数建议保持在数百量级（7 天/小时桶约 168 点，足够）。
- 配色上限 8 档；超过需要合并“其他”或改用图表类型（柱/表）而非加色。
- `color-mix()` 用于面积/色阶/轨道，需要较新的浏览器（Chrome 111+、Safari 16.2+、Firefox 113+）。
