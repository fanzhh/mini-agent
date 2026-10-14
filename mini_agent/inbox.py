"""mini_agent.inbox：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from .model import chat_completion
from .router import relative_date_range
from .trace import trace
from datetime import datetime
import json
import re

def resolve_inbox_path():
    """返回 Inbox.md Path；优先 MINI_AGENT_INBOX，否则 KB 候选路径。"""
    if config.INBOX_PATH and config.INBOX_PATH.is_file():
        return config.INBOX_PATH
    if config.KB and config.KB.is_dir():
        for rel in config.INBOX_CANDIDATES:
            p = config.KB / rel
            if p.is_file():
                return p.resolve()
    return None


def inbox_today():
    if config.INBOX_TODAY and re.fullmatch(r"\d{4}-\d{2}-\d{2}", config.INBOX_TODAY):
        return config.INBOX_TODAY
    return datetime.now().strftime("%Y-%m-%d")


def parse_inbox_line(line, line_no, today):
    """解析单行任务；非任务行返回 None。"""
    m = config._TASK_LINE_RX.match(line.strip())
    if not m:
        return None
    mark, body = m.group(2), m.group(3)
    is_completed = mark in ("x", "X")
    done_m = re.search(r"✅\s*(\d{4}-\d{2}-\d{2})", body)
    plan_m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", body)
    done_date = done_m.group(1) if done_m else None
    plan_date = plan_m.group(1) if plan_m else None
    tags = config._TAG_RX.findall(body)
    tags_l = [t.lower() for t in tags]
    is_work = any(t in config._WORK_TAGS for t in tags_l)
    is_overdue = False
    days_overdue = None
    if not is_completed and plan_date and plan_date < today:
        is_overdue = True
        d0 = datetime.strptime(today, "%Y-%m-%d")
        d1 = datetime.strptime(plan_date, "%Y-%m-%d")
        days_overdue = (d0 - d1).days
    title = body
    title = re.sub(r"✅\s*\d{4}-\d{2}-\d{2}", "", title)
    title = re.sub(r"📅\s*\d{4}-\d{2}-\d{2}", "", title)
    title = re.sub(r"📆\s*\S+", "", title)
    title = config._TAG_RX.sub("", title)
    title = re.sub(r"[🔼⏫🔽]", "", title).strip()
    return {
        "line": line_no,
        "status": "completed" if is_completed else "pending",
        "title": title,
        "tags": tags,
        "planDate": plan_date,
        "doneDate": done_date,
        "isWork": is_work,
        "isOverdue": is_overdue,
        "daysOverdue": days_overdue,
    }


def parse_inbox_filters(question):
    """从问题确定性解析过滤选项。"""
    q = (question or "").strip()
    opts = {
        "status": "all",
        "overdue_only": False,
        "work_only": None,
        "personal_only": False,
        "keyword": None,
        "start_date": None,
        "end_date": None,
    }
    if re.search(r"超期|逾期|过期", q):
        opts["overdue_only"] = True
        opts["status"] = "pending"
    elif re.search(r"未完成|待办|pending", q, re.I):
        opts["status"] = "pending"
    elif re.search(r"已完成|completed", q, re.I):
        opts["status"] = "completed"
    if re.search(r"个人|生活|非工作", q):
        opts["personal_only"] = True
    elif re.search(r"收件箱|inbox", q, re.I) and re.search(r"工作", q) and not re.search(
            r"工作事项|工作汇总|工作摘要|月度工作", q):
        opts["work_only"] = True
    m = re.search(
        r"(\d{4}-\d{2}-\d{2})\s*(?:到|至|~|—|–|-|～)\s*(\d{4}-\d{2}-\d{2})", q)
    if m:
        opts["start_date"], opts["end_date"] = m.group(1), m.group(2)
    else:
        rel = relative_date_range(q)
        if rel:
            opts["start_date"], opts["end_date"] = rel
    # 关键词：优先「关于X的」；否则少量显式词
    m = re.search(r"关于\s*([^\s的]{2,40}?)\s*的", q)
    if m:
        opts["keyword"] = m.group(1).strip()
    else:
        for kw in ("学习", "买票", "培训", "阅读", "GMP", "Rust"):
            if kw.lower() in q.lower() or kw in q:
                opts["keyword"] = kw
                break
    return opts


def query_inbox_tasks(path, options=None):
    """解析并过滤 Inbox.md；对齐 extra-pi queryInboxTasks。"""
    options = options or {}
    today = options.get("today") or inbox_today()
    text = path.read_text(encoding="utf-8", errors="replace")
    status_filter = options.get("status") or "all"
    work_only = options.get("work_only")
    personal_only = options.get("personal_only")
    keyword = (options.get("keyword") or "").strip().lower() or None
    overdue_only = bool(options.get("overdue_only"))
    start_date = options.get("start_date")
    end_date = options.get("end_date")
    items = []
    for idx, line in enumerate(text.splitlines(), 1):
        item = parse_inbox_line(line, idx, today)
        if not item:
            continue
        if personal_only and item["isWork"]:
            continue
        if work_only is True and not item["isWork"]:
            continue
        if work_only is False and item["isWork"] and not personal_only:
            continue
        if status_filter == "completed" and item["status"] != "completed":
            continue
        if status_filter == "pending" and item["status"] != "pending":
            continue
        if overdue_only and not item["isOverdue"]:
            continue
        if start_date or end_date:
            plan, done = item.get("planDate"), item.get("doneDate")
            ok = False
            for d in (plan, done):
                if not d:
                    continue
                if start_date and d < start_date:
                    continue
                if end_date and d > end_date:
                    continue
                ok = True
                break
            if not ok:
                continue
        if keyword:
            blob = (item["title"] + " " + " ".join(item["tags"])).lower()
            if keyword.lower() not in blob:
                continue
        items.append(item)
    summary = {
        "completedCount": sum(1 for i in items if i["status"] == "completed"),
        "pendingCount": sum(1 for i in items if i["status"] == "pending"),
        "workCount": sum(1 for i in items if i["isWork"]),
        "personalCount": sum(1 for i in items if not i["isWork"]),
        "overdueCount": sum(1 for i in items if i["isOverdue"]),
    }
    return {
        "file": path.name,
        "totalFound": len(items),
        "filters": {
            "status": status_filter,
            "overdue_only": overdue_only,
            "work_only": work_only,
            "personal_only": personal_only,
            "keyword": keyword,
            "start_date": start_date,
            "end_date": end_date,
        },
        "today": today,
        "summary": summary,
        "items": items[:20],
        "note": "完成以 ✅ 日期为准；排期以 📅 为准；未完成且排期早于今天为超期",
    }


def format_inbox_summary_text(result):
    if not result or result.get("totalFound", 0) == 0:
        return "查不到：没有符合条件的 Inbox 事项。"
    s = result["summary"]
    lines = [
        f"Inbox（{result['file']}）共 {result['totalFound']} 条"
        f"（未完成 {s['pendingCount']}，已完成 {s['completedCount']}，"
        f"超期 {s['overdueCount']}，工作 {s['workCount']}，个人 {s['personalCount']}）",
        f"今天：{result['today']}；{result['note']}",
        "事项：",
    ]
    for it in result.get("items", [])[:12]:
        flag = "超期" if it.get("isOverdue") else it["status"]
        plan = it.get("planDate") or "-"
        od = f" +{it['daysOverdue']}d" if it.get("daysOverdue") is not None else ""
        lines.append(f"- L{it['line']} [{flag}] {it['title']} 📅{plan}{od}")
    return "\n".join(lines)


def run_inbox(question, quiet):
    """inbox 意图确定性流程。"""
    tools_used = ["inbox_query"]
    path = resolve_inbox_path()
    if not path:
        answer = "查不到：未配置 Inbox（请设置 MINI_AGENT_INBOX 或 MINI_AGENT_KB）。"
        trace("inbox_disabled", quiet, intent="inbox")
        trace("final", quiet, intent="inbox", tools=tools_used, answer=answer)
        return {"intent": "inbox", "risk": "none", "tools_used": tools_used, "answer": answer}
    opts = parse_inbox_filters(question)
    opts["today"] = inbox_today()
    try:
        result = query_inbox_tasks(path, opts)
    except Exception as exc:
        answer = f"查不到：Inbox 读取失败（{type(exc).__name__}）。"
        trace("inbox_error", quiet, intent="inbox", error=type(exc).__name__)
        trace("final", quiet, intent="inbox", tools=tools_used, answer=answer)
        return {"intent": "inbox", "risk": "none", "tools_used": tools_used, "answer": answer}
    payload = json.dumps(result, ensure_ascii=False)
    trace("tool_result", quiet, step=1, tool="inbox_query", result=payload[:1200])
    if result.get("totalFound", 0) == 0:
        answer = "查不到：没有符合条件的 Inbox 事项。"
        trace("final", quiet, intent="inbox", tools=tools_used, answer=answer)
        return {"intent": "inbox", "risk": "none", "tools_used": tools_used, "answer": answer}
    sys_prompt = ("你是 Inbox 待办助手。只根据给出的统计摘要用简洁中文复述；"
                  "不要编造未出现的事项或数字；totalFound=0 则答查不到；"
                  "说明超期口径（未完成且排期早于今天）。")
    user_msg = f"摘要 JSON：\n{payload}\n\n用户问题：{question}\n请只用摘要回答，简洁中文。"
    content = chat_completion(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_msg}],
        json_mode=False, max_tokens=max(config.MAX_TOKENS, 500),
        timeout=max(config.TIMEOUT, 60))
    answer = (content or "").strip()
    ground = payload + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or ("查不到" in answer and len(answer) < 40):
        answer = format_inbox_summary_text(result)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_inbox_summary_text(result)
    trace("final", quiet, intent="inbox", tools=tools_used, answer=answer[:500])
    return {"intent": "inbox", "risk": "none", "tools_used": tools_used, "answer": answer}
