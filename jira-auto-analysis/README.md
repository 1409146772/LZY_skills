# jira-log-analysis

Jira 工单 → Log 附件解析 → 代码感知分析 → 邮件 的流水线工具。

是 `jira-ticket-analysis` 的**精简版**：拿掉了"用户工作区扫描 / 代码根探测 / CLAUDE.md 自动发现 /
文档降维 / DBC 索引与自动选型"这一整层（约 2600 行），把三个输入路径改成**调用时显式给出**。

## 与原版的差异

| | jira-ticket-analysis | jira-log-analysis |
| --- | --- | --- |
| 代码/DBC/文档来源 | `--src` 工作区 + 自动扫描探测 | **`inputs.json` 显式给三个路径** |
| 文档处理 | xlsx/docx/csv→md 降维 + reach 分类 | **不做**，你给现成的 md |
| DBC 选择 | 建索引 + 按版本/日期/命中率自动选型 | **你给哪份就用哪份** |
| 增量水位 | state.json（原实现里其实是死代码） | 无，每次全跑 |
| 脚本模块 | 11 个 | **7 个** |
| 依赖 | jira/requests/cantools/python-can/pillow/openpyxl/python-docx | **jira/requests/cantools/python-can** |

**保留的部分**（与原版同源，行为一致）：Jira 拉取与附件下载、5AA5 帧日志解析（含按目录跨文件
时间合并）、BLF+DBC 解码与 digest、证据索引、**每单一 subagent 的隔离分析架构**、回执与
SUMMARY 生成、lark-cli 邮件直发。

## 目录结构

```
jira-log-analysis/
├── SKILL.md                 skill 入口（触发词与调用方式）
├── README.md                本文件（工程视角）
├── config.json              默认值：JQL / 收件人 / projects / vendor 路径
├── inputs.json              每次调用的三个输入路径（gitignore，不入库）
├── inputs.json.example      模板
├── jla.cmd                  Windows 启动器（纯 ASCII，见下）
├── requirements.txt
├── prompts/
│   ├── orchestrator.md      主 agent 提示词：判归属 → 派 subagent → 汇总
│   └── ticket_worker.md     单票 subagent 提示词 + 报告模板 + 回执格式
├── scripts/
│   ├── jla.py               统一 CLI
│   ├── jla_env.py           环境真理源：路径/venv/子进程/只读闸门/只读 git
│   ├── jla_fetch.py         拉工单 + 下载附件
│   ├── jla_attach.py        附件分类/解包/5AA5/BLF 解析/证据索引/digest
│   ├── jla_bundle.py        组装 bundle.md + worker_prompt.md
│   ├── jla_summary.py       回执解析 + SUMMARY 生成 + 派发材料
│   └── jla_mail.py          lark-cli 发 HTML 邮件
├── vendor/                  内嵌外部工具（自包含）
│   ├── jira-tool/           python-jira ops 脚本（search/get_issue/attachments…）
│   ├── frame_extractor/     5AA5 SOC↔MCU 帧提取（纯标准库）
│   ├── canlog/              BLF + DBC 解码
│   └── lark-cli/            邮件发送（49MB，用户自备）
├── tools/                   开发期脚本，不随分发
│   ├── sync_vendor.py       从本机上游刷新 vendor/
│   └── install_skill.py     镜像到 ~/.claude/skills/
└── workspace/               运行时产物（gitignore）
    └── out/<日期>/<KEY>/    ticket.md attachments/ evidence/ bundle.md
                             worker_prompt.md analysis.md receipt.json
```

## 三个输入路径（只读）

由 `inputs.json` 给出（调用前由 Claude 会话用 Write 工具写）：

```json
{
  "code": "D:\\projects\\weiqiao\\WB101\\workspace\\FreeRTOS_S32K312",
  "dbc":  "D:\\projects\\weiqiao\\WB101\\WB101_IHU_CAN矩阵_V2.4_20260722.dbc",
  "docs": "D:\\projects\\weiqiao\\WB101\\doc",
  "keys": ["LH2512024-5408"],
  "code_note": "FreeRTOS_S32K312 车载仪表主控，含分层 CLAUDE.md"
}
```

- `code` 必需；`dbc` / `docs` 可选。`keys` 缺省时按 `config.jira.jql` 拉。
- **这三个根只读**，由 `jla_env.assert_writable()` 在写入原语层强制 —— 不是文档约定。
  `write_text` / `write_json` / `ensure_dir` 都要过这道闸门。
- `code` 上的 git 只容许读取类子命令（`jla_env.git_readonly()` 白名单：
  rev-parse / rev-list / log / status / show-ref / symbolic-ref / describe / ls-files / branch / remote）。

## 关键实现约束

| 约束 | 原因 |
| --- | --- |
| `jla.cmd` **必须纯 ASCII** | cmd.exe 按 OEM 码页（cp936）解析 `.cmd`，中文注释会损坏脚本。中文放 SKILL.md / README.md |
| **不要写 `endlocal ^& exit /b %RC%`** | 转义后的 `&` 会让 cmd 继续往下执行，而 `endlocal` 已清掉 `%TOOL%`，于是打印一堆空路径的报错。用 `exit /b %RC%` 让 setlocal 自动释放 |
| 所有子进程经 `jla_env.run_*` | 统一注入 `PYTHONUTF8=1`（否则 frame_extractor 在 cp936 下崩、退出码 120）与安全 cwd |
| 所有写入经 `env.write_*` | 裸调 `Path.mkdir()` 会绕过只读闸门 |
| 绝不整读 `_frames.jsonl` | 可达 10MB+，会冲爆上下文；只看 `*_digest.md` 或定向 Grep |
| `.pdbc` 不可用 | TSMaster 私有容器，喂给 cantools 会让 BLF 解码**直接失败** |
| 不回写 Jira | 不评论、不改状态、不做构建/烧写/签名 |

## DLP（公司加密）注意事项

本机装了 DLP agent，对本仓库的 `.py` / `.md` 做**透明加解密**：

- `bash` 的 `cat` / `head` 看到的是密文（`%TSD-Header` 开头），**文件大小与行数也是密的**。
- 但 **Python 与 Claude 的 Read/Edit/Grep 工具都能正常读写明文**（走的是同一套透明解密）。
- 所以：判断文件内容**不要用 `wc -l` 或 `head`**，用 Read 工具或 Python。

对 venv 的影响：DLP 会把部分包的 `.py` 改名成 `.py.IPGSD`，包退化成命名空间包。
实测中招的有 `urllib3` / `packaging` / `charset_normalizer`。修复见 SKILL.md 故障排查。

> 重装后 `.py` 与 `.py.IPGSD` 会**并存**，这是正常的：Python 走透明解密读 `.py`，
> 那些 `.IPGSD` 是上一次被污染的缓存残留，不影响运行。

## 开发期命令

```bash
TOOL=D:\LZY_project\AI\tool\jira-log-analysis

# 建 venv（首次）
uv venv --python 3.12 "$TOOL/.venv"
uv pip install --python "$TOOL/.venv/Scripts/python.exe" -r "$TOOL/requirements.txt"

# 从本机上游刷新 vendor/（jira-tool / frame_extractor / canlog）
python "$TOOL/tools/sync_vendor.py"
python "$TOOL/tools/sync_vendor.py" --check     # 只校验完整性

# 镜像到 ~/.claude/skills/jira-log-analysis/
python "$TOOL/tools/install_skill.py" --dry-run
python "$TOOL/tools/install_skill.py"
```

`sync_vendor.py` **永不复制** `jira-tool/scripts/config.json`（里面有活的 PAT），
并在每次同步后复位 `canlog_config.json` 里机器相关的字段（`output_root` / `default_dbc` / `history`）——
否则"没给 --dbc"时会静默指向一个本机不存在的 DBC。

`install_skill.py` 会把目标目录清空重拷，所以它拒绝往"不像本 skill 安装点"的目录写，
也拒绝覆盖一个还留着已删除旧模块（`jta_*.py`）的陈旧副本 —— 让漂移显式报错而不是静默发生。

## 退出码

沿用 jira-tool 约定：`0` 成功 / `1` 脚本错误 / `2` 参数错误 / `3` 鉴权失败 / `4` 上游 API 错误 / `5` 有单失败。
