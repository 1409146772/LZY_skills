---
name: jira-log-analysis
description: 拉取 Jira 工单（名下未关闭）→ 下载并解析 Log 附件（截图 / CAN BLF / 5AA5 串口日志）→ 结合**你给的工程代码 + DBC + md 文档**做单票隔离分析 → 产出问题分析+解决计划 → 邮件发你审核。触发词：「分析工单」「跑工单分析」「工单分析 LH2512024-5408」「看下我的工单」「结合代码分析这个工单」「Log 分析工单」。
---

# jira-log-analysis

把「翻 Jira → 下附件 → 解日志 → 对照代码 → 写分析 → 发邮件」这条人工链路做成流水线。
**每单一个隔离 subagent**，避免上下文爆炸与跨单污染。

只做四件事：**拉 Jira 工单 → 解析 Log 附件 → 结合代码分析 → 发邮件**。
不做工作区扫描、不做代码根探测、不做文档降维、不做 DBC 自动选型 —— 那些由你在调用时给路径。

## 调用时你要给我三个路径

```bash
# 先用 Write 工具写出 inputs.json，再跑：
./jla.cmd all --keys LH2512024-5408
```

`inputs.json`（写在工具根目录，每次调用覆盖重写）：

```json
{
  "code": "D:\\projects\\weiqiao\\WB101\\workspace\\FreeRTOS_S32K312",
  "dbc":  "D:\\projects\\weiqiao\\WB101\\WB101_IHU_CAN矩阵_V2.4_20260722.dbc",
  "docs": "D:\\projects\\weiqiao\\WB101\\doc",
  "keys": ["LH2512024-5408"],
  "code_note": "FreeRTOS_S32K312 车载仪表主控，含分层 CLAUDE.md"
}
```

| 键 | 必需 | 说明 |
| --- | --- | --- |
| `code` | ✅ | 工程代码根（含 CLAUDE.md 的 checkout）。**没代码就没法"结合代码分析"** |
| `dbc` | ○ | CAN 矩阵；缺省则 BLF 只能按裸 ID 解析，信号名不可用 |
| `docs` | ○ | 你整理好的 md 文档目录（**我不做 xlsx/docx/pdf 降维**，请自己转成 md） |
| `keys` | ○ | 只跑指定工单；缺省按 `config.jira.jql` 拉名下全部未关闭单 |
| `code_note` / `code_project` | ○ | 一句话说明这是什么工程，供 orchestrator 判断工单归属 |

**这三个路径我绝对只读** —— 只读它们，绝不写入。（不是口头约定：由 `jla_env.assert_writable`
在写入原语层强制，任何落到这三个根内的写操作都会直接抛异常。）
`code` 往往是你**正在用的 checkout，带真实 `.git`** —— 所以连 git 写操作也一并禁止。

## 环境（唯一入口）

```bash
./jla.cmd validate                     # 自检（venv / 依赖 / config / Jira 探活 / inputs.json / vendor）
./jla.cmd list                         # 只看有哪些单，不下载
./jla.cmd run                          # 拉单 + 解析附件 + 组装（到"可派发"为止）
./jla.cmd summary --date <日期>        # 从回执生成 SUMMARY.md
./jla.cmd mail-only --date <日期>      # 发邮件（加 --dry-run 只看命令）
```

`jla.cmd` 等价于下面这条（非 Windows 或想手工调用时用）：

```bash
TOOL="<本工具所在目录>"       # 解压到哪就是哪，不用改配置
"$TOOL/.venv/Scripts/python.exe" "$TOOL/scripts/jla.py" validate
```

所有步骤都用工具自带的 `.venv`（自包含，与系统环境隔离）。

> **首次使用**：分发包不含 `.venv`，先建一次：
>
> ```bash
> uv venv --python 3.12 "$TOOL/.venv"
> uv pip install --python "$TOOL/.venv/Scripts/python.exe" -r "$TOOL/requirements.txt"
> ```
>
> ⚠ 公司 DLP 会把 venv 里部分包的 `.py` 改名成 `.py.IPGSD`（实测 urllib3 / packaging /
> charset_normalizer 会中招），报 `No module named 'urllib3.exceptions'`。合法修复：
> `uv pip install --python "$TOOL/.venv/Scripts/python.exe" --no-cache --reinstall urllib3 packaging charset_normalizer`
> **不要手工把 `.IPGSD` 改回 `.py`** —— 那是绕过 DLP 管控。
>
> 再配 Jira 凭据（见下方「配置」）。

## 使用流程（TL;DR）

```bash
./jla.cmd validate                   # 0. 环境自检（首次或环境变动后）
# 写出 inputs.json（用 Write 工具）
./jla.cmd all --keys <KEY>           # 1. 拉单+解析+组装；输出会提示几单已就绪、worker_prompt 路径
# 2. 【Claude 会话动作】按提示，对每单派一个 subagent（prompt 取 worker_prompt.md）
./jla.cmd summary --date <日期>      # 3. 汇总（从 receipt 生成 SUMMARY.md）
./jla.cmd mail-only --date <日期>    # 4. 发邮件
```

### 1. 拉工单 + 解析附件 + 组装输入包

```bash
./jla.cmd run                        # 按 config.jira.jql 拉（默认名下未关闭单）
./jla.cmd run --keys LH2512024-5408  # 只跑指定单（--keys 优先于 inputs.json 的 keys）
./jla.cmd list                       # 先看看有哪些单，不下载
```

会打印本次用的 代码 / CLAUDE.md / DBC / 文档 四个路径 —— **先确认这四个对不对**。

产出在 `<工具目录>/workspace/out/<日期>/<KEY>/`：
`ticket.md` `attachments/` `evidence/` `bundle.md` `worker_prompt.md`

### 2. 派发隔离 subagent（本 skill 的核心动作）

`run` 完成后，**你（Claude）要按 `prompts/orchestrator.md` 执行**：

1. 读 `out/<日期>/_assignments.json` 与各单 `ticket.md` 前 80 行，
   对照 `config.json` 的 `projects[].desc`，**逐单判断工程归属**并把判断回写进 `_assignments.json`。
2. 若判断结果与占位不同，重跑 `./jla.cmd prepare --date <日期>` 使 bundle 用上新归属。
3. 对每单，**用 Agent 工具派一个 `general-purpose` subagent**，
   prompt 直接取该单的 `worker_prompt.md` 全文。
   - **一次一票，顺序执行**，不要并发。
   - 单票失败不中断，记下原因继续下一单。
4. subagent 会写 `analysis.md` 与 `receipt.json`。

**你绝对不要读** `_frames.jsonl`、`evidence/INDEX.md` 全文或大附件 —— 那是 subagent 的活。
你只读各单 `receipt.json`。

### 3. 汇总与发邮件

```bash
./jla.cmd summary --date <日期>              # 从 receipt 生成 SUMMARY.md
./jla.cmd mail-only --date <日期> --dry-run  # 只看命令不发送
./jla.cmd mail-only --date <日期>            # 真发
```

## 配置（你只需要管两件事）

1. **每次调用给我三个路径** —— 写进 `inputs.json`（见上）。这是唯一需要每次做的事。
2. **配 Jira 凭据**（不进版本库）：

   ```bash
   cp vendor/jira-tool/scripts/config.example.json vendor/jira-tool/scripts/config.json
   # 编辑填入 server / email / token（PAT）
   ```

   或者改用环境变量 `JIRA_PAT` / `JIRA_SERVER`（优先级更高）。

`config.json` 里**不放任何 token**。另外 `config.json` 的 `projects[]` 用于工程归属判断，
`email.to` / `email.cc` 决定邮件收件人 —— 都改一次就行，不必每次动。

> 想发邮件还要把 `lark-cli.exe` 放进 `vendor/lark-cli/` —— 它 49MB 且换机器要重新登录，
> 所以**不随分发包提供**。见该目录下的 `README.md`。不发邮件的话整条分析链路照跑。

## 关键设计约束（改代码前必读）

| 约束 | 原因 |
| --- | --- |
| **code / dbc / docs 三个根只读，绝不写入** | 用户明确要求。由 `jla_env.assert_writable` 在写入原语层强制，不是文档约定 |
| 只在工具内的 `workspace/` 下工作 | 产出（附件副本/证据/报告）全在这里；输入路径只读引用 |
| 所有路径经 `jla_env.tool_path()` / `cfg_path()` 解析 | config 里只写相对路径，工具才能整包搬走；`inputs.json` 是唯一例外（按定义在工具之外） |
| 所有写入经 `write_text/write_json/ensure_dir` | 不要裸调 `Path.mkdir()`，否则会绕过只读闸门 |
| 代码目录的 `.git` 绝不触碰 | 那是用户真实的 checkout；git 只容许读取类子命令（`jla_env.git_readonly` 白名单） |
| 所有 Python 子进程经 `jla_env.run_*` | 统一注入 `PYTHONUTF8=1`（否则 frame_extractor 在 cp936 下崩，退出码 120）与安全 cwd |
| 绝不整读 `_frames.jsonl` | 可达 10MB+，会冲爆上下文；只用 `digest.md` + 定向 Grep |
| 分析只读，不回写 Jira | 不评论、不改状态、不做构建/烧写/签名 |

## 故障排查

| 现象 | 处置 |
| --- | --- |
| 找不到 `.venv` / `No module named ...` | 分发包不含 venv，按上面「首次使用」建一次 |
| `No module named 'urllib3.exceptions'` / `'packaging.version'` | DLP 把 venv 里的 `.py` 改名成 `.py.IPGSD`。用 uv 从 wheel 重装（绕开被污染的缓存）：`uv pip install --python .venv/Scripts/python.exe --no-cache --reinstall urllib3 packaging charset-normalizer`。⚠ 不要手工把 `.IPGSD` 改回 `.py` |
| **未提供 inputs.json** | 报错会把该写什么列出来。用 Write 工具写 `<工具目录>/inputs.json` |
| **`code` 路径不存在** | 在 `prepare` 阶段就报错（不会跑到一半才发现）。检查路径拼写 |
| `vendor/` 某项 ✗ | 重跑 `python tools/sync_vendor.py`；只看完整性用 `--check` |
| `frame_extractor` 崩溃、退出码 120 | `PYTHONUTF8=1` 没生效。确认走的是 `jla_env.run_python`，别裸调 subprocess |
| `ImportError: cannot import name 'JIRA'` | cwd 下有个 `jira/` 同名声明的目录遮蔽了包。`cd` 到工具目录再跑（`jla.cmd` 已自动处理） |
| Jira 退出码 3 / 401 / 403 | PAT 过期或权限不足 → 更新 `vendor/jira-tool/scripts/config.json`，或设 `JIRA_PAT`。**先拿原工具（jira-ticket-analysis）跑同一条命令对照** —— 若它也 403，就是凭据/网络问题，不是本工具的问题 |
| 邮件只拿到 `draft_id` | `jla_mail` 会自动补发草稿；若仍失败，看 `--dry-run` 的 cmd 手工执行 |
| `lark-cli 不存在` 报错 | 不随包分发，按报错提示把它放进 `vendor/lark-cli/`，或在 config 里改 `paths.lark_cli` |
| **BLF 解不出信号名，只有裸 ID** | 没给 `dbc`，或给的 DBC 版本与抓包不符。`evidence/` 下的 `UNKNOWN_IDS.md` 会列出未解码的 ID。换一份 DBC 写进 `inputs.json` 重跑 |
| **发现有 `.pdbc`** | TSMaster 私有容器，cantools 按扩展名直接拒收。需在 TSMaster 里导出成 `.dbc` |
| 文档读不了 | 本工具**不做** xlsx/docx/pdf 降维。请把文档转成 md 再放进 `docs` 目录 |
| 工程归属判断不准 | 改 `config.json` 的 `projects[].desc` 让它更贴合你的工单，或直接读 worker 报告第 9 节的改判建议 |
