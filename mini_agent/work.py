"""mini_agent.work：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
from .model import chat_completion
from .router import relative_date_range
from .trace import trace
from datetime import datetime
import json
import re
import urllib.request

def parse_work_range(question):
    """从问题确定性解析 (start_date, end_date)；失败返回 None。"""
    q = (question or "").strip()
    # System One / UNDERSTANDING hint
    if state.UNDERSTANDING and isinstance(state.UNDERSTANDING.get("range"), dict):
        r = state.UNDERSTANDING["range"]
        if r.get("start") and r.get("end"):
            return r["start"], r["end"]
    rel = relative_date_range(q)
    if rel:
        return rel
    # 显式区间
    m = re.search(
        r"(\d{4}-\d{2}-\d{2})\s*(?:到|至|~|—|–|-|～)\s*(\d{4}-\d{2}-\d{2})", q)
    if m:
        a, b = m.group(1), m.group(2)
        if a <= b:
            return a, b
        return None
    # YYYY年M月
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", q)
    if m:
        return _month_bounds(int(m.group(1)), int(m.group(2)))
    # YYYY-MM or YYYY/M
    m = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})(?!\d)", q)
    if m and "年" not in q[max(0, m.start()-2):m.end()+2]:
        # avoid matching as part of full date already handled
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            # if followed by -DD it's a full date, skip month-only
            after = q[m.end():m.end()+3]
            if not re.match(r"-\d{2}", after):
                return _month_bounds(y, mo)
    now = datetime.now()
    if re.search(r"上月|上个月", q):
        y, mo = now.year, now.month - 1
        if mo == 0:
            y, mo = y - 1, 12
        return _month_bounds(y, mo)
    if re.search(r"本月|这个月|当月", q):
        return _month_bounds(now.year, now.month)
    return None


def _month_bounds(year, month):
    if not (2000 <= year <= 2100 and 1 <= month <= 12):
        return None
    import calendar
    last = calendar.monthrange(year, month)[1]
    prefix = f"{year}-{month:02d}"
    return f"{prefix}-01", f"{prefix}-{last:02d}"


def summarize_work_rows(rows, start_date, end_date, complete=True):
    """纯函数：按 id 去重后生成与 extra-pi 对齐的 summary。rows 已过滤到区间内。"""
    seen, uniq = set(), []
    for row in rows:
        if row.get("id") is None:
            raise ValueError("事项缺少ID，不能去重")
        key = str(row["id"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(row)
    by_type, by_assignee = {}, {}
    for row in uniq:
        t = str(row.get("task_type") or "unknown")
        a = str(row.get("assignee") or "unknown")
        by_type[t] = by_type.get(t, 0) + 1
        by_assignee[a] = by_assignee.get(a, 0) + 1
    examples = []
    for row in uniq[:8]:
        examples.append({
            "id": row.get("id"),
            "task_name": row.get("task_name") or row.get("title") or "",
            "task_type": row.get("task_type"),
            "assignee": row.get("assignee"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
        })
    return {
        "start_date": start_date,
        "end_date": end_date,
        "scope": f"按事项开始日期筛选（{start_date} 至 {end_date}）；包含所有接口可见负责人",
        "complete": bool(complete),
        "fetchedCount": len(uniq),
        "byType": by_type,
        "byAssignee": by_assignee,
        "examples": examples,
        "note": ("以分页结束及唯一ID计数，不使用接口total作为总数；事项不等于已完成工作"
                 if complete else
                 "达到2000条上限，仅为部分结果，不能声称总数"),
    }


def _work_filter_rows(rows, start_date, end_date):
    out = []
    for row in rows:
        if row.get("id") is None:
            raise ValueError("事项缺少ID，不能去重")
        rd = str(row.get("start_date") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd):
            raise ValueError("事项日期无效")
        if rd < start_date or rd > end_date:
            continue
        out.append(row)
    return out


def _work_load_fixture():
    raw = json.loads(config.WORK_FIXTURE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("tasks") or raw.get("data") or []
    if not isinstance(raw, list):
        raise ValueError("fixture 格式无效")
    return raw


def _work_http_page(start_date, end_date, limit, offset):
    """调用 WORK_URL：优先 MCP JSON-RPC tools/call db_query_tasks，其次 REST POST。"""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if config.WORK_AUTH:
        headers["Authorization"] = config.WORK_AUTH
    # MCP style
    mcp_body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "db_query_tasks",
                   "arguments": {"start_date": start_date, "end_date": end_date,
                                 "limit": limit, "offset": offset}},
    }
    req = urllib.request.Request(config.WORK_URL, data=json.dumps(mcp_body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.WORK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception:
        # REST fallback
        rest = {"start_date": start_date, "end_date": end_date,
                "limit": limit, "offset": offset}
        req2 = urllib.request.Request(config.WORK_URL, data=json.dumps(rest).encode("utf-8"),
                                      headers=headers, method="POST")
        with urllib.request.urlopen(req2, timeout=config.WORK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    text = raw
    if "data:" in raw:
        parts = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if parts:
            text = "\n".join(parts)
    obj = json.loads(text if text.strip().startswith("{") else text.split("\n", 1)[0])
    if "result" in obj and isinstance(obj["result"], dict):
        content = obj["result"].get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    obj = json.loads(block.get("text") or "null")
                    break
    if isinstance(obj, dict) and "content" in obj and "success" not in obj:
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    obj = json.loads(block.get("text") or "null")
                    break
    if not isinstance(obj, dict) or not obj.get("success") or not isinstance(obj.get("data"), list):
        raise ValueError("分页响应格式异常")
    return obj["data"]


def query_work_range(start_date, end_date):
    """分页拉取、校验区间、去重统计。对齐 extra-pi queryWorkRange。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start_date or "") or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", end_date or ""):
        raise ValueError("日期格式无效，必须为 YYYY-MM-DD")
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    rows, complete = [], False
    seen = set()
    if config.WORK_FIXTURE and config.WORK_FIXTURE.is_file():
        filtered = _work_filter_rows(_work_load_fixture(), start_date, end_date)
        # simulate paging
        for offset in range(0, 2000, 100):
            page = filtered[offset:offset + 100]
            for row in page:
                key = str(row["id"])
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if len(page) < 100:
                complete = True
                break
        else:
            complete = False
        return summarize_work_rows(rows, start_date, end_date, complete)
    if not config.WORK_URL:
        raise RuntimeError("未配置工作数据源")
    for offset in range(0, 2000, 100):
        page = _work_http_page(start_date, end_date, 100, offset)
        added = 0
        for row in page:
            rd = str(row.get("start_date") or "")[:10]
            if rd < start_date or rd > end_date:
                raise ValueError("接口返回了指定范围外的事项，不能统计")
            if row.get("id") is None:
                raise ValueError("事项缺少ID，不能去重")
            key = str(row["id"])
            if key not in seen:
                seen.add(key)
                rows.append(row)
                added += 1
        if len(page) < 100:
            complete = True
            break
        if not added:
            raise ValueError("分页返回重复数据，无法确认完整性")
    else:
        complete = False
    return summarize_work_rows(rows, start_date, end_date, complete)


def query_work_month(year, month):
    bounds = _month_bounds(year, month)
    if not bounds:
        raise ValueError("月份无效")
    return query_work_range(bounds[0], bounds[1])


def format_work_summary_text(summary):
    """模型不可用或校验失败时的规则模板。"""
    if not summary or summary.get("fetchedCount", 0) == 0:
        return "查不到：该时间范围内没有工作事项。"
    lines = [
        f"工作事项汇总（{summary['start_date']} 至 {summary['end_date']}）",
        f"口径：{summary['scope']}",
        f"计数：{summary['fetchedCount']}（complete={summary['complete']}）",
        "按类型：" + "、".join(f"{k} {v}" for k, v in summary.get("byType", {}).items()),
        "按负责人：" + "、".join(f"{k} {v}" for k, v in summary.get("byAssignee", {}).items()),
        "代表事项：",
    ]
    for ex in summary.get("examples", [])[:8]:
        lines.append(
            f"- [{ex.get('id')}] {ex.get('task_name')} | {ex.get('task_type')} | "
            f"{ex.get('assignee')} | {str(ex.get('start_date') or '')[:10]}"
        )
    lines.append(f"备注：{summary.get('note', '')}")
    return "\n".join(lines)


def run_work(question, quiet):
    """work 意图确定性流程：解析日期 → work_summary → 模型仅据摘要措辞。"""
    tools_used = ["work_summary"]
    if not ((config.WORK_FIXTURE and config.WORK_FIXTURE.is_file()) or config.WORK_URL):
        answer = "查不到：未配置工作数据源（请设置 MINI_AGENT_WORK_FIXTURE 或 MINI_AGENT_WORK_URL）。"
        trace("work_disabled", quiet, intent="work")
        trace("final", quiet, intent="work", tools=tools_used, answer=answer)
        return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}
    bounds = parse_work_range(question)
    if not bounds:
        answer = "查不到：请说明月份（如 2026年9月）或日期区间（YYYY-MM-DD 至 YYYY-MM-DD）。"
        trace("work_parse_fail", quiet, intent="work")
        trace("final", quiet, intent="work", tools=tools_used, answer=answer)
        return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}
    try:
        summary = query_work_range(bounds[0], bounds[1])
    except Exception as exc:
        answer = f"查不到：工作数据查询失败（{type(exc).__name__}）。"
        trace("work_error", quiet, intent="work", error=type(exc).__name__)
        trace("final", quiet, intent="work", tools=tools_used, answer=answer)
        return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}
    summary_json = json.dumps(summary, ensure_ascii=False)
    trace("tool_result", quiet, step=1, tool="work_summary", result=summary_json[:1200])
    if summary.get("fetchedCount", 0) == 0:
        answer = "查不到：该时间范围内没有工作事项。"
        trace("final", quiet, intent="work", tools=tools_used, answer=answer)
        return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}
    sys_prompt = ("你是工作事项汇总助手。只根据给出的统计摘要用简洁中文复述；"
                  "不要编造未出现的数字或人名；fetchedCount=0 则答查不到；"
                  "必须说明按开始日期统计，事项不等于已完成工作。")
    user_msg = (f"统计摘要 JSON：\n{summary_json}\n\n用户问题：{question}\n"
                "请只用摘要回答，简洁中文。")
    content = chat_completion(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_msg}],
        json_mode=False, max_tokens=max(config.MAX_TOKENS, 500),
        timeout=max(config.TIMEOUT, config.WORK_TIMEOUT))
    answer = (content or "").strip()
    ground = summary_json + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or "查不到" in answer and len(answer) < 40:
        answer = format_work_summary_text(summary)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_work_summary_text(summary)
    trace("final", quiet, intent="work", tools=tools_used, answer=answer[:500])
    return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}
