# -*- coding: utf-8 -*-
"""示例/模板：JQL 搜索 → JSON 输出（只读）。

用法（绝对路径 + 中性 cwd，别 cd 到 D:/LZY_project/jira）：
    D:/LZY_project/jira/.venv/Scripts/python.exe example_search.py

复制本文件改 BEG/END 之间即可；jira_bootstrap 负责连库/编码/输出/异常。
"""
import sys

sys.path.insert(0, r"C:\Users\user\.claude\skills\jira-tool\scripts")
import jira_bootstrap as jb  # noqa: E402


@jb.guard
def main():
    jira = jb.connect()
    # >>> BEG：按需改写 <<<
    jql = "project=AERDM order by created desc"
    issues = jira.search_issues(jql, maxResults=50)
    rows = [
        {
            "key": i.key,
            "summary": i.fields.summary,
            "status": getattr(i.fields.status, "name", None),
            "type": getattr(i.fields.issuetype, "name", None),
            "assignee": getattr(i.fields.assignee, "name", None),
            "url": jb.browse_url(i.key),
        }
        for i in issues
    ]
    # <<< END：按需改写 >>>
    jb.out({"jql": jql, "count": len(rows), "issues": rows})


if __name__ == "__main__":
    raise SystemExit(main())
