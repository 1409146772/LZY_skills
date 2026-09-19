---
name: jira-tool
user-invocable: true
description: "USE as a general-purpose FALLBACK for any Jira problem the dedicated cvte-jira-create-task skill or the Atlassian MCP tools cannot solve — arbitrary Jira reads/writes (JQL search, get/update issue, add comment, transition status, assign, attachments, watchers, field introspection, cross-project reporting). Since cvte-jira-create-task only creates a fixed AERDM task and mcp-atlassian is often blocked by a WAF (403 on non-office networks), the AI works against the local python-jira library with the venv interpreter and relays machine-readable JSON. THREE INTENTS, THREE PATHS: (a) FIXED — the 12 common operations (search/get_issue/get_comments/add_comment/create_issue/update_issue/transition/assign/attachments/watchers/projects/createmeta) have ready-made CLI scripts in scripts/ops/ — FIRST check INDEX.md and copy the command directly, do NOT re-write a script; destructive ops still require --dry-run + user confirmation; (b) ONE-TIME 查/看/列/帮我找 not covered by INDEX.md → write a throwaway script to %TEMP% (import scripts/jira_ops.py to reuse functions) and delete after running; (c) REUSABLE 写个脚本/写个工具/可复用/保存到 skill → AskUserQuestion to confirm, then write an engineering-grade script to examples/ with argparse/--help/type hints/explicit exit codes, verify it actually runs (--help + --dry-run or a real read-only call) BEFORE delivery. Teaches the connection recipe (token_auth PAT → https://jira.cvte.com via D:/LZY_project/jira/.venv/Scripts/python.exe) and the python-jira 3.10 API. Server 9.12.1 gotcha: jira.createmeta() throws 'Unsupported JIRA version' — use project_issue_types()/project_issue_fields() instead (scripts/ops/createmeta.py does). DO NOT use for the standard create-a-task-from-description flow (→ cvte-jira-create-task)."
---

# Jira 通用工具（固定脚本 + 写脚本兜底，python-jira）

## 目标

当 `cvte-jira-create-task`（只能建 AERDM 固定任务）或 atlassian MCP 工具**搞不定**某件 Jira 事时，AI 用本地 **python-jira 库**干活并把结果给用户。优先级：**常用操作直接跑固定脚本（INDEX.md）** → 覆盖不了的再写一次性脚本 → 用户要保留的写可复用脚本。

**三类用法必须分流**：
- 固定脚本（`scripts/ops/` 12 个 CLI，查 `INDEX.md` 复制命令直接跑）
- 一次性脚本（写到 `%TEMP%`，跑完即删；可 `import jira_ops` 复用函数）
- 可复用脚本（写到 `examples/`，工程级基线，先验证再交付）

---

## 意图分流（先看这一节，决定走哪条路径）

```
用户的请求是什么意图？
├── 命中 INDEX.md 的 12 个固定操作之一
│    （查/看/评论/建/改字段/流转/指派/附件/watcher/项目/必填字段）
│                                                        → 固定脚本路径（§2，直接复制 INDEX 命令）
├── "查一下 / 看下 / 列一下 / 帮我找 / 这条 issue"（INDEX 覆盖不了）
│                                                        → 一次性路径（§4）
├── "写个脚本 / 写个工具 / 做个 / 搞个 / 可复用
│    / 长期 / 每天 / 每周 / 保存到 skill / 放进 skill"   → 询问一次（见下）
└── 其它 Jira 操作                                       → 默认一次性
```

**触发关键词清单**（中英文，AI 必须扫一遍再判断）：

- 中文：脚本、工具、写一个、做个、搞个、可复用、长期、每天、每周、保存到 skill、放进 skill
- 英文：script、tool、reusable、save、persist、cron、scheduled、daily、weekly

**命中可复用关键词 → 必须先 `AskUserQuestion` 确认一次**：

> "这次要写成可复用脚本（保存到 `examples/`，下次还能跑）吗？"
> - 是 → 走 **可复用路径（§3）**
> - 否 → 走 **一次性路径（§4）**

用户没明说且没命中关键词 → 默认一次性，不要自作主张保存到 `examples/`。

---

## 适用时机（触发本 skill）

- 专用 skill / MCP 报错、缺失该操作、或结果不达标。
- 需要自定义 JQL、字段 introspection、批量、带字段的状态流转、附件、watcher、跨项目报表。
- **MCP 报 403 / WAF 拦截**（见记忆 `jira-waf-blocks-auth-header`，非办公网可能发生）—— 这正是用 python-jira 兜底的典型场景。
- 用户明确说「用 jira-tool / 写个脚本跑一下」。

## 不适用 / 优先用哪个（决策表）

| 场景 | 用哪个 |
|---|---|
| 命中 12 个固定操作（查/看/评论/建/改/流转/指派/附件/watcher/项目/必填字段） | **本 skill 固定脚本路径（§2，查 INDEX.md 复制命令）** |
| 按描述建 AERDM 任务 | `cvte-jira-create-task` |
| git 改动 → 建任务 + commit log | `cvte-jira-from-gitlog` |
| MCP atlassian 能直接做的事 | 先 MCP |
| 用户要「查 / 看 / 列」一次性数据 | **本 skill 一次性脚本路径（§4）** |
| 用户要「写个可复用脚本 / 工具」 | **本 skill 可复用脚本路径（§3）**，先确认 |
| 上面都失败 / 是任意其它 Jira 操作 | **本 skill 一次性脚本路径（§4）**（默认兜底） |

**规则**：先试专用 skill / MCP；固定脚本覆盖的常用操作直接走 §2（最快）；覆盖不了的再升级到一次性脚本。可复用 vs 一次性按上面意图分流判断。用户可强制用本 skill。

---

## §2 固定脚本路径（12 个常用操作，查 INDEX.md 直接跑）

`scripts/ops/` 下有 12 个固定 CLI（共享逻辑在 `scripts/jira_ops.py`），**常用操作不要再现写脚本**：

| 脚本 | 干什么 | 脚本 | 干什么 |
|---|---|---|---|
| `search.py` | JQL/便利过滤搜索 | `create_issue.py` 🔒 | 通用创建（AERDM 标准任务除外） |
| `get_issue.py` | 读单条（`--raw` 查 customfield） | `update_issue.py` 🔒 | 更新字段 |
| `get_comments.py` | 读评论 | `transition.py` 🔒 | 状态流转（`--list` 只读） |
| `add_comment.py` 🔒 | 加评论 | `assign.py` 🔒 | 指派（`--list-assignable` 只读） |
| `attachments.py` | 附件 list/add/download | `watchers.py` | 关注者 list/add/remove |
| `projects.py` | 项目/组件/版本 | `createmeta.py` | 必填字段与枚举 |

🔒 = 写操作：**先 `--dry-run`，把 payload 给用户确认后才实跑**。

- **完整命令表（每条可直接复制）、参数说明、输出 JSON 键、错误对照** → 看 **`INDEX.md`**（与本文件同目录）。
- 退出码约定固定不变：`0` 成功 · `1` 脚本错误 · `2` 参数错误 · `3` 鉴权失败(401/403) · `4` Jira API 错误。
- 传 JSON 用 `--data '{"k":1}'` / `--data @D:/tmp/p.json` / `-`（stdin）；Windows 长中文 JSON 一律 `@file`。
- 🔒 安全细则见下文「安全注意」：transition/update/create/add_comment/add_attachments/add_watchers 实跑前必须确认；assign 必须精确 name 匹配（脚本已强制，模糊匹配会指错人）。

---

## 硬编码常量（运行时，不要改）

- **Jira 服务器**：`https://jira.cvte.com`（Jira Server 9.12.1，PAT 认证）。
- **python 解释器**：`D:/LZY_project/jira/.venv/Scripts/python.exe`（专用 venv，已装 python-jira 3.10.6.dev24）。
- **引导模块**：`C:\Users\user\.claude\skills\jira-tool\scripts\jira_bootstrap.py`。
- **凭证来源**：`C:\Users\user\.claude\skills\jira-tool\scripts\config.json`（**本 skill 独立凭证，与外部解耦；勿提交/勿外发**；可被 `JIRA_PAT`/`JIRA_SERVER` 覆盖）。

## 连接与凭证（优先级；绝不打印 token）

优先级：环境变量 `JIRA_PAT` / `JIRA_SERVER` > 本 skill `scripts/config.json`。**完全解耦**，不读其它 skill 的凭证文件。

强制约定：
- **任何输出都不得打印 PAT / `Authorization` 头 / 完整 config dump**。
- token 只在 `jira_bootstrap.load_credential()` 内部取用，并直接传给 `JIRA(token_auth=...)`。
- 需要向用户展示连接信息时用 `jb.describe_connection()`（只含 server + 当前用户名）。

---

## §3 可复用脚本路径（工程级基线，写到 `examples/`）

适用于：用户确认要保留的脚本（每天跑、批量任务、报表、长期工具等）。**先验证，再交付**。

### 3.1 质量基线（AI 必须遵守）

| 项 | 要求 |
|---|---|
| 模块顶部 | `# -*- coding: utf-8 -*-` + 三引号模块 docstring（用途 / 用法 / 入参 / 退出码） |
| 命令行 | `argparse`，每个参数都有 `help=`；`python <script>.py --help` 必须可用且中文 |
| 类型提示 | 函数签名 `def foo(x: str) -> dict[str, Any]:` 风格；`from __future__ import annotations` |
| 退出码 | 0=成功；1=脚本错误；2=参数错误；3=Jira 鉴权失败；4=Jira API 错误 |
| 输出 | 成功 stdout JSON（`jb.out(...)`）；失败 stderr `❌ ...` + 退出码 |
| 错误处理 | 用 `@jb.guard` 兜底；自检时主动校验参数合法性 |
| 幂等性 | 写操作尽量幂等（按 key 查存在再 update；批量写先 `--dry-run` 打印） |
| 凭证 | **禁止**任何 `print`/`echo` token；只用 `jb.connect()` |
| 命名 | 文件名 `lower_snake_case`，动词在前：`export_aerdm_to_csv.py` / `bulk_update_*.py` |
| docstring 必含 | 一句话用途 / 调用示例（绝对路径）/ 入参表 / 退出码表 / 关联 issue 链接 |

### 3.2 可复用脚本模板（写到 `examples/` 时复制此模板）

```python
# -*- coding: utf-8 -*-
"""<一句话用途>。

用法（绝对路径 + 中性 cwd，别 cd 到 D:/LZY_project/jira）：
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/examples/<name>.py --help
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/examples/<name>.py --project AERDM --limit 100
    D:/LZY_project/jira/.venv/Scripts/python.exe C:/Users/user/.claude/skills/jira-tool/examples/<name>.py --dry-run

入参：
    --project   项目 key，默认 AERDM
    --limit     最多取 N 条，默认 100
    --dry-run   只打印 payload，不实际写

退出码：0 成功；2 参数错误；3 鉴权失败；4 Jira API 错误；1 其它。
关联：https://jira.cvte.com/browse/<KEY>
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

sys.path.insert(0, r"C:\Users\user\.claude\skills\jira-tool\scripts")
import jira_bootstrap as jb  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。参数错误时 argparse 会自己打印 help 并退出码 2。"""
    p = argparse.ArgumentParser(description="<一句话用途>")
    p.add_argument("--project", default="AERDM", help="项目 key，默认 AERDM")
    p.add_argument("--limit", type=int, default=100, help="最多取 N 条，默认 100")
    p.add_argument("--dry-run", action="store_true", help="只打印 payload，不实际写")
    return p.parse_args(argv)


@jb.guard
def main() -> int:
    args = parse_args()
    jira = jb.connect()
    # >>> BEG：按需改写 <<<
    rows: list[dict[str, Any]] = []
    # <<< END <<<
    if args.dry_run:
        jb.out({"dry_run": True, "would_handle": len(rows)})
        return 0
    jb.out({"project": args.project, "count": len(rows), "rows": rows})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### 3.3 准确性：交付前必须验证

写入 `examples/` 之前，AI **必须**实跑一次并把结果贴给用户：

1. **`--help` 自检**（必跑）：脚本 `python.exe <abs_path> --help` 必须打印中文帮助且退出码 0。
2. **只读脚本**：实跑一次实际查询；把 JSON 输出前 1-2 行贴给用户确认。
3. **写操作脚本**：先 `--dry-run`，把 payload 贴给用户；**用户同意后再实跑**。
4. **失败处置**：验证失败 → **不写入 `examples/`**，回到临时脚本继续调试；不要把未验证的脚本放进 skill。

### 3.4 保存位置与命名

- **唯一合法位置**：`C:\Users\user\.claude\skills\jira-tool\examples\<name>.py`
- **命名**：`lower_snake_case`，动词在前：`export_xxx.py` / `bulk_update_xxx.py` / `daily_remind_xxx.py` / `sync_xxx_to_xxx.py`
- **避免冲突**：不要用 `jira` / `bootstrap` / `test_*.py` 等可能与 python-jira 或 pytest 冲突的词

---

## §4 一次性脚本路径（写到 `%TEMP%`，跑完即删）

适用于：用户的「查 / 看 / 列」请求，或不命中的兜底场景。**只跑一次，跑完删文件**。

### 4.1 一次性模板

每次任务都**从模板复制一份**，写完后放到临时目录、跑完删除。`jira_bootstrap` 已处理：UTF-8 输出、凭证解析、已鉴权客户端、JSON 输出、异常包装。

**省力捷径**：业务逻辑优先 `import jira_ops as jo` 直接调函数（`jo.search_issues` / `jo.get_issue` / `jo.get_comments` / `jo.add_comment` / `jo.do_transition` / ...），不要手抄 mapper —— 与固定脚本共享同一份实现，字段结构输出完全一致。

```python
# -*- coding: utf-8 -*-
"""一次性 Jira 脚本。复制后只改 BEG/END 之间。用绝对路径的 venv python 运行。"""
import sys
sys.path.insert(0, r"C:\Users\user\.claude\skills\jira-tool\scripts")
import jira_bootstrap as jb  # noqa: E402

@jb.guard
def main():
    jira = jb.connect()                      # 已验证连接；鉴权失败会被 guard 转成错误
    # >>> BEG：按需改写 <<<
    issues = jira.search_issues("project=AERDM order by created desc", maxResults=100)
    rows = [{"key": i.key, "summary": i.fields.summary,
             "status": getattr(i.fields.status, "name", None)} for i in issues]
    # <<< END：按需改写 >>>
    jb.out({"count": len(rows), "issues": rows})   # stdout 只出 JSON；机器可读

if __name__ == "__main__":
    raise SystemExit(main())
```

### 4.2 运行命令（绝对脚本路径 + 中性 cwd）

```bash
D:/LZY_project/jira/.venv/Scripts/python.exe "C:\Users\user\AppData\Local\Temp\jira_script_1.py"
```

### 4.3 硬性守则

- 绝不 `cd D:/LZY_project/jira` 后运行 —— 该目录名 `jira` 会让 `import jira` 变成命名空间包而失败（`ImportError: cannot import name 'JIRA'`）。
- 绝不从仓库根用 `python -c "..."` / `python -m ...` 跑 jira 代码。
- 临时脚本写到 `%TEMP%\jira_script_<n>.py`，**跑完删除**。
- 一次性脚本**不要保存到 `examples/`** —— 用户没要复用就别污染 skill。

---

## API 速查（python-jira 3.10，Server 非 Cloud）

```python
jira = jb.connect()                                  # 或 jira_bootstrap.connect()
jira.myself()                                        # 验证鉴权，返回当前用户 dict

# 项目
jira.projects(); jira.project("AERDM").name
jira.project_components(p); jira.project_versions(p)

# 单条 issue 与字段
issue = jira.issue("AERDM-123")
issue = jira.issue("AERDM-123", fields="summary,comment")   # 只取必要字段省时
issue.fields.summary; issue.fields.project.key; issue.fields.issuetype.name
issue.fields.status.name; issue.fields.description

# 搜索（JQL）
jira.search_issues("project=AERDM order by created desc")          # 默认 maxResults=50！
jira.search_issues(jql, maxResults=100)                            # 超过 50 必须显式设置
jira.search_issues(jql, fields=["key","summary"], json_result=True)  # 拿原始 JSON 读 total

# 创建（fields dict 形式最稳）
jira.create_issue(fields={...})                                     # 单条；失败抛 JIRAError
jira.create_issues(field_list=[{...}, ...])                         # 批量；逐条失败【不抛异常】……
#     …必须逐条检查返回项中是否存在 errors，别当作全成功

# 元数据内省（Server 9.12.1）—— jira.createmeta() 会抛 Unsupported JIRA version，【不要用】
jira.project_issue_types("AERDM", startAt=0, maxResults=100)        # ResultList[IssueType]，.total
jira.project_issue_fields("AERDM", "3", startAt=0, maxResults=200)  # ResultList[Field]，字段 id 在 raw["fieldId"]
# （固定脚本里已封装：scripts/ops/createmeta.py 或 jo.list_issue_types / jo.list_issue_fields）

# 更新 / 指派
issue.update(summary="...", description="...")
issue.update(fields={"labels":[...]}, notify=False)
jira.assign_issue(issue, "username");  jira.assign_issue(issue, None)  # Server 用 {"name": user_id}！accountId 是 Cloud 独有

# 状态流转（破坏性，需确认）
jira.transitions(issue)                     # 列出当前可用 [{id,name}]（仅限当前用户可用的）
jira.transition_issue(issue, "5", assignee={"name":"x"}, resolution={"id":"3"})
jira.transition_issue(issue, "5", fields={"assignee":{...}, "resolution":{...}})

# 评论
issue.fields.comment.comments;  jira.comments(issue)
jira.add_comment("AERDM-123", "comment text")   # 或 (issue, "text", visibility={...})
comment.update(body="...", notify=False);  comment.delete()

# 附件 / watcher / 远程链接
jira.add_attachment(issue, "C:/path/file");  jira.add_attachment(issue, fh, filename="f")
jira.add_watcher(issue, "user");  jira.remove_watcher(issue, "user")
jira.add_remote_link(issue.id, other_issue)

# 删除（破坏性，需确认）
issue.delete()
```

## 字段格式速查（REST v2，jira.cvte.com 9.12.1 已实测）

| 字段类型 | 格式 |
|---|---|
| project | `{"key":"AERDM"}`（或 `{"id":15308}`） |
| issuetype | `{"name":"任务"}`（或 `{"id":3}`） |
| user（单人/数组） | `{"name":"luziyu"}` / `[{"name":"luziyu"}]` |
| option（下拉） | `{"value":"EVT"}` |
| datetime | `"YYYY-MM-DDTHH:MM:SS.000+0800"`（**必须带 `.000`+`+0800`**，否则 `Error parsing time`） |
| date | `"YYYY-MM-DD"` |

**易踩坑**：datetime 缺 `.000` 或时区偏移会报解析错误；user/option 字段必须用对象（数组），纯字符串报 `expected Object`。非 AERDM 项目的 required 字段与 customfield id 不同 —— 用 `jira.createmeta(...)` / `jira.fields()` / `issue.raw` 发现，**不要猜字段 id**。

---

## 输出给用户（结果规范）

- 脚本只对 **stdout 输出 JSON**（`jb.out(...)`，`ensure_ascii=False, indent=2`）；错误打到 **stderr** `❌ ...`；退出码 `0`=成功、`1`=失败。
- AI 复述给用户时：给出 `issue key`、`� https://jira.cvte.com/browse/<KEY>`、关键字段；**绝不回显 PAT**。
- 简单结果可用 `KEY: value` 行；默认 JSON 以便 AI 解析。

## 安全注意（破坏性操作需确认）

- `issue.delete()`、状态流转 `jira.transition_issue(...)`、批量创建超过阈值 → **先 `AskUserQuestion` 确认**。
- 状态流转前先 `jira.transitions(issue)` 展示可选目标，请用户选；确认后再执行。
- **禁止未经确认的 delete / 状态变更 / 批量写**。

## 错误兜底

固定脚本退出码：`0` 成功 · `1` 脚本错误 · `2` 参数错误 · `3` 鉴权失败(401/403) · `4` Jira API 错误（`jira_bootstrap.guard` 兜底）。

| 场景 | 处理 |
|---|---|
| 401 / 403（退出码 3） | PAT 过期或权限不足；提示换 `JIRA_PAT`，或更新本 skill `scripts/config.json`。若走 MCP 遇 403 则是 WAF → 改用本 skill。 |
| `JIRAError` 400 / 422（退出码 4，stderr 带 `field_errors={...}`） | 对照「字段格式速查」逐字段修；用 `createmeta.py` 查必填/枚举。 |
| 5xx | 提示重试 1 次。 |
| `ImportError: cannot import name 'JIRA'` | 从 `D:/LZY_project/jira` 目录运行导致 import 污染；改用绝对脚本路径从其它目录跑。 |
| 凭证缺失 | 按优先级补齐：`JIRA_PAT`/`JIRA_SERVER` 环境变量，或本 skill `scripts/config.json`。 |
| 中文乱码 | 确认走了 `jb.out`；或设 `PYTHONIOENCODING=utf-8`。 |

## 已知限制

- 只连 `jira.cvte.com`（Server）；非 Cloud —— 别用 `accountId`，user 字段一律用 `{"name":...}`。
- `create_issues` 返回需逐条检查 errors；`search_issues` 默认 50 条，需显式 `maxResults`。
- 破坏性操作需人工确认。
- `jira.createmeta()` 在 Server 9.12.1 **不可用**（抛 Unsupported JIRA version）—— 用 `project_issue_types` / `project_issue_fields`（已封装进 `scripts/ops/createmeta.py`）。
- python-jira 的 assign / add_watcher 内部是**模糊**用户搜索且多命中取第一个 —— 固定脚本 `assign.py` / `watchers.py` 已强制精确 name 校验；自己写脚本时也必须先校验。
- `JIRA()` 构造是**惰性连接**：坏 token 不在构造时抛错，而在第一次真实 API 调用时抛 401（guard 会转成退出码 3）。

## 调试与扩展

```bash
# 自检连接信息（中性 cwd）
D:/LZY_project/jira/.venv/Scripts/python.exe "C:\Users\user\.claude\skills\jira-tool\scripts\jira_bootstrap.py"

# 固定脚本自检（--help 全量 + 只读抽查；完整清单见 INDEX.md §5）
for f in search get_issue get_comments add_comment create_issue update_issue transition assign attachments watchers projects createmeta; do
  D:/LZY_project/jira/.venv/Scripts/python.exe "C:/Users/user/.claude/skills/jira-tool/scripts/ops/$f.py" --help >/dev/null 2>&1 || echo "FAIL: $f"; done
D:/LZY_project/jira/.venv/Scripts/python.exe "C:/Users/user/.claude/skills/jira-tool/scripts/ops/search.py" --project AERDM --limit 3

# 跑示例做集成自检
D:/LZY_project/jira/.venv/Scripts/python.exe "C:\Users\user\.claude\skills\jira-tool\examples\example_search.py"
D:/LZY_project/jira/.venv/Scripts/python.exe "C:\Users\user\.claude\skills\jira-tool\examples\example_issue_detail.py" AERDM-123

# 跑可复用脚本的 --help 自检（交付前必做）
D:/LZY_project/jira/.venv/Scripts/python.exe "C:\Users\user\.claude\skills\jira-tool\examples\<your_script>.py" --help
```

- **`scripts/ops/` 与 `scripts/jira_ops.py` 是固定基建**（不属于 `examples/`，不适用 §3 的用户确认流程）——修改后必须重跑上面的自检，并**同步更新 `INDEX.md`** 对应行。
- 新增项目/类型：用 `scripts/ops/createmeta.py --project X --type Y --required-only --with-allowed` 核对必填字段与枚举，再按字段表拼 dict（`jira.createmeta()` 在 9.12.1 不可用）。
- 新增可复用脚本：按 §3 模板复制到 `examples/`，跑通 `--help` + `--dry-run` 后再交付；逻辑尽量 `import jira_ops as jo` 复用函数，不抄 mapper。
- 凭证安全：永不提交 `config.json`；优先用环境变量 `JIRA_PAT` / `JIRA_SERVER`。
