## P-20261001T104021Z-p5h4n7 写 gh --body-file 临时文件：Write 工具与 Git Bash 的 /tmp 不是同一处
有效性：active
环境：本宿主（Claude 桌面应用 + Git Bash，Windows）；首次观察于 PR #33 审核（2026-10-01，session S-20261001T104021Z-k3d8f2）

### 症状（observed）

用 Write 工具写 `/tmp/pr33-comment.md` 后执行 `gh pr comment 33 --body-file /tmp/pr33-comment.md`，报「文件不存在」；改用 Bash heredoc 写实际落盘路径（`/d/tmp/...`）后成功。

### 根因（inferred）

Write 工具的临时路径解析与 Git Bash 的 `/tmp` 映射不一致（Windows 宿主两者指向不同位置）。

### 规避（verified 一次成功）

`--body-file` 所需临时文件一律用 Bash 直接写：`cat > /tmp/xxx.md << 'EOF' ... EOF`（注意 Git Bash 下 `/tmp` 即 `D:\tmp`，`gh` 能直接读到）；不要用 Write 工具生成该文件。派生审核子 Agent 时同样要在其 prompt 中写明这条。
