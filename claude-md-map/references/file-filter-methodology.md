# 文件过滤方法论（内置权威依据）

> 本文件是 `filter` 模式（文件过滤配置刷新）的方法论权威来源，由团队通用手册《Claude Code 文件过滤通用方案》提炼内置，脱离外部绝对路径依赖、随 skill 分发。
> 适用任何技术栈；只生成 `.claudeignore`（软过滤/效率层）+ `.claude/settings.json`（硬过滤/安全层）。

## 0. 目标与输入输出

- 输入：任意项目根目录
- 输出：`.claudeignore` + `.claude/settings.json`
- 衡量：主动扫描范围缩小 90%+；关键敏感文件无法被显式读/写；不影响正常代码理解

## 1. 分层防御（4 层）

| 层 | 配置 | 性质 | 显式读取 |
|---|---|---|---|
| L1 | `.gitignore` | 版本控制层，默认生效 | — |
| L2 | `.claudeignore` | 软过滤（效率层）不主动发现 | 仍可 Read |
| L3 | `permissions.deny` | 硬过滤（安全层）直接拒绝 | 拒绝 |
| L4 | 文件系统沙箱 | 内置兜底 | — |

**核心原则**：效率问题（大/多）→ `.claudeignore`；安全问题（敏感/不可改）→ `permissions.deny`；两者不互斥，重要文件可两层都加。

## 2. 五条判断规则

任一条答"是"即列入忽略候选：

- **A 源 vs 产物**：删掉重新 build 能否生成？产物忽略（`.o .obj .d .axf .hex .map`、`dist/ build/ target/ out/`）；手写源码保留。
- **B 内容 vs 状态**：换台电脑还需要吗？状态忽略（IDE 布局 `*.uvguix.*`、调试断点 `*.uvoptx`、缓存）；工程主文件/链接脚本/源码保留。
- **C 我的 vs 第三方**：你会改它源码吗？第三方忽略（`node_modules/ vendor/`），**保留 `.h`/接口、忽略实现**；锁文件（`package-lock.json` `Cargo.lock`）偶尔要分析依赖，**不忽略**。
- **D 文本 vs 二进制**：能用文本编辑器理解？二进制忽略（`.exe .dll .so .bin .elf .o .lib .a .pdf .xlsx .png .jpg .zip`），进上下文也是乱码。
- **E 业务 vs 过程**：有长期业务价值？过程产物忽略（`*.log`、`output/ tmp/`、覆盖率报告）；业务文档 `.md`、`CLAUDE.md` 保留。

## 3. 项目类型识别表

| 类型 | 识别信号 | 主要忽略 | 关键敏感 |
|---|---|---|---|
| 嵌入式 C（Keil MDK） | `*.uvprojx` `*.uvoptx` | `MDK/output/` `*.o` `*.axf` `*.map` | 链接脚本 `.sct`、bootloader、二进制固件 |
| 嵌入式 C（IAR） | `*.eww` `*.ewp` | `EWARM/settings/` `*.dep` `*.o` | 链接脚本 `.icf` |
| 嵌入式 C（GCC/Studio） | `Makefile` `CMakeLists.txt` `.cproject` | `build/` `*.o` `*.elf` | 链接脚本 `.ld` |
| Node 前端 | `package.json` | `node_modules/` `dist/` `.next/` `coverage/` | `.env` `.env.*` `credentials.json` |
| Node 后端 | `package.json`（无框架信号） | `node_modules/` `logs/` `*.log` | `.env`、API keys、数据库配置 |
| Python | `requirements.txt` `pyproject.toml` | `__pycache__/` `.venv/` `*.pyc` `.pytest_cache/` | `.env` `credentials.json` `secrets.yaml` |
| Java/Maven | `pom.xml` | `target/` `.m2/` `*.class` | `application-prod.properties` |
| Java/Gradle | `build.gradle` | `build/` `.gradle/` | 同上 |
| Go | `go.mod` | `vendor/` 编译二进制 | `secrets.yaml` `.env` |
| Rust | `Cargo.toml` | `target/` | `.env` |
| C#/.NET | `*.csproj` `*.sln` | `bin/` `obj/` | `appsettings.Production.json` |
| Ruby | `Gemfile` `*.gemspec` | `vendor/bundle/` `*.gem` | `.env` `credentials.yml` |
| PHP | `composer.json` | `vendor/` `storage/logs/` | `.env` |
| Monorepo | `pnpm-workspace.yaml` `lerna.json` | 各子项目 `node_modules/` `dist/` | 各包 `.env` |

## 4. 执行流程

1. **识别项目类型**：`ls -la` / `find . -maxdepth 2 -name "*.uvprojx" -o -name "package.json" -o ...`，套 §3 表；混合项目分别识别。
2. **测量目录大小**：`du -sh */ .* 2>/dev/null | sort -rh | head -15`，top 5 占 80%+ 体积。
3. **应用五条规则**：A 产物→`.claudeignore`；B 状态→`.claudeignore`；C 第三方→`.claudeignore`（保留 `.h`）；D 二进制→`.claudeignore`；E 过程产物→`.claudeignore`。特殊：改了会变砖/破坏核心（链接脚本、bootloader）→ deny 仅写；含密钥/凭据/本地路径 → deny 读+写；大二进制产物 → deny 读。
4. **生成 `.claudeignore`**：按 §5.1 模板填充项目特定内容。
5. **生成 `.claude/settings.json`**：按 §5.2 模板；**不覆盖已存在的 `.claude/settings.local.json`**。
6. **验证**：Glob 被忽略目录返回空；Read 被 deny 文件被拒绝。
7. **报告**：项目类型、top 大目录及决策、两文件路径、预估 token 节省比例。

## 5. 模板

### 5.1 `.claudeignore` 模板

```gitignore
# ============================================================
# <项目名> .claudeignore
# 原则：编译产物 + 第三方库 + 二进制 + 日志 + IDE 状态
# ============================================================

# === 编译产物 ===
# C/C++ 嵌入式
*.o
*.obj
*.d
*.axf
*.elf
*.hex
*.bin
*.map
*.crf
*.dep
*.lnp
*.build_log.htm
*.htm
# Web 前端
node_modules/
dist/
build/
.next/
.out/
coverage/
.nyc_output/
*.tsbuildinfo
# Python
__pycache__/
*.pyc
*.pyo
.venv/
venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
# Java
target/
*.class
.m2/
.gradle/
# Rust
target/
# .NET
bin/
obj/

# === 构建输出目录 ===
<MDK/output/ | build/ | target/ | dist/>

# === IDE 状态 ===
# Keil MDK
*.uvguix.*
*.uvoptx
MDK/RTE/
MDK/EventRecorderStub.scvd
MDK/RAM.ini
MDK/JLinkSettings.ini
MDK/JLinkLog.txt
MDK/build.log
MDK/download.log
# IAR
EWARM/settings/
EWARM/Backup/
# VSCode / JetBrains / Cursor
.vscode/
.idea/
.cursor/
*.swp
<EWARM/ | GCC/ | .vscode/>

# === 第三方库源码（保留 .h，忽略实现）===
<nanopb/examples/ | third_party/src/ | vendor/>

# === 二进制工具 / 安装包 ===
*.exe
*.dll
*.so
*.7z
*.zip
*.tar.gz
*.pyc

# === 日志/临时输出 ===
*.log
log.txt
logs/
<config/USB_LOG/ | tmp/ | output/>

# === 数据库 ===
*.db
*.sqlite
*.sqlite3

# === 二进制文档（保留 .md，跳过二进制）===
*.pdf
*.xlsx
*.xls
*.docx
*.png
*.jpg
*.jpeg
*.gif

# === 例外：必保留 ===
!*/CLAUDE.md
!**/CLAUDE.md
!doc/**/*.md
!*.proto
```

### 5.2 `.claude/settings.json` 模板

```json
{
  "permissions": {
    "deny": [
      "Read(./<build_output>/**)",
      "Read(./<ide_state>/**)",
      "Read(./<binary_output>/**/*.bin)",
      "Read(./<binary_output>/**/*.hex)",
      "Read(./<binary_output>/**/*.axf)",
      "Read(./<binary_output>/**/*.elf)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./**/credentials.json)",
      "Read(./**/secrets.yaml)",
      "Read(./**/secrets.yml)",
      "Read(./**/*.pem)",
      "Read(./**/*.key)",
      "Read(./<local_db>.db)",
      "Read(./<log_dir>/**)",
      "Edit(./<project_main_file>)",
      "Write(./<project_main_file>)",
      "Edit(./<linker_script_dir>/**)",
      "Write(./<linker_script_dir>/**)",
      "Edit(./<bootloader_dir>/**)",
      "Write(./<bootloader_dir>/**)"
    ]
  }
}
```

### 5.3 决策矩阵

| 文件特征 | `.claudeignore` | deny Read | deny Write |
|---|---|---|---|
| 编译产物（大） | ✅ | ✅ | - |
| 编译产物（小） | ✅ | - | - |
| IDE 状态文件 | ✅ | ✅（含本地路径） | - |
| 第三方库源码 | ✅（保留 .h） | - | - |
| 二进制工具 | ✅ | - | - |
| 业务日志 | ✅ | - | - |
| 业务文档（.md） | ❌ | ❌ | - |
| 二进制文档（PDF/图片） | ✅ | - | - |
| 含密钥/凭据 | - | ✅ | - |
| 工程主文件（如 .uvprojx） | ❌ | ❌ | ✅ |
| 链接脚本 | ❌（看场景） | ❌ | ✅ |
| Bootloader 源码 | ❌ | ❌ | ✅（防误改） |
| 锁文件（lock） | ❌ | ❌ | - |

## 6. 完整案例（嵌入式 C / Keil MDK，HC32 DAB）

- **识别**：`MDK/hc32f460petb.uvprojx` → Keil MDK；HC32F460；双槽 OTA。
- **决策表**：

| 目录/文件 | 大小 | 规则 | 决策 |
|---|---|---|---|
| `MDK/output/` | 118MB | A+D | `.claudeignore` + deny Read |
| `MDK/*.uvguix.*` | 小 | B（含本地路径） | `.claudeignore` + deny Read |
| `MDK/*.uvoptx` | 小 | B | `.claudeignore` |
| `MDK/hc32f460petb.uvprojx` | 小 | 工程主文件 | deny Write |
| `MDK/config/linker/*.sct` | 小 | 链接脚本（影响 OTA） | deny Write |
| `L6_Public/nanopb/` | 30MB | C 第三方 | `.claudeignore`（保留 `.h`） |
| `config/ADB_Test/adb/platform-tools/` | 70MB+ | D 工具 | `.claudeignore` |
| `config/USB_LOG/` | 小 | E 日志 | `.claudeignore` + deny Read |
| `EWARM/` `GCC/` `.vscode/` | 小 | B 其他 IDE | `.claudeignore` |
| `prompts.db` | 小 | D + 用户数据 | `.claudeignore` + deny Read |
| `*.bin` | 小 | D 固件 | `.claudeignore` + deny Read |
| `doc/**/*.pdf`, `*.png` | - | D 文档 | `.claudeignore`（保留 `.md`） |

- **效果**：~225MB → ~5MB（97%+）。

## 7. 五个常见误判

1. **链接脚本整个 deny**：应允许 Read（AI 需读 OTA 地址/内存布局），只 deny Write。
2. **锁文件加入 `.claudeignore`**：`package-lock.json`/`Cargo.lock` 偶尔要分析依赖冲突，保留。
3. **`.env` 只加 `.claudeignore`**：软过滤显式 Read 仍能读到、密钥泄露，必须 `permissions.deny`。
4. **忽略整个 `doc/`**：业务文档是 AI 理解上下文的关键，只忽略二进制、保留 `.md`。
5. **通配符误伤 `CLAUDE.md`**：必须 `!*/CLAUDE.md` `!**/CLAUDE.md` 兜底保留。

## 8. 验证清单

- [ ] `.claudeignore` 存在于项目根；`.claude/settings.json` 存在且 JSON 合法
- [ ] `.claude/settings.local.json` 未被覆盖
- [ ] Glob 被忽略目录返回空；Read 被 deny 文件被拒绝
- [ ] `CLAUDE.md` 仍可读；业务文档 `.md` 正常读；编译产物不再出现在搜索结果
- [ ] 工程主文件、链接脚本可读不可写

## 维护

- 新增依赖/工具链 → 检查新产物目录，及时加入 `.claudeignore`
- 新增敏感文件 → 立即 `permissions.deny`
- 定期 `du -sh */ | sort -rh` 看新大目录；`.claude/settings.json` 提交 Git 团队共享，个人偏好放 `settings.local.json`
