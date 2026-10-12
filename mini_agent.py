#!/usr/bin/env python3
"""mini-agent：最小智能体（单文件、仅 Python 标准库）。

流程：System 1 前置路由（config.json 关键词/正则 → Laya /predict → 极短模型分类）
→ 工具循环（模型每步只输出 JSON）→ 规则校验防胡说 → trace 落 stderr 与 sandbox/trace.jsonl。
kb 意图走确定性流程（kb_search → kb_read → 模型仅据摘录作答），知识库只读。
work 意图走确定性流程（解析日期 → 分页拉取/去重/统计 → 模型仅据摘要措辞），工作数据只读。
模型接口不可用时按意图走规则兜底，保证基本可用。
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")
INTENTS = ["chat", "file", "shell", "web", "note", "kb", "work", "out_of_scope"]

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)

BASE_URL = os.environ.get("MINI_AGENT_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
MODEL = os.environ.get("MINI_AGENT_MODEL", "")
API_KEY = os.environ.get("MINI_AGENT_API_KEY", "")  # 可空；绝不打印
SANDBOX = Path(os.environ.get("MINI_AGENT_SANDBOX", "./sandbox")).resolve()
LAYA_URL = os.environ.get("MINI_AGENT_LAYA_URL", "")
TIMEOUT = float(os.environ.get("MINI_AGENT_TIMEOUT", "10"))
MAX_STEPS = int(CFG.get("max_steps", 4))
MAX_TOKENS = int(CFG.get("max_tokens", 256))
DANGER_WORDS = CFG.get("danger_words", [])
SHELL_WHITELIST = CFG.get("shell_whitelist", [])
DANGER_RESPONSE = CFG.get("danger_response", "已拦截：不可逆操作")
TRACE_PATH = SANDBOX / "trace.jsonl"

# 知识库：只读（绝不写入/删除）；MINI_AGENT_KB 为空则禁用
_KB_ENV = os.environ.get("MINI_AGENT_KB", "").strip()
KB = Path(_KB_ENV).expanduser().resolve() if _KB_ENV else None
KB_MAX_FILE = 2 * 1024 * 1024
KB_TIMEOUT = float(os.environ.get("MINI_AGENT_KB_TIMEOUT", "120"))

# 工作事项汇总：只读；fixture 或 URL（MCP/REST），禁止硬编码私有路径/密钥
_WORK_FIXTURE = os.environ.get("MINI_AGENT_WORK_FIXTURE", "").strip()
WORK_FIXTURE = Path(_WORK_FIXTURE).expanduser().resolve() if _WORK_FIXTURE else None
WORK_URL = os.environ.get("MINI_AGENT_WORK_URL", "").strip()
WORK_AUTH = os.environ.get("MINI_AGENT_WORK_AUTH", "").strip()  # 完整 Authorization 值；绝不打印
WORK_TIMEOUT = float(os.environ.get("MINI_AGENT_WORK_TIMEOUT", "30"))
KB_SKIP_DIRS = {".obsidian", ".git", "node_modules", "attachments"}

model_available = True  # 一旦连接层失败则置 False，后续直接走规则兜底


# ---------------------------------------------------------------- 危险词 / 路由
def _word_re(word):
    if re.fullmatch(r"[A-Za-z0-9_]+", word):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(word), re.IGNORECASE)


def match_danger(text):
    """命中危险词返回该词，否则返回 None。'curl|sh' 类条目要求管道符同时出现。"""
    for word in DANGER_WORDS:
        if "|" in word:
            parts = word.split("|")
            if "|" in text and all(_word_re(p).search(text) for p in parts):
                return word
        elif _word_re(word).search(text):
            return word
    return None


def classify_rules(question):
    """按 config.json 关键词(+1) / 正则(+2) 打分，返回得分最高的意图，全不命中返回 None。"""
    best = None
    best_score = 0
    # 文件名（如 hello.txt）先单独识别：命中就偏向 file 意图，并从打分文本里去掉，
    # 免得 hello.txt 里的 "hello" 被当成打招呼。
    fname_rx = r"[\w\-一-龥]+\.[A-Za-z0-9]{1,6}\b"
    has_fname = re.search(fname_rx, question) is not None
    question = re.sub(fname_rx, " ", question)
    q = question.lower()
    scores = {}
    for name, spec in CFG["intents"].items():
        score = 3 if (has_fname and name == "file") else 0
        for kw in spec.get("keywords", []):
            if kw.lower() in q:
                score += 1
        for rx in spec.get("regex", []):
            try:
                if re.search(rx, question):
                    score += 2
            except re.error:
                pass
        scores[name] = score
        if score > best_score:
            best, best_score = name, score
    # work 优先于 note/file：工作事项/月度汇总等
    work_score = scores.get("work", 0)
    if work_score > 0 and work_score >= best_score:
        return "work"
    # kb 优先于 note：「笔记里/我的笔记/知识库」查知识库；
    # 「记一下：…」kb 不命中（0 分），仍归 note。
    kb_score = scores.get("kb", 0)
    if kb_score > 0 and kb_score >= scores.get("note", 0) and kb_score >= best_score:
        return "kb"
    return best


def laya_predict(question):
    """规则不命中且设置了 LAYA_URL 时 POST /predict；任何失败都忽略并返回 None。"""
    if not LAYA_URL:
        return None
    try:
        body = json.dumps({"query": question}).encode("utf-8")
        req = urllib.request.Request(LAYA_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    for cand in (data, data.get("data") if isinstance(data, dict) else None):
        if isinstance(cand, dict):
            for key in ("intent", "prediction", "label", "class", "category"):
                if cand.get(key) in INTENTS:
                    return cand[key]
    return None


# ---------------------------------------------------------------- 模型接口
def chat_completion(messages, json_mode=True, max_tokens=None, timeout=None):
    """调 OpenAI 兼容 /chat/completions（temperature=0，小 max_tokens）。
    附 response_format=json_object；服务器报 4xx 则去掉后重试一次。连接层失败标记模型不可用。"""
    global model_available
    if not model_available:
        return None
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens or MAX_TOKENS}
    if MODEL:
        body["model"] = MODEL
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    use_rf = json_mode
    while True:
        if use_rf:
            body["response_format"] = {"type": "json_object"}
        else:
            body.pop("response_format", None)
        req = urllib.request.Request(BASE_URL + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data["choices"][0]["message"].get("content") or ""
        except urllib.error.HTTPError as exc:
            if use_rf and 400 <= exc.code < 500:
                use_rf = False  # 去掉 response_format 重试
                continue
            return None
        except Exception:
            model_available = False
            return None


# ---------------------------------------------------------------- 知识库（只读）
KB_STOP_EN = {"a", "an", "the", "is", "are", "was", "were", "be", "been",
              "of", "in", "on", "at", "to", "for", "with", "and", "or",
              "not", "no", "what", "how", "who", "why", "when", "where",
              "my", "i", "you", "it", "we", "they", "do", "did", "does"}
KB_STOP_ZH = ("什么", "怎么", "怎样", "我", "的", "吗", "呢", "了", "是",
              "在", "有", "哪些", "哪个", "多少", "知识库", "笔记", "里", "中",
              "写过", "记过", "关于", "一下", "请", "告诉", "我的", "文章",
              "和", "与", "么", "如何", "为什么")
_KB_STOP_RX = re.compile("|".join(sorted(set(KB_STOP_ZH), key=len, reverse=True)))


def kb_keywords(query):
    """确定性关键词抽取：英文/数字 token + 中文 run（2-4 字整体，更长再加二元组），去重封顶 12 个。"""
    kws, seen = [], set()

    def add(kw):
        kw = kw.strip().lower()
        if len(kw) >= 2 and kw not in seen:
            seen.add(kw)
            kws.append(kw)

    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]+", query):
        tok = tok.lower()
        if len(tok) >= 2 and tok not in KB_STOP_EN:
            add(tok)
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        for part in _KB_STOP_RX.split(run):
            if len(part) <= 1:
                continue
            add(part)
            if len(part) > 4:  # 长 run 再补重叠二元组
                for i in range(len(part) - 1):
                    add(part[i:i + 2])
    return kws[:12]


def _kb_hit_spots(text, kws):
    low = text.casefold()
    spots = []
    for kw in kws:
        for m in re.finditer(re.escape(kw), low):
            spots.append(m.start())
    spots.sort()
    return spots


def _kb_snippets(text, kws, limit=3, width=120):
    snippets, seen = [], set()
    for i in _kb_hit_spots(text, kws):
        a = max(0, i - 40)
        snip = text[a:a + width].strip().replace("\n", " ")
        if snip and snip not in seen:
            seen.add(snip)
            snippets.append(snip)
            if len(snippets) >= limit:
                break
    return snippets


def _kb_rg_rels(kws):
    """rg 一次检索全部关键词：rg -i -c --glob '*.md' -g 跳过目录 -F -e kw1 -e kw2 ... ."""
    cmd = ["rg", "-i", "-c", "--glob", "*.md",
           "-g", "!.obsidian", "-g", "!.git", "-g", "!node_modules",
           "-g", "!attachments", "-F"]
    for kw in kws:
        cmd += ["-e", kw]
    cmd.append(".")  # 显式指定当前目录（KB），某些环境缺省路径会漏搜
    try:
        proc = subprocess.run(cmd, cwd=str(KB), capture_output=True,
                              text=True, timeout=TIMEOUT)
    except Exception:
        return None  # rg 失败则退回 os.walk
    if proc.returncode > 1:
        return None  # rg 出错（rc=2）也退回 os.walk
    rels = []
    for line in proc.stdout.splitlines():
        path, sep, cnt = line.rpartition(":")
        if sep and cnt.strip().isdigit():
            rels.append(path.removeprefix("./"))
    return rels


def _kb_walk_rels():
    """os.walk 兜底：跳过相同目录，仅 .md。"""
    rels = []
    for dirpath, dirnames, filenames in os.walk(KB):
        dirnames[:] = [d for d in dirnames if d not in KB_SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.lower().endswith(".md"):
                rels.append(str((Path(dirpath) / fn).relative_to(KB)))
    return rels


def _kb_score(rel, kws):
    """单文件打分：命中数(封顶 5) + 3×命中词数 + 文件名/标题命中各 5 + mtime 新鲜度。"""
    full = KB / rel
    try:
        if full.stat().st_size > KB_MAX_FILE:
            return None
        text = full.read_text(encoding="utf-8", errors="replace")
        mtime = full.stat().st_mtime
    except OSError:
        return None
    low = text.casefold()
    counts = {kw: low.count(kw) for kw in kws if kw in low}
    if not counts:
        return None
    heading = ""
    for line in text.splitlines():
        if line.strip().startswith("#"):
            heading = line.strip()
            break
    score = sum(min(c, 5) for c in counts.values()) + 3 * len(counts)
    name_low, head_low = Path(rel).name.casefold(), heading.casefold()
    score += 5 * sum(1 for kw in counts if kw in name_low or kw in head_low)
    age = time.time() - mtime
    if age <= 7 * 86400:
        score += 3
    elif age <= 30 * 86400:
        score += 2
    return {"path": rel, "score": score, "snippets": _kb_snippets(text, counts)}


def kb_search(query):
    """搜索知识库 .md，按打分返回 top 5（相对路径 + 分数 + 摘录）。"""
    if not KB or not KB.is_dir():
        return []
    kws = kb_keywords(query)
    if not kws:
        return []
    rels = _kb_rg_rels(kws) if shutil.which("rg") else None
    if rels is None:
        rels = _kb_walk_rels()
    results = [r for r in (_kb_score(rel, kws) for rel in rels) if r]
    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results[:5]


def kb_read(path, query):
    """读知识库内单个文件（相对路径，禁绝对路径与 ..）：返回标题行 + 命中最密集处 ≤3000 字窗口。"""
    if not KB:
        raise PermissionError("未配置知识库")
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise PermissionError("非法路径（仅限知识库内相对路径）")
    full = (KB / p).resolve()
    if full != KB and KB not in full.parents:
        raise PermissionError("路径越权（仅限知识库内）")
    if not full.is_file():
        raise FileNotFoundError(path)
    text = full.read_text(encoding="utf-8", errors="replace")
    spots = _kb_hit_spots(text, kb_keywords(query))
    if spots:
        best = max(spots, key=lambda i: sum(i <= j < i + 3000 for j in spots))
        a = max(0, min(best, len(text) - 3000))
        window = text[a:a + 3000].strip()
    else:
        window = text[:3000].strip()
    title = ""
    for line in text.splitlines():
        if line.strip().startswith("#"):
            title = line.strip()
            break
    return (title + "\n" + window if title else window)[:3000]


def t_kb_search(args):
    return json.dumps(kb_search(args.get("query") or args.get("q") or ""),
                      ensure_ascii=False)


def t_kb_read(args):
    return kb_read(args.get("path") or "", args.get("query") or args.get("q") or "")


def kb_fallback(question):
    """规则未命中且配置了知识库：提到“我/我的”的事实性问题归 kb（在模型分类之前）。"""
    if not KB:
        return False
    q = question.strip()
    if "我" not in q:
        return False
    return re.search(r"什么|怎么|怎样|为何|为什么|谁|哪|哪些|哪个|多少|是否|吗|呢|[?？]", q) is not None


def run_kb(question, quiet):
    """kb 意图确定性流程（模型不做工具选择）：kb_search → kb_read top 1-2 → 模型据摘录作答并校验。"""
    tools_used = ["kb_search"]
    if not KB:
        answer = "查不到：未配置知识库（请设置 MINI_AGENT_KB）。"
        trace("kb_disabled", quiet, intent="kb")
        trace("final", quiet, intent="kb", tools=tools_used, answer=answer)
        return {"intent": "kb", "risk": "none", "tools_used": tools_used,
                "answer": answer}
    results = kb_search(question)
    trace("tool_result", quiet, step=1, tool="kb_search",
          result=json.dumps(results, ensure_ascii=False)[:1000])
    if not results:
        answer = "查不到：知识库里没有相关笔记。"
        trace("final", quiet, intent="kb", tools=tools_used, answer=answer)
        return {"intent": "kb", "risk": "none", "tools_used": tools_used,
                "answer": answer}
    excerpts = []
    for r in results[:2]:
        try:
            content = kb_read(r["path"], question)
        except Exception as exc:
            trace("tool_error", quiet, step=2, tool="kb_read",
                  result=f"{type(exc).__name__}: {exc}")
            continue
        if "kb_read" not in tools_used:
            tools_used.append("kb_read")
        trace("tool_result", quiet, step=2, tool="kb_read", result=content[:1000])
        excerpts.append((r["path"], content))
    if not excerpts:
        answer = "查不到：知识库里没有相关笔记。"
        trace("final", quiet, intent="kb", tools=tools_used, answer=answer)
        return {"intent": "kb", "risk": "none", "tools_used": tools_used,
                "answer": answer}
    corpus = "\n\n".join(f"[{p}]\n{c}" for p, c in excerpts)
    sys_prompt = ("你是知识库问答助手。只根据给出的知识库摘录回答，简洁中文；"
                  "摘录中没有答案就答“查不到”，不要编造；注明引用的文件路径。")
    user_msg = (f"知识库摘录如下（标签 [路径]）：\n{corpus}\n\n问题：{question}\n"
                "请只用上面的摘录回答，简洁中文，并注明来源路径。")
    content = chat_completion(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_msg}], json_mode=False,
        max_tokens=max(MAX_TOKENS, 400), timeout=max(TIMEOUT, KB_TIMEOUT))
    answer = (content or "").strip()
    used_paths = [p for p, _ in excerpts]
    # 数字校验：答案里的数字必须出现在摘录中，否则换成抽取式兜底（top 片段 + 来源）
    # 忽略列表序号（行首 "1." / "1、"）；摘录、路径、问题里出现过的数字都算有据
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    ground = corpus + "\n" + question
    if "查不到" in answer and len(answer) < 40:
        answer = "查不到：知识库摘录里没有这个问题的答案。"
    elif not answer or any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        snippets = []
        for r in results[:2]:
            snippets.extend(r["snippets"][:2])
        answer = "；".join(snippets) + "\n来源：" + ", ".join(used_paths)
    elif any(p not in answer for p in used_paths):
        answer += "\n来源：" + ", ".join(used_paths)
    trace("final", quiet, intent="kb", tools=tools_used, answer=answer[:500])
    return {"intent": "kb", "risk": "none", "tools_used": tools_used,
            "answer": answer}


def classify_model(question):
    """一次极短模型分类：只输出类别名；不在白名单则 chat。"""
    sys_prompt = ("你是意图分类器。只输出以下类别之一：" + ", ".join(INTENTS) +
                  "。只输出类别名，不要任何解释。")
    content = chat_completion([{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": question}],
                              json_mode=False, max_tokens=16)
    if not content:
        return "chat"
    text = content.strip().lower()
    for intent in INTENTS:
        if re.search(r"\b" + intent + r"\b", text):
            return intent
    return "chat"



# ---------------------------------------------------------------- 工作事项汇总（只读）
def parse_work_range(question):
    """从问题确定性解析 (start_date, end_date)；失败返回 None。"""
    q = (question or "").strip()
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
    raw = json.loads(WORK_FIXTURE.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("tasks") or raw.get("data") or []
    if not isinstance(raw, list):
        raise ValueError("fixture 格式无效")
    return raw


def _work_http_page(start_date, end_date, limit, offset):
    """调用 WORK_URL：优先 MCP JSON-RPC tools/call db_query_tasks，其次 REST POST。"""
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if WORK_AUTH:
        headers["Authorization"] = WORK_AUTH
    # MCP style
    mcp_body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "db_query_tasks",
                   "arguments": {"start_date": start_date, "end_date": end_date,
                                 "limit": limit, "offset": offset}},
    }
    req = urllib.request.Request(WORK_URL, data=json.dumps(mcp_body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=WORK_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except Exception:
        # REST fallback
        rest = {"start_date": start_date, "end_date": end_date,
                "limit": limit, "offset": offset}
        req2 = urllib.request.Request(WORK_URL, data=json.dumps(rest).encode("utf-8"),
                                      headers=headers, method="POST")
        with urllib.request.urlopen(req2, timeout=WORK_TIMEOUT) as resp:
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
    if WORK_FIXTURE and WORK_FIXTURE.is_file():
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
    if not WORK_URL:
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
    if not ((WORK_FIXTURE and WORK_FIXTURE.is_file()) or WORK_URL):
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
        json_mode=False, max_tokens=max(MAX_TOKENS, 500),
        timeout=max(TIMEOUT, WORK_TIMEOUT))
    answer = (content or "").strip()
    ground = summary_json + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or "查不到" in answer and len(answer) < 40:
        answer = format_work_summary_text(summary)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_work_summary_text(summary)
    trace("final", quiet, intent="work", tools=tools_used, answer=answer[:500])
    return {"intent": "work", "risk": "none", "tools_used": tools_used, "answer": answer}


# ---------------------------------------------------------------- JSON 提取 / 校验
def extract_json(text):
    """稳健提取：去代码块与零宽字符，先整体解析，失败再抓首个 {...} 片段。"""
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?|```", "", text)
    cleaned = re.sub(r"[​‌‍﻿ ]", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def valid_step(obj, allowed):
    if not isinstance(obj, dict):
        return False
    if "answer" in obj:
        return isinstance(obj["answer"], str)
    if "tool" in obj:
        return obj["tool"] in allowed and isinstance(obj.get("args"), dict)
    return False


def feedback_for(obj, allowed):
    return ("上一步输出无效。请只输出一个 JSON 对象："
            '{"tool":"工具名","args":{...}} 或 {"answer":"回答","sources":[...]}。'
            "本意图可用工具：" + (", ".join(allowed) if allowed else "无，只能给 answer") +
            "。原始输出：" + (json.dumps(obj, ensure_ascii=False) if obj is not None else "无法解析为 JSON"))


# ---------------------------------------------------------------- 工具
def safe_resolve(path):
    """把路径解析到沙箱内；越权（含 .. 逃逸）抛 PermissionError。"""
    p = Path(path)
    p = p.resolve() if p.is_absolute() else (SANDBOX / p).resolve()
    if p != SANDBOX and SANDBOX not in p.parents:
        raise PermissionError("路径越权（仅限沙箱内）")
    return p


def t_read_file(args):
    path = args.get("path") or args.get("file") or args.get("name")
    if not path:
        raise ValueError("缺少 path")
    text = safe_resolve(str(path)).read_text(encoding="utf-8", errors="replace")
    return text[:8000] + ("\n...[截断]" if len(text) > 8000 else "")


def t_search_files(args):
    query = args.get("query") or args.get("pattern") or args.get("q") or ""
    root = safe_resolve(str(args.get("path", ".")))
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        name = p.name
        if not query or query.lower() in name.lower():
            hits.append(str(p.relative_to(SANDBOX)))
    return "\n".join(hits[:50]) or "（未找到匹配文件）"


def t_run_shell(args):
    command = args.get("command") or args.get("cmd") or ""
    if not command or "\n" in command:
        raise ValueError("非法命令")
    for ch in "|;&<>`$":  # 禁止管道、重定向、后台、命令替换
        if ch in command:
            raise ValueError("禁止管道/重定向/控制字符")
    parts = shlex.split(command)
    if not parts or parts[0] not in SHELL_WHITELIST:
        raise ValueError("命令不在白名单内")
    for arg in parts[1:]:
        safe_resolve(arg)  # 参数也必须在沙箱内（防 .. 逃逸与绝对路径越权）
    proc = subprocess.run(parts, cwd=str(SANDBOX), capture_output=True,
                          text=True, timeout=10)
    out = (proc.stdout + proc.stderr).strip()
    return out[:4000] or "(无输出)"


def t_web_fetch(args):
    url = args.get("url") or args.get("u") or ""
    if not url.startswith(("http://", "https://")):
        raise ValueError("仅支持 http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": "mini-agent/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(200000)
    text = raw.decode("utf-8", "ignore")
    return text[:4000] + ("...[截断]" if len(text) > 4000 else "")


def t_note_append(args):
    content = args.get("content") or args.get("note") or args.get("text")
    if not content:
        raise ValueError("缺少笔记内容")
    SANDBOX.mkdir(parents=True, exist_ok=True)
    with open(SANDBOX / "notes.md", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {content}\n")
    return "已追加到 notes.md"


TOOLS = {"read_file": t_read_file, "search_files": t_search_files,
         "run_shell": t_run_shell, "web_fetch": t_web_fetch,
         "note_append": t_note_append, "kb_search": t_kb_search,
         "kb_read": t_kb_read}
# work_summary 由 run_work 确定性调用，不经模型选工具


# ---------------------------------------------------------------- 规则兜底（模型不可用时）
def fallback_answer(intent, question):
    """返回 (answer, evidence)；evidence 视为工具结果供校验器使用。"""
    q = question.strip()
    if intent == "file":
        cand = None
        m = re.search(r"[\w\-. ]+\.\w+", q)
        if m:
            try:
                p = safe_resolve(m.group(0).strip())
                if p.is_file():
                    cand = p
            except Exception:
                cand = None
        if cand is None:
            tokens = [t for t in re.findall(r"[\w]+", q) if len(t) > 1]
            for p in sorted(SANDBOX.rglob("*")):
                if p.is_file() and any(t.lower() in p.name.lower() for t in tokens):
                    cand = p
                    break
        if cand is not None:
            content = cand.read_text(encoding="utf-8", errors="replace")[:2000]
            evidence = f"read_file({cand.name}) = {content}"
            return f"根据文件 {cand.name} 的内容：{content}", evidence
        return "查不到相关信息", ""
    if intent == "shell":
        m = re.search(r"\b(" + "|".join(SHELL_WHITELIST) + r")\b", q)
        cmd = m.group(1) if m else "ls"
        if cmd == "echo" and m:
            cmd = q[m.end():].strip() and cmd + " " + q[m.end():].strip() or cmd
        try:
            out = t_run_shell({"command": cmd})
            return f"执行 {cmd} 的结果：\n{out}", f"run_shell({cmd}) = {out}"
        except Exception as exc:
            return f"查不到相关信息（{exc}）", ""
    if intent == "web":
        m = re.search(r"https?://\S+", q)
        if m:
            try:
                out = t_web_fetch({"url": m.group(0)})
                return "网页内容：\n" + out, f"web_fetch = {out}"
            except Exception:
                pass
        return "查不到相关信息", ""
    if intent == "note":
        text = q
        for kw in CFG["intents"]["note"].get("keywords", []):
            if kw in text:
                text = text.replace(kw, "", 1)
                break
        text = text.lstrip("：:，, \t")
        if text:
            try:
                t_note_append({"content": text})
                return f"已记录：{text}", f"note_append({text})"
            except Exception:
                return "记录失败", ""
        return "查不到相关信息", ""
    if intent == "out_of_scope":
        return "对不起，我无法回答此类问题。", ""
    # chat
    if any(kw.lower() in q.lower() for kw in CFG["intents"]["chat"].get("keywords", [])):
        return "你好！有什么可以帮你的吗？", ""
    return "不知道，我无法回答这个问题。", ""


# ---------------------------------------------------------------- 防胡说校验
def validate_answer(answer, intent, tool_text, question):
    if not answer:
        return "查不到相关信息"
    corpus = (tool_text or "") + "\n" + question
    for num in re.findall(r"\d+(?:\.\d+)?", answer):
        if num not in corpus:
            return "查不到可靠信息"
    if intent != "chat" and not tool_text.strip() and "不知道" not in answer and "查不到" not in answer:
        return "查不到相关信息"
    return answer


# ---------------------------------------------------------------- trace
def trace(event, quiet, **data):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
    rec.update(data)
    line = json.dumps(rec, ensure_ascii=False)
    if not quiet:
        print(line, file=sys.stderr)
    try:
        SANDBOX.mkdir(parents=True, exist_ok=True)
        with open(TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- 主流程

def tool_msg(tool, result, question):
    """工具结果用普通 user 消息回传：很多本地服务（如 Ollama）不认没有 tool_call_id 的 tool 角色。"""
    return (f"工具 {tool} 返回的内容如下：\n<<<\n{result}\n>>>\n"
            f"请只根据上面的内容回答原问题：{question}\n内容里确实没有答案才答“查不到”。")

def run_agent(question, quiet=False):
    danger = match_danger(question)
    rule_intent = classify_rules(question)
    if danger:  # 危险词直接拦截，不进模型
        intent = rule_intent or "out_of_scope"
        trace("danger", quiet, intent=intent, word=danger)
        return {"intent": intent, "risk": "danger", "tools_used": [],
                "answer": DANGER_RESPONSE}

    if rule_intent:
        intent = rule_intent
        trace("intent_rules", quiet, intent=intent)
    elif (intent := laya_predict(question)) is not None:
        trace("intent_laya", quiet, intent=intent)
    elif kb_fallback(question):
        intent = "kb"
        trace("intent_kb_fallback", quiet, intent=intent)
    else:
        intent = classify_model(question)
        trace("intent_model", quiet, intent=intent)

    if intent == "kb":  # 确定性流程：模型不参与工具选择
        return run_kb(question, quiet)
    if intent == "work":
        return run_work(question, quiet)

    spec = CFG["intents"][intent]
    allowed = list(spec.get("tools", []))
    format_hint = ('\n每步只输出 JSON：{"tool":"工具名","args":{...}} 或 '
                   '{"answer":"回答","sources":[...]}，不要其他文字。')
    messages = [{"role": "system", "content": spec.get("system", "") + format_hint},
                {"role": "user", "content": question}]
    tool_text = ""
    tools_used = []
    answer = None

    if intent == "file":  # 确定性前置：问题点名了沙箱内存在的文件则先读出
        m = re.search(r"[\w\-.]+\.\w+", question)
        try:
            cand = safe_resolve(m.group(0).strip()) if m else None
            if cand is not None and cand.is_file():
                result = TOOLS["read_file"]({"path": str(cand)})
                tools_used.append("read_file")
                tool_text += "\n" + result
                messages += [
                    {"role": "assistant", "content": json.dumps(
                        {"tool": "read_file", "args": {"path": cand.name}},
                        ensure_ascii=False)},
                    {"role": "user", "content": tool_msg("read_file", result, question)}]
                trace("tool_result", quiet, step=0, tool="read_file",
                      result=result[:1000])
        except Exception:
            pass

    for step in range(1, MAX_STEPS + 1):
        obj = ask_model(messages)
        if not valid_step(obj, allowed):  # 失败带错误反馈重试 1 次
            obj2 = ask_model(messages, feedback_for(obj, allowed))
            obj = obj2 if valid_step(obj2, allowed) else obj
        if not valid_step(obj, allowed):
            trace("step_invalid", quiet, step=step)
            break
        if "answer" in obj and intent != "chat" and not tools_used:
            # 还没调用任何工具就给出答案：追加提示重试一次
            retry = ask_model(messages, "你还没有调用任何工具，必须先调用工具")
            if valid_step(retry, allowed):
                obj = retry
        if "answer" in obj:
            answer = obj["answer"]
            trace("step_answer", quiet, step=step, answer=answer[:500])
            break
        tool, targs = obj["tool"], obj["args"]
        arg_danger = match_danger(json.dumps(targs, ensure_ascii=False))
        if arg_danger:
            result = DANGER_RESPONSE
            trace("tool_blocked", quiet, step=step, tool=tool, word=arg_danger)
        else:
            try:
                result = TOOLS[tool](targs)
            except Exception as exc:
                result = f"工具错误：{type(exc).__name__}: {exc}"
            trace("tool_result", quiet, step=step, tool=tool, result=result[:1000])
        tools_used.append(tool)
        tool_text += "\n" + result
        messages.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})
        messages.append({"role": "user", "content": tool_msg(tool, result, question)})

    if answer is None:  # 模型不可用或步数耗尽 → 规则兜底
        answer, evidence = fallback_answer(intent, question)
        tool_text += "\n" + evidence
        trace("fallback", quiet, intent=intent)
    answer = validate_answer(answer, intent, tool_text, question)
    trace("final", quiet, intent=intent, tools=tools_used, answer=answer[:500])
    return {"intent": intent, "risk": "none", "tools_used": tools_used, "answer": answer}


def ask_model(messages, feedback=None):
    if feedback is not None:
        messages.append({"role": "user", "content": feedback})
    content = chat_completion(messages)
    return extract_json(content)


# ---------------------------------------------------------------- 入口
def repl(quiet=False):
    print("mini-agent REPL（输入 exit 退出）", file=sys.stderr)
    while True:
        try:
            line = input("mini-agent> ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        print(run_agent(line, quiet)["answer"])


def main():
    ap = argparse.ArgumentParser(description="最小智能体（OpenAI 兼容接口 + 工具循环）")
    ap.add_argument("question", nargs="?", help='问题；缺省进入 REPL')
    ap.add_argument("--json", action="store_true", help='输出最终 JSON：{intent,risk,tools_used,answer}')
    ap.add_argument("--quiet", action="store_true", help="关闭 stderr trace")
    args = ap.parse_args()
    SANDBOX.mkdir(parents=True, exist_ok=True)
    if args.question is None:
        repl(args.quiet)
        return
    result = run_agent(args.question, args.quiet)
    if args.json:
        print(json.dumps({"intent": result["intent"], "risk": result["risk"],
                          "tools_used": result["tools_used"],
                          "answer": result["answer"]}, ensure_ascii=False))
    else:
        print(result["answer"])


if __name__ == "__main__":
    main()
