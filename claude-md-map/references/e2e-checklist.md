# 端到端验证清单(mdmap-e2e)

样例工程:`%TEMP%\mdmap-e2e\`(Git Bash 下 `$(cygpath -u "$TEMP" 2>/dev/null || echo /tmp)/mdmap-e2e/`),手造 monorepo:`services/api`、`services/worker`、`web`、`shared/lib`、`docs/`,各放几个真实可读的源码文件,`git init` + 一次 commit。

## 1. init
- [ ] `detect` → MODE=INIT;`--ensure-workspace` 后 `.claude-md-map/`、`tmp/draft/`、`.gitignore(tmp/)` 就位
- [ ] `scan` stdout ≤30 行,scan.json 落盘;`plan` 划分表合理(4-5 模块,docs/ 被并组或忽略)
- [ ] worker 草稿:根 ≤60 行、每个模块 ≤80 行
- [ ] `lint --draft-dir` PASS;`report` 可读;用户确认后 `install` 成功
- [ ] state.json 有 reviewed_commit(=HEAD)与各文件 content_hash

## 2. noop
- [ ] 立刻重跑 `detect` → MODE=NOOP,零 subagent 派发,零文件写入

## 3. 定向更新(核心)
- [ ] 改 `services/api` 2 个文件 + `web/` 加 1 个,commit
- [ ] `detect-changes` 受影响模块恰为 [api, web];root_dirty=false
- [ ] update brief 只含变更文件清单;worker 只改失真处
- [ ] `install --mode update` 后 `git status --porcelain` 只含:CLAUDE.md(若根同步)、`services/api/CLAUDE.md`、`web/CLAUDE.md`、state.json
- [ ] **`services/worker/CLAUDE.md` 与 `shared/lib/CLAUDE.md` 零改动**(diff 验证)

## 4. 非 git
- [ ] `rm -rf .git` 后重新 install 一次 → state.vcs=none,reviewed_commit=null
- [ ] 改动后 `detect-changes` → 降级 content_hash 粒度,报告仍能指出受影响模块

## 5. 手改冲突
- [ ] init 后手工在 `web/CLAUDE.md` 加一行独特文本,再改 web 代码并 commit
- [ ] update 后该独特行仍存在(grep 验证),worker 报告标注"已保留手写 N 处"

## 6. 上下文预算(主会话自检)
- [ ] scan/plan/detect/detect-changes stdout 均 ≤200 行
- [ ] 全程主会话 Read/Grep/Glob 过的业务源码文件数 = 0(源码只被 worker 读)
- [ ] dispatch prompt 均 ≤25 行;worker 回执均 ≤15 行

## 7. 边界
- [ ] `detect` 在 home 或 <5 源文件目录 → 退出码 4
- [ ] 删掉一个模块目录 → detect-changes 报 deleted,确认后清理地图行与 state
- [ ] state.json 被删 → detect 退化 ADOPT,合并式重排不覆盖手写内容
