# 字符替换为下划线的逻辑审查报告

日期：2026-10-07。范围：当前工作区源码；仅审查并生成报告，未修改业务代码。

## 结论

共找到 **9 处把字符替换为 `_` 的生产逻辑**，全部位于后端，其中 8 处涉及文件或任务命名，1 处仅用于配置键识别。没有发现前端、独立 TTS Worker 或启动脚本中的同类替换。

替换 Windows 禁用字符本身合理，内部任务产物命名和角色候选音频命名也有合理用途。主要问题集中在 `safe_display_name`：它把大量合法标点也替换成 `_`，承担了显示名、磁盘名、目录名、ZIP 成员名等多种职责，还直接截断整个文件名。不同阶段使用不同规则，因此需要额外的别名回退才能维持流程一致。

建议优先修复扩展名丢失和 ZIP 成员重名，然后收敛命名规则，补齐 Windows 保留名称检查。修改命名规则需要兼容历史文件，不能直接全局替换后重命名用户数据。

## 一、检查方法与边界

- 全仓搜索 `replace`、正则 `sub`、`translate`、字符遍历、SQL `replace` 以及 Python、TypeScript、Vue、JavaScript、PowerShell、Shell 中的下划线字符串；复核 Git 跟踪源码。
- 排除依赖目录、构建产物、Git 元数据、运行时用户数据。测试和文档用于确认既有契约，不计入生产逻辑数量。
- 逐项追踪上传、章节生成、任务发布、文件读取、解析状态、音频合并、ZIP 导出的调用路径。
- 从源码提取函数进行无文件写入的边界复现；另用内存 ZIP 验证重复成员行为。
- 执行相关现有回归：**16 passed**。未执行 Windows 真机验收、真实音频合成或完整后端套件。

本报告将函数输出已复现的问题与调用链推导的风险分别注明，不把已有回退机制覆盖的行为误判成必然失败。

## 二、全部替换点

| # | 位置 | 替换规则和用途 | 判断 |
|---|---|---|---|
| 1 | `backend/platform/storage.py:35–38`，`safe_display_name` | `[^\w.()\- ]+` → `_`；取 basename，去首尾空格和点，限制 180 字符。用于上传、发布、临时文件、用户目录、导出等 | **需要调整**：清洗过度，且截断损坏扩展名；不能独自保证唯一性或完整 Windows 合法性 |
| 2 | `backend/engines/book.py:871–874`，`sanitize_file_name` | `\\ / : * ? " < > \|`、ASCII 控制字符和 DEL → `_`；归并空白。用于分册及全书文件名 | **基本合理**：保留中文合法标点；但最终发布又被 #1 清洗，且未处理长度和设备保留名 |
| 3 | `backend/engines/voices.py:249–250`，`_sanitize` | 除 Unicode `\w` 和 `-` 外逐字符替换，再转小写；角色名形成候选 WAV 名的一部分 | **基本合理**：内部文件标签允许更严格；真实角色身份仍用原名，文件名还带命名空间和候选序号 |
| 4 | `backend/core/paths.py:35–39`，`merged_audio_filename` | 九类 Windows 路径禁用字符逐字符 → `_`；strip 后加 `.mp3`，空值回退 `audiobook` | **方向合理，规则不完整**：输出名与锁标识复用值得保留；控制字符、设备保留名和长度未覆盖 |
| 5 | `backend/engines/tts_manifest.py:211–219`，`_safe_package_name` | 同 #4，返回 stem；用于合并产物定位、失效清理、BGM 侧文件定位 | **兼容性设计合理**：明确保留历史命名；但与 #4 重复维护，合法性遗漏相同 |
| 6 | `backend/api/bgm.py:208–224`，`_source_txt_base` | 同 #2；以源 TXT 的 stem 生成打包目录名，空值回退“有声书” | **局部合理，但后续不一致**：Worker 打包时再次调用 #1，合法标点仍会丢失 |
| 7 | `backend/platform/task_engine_support.py:127` | 任务类型中的 `.` → `_`，再拼接 `attempt_id`，生成结果 JSON 文件名 | **合理**：内部固定任务类型加独立 attempt 标识；原任务类型也保存在元数据中，无需用户可读名称保真 |
| 8 | `backend/platform/task_worker.py:148–151` | 在 SQL 中对包名执行九次字符 → `_` 替换；用于筛选互不冲突的任务 | **用途合理，但需防漂移**：必须与 #4 的真实目标名保持一致；SQL trim/lower 与 Python strip/casefold 并非完全相同 |
| 9 | `backend/services/resource_center.py:20`，`_redact_configuration` | 配置键中的 `-` → `_`，辅助识别需要遮蔽的凭据字段 | **合理**：仅修改临时识别值，返回对象的键保持原样 |

不计入上述九处：`backend/platform/resource_inventory.py:366` 把 `_` 转义成 `\_`，是 SQL LIKE 转义；`backend/engines/audio.py:206` 是追加分隔符，均不是本次目标替换。

## 三、主要发现

### 1. 合法标点被过度清洗，形成多对一别名

**优先级：中。证据：函数输出已复现，调用链已有专门兼容逻辑。**

`safe_display_name` 使用允许列表而非 Windows 禁用字符列表；Unicode `\w` 能保留汉字，却不保留中文标点、全角括号、破折号、Emoji 等。

实际输出：

| 输入 | 章节引擎输出 | 存储层输出 |
|---|---|---|
| `第 001 章 你好，世界—再见！.txt` | 原样保留 | `第 001 章 你好_世界_再见_.txt` |
| `甲，乙.txt` | 原样保留 | `甲_乙.txt` |
| `甲—乙.txt` | 原样保留 | `甲_乙.txt` |
| `甲_乙.txt` | 原样保留 | `甲_乙.txt` |
| `a??b.txt` | `a__b.txt` | `a_b.txt` |

其中 `+` 使连续非法字符只生成一个 `_`，进一步扩大不同名称之间的合并范围。合法标点无需为普通 Windows 文件系统兼容而删除，参见 [Microsoft 文件命名规则](https://learn.microsoft.com/en-us/windows/desktop/fileio/naming-a-file)。

程序已经承担规则漂移的维护成本：

- `backend/api/files.py:114–121`：精确名找不到时，尝试存储清洗别名。
- `backend/services/script_parse_state.py:345–380`、`:532` 起：解析状态、输入匹配和任务在途判定建立清洗后的索引。
- `backend/tests/test_script_parse_state.py:475` 起：已有中文标点引发磁盘名与表名不同的回归案例。

这些回退解决了已覆盖场景的读取问题，但别名是多对一，无法普遍证明两个名称代表同一个文件。建议保留合法标点，用 `file_id` 和实际 `object_key` 定位；历史别名回退应检测歧义。

### 2. 180 字符截断会丢掉扩展名

**优先级：高。证据：函数输出已复现，下游按扩展名扫描的影响由源码推导。**

`backend/platform/storage.py:38` 对整个文件名执行 `candidate[:180]`。

输入 `"file" + "a" * 176 + ".txt"`，清洗后的结果只剩前 180 字符，`.txt` 完全消失。长章节标题同样可能让末尾 `.txt` 被截掉。

上传会先调用该函数，后续分册发布也会经过该函数。依赖 `*.txt`、`*.json` 或 `.suffix` 的阶段可能不再把产物识别为对应资源。这里的限制与替换共同构成命名契约，不能只检查禁用字符。

`available_file_name` 在添加重名后缀时有扩展名预算逻辑，但它先执行 `safe_display_name`，因此无法恢复已经被截掉的扩展名。现有长名称测试覆盖了 suffix 添加，没有覆盖这类初始截断。

建议按“stem + 重名后缀 + extension”统一分配长度预算，扩展名优先保留；同时分别考虑 Windows 路径限制与 Linux 文件名的字节限制，180 个 Unicode 字符并不保证跨平台落盘成功。

### 3. ZIP 清洗后没有去重，可产生同名成员

**优先级：高。证据：函数映射和内存 ZIP 行为已复现，端到端任务未执行。**

`backend/platform/engine_task_executor.py:257–260` 对每个音频 ZIP 成员执行 `safe_display_name` 后直接 `output.write(..., arcname=name)`，没有占用名集合或冲突检查。API 允许提交成员显示名，见 `backend/api/audio.py:185–223`。

两个不同音频的显示名为 `甲，乙.mp3`、`甲—乙.mp3` 时，都变成 `甲_乙.mp3`。内存 ZIP 复现得到两个同名成员；按名字读取取得后写入的数据。解压工具如何处理重名因工具而异，但这种归档无法稳定地逐名引用两个不同文件。

上传已有 `available_file_name` 和排他创建保护，不能据此推断 ZIP 也安全。

建议对最终 arcname 去重，大小写不敏感比较，必要时生成 ` (2)` 后缀，并保留源文件与归档名的对应关系。

### 4. Windows 安全性检查仍有遗漏

**优先级：中。证据：未过滤值已复现，Windows 真机行为未执行。**

- `safe_display_name("CON.txt")` 仍为 `CON.txt`；`NUL`、`PRN`、`AUX`、`COM1` 等设备保留名也未被统一处理。
- `_safe_package_name` 和 `merged_audio_filename` 不过滤 `\x00`、内部制表符及其他 ASCII 控制字符。例如后者将 `a\x00b` 转为仍带 NUL 的 MP3 名。
- `sanitize_file_name` 不去除末尾点；当前主要调用方通常会附加 `.txt`，因此不能把该遗漏一概视为当前章节路径的必现故障。
- 上述音频包函数没有文件名长度预算。

Windows 禁用控制字符、设备保留名以及末尾点/空格等，参见 [Microsoft 文件命名规则](https://learn.microsoft.com/en-us/windows/desktop/fileio/naming-a-file)。DEL 的额外过滤可以作为应用策略，但不应与 Windows 官方禁用范围混为一谈。

项目目录验证已有保留名处理，见 `backend/platform/workspace_layout.py:33–39`；文件命名入口尚未共享同样的底线。建议在 core 层建立最小跨平台命名规则，避免 engines 反向导入 platform。

### 5. 用户名目录发生多对一映射

**优先级：中。证据：校验规则和函数输出已复核，未执行注册流程。**

`backend/api/auth.py:35–39` 允许用户名尾部出现点，数据库按用户名精确判重。但目录使用 `safe_display_name(username)`。

`reader`、`reader.`、`reader..` 均满足长度和字符规则，并映射到目录段 `reader`。这不是字符转 `_` 本身造成的，而是同一清洗函数的去尾点行为造成的身份合并，故作为关联问题记录。

项目目录冲突检查仍然存在；本审查未证明越权读取。但不同账号共享同一父目录命名空间，会导致目录冲突或额外的隔离维护风险。建议以不可变用户 ID 作为存储身份，或在注册时拒绝会发生目录别名冲突的用户名；历史目录需要兼容迁移。

### 6. 原始名、实际名和发布身份仍混用

**优先级：中。证据：调用链确认，具体覆盖场景取决于输入。**

`write_task_outcome` 在 `backend/platform/task_engine_support.py:58、67` 提前清洗输出名；分册 metadata 的 `files[].name` 则使用章节引擎原始名。`backend/platform/task_worker.py:1246` 又按清洗名选取发布目标和文件记录；带 `publish_module` 的分支会复用同一目标，未走 `available_file_name`。

同一阶段重新执行时覆盖同一逻辑产物是合理的，但不能把“两个名称清洗后相同”自动认定为“同一逻辑产物”。本次未证明正常智能分册会发生不同章节互相覆盖：其章节编号位于文件名前部，能够区分章节。

建议发布前同时检查原始身份和最终目标名；保持独立的原始显示名、实际落盘名、文件 ID。对同一批次的最终文件名应做唯一性断言，而不是依赖清洗函数。

### 7. SQL 与 Python 的目标名规则重复维护

**优先级：低。证据：源码确认；已有执行期锁降低影响。**

`merged_audio_filename` 与 `_safe_package_name` 当前九类字符替换一致，测试也确认既有快照。SQL 再复制一份字符替换，却用数据库 trim/lower，Python 用 strip/casefold；特殊空白和 Unicode 大小写存在差异。

SQL 层未完全复刻空值回退。现有 Python 二次领取检查及执行期按目标音频名生成的排他锁提供保护，见 `backend/platform/task_worker.py:197–218、243–257`；不能因此直接下结论为并发覆盖漏洞。

建议由核心命名函数产生规范化目标身份，在任务提交时记录，再由 SQL 筛选、Python 检查和锁共同使用。长期避免三处重复实现。

## 四、其余逻辑为什么可以保留

- **角色候选音频**：`_sanitize` 会失真，但只作为内部文件标签。`backend/engines/voices.py:826–847` 另有每角色命名空间和候选序号，原角色名仍是配置键，因此不需要为了显示保真而强行保留全部标点。命名空间目前用 `time.time_ns()`；若要严格保证唯一，宜改显式随机 ID，不能把时间戳当作数学上的唯一性保证。
- **任务结果 JSON**：任务类型中的点改成下划线只是固定内部命名习惯，attempt ID 区分不同尝试。原始任务类型保存在 `metadata["engine"]`。
- **配置脱敏**：横线改下划线只发生在临时识别值中，输出仍用原键。当前敏感词都是子串判断，因此该替换对既有词表影响有限，但不会损坏配置名称。
- **BGM 包名**：源名优先选择未排版 TXT、回退最新候选的策略，与本次字符替换无直接冲突；问题在于生成 base 后，Worker 再执行更严格清洗。

## 五、建议实施顺序

1. 修复初始截断的扩展名丢失；对 ZIP 最终成员名增加去重。这两项可先局部修复。
2. 明确显示名、文件 ID、存储名和锁目标各自的契约；减少依赖字符串别名推断文件身份。
3. 在 core 层统一最小文件名安全规则：保留合法标点，覆盖控制字符、设备保留名、尾点和长度预算；所有路径仍需独立做目录边界校验。
4. 保留历史命名版本和旧路径读取；迁移前检查碰撞，不能自动覆盖历史数据。
5. 补充边界回归：中文标点、连续禁用字符、不同名同一别名、180 字符附近扩展名、Unicode 字节长度、ZIP 重名、用户名尾点、Windows 设备名，以及锁目标一致性。

## 六、验证记录

执行命令：

```bash
.venv/bin/python -m pytest backend/tests/test_book.py backend/tests/test_merge.py backend/tests/test_voices.py backend/tests/test_tts_batch.py backend/tests/test_workspace_layout.py backend/tests/test_script_parse_state.py -k 'sanitiz or filename or safe_package or long_collision or storage_name' -n 4 --dist loadscope -q
```

结果：**16 passed in 1.21s**。

此外对五个命名函数做了源码提取复现，对重复 ZIP 成员做了内存验证。测试通过说明既有约定仍成立，不表示上述新边界已经被覆盖。


## 七、修复记录（2026-10-07）

按后续修复请求落实了以下改动，前面的检查结论保留作为修复前记录：

- 在 `backend/core/filenames.py` 集中定义文件名规则，保留合法 Unicode 标点，处理控制字符、Windows 设备保留名和首尾点/空格。
- 按扩展名、字符数、UTF-8 字节数分配长度预算；缩短名称时加入稳定摘要，避免只靠共同前缀识别文件。
- ZIP 最终成员名按大小写不敏感规则去重；同时修复归档关闭前提取字节导致缺少中央目录的问题。
- 发布前拒绝同批重名，以及与已有不同名称文件的冲突，避免覆盖原文件；原始显示名与落盘名分别保存。
- 解析状态优先按输入文件 ID 匹配；旧名称仅通过唯一的记录路径/别名匹配，保留历史文件读取兼容。
- 音频合并、失效清理和任务锁共用目标命名函数；任务提交时由服务端写入调度身份，SQL 不再重复清洗逻辑。旧任务缺少此身份时保守串行领取。
- 新注册拒绝保留名称、尾点及与历史账号存储目录冲突的用户名；已有账号继续使用原目录，没有批量重命名或数据迁移。
- 角色候选音频采用受长度预算约束的文件名；批内命名空间显式按角色序号区分。

最终验证：完整后端套件 `-n 4 --dist loadscope`：**1328 passed、15 skipped**；新增文件名回归共 23 个测试用例，包含发布冲突和 ZIP 内容完整性验证。`npm run typecheck`、`npm run build`、`npm run lint:imports`、`npm run test:script-parse-workbench`（13 项）和 `npm run test:workbench`（22 项）均通过。未执行 Windows 真机或真实模型验收。
