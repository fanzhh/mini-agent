"""mini_agent.defects：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
from .model import chat_completion
from .trace import trace
import json
import re
import urllib.request

def _defects_clean_company(name):
    """去掉开头的查询动词，保留实体名；过短则视为未提取到。"""
    for v in config._DEFECTS_VERBS:
        if name.startswith(v):
            name = name[len(v):]
            break
    return name if len(name) >= 3 else None


def map_defects_level(text):
    """中文级别 → serious/major/minor；无数值级别返回 None。"""
    for zh, en in config.DEFECTS_LEVEL_MAP:
        if zh in (text or ""):
            return en
    return None


def parse_defects_filters(question):
    """确定性解析企业名/级别/关键词/日期区间；保守提取，宁缺毋滥。"""
    _s1 = state.UNDERSTANDING if isinstance(state.UNDERSTANDING, dict) else None
    q = (question or "").strip()
    opts = {"company_name": None, "level": None, "content_keywords": None,
            "law": None, "start_date": None, "end_date": None}
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:到|至|~|—|–|-|～)\s*(\d{4}-\d{2}-\d{2})", q)
    if m and m.group(1) <= m.group(2):
        opts["start_date"], opts["end_date"] = m.group(1), m.group(2)
    opts["level"] = map_defects_level(q)
    m = re.search(r"关于\s*([^\s的]{2,40}?)\s*的", q)
    if m:
        name = m.group(1)
        if re.search(r"公司|药业|企业|厂|制药", name):
            opts["company_name"] = name
        elif opts["content_keywords"] is None:
            opts["content_keywords"] = name
    if opts["company_name"] is None:
        m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,30}?(?:公司|药业|制药|集团|厂))", q)
        if m:
            name = m.group(1)
            # 紧邻枚举后缀（示例药业甲/乙）并入企业名，只吃 1 个字符
            nxt = q[m.end()] if m.end() < len(q) else ""
            if config._DEFECTS_NAME_SUFFIX_RX.fullmatch(nxt):
                name += nxt
            opts["company_name"] = _defects_clean_company(name)
    for kw in ("清洁验证", "偏差", "储存", "温控", "检验", "培训", "设备"):
        if kw in q:
            opts["content_keywords"] = opts["content_keywords"] or kw
            break
    if _s1:
        ent = _s1.get("entities") or {}
        if ent.get("company") and not opts.get("company_name"):
            opts["company_name"] = ent["company"]
        if ent.get("level") and not opts.get("level"):
            opts["level"] = ent["level"]
        if ent.get("keywords") and not opts.get("content_keywords"):
            opts["content_keywords"] = " ".join(ent["keywords"][:4])
        rng = _s1.get("range") or {}
        if rng.get("start") and rng.get("end") and not (opts.get("start_date") and opts.get("end_date")):
            opts["start_date"], opts["end_date"] = rng["start"], rng["end"]
    return opts


def dedupe_defect_rows(rows):
    """纯函数：按 id 去重，保持出现顺序；缺 id 抛错。"""
    seen, uniq = set(), []
    for row in rows:
        if row.get("id") is None:
            raise ValueError("缺陷记录缺少ID，不能去重")
        key = str(row["id"])
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    return uniq


def summarize_defects(rows, filters, complete=True):
    """纯函数：分页/去重后生成给模型的摘要（examples 截断，不含密钥）。"""
    uniq = dedupe_defect_rows(rows)
    by_level = {"serious": 0, "major": 0, "minor": 0}
    for row in uniq:
        lv = str(row.get("level") or "").strip().lower()
        if lv in by_level:
            by_level[lv] += 1
    examples = []
    for row in uniq[:8]:
        content = str(row.get("content") or "")
        examples.append({
            "id": row.get("id"),
            "company_name": row.get("company_name"),
            "check_date": str(row.get("check_date") or "")[:10],
            "level": row.get("level"),
            "content": content[:120],
            "law": row.get("law"),
        })
    return {
        "fetchedCount": len(uniq),
        "complete": bool(complete),
        "filters": {k: v for k, v in filters.items() if v is not None},
        "byLevel": by_level,
        "examples": examples,
        "note": "分页结果不能当作全库总数；级别枚举 serious/major/minor",
    }


def _defects_load_fixture():
    raw = json.loads(config.DEFECTS_FIXTURE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("data") or raw.get("defects") or []
    if not isinstance(raw, list):
        raise ValueError("fixture 格式无效")
    return raw


def _defects_match_fixture(row, filters):
    cn = filters.get("company_name")
    if cn and cn not in str(row.get("company_name") or ""):
        return False
    cid = filters.get("company_id")
    if cid is not None and str(row.get("company_id")) != str(cid):
        return False
    lv = filters.get("level")
    if lv and str(row.get("level") or "").strip().lower() != lv:
        return False
    law = filters.get("law")
    if law and law not in str(row.get("law") or ""):
        return False
    kw = filters.get("content_keywords")
    if kw and kw not in str(row.get("content") or ""):
        return False
    d = str(row.get("check_date") or "")[:10]
    if filters.get("start_date") and d < filters["start_date"]:
        return False
    if filters.get("end_date") and d > filters["end_date"]:
        return False
    return True


def _defects_parse_page_obj(obj):
    """解开 JSON-RPC / MCP content 包装，返回 {success, data:[...]}。"""
    if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
        obj = obj["result"]
    if isinstance(obj, dict) and "content" in obj and not isinstance(obj.get("success"), bool):
        content = obj.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        obj = json.loads(block.get("text") or "null")
                    except Exception:
                        pass
                    break
    if not isinstance(obj, dict) or not obj.get("success") or not isinstance(obj.get("data"), list):
        raise ValueError("缺陷分页响应格式异常")
    return obj["data"]


def _defects_http_page(arguments):
    """JSON-RPC tools/call defects_search_defects（只读）；解析 SSE 与 MCP content 包装。"""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if config.DEFECTS_AUTH:
        headers["Authorization"] = config.DEFECTS_AUTH
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "defects_search_defects", "arguments": arguments}}
    req = urllib.request.Request(config.DEFECTS_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.DEFECTS_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8", "replace")
    text = raw
    if text.lstrip().startswith("event:") or "\ndata:" in text or text.startswith("data:"):
        parts = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if parts:
            text = "\n".join(parts)
    obj = json.loads(text if text.strip().startswith("{") else text.split("\n", 1)[0])
    return _defects_parse_page_obj(obj)


def query_defects(filters):
    """分页（limit≤50，步进至页长<limit 或累计≥200）、按 id 去重、生成摘要。只读。"""
    filters = {k: v for k, v in (filters or {}).items() if k in config.DEFECTS_ALLOWED_ARGS}
    if config.DEFECTS_FIXTURE and config.DEFECTS_FIXTURE.is_file():
        matched = [r for r in _defects_load_fixture() if _defects_match_fixture(r, filters)]
        rows, complete = [], True
        for offset in range(0, 200, 50):
            page = matched[offset:offset + 50]
            rows.extend(page)
            if len(page) < 50:
                break
        else:
            complete = len(matched) < 200
        return summarize_defects(rows, filters, complete)
    if not config.DEFECTS_URL:
        raise RuntimeError("未配置缺陷数据源")
    rows, seen, complete = [], set(), False
    total = 0
    offset = 0
    while total < 200:
        limit = 50
        arguments = {k: v for k, v in filters.items() if v is not None}
        arguments["limit"] = limit
        arguments["offset"] = offset
        page = _defects_http_page(arguments)
        added = 0
        for row in page:
            if not isinstance(row, dict):
                raise ValueError("缺陷响应行格式异常")
            if row.get("id") is None:
                raise ValueError("缺陷记录缺少ID，不能去重")
            key = str(row["id"])
            if key not in seen:
                seen.add(key)
                rows.append(row)
                added += 1
        total += len(page)
        if len(page) < limit:
            complete = True
            break
        if not added:
            raise ValueError("分页返回重复数据，无法确认完整性")
        offset += limit
    return summarize_defects(rows, filters, complete)


def format_defects_summary_text(summary):
    """模型不可用或校验失败时的规则模板。"""
    if not summary or summary.get("fetchedCount", 0) == 0:
        return "查不到：没有符合条件的缺陷记录。"
    lines = [
        f"缺陷记录检索结果：共 {summary['fetchedCount']} 条（complete={summary['complete']}）",
        "条件：" + "、".join(f"{k}={v}" for k, v in summary.get("filters", {}).items()) or "无",
        "按级别：" + "、".join(f"{k} {v}" for k, v in summary.get("byLevel", {}).items()),
        "示例：",
    ]
    for ex in summary.get("examples", [])[:8]:
        lines.append(
            f"- [{ex.get('id')}] {ex.get('company_name')} | {ex.get('check_date')} | "
            f"{ex.get('level')} | {ex.get('content')}"
        )
    lines.append(f"备注：{summary.get('note', '')}")
    return "\n".join(lines)


def run_defects(question, quiet):
    """defects 意图确定性流程：解析条件 → defects_search → 模型仅据摘要措辞。只读。"""
    tools_used = ["defects_search"]
    if not ((config.DEFECTS_FIXTURE and config.DEFECTS_FIXTURE.is_file()) or config.DEFECTS_URL):
        answer = "查不到：未配置缺陷数据源（请设置 MINI_AGENT_DEFECTS_FIXTURE 或 MINI_AGENT_WORK_URL）。"
        trace("defects_disabled", quiet, intent="defects")
        trace("final", quiet, intent="defects", tools=tools_used, answer=answer)
        return {"intent": "defects", "risk": "none", "tools_used": tools_used, "answer": answer}
    filters = parse_defects_filters(question)
    try:
        summary = query_defects(filters)
    except Exception as exc:
        answer = f"查不到：缺陷数据查询失败（{type(exc).__name__}）。"
        trace("defects_error", quiet, intent="defects", error=type(exc).__name__)
        trace("final", quiet, intent="defects", tools=tools_used, answer=answer)
        return {"intent": "defects", "risk": "none", "tools_used": tools_used, "answer": answer}
    summary_json = json.dumps(summary, ensure_ascii=False)
    trace("tool_result", quiet, step=1, tool="defects_search", result=summary_json[:1200])
    if summary.get("fetchedCount", 0) == 0:
        answer = "查不到：没有符合条件的缺陷记录。"
        trace("final", quiet, intent="defects", tools=tools_used, answer=answer)
        return {"intent": "defects", "risk": "none", "tools_used": tools_used, "answer": answer}
    sys_prompt = ("你是缺陷记录检索助手。只根据给出的统计摘要用简洁中文复述；"
                  "不要编造未出现的数字、企业名或条款；fetchedCount=0 则答查不到；"
                  "必须说明结果来自分页检索，不能当作全库总数；级别为 serious/major/minor。")
    user_msg = (f"统计摘要 JSON：\n{summary_json}\n\n用户问题：{question}\n"
                "请只用摘要回答，简洁中文。")
    content = chat_completion(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_msg}],
        json_mode=False, max_tokens=max(config.MAX_TOKENS, 500),
        timeout=max(config.TIMEOUT, config.DEFECTS_TIMEOUT))
    answer = (content or "").strip()
    ground = summary_json + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or ("查不到" in answer and len(answer) < 40):
        answer = format_defects_summary_text(summary)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_defects_summary_text(summary)
    trace("final", quiet, intent="defects", tools=tools_used, answer=answer[:500])
    return {"intent": "defects", "risk": "none", "tools_used": tools_used, "answer": answer}
