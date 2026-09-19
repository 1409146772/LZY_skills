# filter 模式:文件过滤配置刷新(当前工程)

> 范围 = **当前工程根**(会话工作目录所在工程)。方法论文档为 skill 内置 `references/file-filter-methodology.md`。

## 前置(必做)

1. **先读 `<skill>/references/file-filter-methodology.md` 全文**——它是内置权威依据:§2 五条判断规则、§3 项目类型识别表、§4 执行流程、§5.1 .claudeignore 模板、§5.2 settings.json 模板、§5.3 决策矩阵、§6 完整案例、§7 五个常见误判、§8 验证清单。
2. (可选覆盖)若 `config/config.conf` 仍配了外部 `file_filter_methodology_doc` 路径,则改读该外部文档并以其为准(为兼容旧配置;默认不配,用内置 reference)。
3. 按 §3 识别当前工程类型:标记文件 `*.uvprojx`=Keil MDK、`.cproject`=S32 Design Studio、`package.json`=Node、`CMakeLists.txt`=CMake 等;识别不出 → 停下让用户告知技术栈。

## 变更检测(减少无效写入)

```
du -sh */ 2>/dev/null | sort -rh | head -10
```

- 与现有 `.claudeignore` 比对是否新增需忽略的大头目录/产物目录。
- **top10 大目录无变化且无新增产物目录 → 报告「本次无变更,跳过」,禁止强行写入。**
- 无 `.claudeignore` → 首次生成(init 场景),不走跳过。

## 无人值守默认决策规则(本任务无人在场确认)

1. **文件过滤决策表不等人确认**:按 §2/§5.3 自动生成决策并直接写入文件,决策表纳入最终报告。
2. **`.claude/settings.json` 已存在 → 合并模式**:保留现有 deny 条目,仅追加缺失项,**绝不整文件覆盖**;不存在 → 按 §5.2 模板新建。
3. **疑似密钥/凭据文件未被 .gitignore 跟踪 → 自动加入 `permissions.deny(Read)`**,并在报告高亮告警待用户复核。
4. **top 大目录中出现 §3 表外的未知目录 → 按第三方/产物保守忽略**,在报告列出待用户复核。
5. **项目根无 .gitignore → 跳过文件过滤子任务并报告**(有 .gitignore 的工程正常不会触发)。

## Scope / Constraints

- 只修改当前工程根的 `.claudeignore` 与 `.claude/settings.json`;不修改 `.gitignore`;不覆盖 `.claude/settings.local.json`。
- 严格按 §2 五条判断规则(源/产物、内容/状态、我的/第三方、文本/二进制、业务/过程)决策。
- 严格按 §5.3 决策矩阵选择机制(.claudeignore / deny Read / deny Write)。
- 第三方库源码忽略实现(.c/.py/.js),但必须保留接口(.h/类型定义),如 nanopb。
- 所有层级 CLAUDE.md 必须可读,通配符不能误伤(**用 `!**/CLAUDE.md` 兜底**)。
- 链接脚本、工程主文件(`*.uvprojx`/`.cproject` 等)只 deny Write,不 deny Read。
- 含密钥/凭据/本地路径的文件必须用 `permissions.deny`,不能只用 `.claudeignore`。
- 业务文档(.md)必须可读,只忽略二进制文档(.pdf/.xlsx/.png)。
- 锁文件不要忽略。

## Done when

1. 输出项目类型识别结果(标记文件)。
2. 输出 top10 大目录及大小。
3. 输出决策表(目录/文件 | 大小 | 应用规则 A-E | 决策)。
4. 输出更新后的 `.claudeignore` 内容。
5. 输出合并后的 `.claude/settings.json` 内容(标注本次新增的 deny 条目)。
6. 报告预估 token 节省比例。
7. 写入两个文件并报告路径。

## Stop if

- 项目根没有 `.gitignore`:先建议用户创建并提交,再继续。
- 项目类型无法识别:让用户告知技术栈。
- 已存在 `.claude/settings.json`:合并模式处理。
- 检测到疑似密钥/凭据文件未被 .gitignore 跟踪:自动 deny(Read) 并告警。
- top 5 大目录中有未知目录:保守忽略并报告待复核。

## 输出与报告(文字总结到对话 + 落盘统一报告)

- 本节 1-6 项内容同时作为 filter 章节写入统一报告文件(路径规则见 SKILL.md「统一报告文件」)。

- 本次是否有变更(有/无,无则说明跳过原因)。
- 改动的文件路径清单。
- 文件过滤:决策表、top10 大目录、token 节省估算、`.claudeignore` 与 `settings.json` 的 diff(新增了哪些行)。
- 所有告警/待用户复核项汇总(疑似密钥文件、未知大目录)。

## 硬约束(全流程不可违反)

- 只允许改/新建当前工程根的:`.claudeignore`、`.claude/settings.json`。
- 禁止碰:任何 `.c/.h/.s` 源码、构建工程文件(`.uvprojx/.uvoptx/.ewp/.cproject` 等)、`.gitignore`、`.claude/settings.local.json`、CLAUDE.md、CODE_MAP.md。
- 不得在无变更时强行写入。
