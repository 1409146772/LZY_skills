---
name: cvte-jira-create-task
description: "Use when the user wants to create a Jira task from a plain TASK DESCRIPTION (no code, no git). Workflow: parse optional flags, read config.yaml defaults, AI infers summary/description/difficulty, build a Jira REST v2 fields dict, invoke a python script using the local jira lib to create the issue, then return the issue key + browse URL. Defaults: project=AERDM, issue_type=任务, assignee/responsible=luziyu, reviewer=wuhaoheng, stage=EVT, domain=SW, priority=中, difficulty=B. Supports --project, --issue-type, --title, --description, --assignee, --start-time, --dry-run. Start time defaults to today (default_start_time); a user-provided date/time is honored instead."
---

# CVTE Jira 任务创建（对话式，python-jira）

## 目标

用户**只给出任务描述**，AI 直接拼装字段，调用本地 **python-jira 库**在 `jira.cvte.com` 上创建 Jira 任务，返回 issue key 和链接。

与 `cvte-jira-from-gitlog` 的区别：
- 本 skill **不依赖 git、不读暂存区、不生成 commit log**，走 `scripts/create_issue.py`（python-jira 库），默认不经过 MCP。
- `cvte-jira-from-gitlog` 从 git 改动建任务 + 生成 commit log，走 mcp-atlassian MCP。

## 适用时机

- 用户说「建个任务 / 帮我创建 Jira 任务 / 写个 task」并给出描述，**与代码和 commit 无关**。
- 仅需默认项目 AERDM、类型任务、责任人 luziyu 的日常任务登记。

## 调用方式

```bash
/cvte-jira-create-task  <任务描述...>                    # 默认 AERDM Task
/cvte-jira-create-task "给XX模块加XX功能" --assignee=luziyu
/cvte-jira-create-task --title="XX" --description="...\"  # 只给必填标题/描述
/cvte-jira-create-task <描述> --project=AESW --issue-type=需求
/cvte-jira-create-task <描述> --start-time="10:30"        # 指定今天 10:30 开始
/cvte-jira-create-task <描述> --dry-run                   # 只打印 payload，不创建
```

**参数**（均可选，省略走默认）：
- `--project=<key>`：默认 `AERDM`
- `--issue-type=<名称>`：默认 `任务`
- `--title=<summary>`：提供则不做 AI 推断
- `--description=<正文>`：提供则不做 AI 推断
- `--assignee=<用户名>`：覆盖默认经办人/责任人 `luziyu`
- `--start-time=<开始时间>`：覆盖开始时间，日期+时间优先于描述里的自然语言时间。可为 `HH:MM`（当天）/`YYYY-MM-DD HH:MM`/完整 `YYYY-MM-DDTHH:MM:SS.000+0800`；只给时间则日期=今天。未提供则默认当天 `date_defaults.default_start_time`
- `--dry-run`：只打印 payload

## 硬编码常亮（运行时）—— 不要改这些

- **Jira 服务器**：`https://jira.cvte.com`（存于 `scripts/config.json`，可被环境变量 `JIRA_SERVER` 覆盖）
- **python 解释器**：`D:/LZY_project/jira/.venv/Scripts/python.exe`（专用 venv，已装 jira 库）
- **脚本路径**：本目录 `scripts/create_issue.py`
- **凭证**：`scripts/config.json` 的 `token`，可被环境变量 `JIRA_PAT` 覆盖。保密，勿提交/勿外发。

调用脚本命令（**用绝对路径，不要 cd 到 D:/LZY_project/jira**，否则会污染 import）：

```bash
D:/LZY_project/jira/.venv/Scripts/python.exe \
  "<本 skill 目录>/scripts/create_issue.py" --payload <json 文件或字符串> --expect-summary "<summary核心子串>" [--dry-run]
```

`--expect-summary` 为创建前守卫：payload 的 summary 不含该子串时脚本拒绝创建（退出码 2）。真实创建时应始终传入（传不含代号前缀的核心中文）。

## 字段格式速查（AERDM 任务，jira REST v2，已实测）

| 字段 | 类型 | 必填 | REST 值格式 |
|------|------|:---:|------|
| `project` | project | ✓ | `{"key":"AERDM"}` |
| `issuetype` | issuetype | ✓ | `{"name":"任务"}` |
| `summary` | string | ✓ | 主题，≤60 字；以 `[代号]` 开头（如 `[WB101]标题`） |
| `customfield_13209` | datetime | ✓ | `"YYYY-MM-DDTHH:MM:SS.000+0800"`（必须毫秒+时区）；默认当天 09:00，用户给日期/时间则以提供为准（见 Step 4） |
| `duedate` | date | ✓ | `"YYYY-MM-DD"` |
| `customfield_10447` | array<user> | ✓ | `[{"name":"luziyu"}]` |
| `customfield_10310` | option | ✓ | `{"value":"EVT"}` |
| `assignee` | user | 常用 | `{"name":"luziyu"}` |
| `customfield_14804` | user | 可选 | `{"name":"wuhaoheng"}`（单对象，**不是**数组） |
| `customfield_21424` | option | 可选 | `{"value":"SW"}` |
| `customfield_17402` | option | 可选 | `{"value":"平台"}` |
| `priority` | option | 可选 | `{"name":"中"}` |
| `customfield_11110` | option | 可选 | `{"value":"B"}` |
| `description` | string | 可选 | 正文，保留换行 |

**易踩坑**：
- datetime 缺 `.000` 或时区偏移 → 报 `Error parsing time ... expected Object` / `必须为字符串`。
- 下拉/用户字段必须用对象或对象数组，纯字符串会报 `expected Object`。

## 工作流（严格按顺序）

### Step 1：解析参数
从 args 提取 `--project / --issue-type / --title / --description / --assignee / --start-time / --dry-run`，其余视为任务描述。标准化成小写 `project_key`、`issue_type`、`dry_run` 布尔。

参数不合法（如未知 `--issue-type`）→ 报错并列出支持的 type（任务/需求/缺陷），不继续。

### Step 2：加载配置
读取本目录 `config.yaml`，得到 `defaults`（默认值）、`date_defaults`（日期格式）、`enums`（校验枚举）、`ai_infer`（推断指令）。

文件缺失 → 报错 `config.yaml 未找到：<绝对路径>`。

### Step 3：推断字段（AI）
- **subject/summary**：用 `--title`；否则读 `ai_infer.summary` 从任务描述提炼（≤60 字，去句号）。项目代号统一用 `[代号]` 前缀、**仅标注开头的代号**：`WB101_标题` → `[WB101]标题`；代号在中间/重复出现、以及内部下划线一律保留原样。
- **description**：用 `--description`；否则用任务描述正文（保留换行）。
- **customfield_11110 难易程度**：用 `ai_infer.customfield_11110` 评估，无线索用默认 `B`。
- **人员/选项**：默认全部取 `config.yaml defaults`；用 `--assignee` 覆盖经办人+责任人。

**必填校验**：`summary`、`customfield_13209`、`duedate`、`customfield_10447`、`customfield_10310` 必须有值。若 `summary` 无法从描述推断 → 用一次 `AskUserQuestion` 询问主题（含"可由你自定义"选项）；仍无 → 报错退出。

### Step 4：装配 fields dict
按字段格式表拼装。**日期默认当天，用户提供的日期/时间优先**：

- `customfield_13209`（开始时间）取值优先级：
  1. `--start-time`（若给）
  2. 任务/描述里显式提到的日期 + 时间（如「明天下午2点」「10:30」「2026-08-29 14:00」）
  3. 默认：今天 + `config.yaml date_defaults.default_start_time`（`09:00:00`）

  **解析规则**：只给时间（`HH:MM`/`N点`/`下午N点`/`上午N点`）→ 日期=今天；给「明天/后天/N月N日/YYYY-MM-DD」→ 日期=该日；只给日期没给时间 → 用 `default_start_time`。拼好后按 `date_defaults.start_time_format`（`%Y-%m-%dT%H:%M:%S.000+0800`，**必须带毫秒 `.000` 与 `+0800`**，缺了会报 `Error parsing time`）格式化。

- `duedate` = 开始日期 + `due_offset_days`（默认 1），按 `due_format`（`%Y-%m-%d`）。

空值字段直接跳过（不放进 payload）。可选项（审核人/专业领域/适用范围/优先级/难易程度/description/assignee）若为 None 则省略。

### Step 5：生成 JSON 并调用脚本
把 fields dict 写入 **唯一文件名**的临时文件（如 `%TEMP%\cvte-jira-payload-<主题slug或时间戳>.json`，UTF-8），或直接作为 JSON 字符串传给 `--payload`。**禁止复用固定文件名**——会话残留的同名旧文件会被照单创建成错误工单。

- payload 文件用 **Write 工具**写入，不要与脚本调用合并进同一条 Bash 命令（该命令被权限拦截时整体不执行，只重试后半截会读到过期文件；重试必须从写 payload 起整体重放）。
- `--dry-run` → 直接打印 payload 即可，**不调用** create_issue.py（脚本也支持 dry-run，但此处为省一次调用可直接打印）。
- 否则执行（守卫参数必填）：
  ```
  D:/LZY_project/jira/.venv/Scripts/python.exe "<skill>/scripts/create_issue.py" --payload <唯一json文件> --expect-summary "<summary核心子串>"
  ```

**summary 归一化兜底**：`create_issue.py` 在创建前会对 `summary` 做幂等归一化 —— 以「开头代号_」开头的会转成 `[代号]描述`（已带 `[代号]` 前缀则保持不变）。因此即使 AI 推断或 `--title` 没按格式来，主题最终也会统一为 `[代号]描述`。

### Step 6：解析结果
脚本输出 `CREATED:AERDM-XXXXX` 与 `URL:https://jira.cvte.com/browse/...`。
- 成功 → 从中提取 `issue_key`。
- 失败（脚本退出码非 0 / 打到 stderr 的 ❌）→ 把错误信息**原样**给用户；401/403 提示 token 过期或权限；400/422 提示字段格式问题（对照字段表）；5xx 建议重试一次。

### Step 7：输出给用户
```
✅ Jira 任务已创建：**AERDM-XXXXX**
🔗 https://jira.cvte.com/browse/AERDM-XXXXX

📋 主题：<summary>
📝 描述：<description 前若干行>
```
若用户后续要对这批改动生成 commit log，可再调用 `/cvte-jira-from-gitlog`（本 skill 不涉及 commit）。

## 分级决策原则

- **能自己完成，绝不问**：默认值（责任人 luziyu、审核人 wuhaoheng、EVT、SW、中、B）、日期自动、项目/类型默认。
- **真的信息不足才问**：仅当 summary 无法推断、且用户没给 title 时，用一次 `AskUserQuestion`（含"留空/自定义"项）。

## 错误兜底

| 场景 | 处理 |
|------|------|
| `--issue-type` 不合法 | 报错并列出支持的类型 |
| `config.yaml` 缺失 | 报错并给绝对路径 |
| summary 无法推断且用户未给 title | AskUserQuestion 补齐（1 次） |
| 必填字段仍缺 | 报错，指出缺哪个字段 |
| 脚本 401/403 | 提示 PAT 过期/权限不足 |
| 脚本 400/422 | 对照字段格式表指出问题字段 |
| 脚本 5xx | 提示重试 1 次 |
| 脚本其他异常 | 错误原样转给用户，不吞错 |

## 已知限制

- 只支持 jira.cvte.com（Server 9.12.1），需 PAT。
- 默认一套 AERDM 任务字段；其他项目/类型需改 `config.yaml` 或加 `--project/--issue-type`。
- 不生成 commit log、不读 git、不自动 assign 到其他人的字段。

## 调试与扩展

### 本地测试
```bash
# dry-run：只打印 payload，不建任务
D:/LZY_project/jira/.venv/Scripts/python.exe "<skill>/scripts/create_issue.py" --payload <json 文件> --dry-run

# 真建一条测试任务
D:/LZY_project/jira/.venv/Scripts/python.exe "<skill>/scripts/create_issue.py" --payload <json 文件>
```

### 添加新项目类型
1. 复制两处：在 `config.yaml` 加 `defaults`/`enums`（改 field id 与枚举），或新建 `config-<project>-<type>.yaml`。
2. 用 `--project/--issue-type` 指定。
3. 用 `jira.cvte.com/rest/api/2/issue/createmeta/<PROJECT>/issuetypes/<TYPE_ID>` 提前核对必填字段与枚举。

### 凭证安全
- `scripts/config.json` 含 PAT，切勿提交到仓库或发送给他人。
- 建议改用环境变量 `JIRA_PAT`、`JIRA_SERVER`（脚本优先读环境变量）。
