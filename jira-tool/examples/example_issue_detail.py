# -*- coding: utf-8 -*-
"""示例/模板：取单条 issue + 评论 → JSON 输出（只读）。

用法（绝对路径 + 中性 cwd）：
    D:/LZY_project/jira/.venv/Scripts/python.exe example_issue_detail.py AERDM-123

jira_bootstrap 负责连库/编码/输出/异常。复制本文件改 BEG/END 之间即可。
"""
import sys

sys.path.insert(0, r"C:\Users\user\.claude\skills\jira-tool\scripts")
import jira_bootstrap as jb  # noqa: E402


@jb.guard
def main():
    jira = jb.connect()
    key = sys.argv[1] if len(sys.argv) > 1 else "AERDM-1"
    # >>> BEG：按需改写 <<<
    issue = jira.issue(key, fields="summary,status,description,comment,assignee,issuetype")
    f = issue.fields
    comments = [
        {
            "author": getattr(c.author, "name", None),
            "created": c.created,
            "body": getattr(c, "body", None),
        }
        for c in getattr(getattr(f, "comment", None), "comments", []) or []
    ]
    data = {
        "key": issue.key,
        "summary": getattr(f, "summary", None),
        "type": getattr(getattr(f, "issuetype", None), "name", None),
        "status": getattr(getattr(f, "status", None), "name", None),
        "assignee": getattr(getattr(f, "assignee", None), "name", None),
        "description": getattr(f, "description", None),
        "comment_count": len(comments),
        "comments": comments,
        "url": jb.browse_url(issue.key),
    }
    # <<< END：按需改写 >>>
    jb.out(data)


if __name__ == "__main__":
    raise SystemExit(main())
