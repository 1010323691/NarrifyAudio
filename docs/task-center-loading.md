# 任务中心分层加载验收（2026-10-07）

任务中心立即显示八个阶段及页面框架，先读取阶段计数，再读取当前阶段的项目页（5 个项目）。项目弹窗按需读取当前任务页（50 条），不预取其他阶段、项目或任务页。各区域独立加载与重试，自动刷新失败保留旧数据；可见期间每次请求结束 5 秒后刷新，隐藏、停用和退出登录时停止。

## 接口与部署

- `GET /api/v1/tasks/center/summary`：阶段计数，不读取任务事件、日志或结果。
- `GET /api/v1/tasks/center/groups?category=script&page=1`：项目摘要，含整个项目阶段的完成数及控制计数。
- `GET /api/v1/tasks/center/items?category=script&project_id=…&filter=all&page=1`：当前任务页及总数，`filter` 支持 all、active、completed；仅读取该页最新进度。
- `POST /api/v1/tasks/batch-control?compact=true`：批量操作整个项目阶段，只返回 changed；旧调用继续返回 tasks。

读取语义统一为当前用户、未删除项目、隐藏 failed/cancelled 与已替代终态行。数据库按任务注册表中的身份字段一次排序去重，保留所有活动任务；先参与去重再隐藏 failed，cancelled 不参与替代。缺少身份的条目与未注册类型仍保留独立记录。针对全部注册任务类型、数组身份、缺失/null、多字段身份和同时间 ID 排序，已验证新旧去重结果一致。

上线前应用 Alembic `0022_task_center_indexes`（正常启动入口会按需迁移），并部署对应前端构建。新增三个复合索引，只增加查询索引，不清理历史数据，也不引入缓存服务或汇总表。原有历史接口、SSE 与其他工作台的订阅保持兼容。性能测试没有连接部署数据库，也未迁移用户数据。

## 性能记录

隔离的内存 SQLite，1 个项目、1000 条独立已完成任务、20000 条日志（每条日志 120 字符）。同一进程内执行，记录一次代表性运行；这些数值不是生产 PostgreSQL 的耗时承诺，也不包含网络传输和浏览器渲染。

| 读取范围 | 时间 | SQL 次数 | JSON 响应大小 |
|---|---:|---:|---:|
| 原方案：首批 50 条历史 + 首个完整 SSE 快照 | 429.8 ms | 1606 | 816519 B |
| 新方案：阶段摘要 | 9.7 ms | 1 | 包含在下一行 |
| 新方案：阶段摘要 + 首个项目页 | 20.9 ms | 3 | 957 B |

原方案基线没有计入后续最多 9 页历史自动预加载，新方案没有明细预加载。测量直接调用服务及原快照生成路径；旧 SSE 查询次数包含任务刷新、重复读取事件、结果关系及缺失字段查询。

SQLite EXPLAIN QUERY PLAN 验证摘要使用任务复合索引。回归断言一级读取完全不访问 task_events/task_results，明细最多 50 条、最多 3 条 SQL；日志数量和页内任务数量不会增加逐任务 SQL 调用。生产 PostgreSQL 执行计划尚未验证。

## 回归结果

- `python -m pytest backend/tests -n 4 --dist loadscope -q`：1358 passed，15 skipped。
- `python -m pytest backend/tests/test_task_center.py -q -s`：7 passed，含权限、状态、去重、分页、分类、兼容响应及性能记录。
- `npm run test:task-center`：10 passed，含渐进加载、默认阶段、切换取消、缓存隔离、区域错误、后台停刷、页码回退和控制后刷新。
- `npm run test:state-isolation`：18 passed。
- `npm run typecheck`、`npm run build`、`npm run lint:imports`：通过。
- `npm run test:task-center-browser`：1440×900 与 390×844 通过，接口均由隔离测试数据拦截；摘要返回前阶段框架可见，未展开时不请求明细，明细延迟不影响一级内容，5000 条任务每页仅渲染 50 条，翻页及 Escape 关闭通过。

浏览器回归需先启动 Vite，默认使用 127.0.0.1:5173；可通过 NARRIFY_LAYOUT_URL 指定预览地址，NARRIFY_LAYOUT_SCREENSHOTS 指定截图输出目录。本次截图保存在本地 docs/screenshots/task-center/（按仓库现有规则忽略，不纳入源码）。
