# -*- coding: utf-8 -*-
"""jira_ops：固定 Jira CLI（scripts/ops/*.py）与一次性脚本共享的业务函数库。

定位（务必维持）：
  - 纯函数库：【不 print、不 argparse、不 jb.out】，只返回 dict / 抛异常。
  - CLI 层（scripts/ops/）只做 argparse + 调用本库 + jb.out(...)。
  - 一次性脚本（SKILL.md §4）可直接 `import jira_ops as jo` 复用函数，不必再抄 mapper。

退出码约定（由 @jb.guard 兜底实现）：
  0=成功 · 1=脚本错误 · 2=参数错误（本库 arg_fail）· 3=Jira 鉴权失败(401/403) · 4=Jira API 错误

Server 9.12.1 专用事实（勿改回 Cloud 写法）：
  - user 字段一律 {"name": ...}，不是 accountId。
  - jira.createmeta() 会抛 Unsupported JIRA version —— 必须用
    jira.project_issue_types() / jira.project_issue_fields()。
  - search 分页用手动 startAt 循环（JIRA.paginate() 不存在；maxResults=0 无界）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jira_bootstrap as jb  # noqa: E402
from jira.exceptions import JIRAError  # noqa: E402

# --- 常量 ---
SCRIPTS_DIR = Path(__file__).resolve().parent
SEARCH_PAGE = 100        # 搜索翻页页大小
META_PAGE = 200          # 元数据（issue types / fields）翻页页大小
TEXT_TRUNC = 400         # 列表行 summary 截断
BODY_TRUNC_DEFAULT = 4000  # 评论正文默认截断
ALLOWED_LIMIT = 40       # allowedValues 最多列出 N 项
SIMPLE_FIELDS = ("summary", "description", "priority", "assignee", "duedate", "labels")
DEFAULT_SEARCH_FIELDS = ["key", "summary", "status", "issuetype", "assignee",
                         "reporter", "priority", "created", "updated", "duedate", "labels"]


# ============================================================
# 私有 mapper：python-jira Resource / 原始 dict → 纯 dict
# ============================================================
def _gv(obj: Any, name: str, default: Any = None) -> Any:
    """dict 与对象通用的属性/键取值，缺失一律 default（fields 裁剪后属性可能不存在）。"""
    if isinstance(obj, dict):
        v = obj.get(name, default)
    else:
        v = getattr(obj, name, default)
    return default if v is None else v


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        v = _gv(obj, n)
        if v is not None:
            return v
    return default


def _q(value: str) -> str:
    """JQL 字符串值转义双引号。"""
    return str(value).replace('"', '\\"')


def _user(u: Any) -> dict[str, Any] | None:
    """user → 精简 dict。Server 用 name（非 accountId）。全 None 时返回 None。"""
    if not u:
        return None
    if not any(_gv(u, k) for k in ("name", "key", "displayName", "emailAddress")):
        return None
    return {
        "name": _g(u, "name"),
        "key": _g(u, "key"),
        "displayName": _g(u, "displayName"),
        "email": _g(u, "emailAddress"),
        "active": _g(u, "active"),
    }


def _pick_named(res: Any) -> dict[str, Any] | None:
    """status / issuetype / priority / resolution 等命名对象 → {id,name}。"""
    if not res:
        return None
    return {"id": _g(res, "id"), "name": _g(res, "name"),
            "description": _g(res, "description")}


def _status(s: Any) -> dict[str, Any] | None:
    if not s:
        return None
    cat = _g(s, "statusCategory")
    return {"id": _g(s, "id"), "name": _g(s, "name"),
            "category": _g(cat, "name") if cat else _g(s, "statusCategory")}


def _option(v: Any) -> dict[str, Any] | None:
    """option / 级联选择 → {id,value[,child]}。"""
    if not v:
        return None
    d: dict[str, Any] = {"id": _g(v, "id"), "value": _g(v, "value")}
    child = _gv(v, "child")
    if child:
        d["child"] = _option(child)
    return d


def _comment(c: Any, *, body_trunc: int | None = BODY_TRUNC_DEFAULT) -> dict[str, Any]:
    body = _g(c, "body")
    if body_trunc and isinstance(body, str) and len(body) > body_trunc:
        body = body[:body_trunc] + f"…[已截断，共 {len(body)} 字]"
    return {
        "id": _g(c, "id"),
        "author": _user(_g(c, "author")),
        "created": _g(c, "created"),
        "updated": _g(c, "updated"),
        "body": body,
        "visibility": _g(c, "visibility"),
    }


def _attachment(a: Any) -> dict[str, Any]:
    return {
        "id": _g(a, "id"),
        "filename": _g(a, "filename"),
        "size": _g(a, "size"),
        "mime_type": _g(a, "mimeType"),
        "created": _g(a, "created"),
        "author": _user(_g(a, "author")),
        "content_url": _g(a, "content"),
    }


def _short_issue(i: Any) -> dict[str, Any]:
    """搜索结果行：精简字段 + summary 截断，防止 stdout 刷爆。"""
    f = getattr(i, "fields", None)
    summary = _g(f, "summary")
    if isinstance(summary, str) and len(summary) > TEXT_TRUNC:
        summary = summary[:TEXT_TRUNC] + "…"
    key = _g(i, "key")
    return {
        "key": key,
        "id": _g(i, "id"),
        "url": jb.browse_url(key or ""),
        "summary": summary,
        "status": _status(_g(f, "status")),
        "issuetype": _pick_named(_g(f, "issuetype")),
        "priority": _pick_named(_g(f, "priority")),
        "assignee": _user(_g(f, "assignee")),
        "reporter": _user(_g(f, "reporter")),
        "created": _g(f, "created"),
        "updated": _g(f, "updated"),
        "duedate": _g(f, "duedate"),
        "labels": _g(f, "labels", default=[]) or [],
    }


def _full_issue(i: Any, *, include_raw: bool = False, comment_limit: int = 20) -> dict[str, Any]:
    f = getattr(i, "fields", None)
    container = _g(f, "comment")
    raw_comments = list(_g(container, "comments", default=[]) or [])
    total_comments = _g(container, "total")
    if comment_limit and comment_limit > 0:
        comments = [_comment(c) for c in raw_comments[:comment_limit]]
    else:
        comments = []
    key = _g(i, "key")
    data: dict[str, Any] = {
        "key": key,
        "id": _g(i, "id"),
        "url": jb.browse_url(key or ""),
        "project": _pick_named(_g(f, "project")),
        "issuetype": _pick_named(_g(f, "issuetype")),
        "status": _status(_g(f, "status")),
        "summary": _g(f, "summary"),
        "description": _g(f, "description"),
        "priority": _pick_named(_g(f, "priority")),
        "assignee": _user(_g(f, "assignee")),
        "reporter": _user(_g(f, "reporter")),
        "creator": _user(_g(f, "creator")),
        "created": _g(f, "created"),
        "updated": _g(f, "updated"),
        "duedate": _g(f, "duedate"),
        "resolution": _pick_named(_g(f, "resolution")),
        "labels": _g(f, "labels", default=[]) or [],
        "components": [_pick_named(c) for c in (_g(f, "components", default=[]) or [])],
        "comment_total": int(total_comments) if total_comments is not None else (
            len(raw_comments) if raw_comments else None),
        "comments": comments,
        "comments_truncated": bool(
            total_comments is not None and int(total_comments) > len(comments)
            and (comment_limit or 0) > 0),
    }
    if include_raw:
        raw = getattr(i, "raw", None)
        if isinstance(raw, dict):
            data["raw_fields"] = raw.get("fields")   # 查 customfield 实际值的关键通道
    return data


def _allowed_values(vals: Any, limit: int = ALLOWED_LIMIT) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    lst = list(vals or [])
    for v in lst[:limit]:
        if isinstance(v, dict):
            d: dict[str, Any] = {"id": v.get("id"), "name": v.get("name")}
            if v.get("value") is not None:
                d["value"] = v.get("value")
            if v.get("child") is not None:
                d["child"] = {"value": (v.get("child") or {}).get("value")}
            out.append(d)
    if len(lst) > limit:
        out.append({"_note": f"还有 {len(lst) - limit} 项未列出"})
    return out


def _field_meta(raw: dict[str, Any]) -> dict[str, Any]:
    """字段元数据 → 精简 dict。字段 id 在 Server 9.12.1 的 raw["fieldId"]（三重回退）。"""
    schema = raw.get("schema") or {}
    return {
        "id": raw.get("fieldId") or raw.get("key") or raw.get("id"),
        "name": raw.get("name"),
        "required": bool(raw.get("required")),
        "type": schema.get("type") or raw.get("type"),
        "system": schema.get("system"),
        "custom": schema.get("custom"),
        "custom_id": schema.get("customId"),
        "has_default": raw.get("hasDefaultValue"),
        "operations": raw.get("operations"),
    }


def _project_brief(p: Any) -> dict[str, Any]:
    return {
        "key": _g(p, "key"),
        "id": _g(p, "id"),
        "name": _g(p, "name"),
        "lead": _user(_g(p, "lead")),
        "projectTypeKey": _g(p, "projectTypeKey"),
    }


# ============================================================
# 通用助手（CLI 与一次性脚本共用）
# ============================================================
def arg_fail(msg: str) -> NoReturn:
    """参数错误统一出口：stderr ❌ + 退出码 2。"""
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(2)


def load_json_arg(value: str, *, what: str = "JSON") -> Any:
    """JSON 参数三形式：内联字符串 / @文件路径 / -（stdin）。UTF-8 读取。

    Windows 命令行传含中文/双引号的 JSON 极易转义失败 —— 长内容一律用 @file。
    """
    raw: str
    if value == "-":
        raw = sys.stdin.read()
    elif value.startswith("@"):
        p = Path(value[1:])
        if not p.exists():
            arg_fail(f"{what} 文件不存在：{p}")
        raw = p.read_text(encoding="utf-8")
    else:
        raw = value
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        arg_fail(f"{what} 不是合法 JSON（{e}）")


def load_fields_arg(value: str, *, what: str = "--data") -> dict[str, Any]:
    data = load_json_arg(value, what=what)
    if not isinstance(data, dict):
        arg_fail(f"{what} 必须是 JSON 对象（dict），实际为 {type(data).__name__}")
    return data


def read_body_file(value: str) -> str:
    """评论/正文文件：-（stdin）、@路径 或 直接路径。内容是纯文本，不是 JSON。"""
    if value == "-":
        return sys.stdin.read()
    p = Path(value[1:] if value.startswith("@") else value)
    if not p.exists():
        arg_fail(f"文件不存在：{p}")
    return p.read_text(encoding="utf-8")


def make_parser(description: str, epilog: str = "") -> "argparse.ArgumentParser":
    """统一 ArgumentParser：禁缩写（防 --jsommary 悄悄命中）+ RawDescriptionHelpFormatter。"""
    import argparse

    return argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )


def add_dry_run(p: "argparse.ArgumentParser", *, help_text: str =
                "只打印将要执行的 payload，不实际写 Jira") -> None:
    p.add_argument("--dry-run", action="store_true", help=help_text)


def build_jql(project: str | None = None, *, text: str | None = None,
              status: str | None = None, assignee: str | None = None,
              reporter: str | None = None, itype: str | None = None,
              since: str | None = None, order: str | None = None) -> str:
    """便利过滤参数 → JQL。--assignee 为 "-" 或 "unassigned" → assignee is EMPTY。"""
    parts: list[str] = []
    if project:
        parts.append(f"project = {_q(project)}")
    if text:
        parts.append(f'text ~ "{_q(text)}"')
    if status:
        parts.append(f'status = "{_q(status)}"')
    if assignee:
        if assignee in ("-", "unassigned"):
            parts.append("assignee is EMPTY")
        else:
            parts.append(f"assignee = {_q(assignee)}")
    if reporter:
        parts.append(f"reporter = {_q(reporter)}")
    if itype:
        parts.append(f'type = "{_q(itype)}"')
    if since:
        parts.append(f"created >= {_q(since)}")
    if not parts:
        arg_fail("至少需要一个过滤条件（--project / --text / --status / --assignee / "
                 "--reporter / --type / --since），或直接给 --jql")
    jql = " AND ".join(parts)
    if order:
        jql += f" order by {_q(order)}"
    return jql


def jira_error_detail(e: JIRAError) -> dict[str, Any]:
    """JIRAError → {errors:{field:msg}, errorMessages:[...]}（从 response.text 解析）。"""
    detail: dict[str, Any] = {}
    resp = getattr(e, "response", None)
    text = getattr(resp, "text", None) if resp is not None else None
    if text:
        try:
            j = json.loads(text)
            if isinstance(j, dict):
                if j.get("errors"):
                    detail["errors"] = j["errors"]
                if j.get("errorMessages"):
                    detail["errorMessages"] = j["errorMessages"]
        except Exception:
            pass
    if not detail:
        t = getattr(e, "text", None)
        if t:
            detail["message"] = str(t)[:300]
    return detail


def raise_write_error(e: JIRAError, *, op: str) -> NoReturn:
    """写操作失败的统一出口：stderr 先打 field_errors，再以退出码 4 退出。"""
    status = getattr(e, "status_code", "?")
    print(f"field_errors={json.dumps(jira_error_detail(e), ensure_ascii=False)}",
          file=sys.stderr)
    raise SystemExit(jb.err(f"{op}失败 HTTP {status}", e, code=4))


def exact_user_check(jira: Any, user: str | None, *, key: str | None = None,
                     project: str | None = None,
                     assignable_only: bool = False) -> dict[str, Any]:
    """精确 name 匹配校验。python-jira 内部 assign/watcher 是【模糊】用户搜索且多命中取
    第一个 —— 直接传 --user 有指派错人的实际风险，实跑前必须过这里；匹配失败退出码 2。"""
    if not user:
        return {}
    if assignable_only:
        cands = resolve_assignable(jira, user, key=key, project=project, max_results=50)
    else:
        users = jira.search_users(user=user, maxResults=50)
        cands = [{"name": _g(u, "name"), "displayName": _g(u, "displayName"),
                  "email": _g(u, "emailAddress"), "active": _g(u, "active")} for u in users]
    for c in cands:
        if c.get("name") == user:
            return c
    arg_fail(f"用户 {user!r} 不是精确匹配的 name（模糊匹配可能指错人）。候选："
             + json.dumps(cands[:10], ensure_ascii=False))


# ============================================================
# 业务函数：搜索 / 读
# ============================================================
def search_issues(jira: Any, jql: str, *, limit: int = 50, fetch_all: bool = False,
                  fields: list[str] | str | None = None, start_at: int = 0,
                  validate_query: bool = True) -> dict[str, Any]:
    """JQL 搜索，手动 startAt 分页（页大小 SEARCH_PAGE），limit 为硬上限。

    返回 {jql, start_at, limit, fetch_all, total, returned, truncated, issues:[_short_issue]}
    """
    flds = fields if fields is not None else list(DEFAULT_SEARCH_FIELDS)
    rows: list[dict[str, Any]] = []
    pos = start_at
    total: int | None = None
    while len(rows) < limit:
        want = min(SEARCH_PAGE, limit - len(rows))
        res = jira.search_issues(jql, startAt=pos, maxResults=want, fields=flds,
                                 validate_query=validate_query)
        total = _g(res, "total")
        got = list(res)
        rows.extend(_short_issue(i) for i in got)
        pos += len(got)
        if not fetch_all or not got:
            break
        if total is not None and pos >= int(total):
            break
    end = start_at + len(rows)
    return {
        "jql": jql, "start_at": start_at, "limit": limit, "fetch_all": fetch_all,
        "total": int(total) if total is not None else len(rows),
        "returned": len(rows),
        "truncated": bool(total is not None and end < int(total)),
        "issues": rows,
    }


def get_issue(jira: Any, key: str, *, fields: list[str] | None = None,
              include_comments: bool = True, comment_limit: int = 20,
              include_raw: bool = False) -> dict[str, Any]:
    """读单条 issue。include_raw=True 时附 raw_fields（查 customfield 实际值必开）。"""
    flds = list(fields) if fields else [
        "summary", "description", "status", "issuetype", "priority", "assignee",
        "reporter", "creator", "created", "updated", "duedate", "resolution",
        "labels", "components", "project"]
    if include_comments and "comment" not in flds:
        flds = flds + ["comment"]
    issue = jira.issue(key, fields=",".join(flds))
    return _full_issue(issue, include_raw=include_raw,
                       comment_limit=comment_limit if include_comments else 0)


def get_comments(jira: Any, key: str, *, limit: int = 200, offset: int = 0,
                 order: str | None = "created", body_trunc: int = BODY_TRUNC_DEFAULT,
                 fetch_all: bool = True) -> dict[str, Any]:
    """读评论：先 issue(key, fields="comment") 拿 total，超页时用 jira.comments() 续拉。"""
    f = getattr(jira.issue(key, fields="comment"), "fields", None)
    container = _g(f, "comment")
    total = int(_g(container, "total", default=0) or 0)
    comments: list[Any] = list(_g(container, "comments", default=[]) or [])
    pos = len(comments)
    want = offset + limit
    while fetch_all and pos < total and pos < want:
        batch = list(jira.comments(key, start_at=pos,
                                   max_results=min(SEARCH_PAGE * 5, want - pos),
                                   order_by=order))
        if not batch:
            break
        comments.extend(batch)
        pos += len(batch)
    sel = comments[offset:want] if (offset or limit) else comments
    return {
        "key": key, "url": jb.browse_url(key),
        "total": total, "returned": len(sel),
        "truncated": bool(offset + len(sel) < total),
        "offset": offset,
        "comments": [_comment(c, body_trunc=body_trunc) for c in sel],
    }


def get_comment(jira: Any, key: str, comment_id: str) -> dict[str, Any]:
    return {"key": key, "url": jb.browse_url(key),
            "comment": _comment(jira.comment(key, comment_id))}


# ============================================================
# 业务函数：写（全部先查再写；JIRAError → raise_write_error → 退出码 4）
# ============================================================
def add_comment(jira: Any, key: str, body: str, *,
                visibility: dict[str, str] | None = None,
                dry_run: bool = False) -> dict[str, Any]:
    """加评论。注意：Jira 评论没有 notify 开关（总会通知）。"""
    payload: dict[str, Any] = {"key": key, "body": body}
    if visibility:
        payload["visibility"] = visibility
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True,
                "skipped": True, "payload": payload, "comment": None}
    try:
        c = jira.add_comment(key, body, visibility=visibility)
    except JIRAError as e:
        raise_write_error(e, op="添加评论")
    return {"key": key, "url": jb.browse_url(key), "dry_run": False,
            "skipped": False, "payload": payload, "comment": _comment(c)}


def list_issue_types(jira: Any, project: str, *, hard_cap: int = 500) -> dict[str, Any]:
    """列项目 issue 类型（createmeta 替代 ①：jira.createmeta 在 9.12.1 会抛异常）。"""
    items: list[dict[str, Any]] = []
    pos, total = 0, None
    while pos < hard_cap:
        res = jira.project_issue_types(project, startAt=pos, maxResults=SEARCH_PAGE)
        total = _g(res, "total")
        batch = list(res)
        for t in batch:
            items.append({"id": _g(t, "id"), "name": _g(t, "name"),
                          "subtask": _g(t, "subtask"), "description": _g(t, "description")})
        pos += len(batch)
        if not batch or (total is not None and pos >= int(total)):
            break
    return {"project": project,
            "total": int(total) if total is not None else len(items),
            "issue_types": items}


def resolve_issue_type(jira: Any, project: str, name_or_id: str) -> dict[str, Any]:
    """按名称或 id 精确解析 issue 类型；失败列出全部（退出码 2）。"""
    meta = list_issue_types(jira, project)
    for t in meta["issue_types"]:
        if t.get("id") == str(name_or_id) or t.get("name") == name_or_id:
            return t
    arg_fail(f"项目 {project} 没有类型 {name_or_id!r}。可用："
             + json.dumps(meta["issue_types"], ensure_ascii=False))


def list_issue_fields(jira: Any, project: str, issue_type_id: str, *,
                      hard_cap: int = 2000) -> dict[str, Any]:
    """列项目+类型的字段元数据（createmeta 替代 ②）。字段 id 在 raw["fieldId"]。"""
    items: list[dict[str, Any]] = []
    pos, total = 0, None
    while pos < hard_cap:
        res = jira.project_issue_fields(project, str(issue_type_id),
                                        startAt=pos, maxResults=META_PAGE)
        total = _g(res, "total")
        batch = list(res)
        for fr in batch:
            raw = fr.raw if hasattr(fr, "raw") else fr
            items.append(_field_meta(raw))
        pos += len(batch)
        if not batch or (total is not None and pos >= int(total)):
            break
    return {"project": project, "issue_type_id": str(issue_type_id),
            "total": int(total) if total is not None else len(items), "fields": items}


def required_fields(jira: Any, project: str, issue_type_id: str) -> list[dict[str, Any]]:
    meta = list_issue_fields(jira, project, issue_type_id)
    return [fm for fm in meta["fields"] if fm.get("required")]


def missing_required(fields: dict[str, Any], required: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对照必填清单检查 payload，返回缺失项 [{id,name,why}]（空列表=通过）。"""
    out: list[dict[str, Any]] = []
    for r in required:
        fid = r.get("id")
        if not fid:
            continue
        if fid in fields and fields[fid] not in (None, "", [], {}):
            continue
        out.append({"id": fid, "name": r.get("name"), "why": "缺少或为空"})
    return out


def create_issue(jira: Any, fields: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """创建单条 issue（不用 create_issues：单条失败会抛 JIRAError，好处理）。"""
    if dry_run:
        return {"dry_run": True, "skipped": True, "payload": {"fields": fields},
                "key": None, "id": None, "url": None}
    try:
        issue = jira.create_issue(fields=fields)
    except JIRAError as e:
        raise_write_error(e, op="创建 issue")
    key = _g(issue, "key")
    return {"dry_run": False, "skipped": False, "payload": {"fields": fields},
            "key": key, "id": _g(issue, "id"), "url": jb.browse_url(key or "")}


def _snapshot(jira: Any, key: str, field_ids: list[str]) -> dict[str, Any]:
    """取简单字段的当前值快照（before/after 对比、幂等跳过用）。"""
    want = [f for f in field_ids if f in SIMPLE_FIELDS]
    if not want:
        return {}
    f = getattr(jira.issue(key, fields=",".join(want)), "fields", None)
    snap: dict[str, Any] = {}
    for name in want:
        v = _g(f, name)
        if name == "priority":
            v = (_pick_named(v) or {}).get("name")
        elif name == "assignee":
            v = (_user(v) or {}).get("name")
        snap[name] = v
    return snap


def _no_change(before: dict[str, Any], fields: dict[str, Any], update: dict[str, Any]) -> bool:
    """仅对简单字段判断「值相同则跳过」；含 update 增量或 customfield 时不跳过。"""
    if update:
        return False
    for k, v in fields.items():
        if k not in SIMPLE_FIELDS:
            return False
        cur = before.get(k)
        if isinstance(v, dict):
            v = v.get("name")
        if cur != v:
            return False
    return True


def update_issue(jira: Any, key: str, fields: dict[str, Any] | None = None, *,
                 update: dict[str, Any] | None = None, notify: bool = False,
                 skip_if_same: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """更新字段。update 用于增量操作（如 labels add/remove）。

    幂等防呆：skip_if_same=True 且全部为简单字段且值相同 → skipped=true, reason=no_change。
    """
    fields = dict(fields or {})
    update = dict(update or {})
    if not fields and not update:
        arg_fail("没有任何要更新的内容：给便捷参数或 --data")
    payload: dict[str, Any] = {"fields": fields}
    if update:
        payload["update"] = update
    snap_ids = sorted(set(list(fields.keys()) + list(update.keys())))
    before = _snapshot(jira, key, snap_ids)
    if skip_if_same and not dry_run and _no_change(before, fields, update):
        return {"key": key, "url": jb.browse_url(key), "dry_run": False,
                "skipped": True, "reason": "no_change", "payload": payload,
                "before": before, "after": before, "changed": []}
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True,
                "skipped": True, "reason": None, "payload": payload,
                "before": before, "after": None, "changed": sorted(before.keys())}
    try:
        issue = jira.issue(key)
        issue.update(fields=fields, update=update or None, notify=notify)
    except JIRAError as e:
        raise_write_error(e, op="更新 issue")
    after = _snapshot(jira, key, snap_ids)
    changed = sorted({k for k in set(list(before.keys()) + list(after.keys()))
                      if before.get(k) != after.get(k)})
    return {"key": key, "url": jb.browse_url(key), "dry_run": False,
            "skipped": False, "reason": None, "payload": payload,
            "before": before, "after": after, "changed": changed}


def list_transitions(jira: Any, key: str) -> dict[str, Any]:
    """列当前用户可用流转（expand 出流转屏必填字段 —— 现写脚本拿不到的信息）。"""
    current = _status(_g(getattr(jira.issue(key, fields="status"), "fields", None), "status"))
    trans = jira.transitions(key, expand="transitions.fields")
    items: list[dict[str, Any]] = []
    for t in trans if isinstance(trans, list) else []:
        if not isinstance(t, dict):
            continue
        to = t.get("to") or {}
        required: list[dict[str, Any]] = []
        for fid, fr in sorted((t.get("fields") or {}).items()):
            if isinstance(fr, dict) and fr.get("required"):
                required.append({"id": fid, "name": fr.get("name"),
                                 "type": (fr.get("schema") or {}).get("type"),
                                 "allowed_values": _allowed_values(fr.get("allowedValues"))
                                 if fr.get("allowedValues") else None})
        items.append({
            "id": t.get("id"), "name": t.get("name"),
            "to_status": to.get("name"),
            "to_status_category": (to.get("statusCategory") or {}).get("name")
            if isinstance(to.get("statusCategory"), dict) else to.get("statusCategory"),
            "has_screen": bool(t.get("hasScreen")),
            "is_global": bool(t.get("isGlobal")),
            "is_conditional": bool(t.get("isConditional")),
            "required_fields": required,
        })
    return {"key": key, "url": jb.browse_url(key),
            "current_status": current, "transitions": items}


def do_transition(jira: Any, key: str, transition: str, *, fields: dict[str, Any] | None = None,
                  comment: str | None = None, resolution: str | dict | None = None,
                  assignee: str | None = None, expect_status: str | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """状态流转。先校验 id/name 对当前用户可用；expect_status 不符 → 退出码 2（幂等防呆）。"""
    fields = dict(fields or {})
    listing = list_transitions(jira, key)
    before_status = (listing.get("current_status") or {}).get("name")
    avail = listing["transitions"]
    tid = tname = None
    for t in avail:
        if str(t.get("id")) == str(transition) or t.get("name") == transition:
            tid, tname = t.get("id"), t.get("name")
            break
    if tid is None:
        arg_fail(f"流转 {transition!r} 对当前用户不可用。可用："
                 + json.dumps([{"id": t["id"], "name": t["name"]} for t in avail],
                              ensure_ascii=False))
    if expect_status and expect_status.strip() != (before_status or ""):
        arg_fail(f"--expect-status {expect_status.strip()!r} 与当前状态 {before_status!r} "
                 "不符，拒绝执行")
    if resolution is not None:
        fields["resolution"] = resolution if isinstance(resolution, dict) else {"name": resolution}
    if assignee is not None:
        fields["assignee"] = {"name": assignee}
    payload: dict[str, Any] = {"transition": {"id": tid}}
    if fields:
        payload["fields"] = fields
    if comment:
        payload["comment"] = comment
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True, "skipped": True,
                "available_transitions": [{"id": t["id"], "name": t["name"]} for t in avail],
                "transition": {"id": tid, "name": tname},
                "before_status": before_status, "after_status": None, "payload": payload}
    try:
        jira.transition_issue(key, tid, fields=fields or None, comment=comment)
    except JIRAError as e:
        raise_write_error(e, op="状态流转")
    after = _status(_g(getattr(jira.issue(key, fields="status"), "fields", None), "status"))
    return {"key": key, "url": jb.browse_url(key), "dry_run": False, "skipped": False,
            "transition": {"id": tid, "name": tname},
            "before_status": before_status, "after_status": (after or {}).get("name"),
            "payload": payload}


def resolve_assignable(jira: Any, query: str, *, key: str | None = None,
                       project: str | None = None, max_results: int = 20) -> list[dict[str, Any]]:
    """列可指派用户（assign.py --list-assignable 的数据源）。"""
    kw: dict[str, Any] = {"maxResults": max_results}
    if key:
        kw["issueKey"] = key
    if project:
        kw["project"] = project
    if query:
        kw["username"] = query
    users = jira.search_assignable_users_for_issues(**kw)
    return [{"name": _g(u, "name"), "key": _g(u, "key"),
             "displayName": _g(u, "displayName"), "email": _g(u, "emailAddress"),
             "active": _g(u, "active")} for u in users]


def assign_issue(jira: Any, key: str, user: str | None, *,
                 dry_run: bool = False) -> dict[str, Any]:
    """指派（user=None 取消指派）。Server payload 是 {"name": ...}。调用前先 exact_user_check。"""
    before = _user(_g(getattr(jira.issue(key, fields="assignee"), "fields", None), "assignee"))
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True, "skipped": True,
                "assignee_before": before, "assignee_after": None, "resolved_name": user}
    try:
        jira.assign_issue(key, user)
    except JIRAError as e:
        raise_write_error(e, op="指派")
    after = _user(_g(getattr(jira.issue(key, fields="assignee"), "fields", None), "assignee"))
    return {"key": key, "url": jb.browse_url(key), "dry_run": False, "skipped": False,
            "assignee_before": before, "assignee_after": after, "resolved_name": user}


# ============================================================
# 业务函数：附件 / watcher / 项目
# ============================================================
def list_attachments(jira: Any, key: str) -> dict[str, Any]:
    f = getattr(jira.issue(key, fields="attachment"), "fields", None)
    atts = list(_g(f, "attachment", default=[]) or [])
    return {"key": key, "url": jb.browse_url(key), "total": len(atts),
            "attachments": [_attachment(a) for a in atts]}


def add_attachments(jira: Any, key: str, paths: list[str], *,
                    filenames: list[str] | None = None,
                    dry_run: bool = False) -> dict[str, Any]:
    """上传附件。不存在的路径收进 missing；全部缺失时退出码 2。"""
    plan: list[tuple[Path, str, int]] = []
    missing: list[str] = []
    for idx, p in enumerate(paths):
        path = Path(p)
        if not path.exists():
            missing.append(str(path))
            continue
        fname = path.name
        if filenames and len(paths) == 1 and idx < len(filenames):
            fname = filenames[idx]
        plan.append((path, fname, path.stat().st_size))
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True, "skipped": True,
                "would_add": [{"path": str(p), "filename": fn, "size": sz} for p, fn, sz in plan],
                "missing": missing}
    if not plan:
        arg_fail("所有文件都不存在：" + ", ".join(missing))
    added: list[dict[str, Any]] = []
    for p, fn, sz in plan:
        try:
            att = jira.add_attachment(key, str(p), filename=fn)
        except JIRAError as e:
            raise_write_error(e, op=f"上传附件 {p}")
        added.append({"path": str(p), "filename": _g(att, "filename") or fn,
                      "size": _g(att, "size") or sz, "attachment_id": _g(att, "id")})
    return {"key": key, "url": jb.browse_url(key), "dry_run": False, "skipped": False,
            "added": added, "missing": missing}


def download_attachment(jira: Any, *, attachment_id: str | None = None, key: str | None = None,
                        name: str | None = None, dest: str | None = None,
                        force: bool = False) -> dict[str, Any]:
    """下载附件到本地。两种定位：--id，或 --key + --name（按文件名精确匹配，需恰好 1 个）。"""
    aid: str | None = None
    if attachment_id:
        att = jira.attachment(attachment_id)
        aid = attachment_id
    elif key and name:
        meta = list_attachments(jira, key)
        hits = [a for a in meta["attachments"] if a.get("filename") == name]
        if len(hits) != 1:
            arg_fail(f"按文件名 {name!r} 匹配到 {len(hits)} 个附件（需恰好 1 个）。现有："
                     + json.dumps([a.get("filename") for a in meta["attachments"]],
                                  ensure_ascii=False))
        aid = hits[0]["id"]
        att = jira.attachment(aid)
    else:
        arg_fail("download 需要 --id <attachment_id>，或 --key <issue> --name <文件名>")
    data = att.get()  # bytes
    fname = _g(att, "filename") or "attachment.bin"
    dest_path = Path(dest) if dest else Path.cwd()
    if dest_path.suffix and not dest_path.is_dir():
        target = dest_path
    else:
        target = dest_path / fname
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        arg_fail(f"目标文件已存在：{target}（加 --force 覆盖）")
    target.write_bytes(data)
    return {"attachment_id": aid or _g(att, "id"), "filename": fname, "size": len(data),
            "jira_size": _g(att, "size"), "path": str(target),
            "content_url": _g(att, "content")}


def list_watchers(jira: Any, key: str) -> dict[str, Any]:
    raw = getattr(jira.watchers(key), "raw", {}) or {}
    return {"key": key, "url": jb.browse_url(key), "watch_count": raw.get("watchCount"),
            "watchers": [_user(u) for u in (raw.get("watchers") or [])]}


def modify_watchers(jira: Any, key: str, *, add: list[str] | None = None,
                    remove: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """加/移除 watcher。add 前做精确用户校验；单用户失败记入 failed 不中断整批。"""
    add = list(add or [])
    remove = list(remove or [])
    if not add and not remove:
        arg_fail("没有要 add/remove 的用户（--user）")
    if dry_run:
        return {"key": key, "url": jb.browse_url(key), "dry_run": True, "skipped": True,
                "add": add, "remove": remove}
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for u in add:
        exact_user_check(jira, u, key=key)
        try:
            jira.add_watcher(key, u)
            added.append({"name": u})
        except JIRAError as e:
            failed.append({"name": u, "op": "add",
                           "why": str(getattr(e, "text", e))[:200]})
    for u in remove:
        try:
            jira.remove_watcher(key, u)
            removed.append({"name": u})
        except JIRAError as e:
            failed.append({"name": u, "op": "remove",
                           "why": str(getattr(e, "text", e))[:200]})
    return {"key": key, "url": jb.browse_url(key), "dry_run": False, "skipped": False,
            "added": added, "removed": removed, "failed": failed}


def list_projects(jira: Any, *, text: str | None = None, limit: int = 500) -> dict[str, Any]:
    """列全部可见项目（无服务端分页，一次全量 + 本地过滤/截断）。"""
    ps = list(jira.projects())
    items = [_project_brief(p) for p in ps]
    if text:
        t = text.lower()
        items = [p for p in items
                 if t in (p.get("key") or "").lower() or t in (p.get("name") or "").lower()]
    return {"total": len(ps), "filtered": len(items), "filter": text,
            "truncated": len(items) > limit, "projects": items[:limit]}


def get_project(jira: Any, key: str, *, include_components: bool = False,
                include_versions: bool = False) -> dict[str, Any]:
    p = jira.project(key)
    data = _project_brief(p)
    data["description"] = _g(p, "description")
    if include_components:
        data["components"] = [{"id": _g(c, "id"), "name": _g(c, "name"),
                               "description": _g(c, "description")}
                              for c in jira.project_components(key)]
    if include_versions:
        data["versions"] = [{"id": _g(v, "id"), "name": _g(v, "name"),
                             "released": _g(v, "released"), "archived": _g(v, "archived")}
                            for v in jira.project_versions(key)]
    return data
