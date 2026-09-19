# jira-tool 固定操作索引 —— 常用操作直接复制命令，不要再写脚本

**运行器固定**：`D:/LZY_project/jira/.venv/Scripts/python.exe`（下表写作 `$PY`）
**cwd 任意，但绝不 `cd D:/LZY_project/jira`**（目录名 `jira` 会污染 `import jira`）。
**脚本目录**：`C:/Users/user/.claude/skills/jira-tool/scripts/ops/`（固定基建，别随手改；改完必须重跑自检并同步本表）。

## 0. 读这张表前先记住

| 项 | 值 |
|---|---|
| 🔒 标记 | 写/破坏性操作 → **必须先 `--dry-run`，把 payload 给用户确认后才实跑**（去掉 `--dry-run` 重跑同一条命令） |
| 成功输出 | stdout 纯 JSON（`--compact` 变一行省 token） |
| 失败输出 | stderr `❌ ...`（写操作另附 `field_errors={...}` 逐字段原因） |
| 退出码 | 0 成功 · 1 脚本错误 · 2 参数错误 · 3 鉴权失败(401/403) · 4 Jira API 错误 |
| 传 JSON | `--data '{"k":1}'` 或 `--data @D:/tmp/p.json` 或 `--data -`（stdin）；**Windows 长中文 JSON 一律 `@file`** |
| 更安全第一步 | `--data-only`（连请求都不发，只打印合并后的 payload） |
| Server 非 Cloud | user 字段一律 `{"name": ...}`；datetime 必须 `YYYY-MM-DDTHH:MM:SS.000+0800`；`jira.createmeta()` 不可用（本表 createmeta.py 已用替代 API） |

## 1. 一眼选命令

| 我要… | 用这个 |
|---|---|
| 查一批 issue | `search.py` |
| 看一条 issue 详情 / 查 customfield 实际值 | `get_issue.py --raw` |
| 看评论 | `get_comments.py` |
| 加评论 | 🔒 `add_comment.py` |
| 建 issue（非 AERDM 标准任务） | 🔒 `create_issue.py` |
| 改字段 | 🔒 `update_issue.py` |
| 看有哪些流转 / 流转状态 | `transition.py --list` / 🔒 `--transition-id` |
| 看能指派给谁 / 指派 | `assign.py --list-assignable` / 🔒 `--user` |
| 附件 列/传/下 | `attachments.py list\|add\|download` |
| 关注者 列/加/删 | `watchers.py list\|add\|remove` |
| 列项目 / 看单项目 | `projects.py` |
| 查项目+类型的必填字段与枚举 | `createmeta.py --type 任务` |

## 2. 命令速查（复制改参数即可）

| # | 操作 | 命令 | 只读/🔒 | 输出要点 |
|---|---|---|---|---|
| 1 | 搜索 | `$PY C:/Users/user/.claude/skills/jira-tool/scripts/ops/search.py --project AERDM --limit 3` | 只读 | `total / returned / truncated / issues[].key,summary,status` |
| 2 | 搜索(便利过滤) | `$PY .../ops/search.py --project AERDM --status 进行中 --assignee luziyu --limit 20` | 只读 | 同上 |
| 3 | 搜索(全量翻页) | `$PY .../ops/search.py --project AERDM --all --limit 500` | 只读 | `truncated:false` 才是全量 |
| 4 | 搜索(自由文本) | `$PY .../ops/search.py --project AERDM --text "WB101 串口" --limit 10` | 只读 | 同上 |
| 5 | 搜索(完整 JQL) | `$PY .../ops/search.py --jql "project=AERDM AND assignee=luziyu order by updated desc"` | 只读 | 同上 |
| 6 | 单条详情 | `$PY .../ops/get_issue.py AERDM-123 --raw` | 只读 | `raw_fields` 含全部 customfield 实际值 |
| 7 | 单条(省流量) | `$PY .../ops/get_issue.py AERDM-123 --no-comments --fields summary,status` | 只读 | 只取指定字段 |
| 8 | 评论列表 | `$PY .../ops/get_comments.py AERDM-123 --limit 50` | 只读 | `total / returned / comments[].id,body,author` |
| 9 | 单条评论 | `$PY .../ops/get_comments.py AERDM-123 --id 10501` | 只读 | `comment.body` |
| 10 | 加评论 | 🔒 `$PY .../ops/add_comment.py AERDM-123 --body "内容" --dry-run`（确认后去掉 `--dry-run`） | 🔒 | `comment.id / comment.body`；长评论用 `--body-file D:/tmp/c.txt` |
| 11 | 建 issue(看字段) | 🔒 `$PY .../ops/create_issue.py --project AESW --type 缺陷 --summary "标题" --data-only` | 只读 | `payload.fields`（零请求） |
| 12 | 建 issue | 🔒 `$PY .../ops/create_issue.py --project AERDM --type 任务 --summary "标题" --dry-run`（确认后去掉） | 🔒 | `payload + required + missing_required`；customfield 走 `--data @file` |
| 13 | 改字段(看快照) | 🔒 `$PY .../ops/update_issue.py AERDM-123 --summary "新标题" --dry-run` | 只读 | `payload + before`（零写入；`--data-only` 连读都不发） |
| 14 | 改字段 | 🔒 `$PY .../ops/update_issue.py AERDM-123 --add-labels x,y --dry-run`（确认后去掉） | 🔒 | `changed[]`；值相同自动跳过（`reason:no_change`） |
| 15 | 看流转 | `$PY .../ops/transition.py AERDM-123 --list` | 只读 | `transitions[].id,name,to_status,required_fields`（流转屏必填） |
| 16 | 流转 | 🔒 `$PY .../ops/transition.py AERDM-123 --transition-id 31 --expect-status 待验证 --comment "原因" --dry-run`（确认后去掉） | 🔒 | `before_status / after_status`；`--expect-status` 不符直接拒执行 |
| 17 | 指派候选 | `$PY .../ops/assign.py AERDM-123 --list-assignable --query luzi` | 只读 | `candidates[].name,displayName` |
| 18 | 指派 | 🔒 `$PY .../ops/assign.py AERDM-123 --user luziyu --dry-run`（确认后去掉） | 🔒 | `resolved_name` 必须**精确**等于候选之一（模糊匹配会指错人） |
| 19 | 附件列表 | `$PY .../ops/attachments.py list AERDM-123` | 只读 | `attachments[].id,filename,size` |
| 20 | 传附件 | 🔒 `$PY .../ops/attachments.py add AERDM-123 --file D:/tmp/a.log --dry-run`（确认后去掉） | 🔒 | `added[].attachment_id` |
| 21 | 下附件 | `$PY .../ops/attachments.py download --id 10001 --out D:/tmp` | 只读 | `path / size`；也可 `--key AERDM-123 --name 文件名` |
| 22 | 关注者 | `$PY .../ops/watchers.py list AERDM-123` / 🔒 `add AERDM-123 --user luziyu --dry-run` | 只读/🔒 | `watchers[].name / added[]` |
| 23 | 列项目 | `$PY .../ops/projects.py --filter AE` | 只读 | `projects[].key,name,lead` |
| 24 | 项目详情 | `$PY .../ops/projects.py --key AERDM --with-components --with-versions` | 只读 | `components / versions` |
| 25 | 必填字段 | `$PY .../ops/createmeta.py --project AERDM --type 任务 --required-only --with-allowed` | 只读 | `required[].id,name,type,allowed_values`（AERDM/任务=7 项） |
| 26 | 类型清单 | `$PY .../ops/createmeta.py --project AERDM --list-types` | 只读 | `issue_types[].id,name,subtask` |

> 表中 `.../ops/` = `C:/Users/user/.claude/skills/jira-tool/scripts/ops`。

## 3. 什么时候**不用**这张表

| 场景 | 走哪 |
|---|---|
| 按描述建 AERDM 标准任务 | `cvte-jira-create-task`（字段默认/推断/守卫都在那边） |
| 表里没有的复杂需求（跨项目报表、批量带逻辑、导出 CSV） | SKILL.md §3/§4 现写脚本 —— 可 `sys.path.insert(0, r"...\jira-tool\scripts"); import jira_ops as jo` 直接复用函数，不必抄 mapper |
| mcp-atlassian 能直接做的 | 先 MCP（但 403/WAF 时回来用本表） |
| 删除 issue | 无固定脚本（低频高危）—— 现写脚本 + `AskUserQuestion` 确认 |

## 4. 出错对照

| stderr 特征 | 退出码 | 处置 |
|---|---|---|
| `Jira API 错误 HTTP 401/403` 或 `Jira 鉴权失败` | 3 | PAT 过期/权限不足 → 换环境变量 `JIRA_PAT`，或更新本 skill `scripts/config.json` 的 token |
| `{op}失败 HTTP 400/422` + `field_errors={...}` | 4 | 对照 `field_errors` 逐字段修 payload；缺枚举用 `createmeta.py` 查；datetime 补 `.000+0800`；user/option 用对象 |
| `❌ ... 文件不存在` / `不是合法 JSON` / `不是精确匹配的 name` | 2 | 修 `@file` 路径或 JSON 语法；指派/关注者改用 `--list-assignable` 查到的精确 name |
| `流转 'xx' 对当前用户不可用。可用：[...]` | 2 | 从 `--list` 输出里选正确流转 id |
| `ImportError: cannot import name 'JIRA'` | 1 | 你 `cd` 进了 `D:/LZY_project/jira` → 换任意其它 cwd 用绝对脚本路径重跑 |
| `❌ 脚本执行失败：...` | 1 | 脚本内部错误 → 原样把 stderr 给用户，别猜 |

## 5. 自检（改动 ops/ 后必跑）

```bash
PY=D:/LZY_project/jira/.venv/Scripts/python.exe
OPS=C:/Users/user/.claude/skills/jira-tool/scripts/ops
# ① 12 个 --help 全部退出码 0
for f in search get_issue get_comments add_comment create_issue update_issue transition assign attachments watchers projects createmeta; do
  "$PY" "$OPS/$f.py" --help >/dev/null 2>&1 || echo "FAIL: $f"; done
# ② 只读抽查
"$PY" "$OPS/search.py" --project AERDM --limit 3
"$PY" "$OPS/createmeta.py" --project AERDM --type 任务 --required-only
# ③ 写脚本只跑到 --dry-run / --data-only，不实跑
```
