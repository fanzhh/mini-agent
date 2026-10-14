"""mini_agent.router：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from .jsonutil import extract_json
from .model import chat_completion
from .trace import trace
from datetime import datetime
import json
import re
import time
import urllib.request

def _word_re(word):
    if re.fullmatch(r"[A-Za-z0-9_]+", word):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(word), re.IGNORECASE)


def match_danger(text):
    """命中危险词返回该词，否则返回 None。'curl|sh' 类条目要求管道符同时出现。"""
    for word in config.DANGER_WORDS:
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
    for name, spec in config.CFG["intents"].items():
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
    # add_event 优先于 work/note：明确「增加/创建事件」写入，不是查询汇总
    add_event_score = scores.get("add_event", 0)
    if add_event_score > 0 and add_event_score >= scores.get("work", 0)             and add_event_score >= scores.get("note", 0)             and add_event_score >= scores.get("inbox", 0):
        return "add_event"
    # inbox 优先于 note：收件箱/待办/超期（与 work 冲突时：工作事项/汇总归 work）
    inbox_score = scores.get("inbox", 0)
    work_score = scores.get("work", 0)
    if inbox_score > 0 and inbox_score >= work_score and inbox_score >= best_score:
        return "inbox"
    # work 优先于 note/file：工作事项/月度汇总等
    if work_score > 0 and work_score >= best_score:
        return "work"
    # defects 优先于 kb/note（及 work/inbox 之外冲突时）：缺陷/不符合项
    defects_score = scores.get("defects", 0)
    if defects_score > 0 and defects_score >= scores.get("kb", 0) \
            and defects_score >= scores.get("note", 0) \
            and defects_score >= scores.get("work", 0) \
            and defects_score >= scores.get("inbox", 0) \
            and defects_score >= scores.get("file", 0) \
            and defects_score >= scores.get("shell", 0):
        return "defects"
    # kb 优先于 note：「笔记里/我的笔记/知识库」查知识库；
    # 「记一下：…」kb 不命中（0 分），仍归 note。
    kb_score = scores.get("kb", 0)
    if kb_score > 0 and kb_score >= scores.get("note", 0) and kb_score >= best_score:
        return "kb"
    return best


def laya_predict(question):
    """规则不命中且设置了 LAYA_URL 时 POST /predict；任何失败都忽略并返回 None。"""
    if not config.LAYA_URL:
        return None
    try:
        body = json.dumps({"query": question}).encode("utf-8")
        req = urllib.request.Request(config.LAYA_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=config.TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:
        return None
    for cand in (data, data.get("data") if isinstance(data, dict) else None):
        if isinstance(cand, dict):
            for key in ("intent", "prediction", "label", "class", "category"):
                if cand.get(key) in config.INTENTS:
                    return cand[key]
    return None


def soft_intent_fallback(question):
    """S1 失败时的软规则兜底（非 highConfidence）。"""
    q = (question or "").strip()
    ql = q.lower()
    if re.search(r"(?i)\b(my|mine)\b|我的", q) and re.search(
            r"(?i)work|task|plan|schedule|工作|任务|计划|安排|忙", q):
        return "work"
    if re.search(r"(?i)\b(my|mine)\b|我的", q) and re.search(
            r"(?i)todo|inbox|待办|收件箱|overdue", q):
        return "inbox"
    if (re.search(r"(?i)\b(my|mine)\b|我的", q) and re.search(
            r"(?i)notes?|知识|笔记", q)) or re.search(r"(?i)knowledge\s+base", q):
        return "kb" if config.KB else None
    if re.search(r"(?i)defect|finding|缺陷|不符合", q):
        return "defects"
    if relative_date_range(q) and re.search(r"(?i)work|plan|task|工作|计划|安排|忙", q):
        return "work"
    return None


def kb_fallback(question):

    """规则未命中且配置了知识库：提到“我/我的”的事实性问题归 kb（在模型分类之前）。"""
    if not config.KB:
        return False
    q = question.strip()
    if "我" not in q:
        return False
    return re.search(r"什么|怎么|怎样|为何|为什么|谁|哪|哪些|哪个|多少|是否|吗|呢|[?？]", q) is not None


def classify_model(question):
    """一次极短模型分类：只输出类别名；不在白名单则 chat。"""
    sys_prompt = ("你是意图分类器。只输出以下类别之一：" + ", ".join(config.INTENTS) +
                  "。只输出类别名，不要任何解释。")
    content = chat_completion([{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": question}],
                              json_mode=False, max_tokens=16)
    if not content:
        return "chat"
    text = content.strip().lower()
    for intent in config.INTENTS:
        if re.search(r"\b" + intent + r"\b", text):
            return intent
    return "chat"


def ref_today():
    """返回今天 YYYY-MM-DD（可用 MINI_AGENT_TODAY 覆盖）。"""
    if config.TODAY_OVERRIDE and re.fullmatch(r"\d{4}-\d{2}-\d{2}", config.TODAY_OVERRIDE):
        return config.TODAY_OVERRIDE
    return datetime.now().strftime("%Y-%m-%d")


def relative_date_range(question, today=None):
    """解析相对日期 → (start, end)；周=周一至周日。无匹配返回 None。"""
    q = (question or "").strip()
    ql = q.lower()
    today = today or ref_today()
    d = datetime.strptime(today, "%Y-%m-%d")
    # week: Monday=0
    mon = d.fromordinal(d.toordinal() - d.weekday())
    sun = mon.fromordinal(mon.toordinal() + 6)

    def fmt(x):
        return x.strftime("%Y-%m-%d")

    if re.search(r"(?i)\bthis\s+week\b|本周|这周|这礼拜", q):
        return fmt(mon), fmt(sun)
    if re.search(r"(?i)\blast\s+week\b|上周", q):
        return fmt(mon.fromordinal(mon.toordinal() - 7)), fmt(mon.fromordinal(mon.toordinal() - 1))
    if re.search(r"(?i)\bnext\s+week\b|下周", q):
        return fmt(mon.fromordinal(mon.toordinal() + 7)), fmt(mon.fromordinal(mon.toordinal() + 13))
    if re.search(r"(?i)\bthis\s+month\b|本月|这个月|当月", q):
        start = d.replace(day=1)
        if d.month == 12:
            end = d.replace(year=d.year + 1, month=1, day=1).fromordinal(
                d.replace(year=d.year + 1, month=1, day=1).toordinal() - 1)
        else:
            end = d.replace(month=d.month + 1, day=1).fromordinal(
                d.replace(month=d.month + 1, day=1).toordinal() - 1)
        # simpler with calendar
        import calendar
        last = calendar.monthrange(d.year, d.month)[1]
        return f"{d.year}-{d.month:02d}-01", f"{d.year}-{d.month:02d}-{last:02d}"
    if re.search(r"(?i)\blast\s+month\b|上个月|上月", q):
        import calendar
        y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        last = calendar.monthrange(y, m)[1]
        return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"
    if re.search(r"(?i)\bnext\s+month\b|下个月|下月", q):
        import calendar
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        last = calendar.monthrange(y, m)[1]
        return f"{y}-{m:02d}-01", f"{y}-{m:02d}-{last:02d}"
    if re.search(r"(?i)\btoday\b|今天|今日", q):
        return today, today
    if re.search(r"(?i)\byesterday\b|昨天", q):
        y = d.fromordinal(d.toordinal() - 1)
        return fmt(y), fmt(y)
    if re.search(r"(?i)\btomorrow\b|明天", q):
        t = d.fromordinal(d.toordinal() + 1)
        return fmt(t), fmt(t)
    # relative token from System One
    token = ql.strip()
    token_map = {
        "this_week": lambda: relative_date_range("this week", today),
        "last_week": lambda: relative_date_range("last week", today),
        "next_week": lambda: relative_date_range("next week", today),
        "this_month": lambda: relative_date_range("this month", today),
        "last_month": lambda: relative_date_range("last month", today),
        "next_month": lambda: relative_date_range("next month", today),
        "today": lambda: relative_date_range("today", today),
        "yesterday": lambda: relative_date_range("yesterday", today),
        "tomorrow": lambda: relative_date_range("tomorrow", today),
    }
    if token in token_map:
        return token_map[token]()
    return None


def high_confidence_rule(question):
    """仅强匹配；返回 (intent, reason) 或 None。弱匹配交给 System One。"""
    q = (question or "").strip()
    if re.search(r"(增加|添加|新建|创建).{0,12}(事件|工作事项)", q) or        re.search(r"向.{0,16}(数据库|six).{0,16}(增加|添加|新建|写入)", q) or        re.search(r"(?i)\b(add|create)\b.{0,20}\b(event|task)\b", q):
        return "add_event", "highConfidence: create event/task"
    if re.search(r"(?i)\b(work\s+plans?|work\s+tasks?|my\s+work|weekly\s+work|work\s+summary)\b", q) and \
       re.search(r"(?i)this\s+week|last\s+week|next\s+week|本周|这周|这礼拜|上周|下周|本月", q):
        return "work", "highConfidence: work+relative"
    if re.search(r"工作事项|工作汇总|工作摘要|月度工作|本周工作计划", q):
        return "work", "highConfidence: zh work phrase"
    if re.search(r"(?i)(超期待办|anything\s+overdue|pending\s+tasks?)|(收件箱.{0,6}(超期|待办))", q):
        return "inbox", "highConfidence: inbox/overdue"
    if re.search(r"(?i)(serious\s+findings?|严重缺陷)|缺陷记录", q):
        return "defects", "highConfidence: defects"
    if re.search(r"(?i)what\s+did\s+my\s+notes|notes\s+say\s+about|我的笔记里|知识库里", q):
        return "kb", "highConfidence: kb notes"
    if re.fullmatch(r"(?i)\s*(hi|hello|你好|早|晚安|再见)\s*[!！.。]?\s*", q):
        return "chat", "highConfidence: greeting"
    return None


def validate_understanding(obj):
    """校验 System One JSON；合法返回规范化 dict，否则 None。"""
    if not isinstance(obj, dict):
        return None
    intent = obj.get("intent")
    if intent not in config.S1_INTENTS:
        return None
    relative = obj.get("relative")
    if relative in ("null", ""):
        relative = None
    if relative not in config.S1_RELATIVE:
        return None
    rng = obj.get("range")
    start = end = None
    if isinstance(rng, dict):
        start, end = rng.get("start"), rng.get("end")
        if start and end:
            if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(start)) and
                    re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(end)) and start <= end):
                return None
        else:
            start = end = None
            rng = None
    elif rng not in (None, "null"):
        return None
    if start is None and relative:
        bounds = relative_date_range(relative)
        if bounds:
            start, end = bounds
    entities = obj.get("entities") if isinstance(obj.get("entities"), dict) else {}
    level = entities.get("level")
    if level in ("null", ""):
        level = None
    if level not in (None, "serious", "major", "minor"):
        level = None
    status = entities.get("status")
    if status in ("null", ""):
        status = None
    if status not in (None, "pending", "completed", "all"):
        status = None
    kws = entities.get("keywords") or []
    if not isinstance(kws, list):
        kws = []
    return {
        "intent": intent,
        "relative": relative,
        "range": {"start": start, "end": end} if start and end else None,
        "entities": {
            "company": entities.get("company") or None,
            "level": level,
            "keywords": [str(k) for k in kws if k][:8],
            "status": status,
            "overdue_only": bool(entities.get("overdue_only")),
        },
    }


def call_systemone(state, questions, model=None, timeout=None):
    """POST Ollama/Laya /v1/systemone (decision API). Returns answers dict or None."""
    if not config.S1_ENDPOINT:
        return None
    body = {
        "model": (model or config.S1_MODEL),
        "state": state,
        "questions": questions,
    }
    req = urllib.request.Request(
        config.S1_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else config.S1_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("answers") if isinstance(data, dict) else None
    except Exception:
        return None


def system_one_understand(question, quiet=True):
    """System One 主理解：优先 tev1 /v1/systemone；失败则 chat JSON 回退。返回 (understanding|None, duration_ms)。"""
    t0 = time.time()
    answers = call_systemone(question, config.S1_QUESTIONS, model=config.S1_MODEL, timeout=config.S1_TIMEOUT)
    understanding = None
    raw = None
    if isinstance(answers, dict) and answers.get("intent"):
        intent = (answers.get("intent") or {}).get("choice")
        rel = (answers.get("relative") or {}).get("choice")
        if rel in (None, "none", "null", ""):
            rel = None
        overdue_p = (answers.get("overdue_only") or {}).get("noul")
        overdue = bool(isinstance(overdue_p, (int, float)) and overdue_p >= 0.5)
        level = (answers.get("level") or {}).get("choice")
        if level in (None, "none", "null", ""):
            level = None
        obj = {
            "intent": intent,
            "relative": rel,
            "range": None,
            "entities": {
                "company": None,
                "level": level,
                "keywords": [],
                "status": None,
                "overdue_only": overdue,
            },
        }
        understanding = validate_understanding(obj)
        if understanding:
            conf = (answers.get("intent") or {}).get("confidence")
            understanding["confidence"] = conf
        raw = json.dumps(answers, ensure_ascii=False)[:200]
    if understanding is None:
        # Fallback: constrained JSON via chat (for non-decision models like qwen2.5:0.5b / 14b)
        sys_prompt = (
            "You are a request router (System One). Output ONLY one JSON object, no markdown.\n"
            'Schema: {"intent":"kb|work|inbox|defects|add_event|chat|note|file|shell|web|out_of_scope",'
            '"relative":"this_week|last_week|next_week|this_month|last_month|next_month|today|yesterday|tomorrow|null",'
            '"range":null or {"start":"YYYY-MM-DD","end":"YYYY-MM-DD"},'
            '"entities":{"company":null,"level":"serious|major|minor|null","keywords":[],'
            '"status":"pending|completed|all|null","overdue_only":false}}\n'
            "intent guide: work=query job schedule/plans; add_event=create/add a work event/task into six DB; "
            "inbox=personal todos/overdue; defects=inspection findings; "
            "kb=notes/knowledge questions; chat=greeting/chitchat only.\n"
            "For 'this week/本周/这礼拜' set relative=this_week. Prefer work over chat for my work/plans/tasks."
        )
        content = chat_completion(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": question}],
            json_mode=True, max_tokens=96, timeout=config.S1_TIMEOUT, model=config.S1_MODEL)
        raw = (content or "")[:200]
        obj = extract_json(content) if content else None
        understanding = validate_understanding(obj) if obj else None
    ms = int((time.time() - t0) * 1000)
    if understanding:
        trace("intent_systemone", quiet, intent=understanding["intent"],
              relative=understanding.get("relative"), range=understanding.get("range"),
              entities=understanding.get("entities"), duration_ms=ms, model=config.S1_MODEL)
    else:
        trace("intent_systemone_fail", quiet, duration_ms=ms, model=config.S1_MODEL, raw=raw)
    return understanding, ms
