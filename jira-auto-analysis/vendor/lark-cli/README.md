# vendor/lark-cli —— 邮件发送器（手动放入）

这个目录**故意是空的**。发汇总邮件用的 `lark-cli.exe` 需要你手动放进来。

## 为什么不随包分发

1. **49 MB** —— 是工具本体源码的近百倍，zip 体积直接翻倍。
2. **换了机器也得重新登录** —— lark-cli 的登录态存在用户目录下的 `.lark-cli/`（机器本地），
   把 exe 拷过去也没用，必须重新认证一次。

所以它被当作**可选依赖**：不用邮件功能的话，不放也能跑完整个分析流水线。

## 怎么放

把 `lark-cli.exe` 拷到本目录，最终路径应为：

```
vendor/lark-cli/lark-cli.exe
```

原始位置见 `vendor/sources.json`（本机）或 `vendor/sources.example.json`（模板）里的
`lark-cli.src`。它是 workbuddy 装的 npm 包 `@larksuite/cli` 附带的单文件 exe，典型位置形如：

```
<你的 node/cli-connector-packages>/node_modules/@larksuite/cli/bin/lark-cli.exe
```

或者直接跑 `python tools/sync_vendor.py` 自动搬（需要先把 `sources.example.json` 复制成
`sources.json` 并填好本机源路径）。

## 放好之后

```bash
./jtat.cmd validate          # 第 [6] 段 lark-cli 应该变成 ✓
./jtat.cmd mail-only --date 2026-09-13 --dry-run   # 先看命令拼接对不对
```

## 不在这个目录时的替代做法

不想放这里也可以 —— 在 `config.json` 的 `paths.lark_cli` 里写任意路径即可，
相对路径按**工具根目录**解析，绝对路径原样使用：

```jsonc
"lark_cli": "D:/somewhere/else/lark-cli.exe"
```

## 实测过的坑（改 `scripts/jta_mail.py` 前必读）

1. `--attach` 只接受 **cwd 相对路径** → 发信前必须 cd 到产物目录（`jta_mail` 已处理）。
2. 正文**必须内联 `--body`**，不能用 `--body-file` 指向工程树内文件 ——
   公司 DLP 会把文件重新加密，lark-cli 读到密文，正文整个乱码。
3. 附件要先查 `%TSD-Header` 密文头，被加密的不能发（`jta_mail.is_dlp_encrypted()` 已处理）。
4. 只附小 `.md`（`analysis.md` / `SUMMARY.md`），**不附**几十 MB 的原始日志。
5. 若响应只有 `draft_id` 没有 `message_id`，需补一条 `drafts send`（`jta_mail` 已自动补发）。
