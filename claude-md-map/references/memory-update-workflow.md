# memory 模式:Claude 记忆更新(当前工程)

> 交互发生在哪个工程,就整理哪个工程的 auto-memory。菜单选择即授权,不再二次询问。

## 范围判定

- 优先取当前会话系统上下文给出的 memory 目录路径。
- 否则按 `~/.claude/projects/<cwd 非字母数字字符替换为 ->/memory/` 推导(例:CDC_7255 工程 → `D--wnagjin-Compay-Project-CDC-7255-cdc-7255`)。

## 四个动作

### 1. 会话新知识入库
- 从近期会话提炼 **durable 事实**,分四型:`user`(用户是谁/角色/专长/偏好)、`feedback`(用户对我工作方式的纠正或确认)、`project`(进行中的工作/目标/约束)、`reference`(外部资源指针)。
- 同主题已有文件 → **更新原文件而非新建**;按既有 frontmatter 格式(name/description/metadata.type);feedback/project 加 **Why:**/**How to apply:** 两行;相对日期转绝对;用 `[[双链]]` 关联相关记忆。

### 2. 既有条目校准
- 核对每条记忆引用的文件/函数/宏/路径**仍存在**(Glob/Grep 验证)。
- 失真(名字改了/路径没了/结论已被推翻)→ 修正;明显过时或错误的 → 删除。

### 3. 索引同步
- `MEMORY.md` 每条一行(标题 + hook),不放正文;文件增删后同步索引。

### 4. 报告落盘
- 变更清单表(操作|条目|原因)+ 校准结论,作为 memory 章节写入统一报告文件(路径规则见 SKILL.md「统一报告文件」)。

## 硬规则

- 不存 repo 已记录的(CLAUDE.md / git 历史可推导的)。
- 不存仅本次对话有用的临时信息。
- 删除必须给理由。
- 回复中列变更清单:新建/更新/删除 + 一句话原因。

## 验证清单

- [ ] 新记忆按 frontmatter 模板书写(有 name/description/metadata.type)
- [ ] 索引与文件一一对应,无孤儿条目
- [ ] 引用路径经验证仍存在
- [ ] 变更清单已向用户报告
- [ ] memory 章节已写入统一报告文件 OutPut_YYYYMMDD.html
