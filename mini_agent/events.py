"""mini_agent.events：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
from .router import relative_date_range
from .trace import trace
from datetime import datetime
from pathlib import Path
import json
import re
import urllib.request

def _today_ymd():
    if config.TODAY_OVERRIDE and re.fullmatch(r"\d{4}-\d{2}-\d{2}", config.TODAY_OVERRIDE):
        return config.TODAY_OVERRIDE
    return datetime.now().strftime("%Y-%m-%d")


def _day_bounds_iso(ymd):
    """与 six 工作事项一致的全天区间：T00:00:00+08:00 .. T23:59:00+08:00。"""
    return f"{ymd}T00:00:00+08:00", f"{ymd}T23:59:00+08:00"


def parse_add_event(question):
    """从问题提取 (date_ymd, title)；失败返回 (None, None)。确定性，不靠模型。"""
    q = (question or "").strip()
    if not q:
        return None, None
    # date
    ymd = None
    if state.UNDERSTANDING and state.UNDERSTANDING.get("relative") in (
            "today", "yesterday", "tomorrow"):
        bounds = relative_date_range(state.UNDERSTANDING["relative"])
        if bounds:
            ymd = bounds[0]
    if ymd is None:
        if re.search(r"(?i)今日|今天|\btoday\b", q):
            ymd = _today_ymd()
        elif re.search(r"(?i)昨日|昨天|\byesterday\b", q):
            b = relative_date_range("yesterday")
            ymd = b[0] if b else None
        elif re.search(r"(?i)明日|明天|\btomorrow\b", q):
            b = relative_date_range("tomorrow")
            ymd = b[0] if b else None
        else:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", q)
            if m:
                ymd = m.group(1)
            else:
                ymd = _today_ymd()  # 默认今日
    # title: after 事件/任务[-：:] or quoted or last dash segment
    title = None
    m = re.search(
        r"(?:事件|工作事项|任务)\s*[-—–:：]\s*(.+)$", q)
    if m:
        title = m.group(1).strip()
    if not title:
        m = re.search(r"[「「\"'](.+?)[」」\"']", q)
        if m:
            title = m.group(1).strip()
    if not title:
        m = re.search(
            r"(?:增加|添加|新建|创建|写入)(?:一个)?(?:今日|今天|昨日|昨天|明日|明天)?"
            r"(?:的)?(?:事件|工作事项|任务)\s*[-—–:：]?\s*(.+)$", q)
        if m:
            title = m.group(1).strip().lstrip("-—–:： ").strip()
    if not title:
        # trailing after last separator
        m = re.search(r"[-—–:：]\s*([^-—–:：]{2,})$", q)
        if m:
            title = m.group(1).strip()
    if title:
        title = re.sub(r"\s+", " ", title).strip(" 。.;；")
    if not title or len(title) < 2:
        return ymd, None
    return ymd, title


def build_create_task_args(date_ymd, title):
    """构造 db_create_task 参数（与 query_work_/db_query_tasks 同一事项库）。"""
    start, end = _day_bounds_iso(date_ymd)
    tt = config.ADD_EVENT_TASK_TYPE if config.ADD_EVENT_TASK_TYPE in config._ADD_EVENT_TASK_TYPES else "other"
    args = {
        "title": title,
        "task_type": tt,
        "start": start,
        "end": end,
        "all_day": True,
        "create_by": config.ADD_EVENT_CREATE_BY,
    }
    if config.WORK_PERSON:
        args["person"] = config.WORK_PERSON
    return args


def format_add_event_preview(args):
    body = json.dumps(args, ensure_ascii=False, indent=2)
    return (
        "将调用 six-unified MCP 工具 db_create_task（仅创建，不更新/删除），字段如下：\n"
        f"{body}\n"
        "未确认，未写入。交互输入 y 确认；非交互请加 --yes。"
    )


def _mcp_call_create_task(args):
    """调用 tools/call db_create_task；假后端写 CREATE_SINK。返回 (ok, message)。"""
    if config.WORK_CREATE_SINK:
        sink = Path(config.WORK_CREATE_SINK).expanduser()
        sink.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"),
               "tool": "db_create_task", "arguments": args}
        with open(sink, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True, {"success": True, "data": {"id": "fake-" + str(abs(hash(args["title"])) % 10**8),
                                               "title": args["title"]}, "sink": True}
    if not config.WORK_URL:
        return False, "未配置工作数据源（请设置 MINI_AGENT_WORK_URL 或 MINI_AGENT_WORK_CREATE_SINK）。"
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if config.WORK_AUTH:
        headers["Authorization"] = config.WORK_AUTH
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "db_create_task", "arguments": args}}
    req = urllib.request.Request(config.WORK_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.WORK_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    text = raw
    if "data:" in raw:
        parts = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if parts:
            text = "\n".join(parts)
    obj = json.loads(text if text.strip().startswith("{") else text.split("\n", 1)[0])
    if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
        obj = obj["result"]
    if isinstance(obj, dict) and "content" in obj:
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        obj = json.loads(block.get("text") or "null")
                    except Exception:
                        pass
                    break
    if isinstance(obj, dict) and obj.get("success"):
        return True, obj
    return False, obj if obj is not None else "创建失败"


def run_add_event(question, quiet, yes=False, confirm_fn=None):
    """add_event：解析 → 预览 → 显式确认后仅调用 db_create_task。"""
    tools_used = []
    date_ymd, title = parse_add_event(question)
    if not title:
        answer = "查不到：请说明要创建的事件标题（如：增加一个今日的事件-标题）。"
        trace("final", quiet, intent="add_event", tools=tools_used, answer=answer)
        return {"intent": "add_event", "risk": "none", "tools_used": tools_used,
                "answer": answer, "preview": None}
    if not date_ymd:
        date_ymd = _today_ymd()
    args = build_create_task_args(date_ymd, title)
    preview = format_add_event_preview(args)
    confirmed = bool(yes)
    if not confirmed and confirm_fn is not None:
        print(preview, flush=True)
        try:
            ans = (confirm_fn("确认写入 db_create_task？[y/N] ") or "").strip().lower()
        except EOFError:
            ans = ""
        confirmed = ans in ("y", "yes")
    if not confirmed:
        answer = preview
        trace("add_event_preview", quiet, intent="add_event", args=args, confirmed=False)
        trace("final", quiet, intent="add_event", tools=tools_used, answer=answer[:500])
        return {"intent": "add_event", "risk": "none", "tools_used": tools_used,
                "answer": answer, "preview": args}
    try:
        ok, result = _mcp_call_create_task(args)
    except Exception as exc:
        answer = f"创建失败：{type(exc).__name__}"
        trace("add_event_error", quiet, error=type(exc).__name__)
        trace("final", quiet, intent="add_event", tools=["db_create_task"], answer=answer)
        return {"intent": "add_event", "risk": "none", "tools_used": ["db_create_task"],
                "answer": answer, "preview": args}
    tools_used = ["db_create_task"]
    if ok:
        rid = ""
        if isinstance(result, dict):
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            rid = str((data or {}).get("id") or "")
        answer = f"已创建工作事项：{args['title']}（{args['start'][:10]}"
        if rid:
            answer += f"，id={rid}"
        answer += "）。"
        if isinstance(result, dict) and result.get("sink"):
            answer += "（假后端已记录）"
    else:
        answer = f"创建失败：{result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)[:200]}"
    trace("tool_result", quiet, step=1, tool="db_create_task",
          result=json.dumps(result, ensure_ascii=False)[:800] if not isinstance(result, str) else result[:800])
    trace("final", quiet, intent="add_event", tools=tools_used, answer=answer[:500])
    return {"intent": "add_event", "risk": "none", "tools_used": tools_used,
            "answer": answer, "preview": args}
