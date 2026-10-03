# 模块：角色配音 overlay 键盘焦点
路径：src/views/Voices.vue（handler 与状态）、src/views/voices/VoicesWorkbench.vue（行级入口）
记录状态：known（2026-10-01 PR #33 深读）
来源：session S-20261001T104021Z-k3d8f2、决策 D-20261001T104021Z-r4c6a1

## 现行行为（PR #33 合并后为准）

- `overlayKeydown` 绑定在页根 `.voices-page` 的 `@keydown`（Voices.vue:583），处理三个 overlay（picker/merge/confirm，`activeOverlay` 计算属性裁决优先级 confirm > merge > picker）。
- Escape：请求在途（`pickerBusy`/`mergeBusy`）时不关闭；否则逐级关闭。
- Tab 焦点陷阱：仅遍历 overlay 面板内可用控件（`overlayControls`：未 disabled 的 button/input/textarea/a[href] 且有可见 rect）；**面板零可用控件（请求在途）时早退、不 preventDefault，交由浏览器原生 Tab**（`579867f` 引入，回归用例钉在 `scripts/test-voices-workbench.mjs` "an in-flight picker ... does not swallow Tab"）。
- overlay 打开/关闭的焦点捕获与归还：`overlayReturnFocus`/`confirmReturnFocus` + `watch(activeOverlay)`（`data-initial-focus` 优先）。
- 背景未 inert（已知限制，见决策卡）；行级变更入口位于 `VoicesWorkbench.vue` 的 `:inert="overlayOpen"` 区内（:155），overlay 打开时键盘不可达。

## 开发约束

- 改 overlay 键盘行为必须过 `npm run test:voices-workbench`（node:test + vm 沙箱加载真实 `Voices.vue` script，fixture 模拟 panel 与 document.activeElement）；新增行为应补对应用例。
- 焦点陷阱改动属前端逻辑变更：按 CLAUDE.md 跑 typecheck + build，回归脚本为最低门禁。
