#!/usr/bin/env python3
"""mini-agent：最小智能体（单文件、仅 Python 标准库）。

流程：System 1 前置路由（config.json 关键词/正则 → Laya /predict → 极短模型分类）
→ 工具循环（模型每步只输出 JSON）→ 规则校验防胡说 → trace 落 stderr 与 sandbox/trace.jsonl。
kb 意图走确定性流程（kb_search → kb_read → 模型仅据摘录作答），知识库只读。
work 意图走确定性流程（解析日期 → 分页拉取/去重/统计 → 模型仅据摘要措辞），工作数据只读。
inbox 意图走确定性流程（解析 Inbox.md → 过滤待办/超期 → 模型仅据摘要措辞），只读。
defects 意图走确定性流程（解析过滤条件 → 分页拉取/去重/统计 → 模型仅据摘要措辞），缺陷记录只读。
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
INTENTS = ["chat", "file", "shell", "web", "note", "kb", "work", "inbox", "defects", "out_of_scope"]

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

# Router / System One（主理解层；答题仍用 MODEL）
ROUTER_MODE = os.environ.get("MINI_AGENT_ROUTER_MODE", "hybrid").strip().lower() or "hybrid"
S1_MODEL = os.environ.get("MINI_AGENT_S1_MODEL", "tev1:0.8b").strip() or "tev1:0.8b"
S1_TIMEOUT = float(os.environ.get("MINI_AGENT_INTENT_TIMEOUT", "8"))
# Decision API (/v1/systemone). Default = same host as BASE_URL.
_s1_ep = os.environ.get("MINI_AGENT_S1_ENDPOINT", "").strip()
if _s1_ep:
    S1_ENDPOINT = _s1_ep.rstrip("/")
else:
    # BASE_URL like http://host:11434/v1 -> http://host:11434/v1/systemone
    S1_ENDPOINT = (BASE_URL.rstrip("/") + "/systemone") if BASE_URL else ""
TODAY_OVERRIDE = os.environ.get("MINI_AGENT_TODAY", "").strip()  # YYYY-MM-DD 测相对日期
# System One 解析结果提示（供 run_work/inbox/defects 使用）
UNDERSTANDING = None  # dict | None

# 缺陷检索：只读；fixture 或与 work 共用的 six-unified MCP（可选 DEFECTS_* 覆盖）
_DEFECTS_FIXTURE_ENV = os.environ.get("MINI_AGENT_DEFECTS_FIXTURE", "").strip()
DEFECTS_FIXTURE = Path(_DEFECTS_FIXTURE_ENV).expanduser().resolve() if _DEFECTS_FIXTURE_ENV else None
DEFECTS_URL = (os.environ.get("MINI_AGENT_DEFECTS_URL", "").strip() or WORK_URL)
DEFECTS_AUTH = (os.environ.get("MINI_AGENT_DEFECTS_AUTH", "").strip() or WORK_AUTH)  # 绝不打印
DEFECTS_TIMEOUT = WORK_TIMEOUT

# Inbox：只读 Markdown 任务列表；优先 MINI_AGENT_INBOX，否则在 KB 候选路径查找
_INBOX_ENV = os.environ.get("MINI_AGENT_INBOX", "").strip()
INBOX_PATH = Path(_INBOX_ENV).expanduser().resolve() if _INBOX_ENV else None
INBOX_TODAY = os.environ.get("MINI_AGENT_INBOX_TODAY", "").strip()  # YYYY-MM-DD 可选固定今天
INBOX_CANDIDATES = ["01_AREAS/Inbox.md", "01_AREAS/tasks/🔥 Inbox.md", "Inbox.md"]
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
def chat_completion(messages, json_mode=True, max_tokens=None, timeout=None, model=None):
    """调 OpenAI 兼容 /chat/completions（temperature=0，小 max_tokens）。
    附 response_format=json_object；服务器报 4xx 则去掉后重试一次。连接层失败标记模型不可用。
    model= 可覆盖全局 MODEL（System One 用 S1_MODEL）。"""
    global model_available
    if not model_available:
        return None
    use_model = (model or os.environ.get("MINI_AGENT_MODEL_OVERRIDE_S1") or MODEL or "").strip()
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens or MAX_TOKENS}
    if use_model:
        body["model"] = use_model
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    # Per-call timeout: S1 parse should not wait for answer-model TIMEOUT
    req_timeout = TIMEOUT if timeout is None else timeout
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
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            return data["choices"][0]["message"].get("content") or ""
        except urllib.error.HTTPError as exc:
            if use_rf and 400 <= exc.code < 500:
                use_rf = False  # 去掉 response_format 重试
                continue
            return None
        except Exception:
            # Do not mark answer model unavailable when a temporary S1 override fails
            if not model and not os.environ.get("MINI_AGENT_MODEL_OVERRIDE_S1"):
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
        return "kb" if KB else None
    if re.search(r"(?i)defect|finding|缺陷|不符合", q):
        return "defects"
    if relative_date_range(q) and re.search(r"(?i)work|plan|task|工作|计划|安排|忙", q):
        return "work"
    return None


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

def ref_today():
    """返回今天 YYYY-MM-DD（可用 MINI_AGENT_TODAY 覆盖）。"""
    if TODAY_OVERRIDE and re.fullmatch(r"\d{4}-\d{2}-\d{2}", TODAY_OVERRIDE):
        return TODAY_OVERRIDE
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


S1_INTENTS = {"kb", "work", "inbox", "defects", "chat", "note", "file", "shell", "web", "out_of_scope"}
S1_RELATIVE = {"this_week", "last_week", "next_week", "this_month", "last_month",
               "next_month", "today", "yesterday", "tomorrow", None, "null"}


def validate_understanding(obj):
    """校验 System One JSON；合法返回规范化 dict，否则 None。"""
    if not isinstance(obj, dict):
        return None
    intent = obj.get("intent")
    if intent not in S1_INTENTS:
        return None
    relative = obj.get("relative")
    if relative in ("null", ""):
        relative = None
    if relative not in S1_RELATIVE:
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
    if not S1_ENDPOINT:
        return None
    body = {
        "model": (model or S1_MODEL),
        "state": state,
        "questions": questions,
    }
    req = urllib.request.Request(
        S1_ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout if timeout is not None else S1_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        return data.get("answers") if isinstance(data, dict) else None
    except Exception:
        return None


S1_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "Which intent best matches the user request?",
        "criteria": {
            "work": "Job schedule, work plans, what I was busy with at work",
            "inbox": "Personal todos, overdue tasks, inbox items",
            "defects": "Inspection findings / defect records search",
            "kb": "Notes or knowledge-base questions",
            "chat": "Greeting or chitchat only",
            "note": "User wants to record a short note",
            "file": "Read or list local files",
            "shell": "Run a shell command",
            "web": "Look something up on the web",
            "out_of_scope": "Request outside supported capabilities",
        },
    },
    "relative": {
        "type": "choice",
        "instructions": "Which relative date range does the request refer to?",
        "criteria": {
            "this_week": "This calendar week / this week / 本周 / 这周 / 这礼拜",
            "last_week": "Last week / 上周",
            "next_week": "Next week / 下周",
            "this_month": "This month / 本月",
            "last_month": "Last month / 上月",
            "next_month": "Next month / 下月",
            "today": "Today / 今天",
            "yesterday": "Yesterday / 昨天",
            "tomorrow": "Tomorrow / 明天",
            "none": "No relative date mentioned",
        },
    },
    "overdue_only": {
        "type": "noul",
        "instructions": "Does the user ask specifically for overdue items?",
        "criteria": {"true": "Asks for overdue", "false": "Does not"},
    },
    "level": {
        "type": "choice",
        "instructions": "If asking about defects/findings, which severity level?",
        "criteria": {
            "serious": "Serious / 严重",
            "major": "Major / 主要",
            "minor": "Minor / 一般",
            "none": "Not about defect severity / not a defects query",
        },
    },
}


def system_one_understand(question, quiet=True):
    """System One 主理解：优先 tev1 /v1/systemone；失败则 chat JSON 回退。返回 (understanding|None, duration_ms)。"""
    t0 = time.time()
    answers = call_systemone(question, S1_QUESTIONS, model=S1_MODEL, timeout=S1_TIMEOUT)
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
        raw = json.dumps(answers, ensure_ascii=False)[:200]
    if understanding is None:
        # Fallback: constrained JSON via chat (for non-decision models like qwen2.5:0.5b / 14b)
        sys_prompt = (
            "You are a request router (System One). Output ONLY one JSON object, no markdown.\n"
            'Schema: {"intent":"kb|work|inbox|defects|chat|note|file|shell|web|out_of_scope",'
            '"relative":"this_week|last_week|next_week|this_month|last_month|next_month|today|yesterday|tomorrow|null",'
            '"range":null or {"start":"YYYY-MM-DD","end":"YYYY-MM-DD"},'
            '"entities":{"company":null,"level":"serious|major|minor|null","keywords":[],'
            '"status":"pending|completed|all|null","overdue_only":false}}\n'
            "intent guide: work=job schedule/plans/what I was busy with; "
            "inbox=personal todos/overdue; defects=inspection findings; "
            "kb=notes/knowledge questions; chat=greeting/chitchat only.\n"
            "For 'this week/本周/这礼拜' set relative=this_week. Prefer work over chat for my work/plans/tasks."
        )
        content = chat_completion(
            [{"role": "system", "content": sys_prompt},
             {"role": "user", "content": question}],
            json_mode=True, max_tokens=96, timeout=S1_TIMEOUT, model=S1_MODEL)
        raw = (content or "")[:200]
        obj = extract_json(content) if content else None
        understanding = validate_understanding(obj) if obj else None
    ms = int((time.time() - t0) * 1000)
    if understanding:
        trace("intent_systemone", quiet, intent=understanding["intent"],
              relative=understanding.get("relative"), range=understanding.get("range"),
              entities=understanding.get("entities"), duration_ms=ms, model=S1_MODEL)
    else:
        trace("intent_systemone_fail", quiet, duration_ms=ms, model=S1_MODEL, raw=raw)
    return understanding, ms


def parse_work_range(question):
    """从问题确定性解析 (start_date, end_date)；失败返回 None。"""
    q = (question or "").strip()
    # System One / UNDERSTANDING hint
    global UNDERSTANDING
    if UNDERSTANDING and isinstance(UNDERSTANDING.get("range"), dict):
        r = UNDERSTANDING["range"]
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



# ---------------------------------------------------------------- Inbox 待办（只读）
def resolve_inbox_path():
    """返回 Inbox.md Path；优先 MINI_AGENT_INBOX，否则 KB 候选路径。"""
    if INBOX_PATH and INBOX_PATH.is_file():
        return INBOX_PATH
    if KB and KB.is_dir():
        for rel in INBOX_CANDIDATES:
            p = KB / rel
            if p.is_file():
                return p.resolve()
    return None


def inbox_today():
    if INBOX_TODAY and re.fullmatch(r"\d{4}-\d{2}-\d{2}", INBOX_TODAY):
        return INBOX_TODAY
    return datetime.now().strftime("%Y-%m-%d")


_TASK_LINE_RX = re.compile(r"^([-*]|\d+\.)\s*\[([ xX/])\]\s*(.*)$")
_TAG_RX = re.compile(r"#([\w\u4e00-\u9fff-]+)", re.UNICODE)
_WORK_TAGS = {"work", "six", "case", "gmp"}


def parse_inbox_line(line, line_no, today):
    """解析单行任务；非任务行返回 None。"""
    m = _TASK_LINE_RX.match(line.strip())
    if not m:
        return None
    mark, body = m.group(2), m.group(3)
    is_completed = mark in ("x", "X")
    done_m = re.search(r"✅\s*(\d{4}-\d{2}-\d{2})", body)
    plan_m = re.search(r"📅\s*(\d{4}-\d{2}-\d{2})", body)
    done_date = done_m.group(1) if done_m else None
    plan_date = plan_m.group(1) if plan_m else None
    tags = _TAG_RX.findall(body)
    tags_l = [t.lower() for t in tags]
    is_work = any(t in _WORK_TAGS for t in tags_l)
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
    title = _TAG_RX.sub("", title)
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
        json_mode=False, max_tokens=max(MAX_TOKENS, 500),
        timeout=max(TIMEOUT, 60))
    answer = (content or "").strip()
    ground = payload + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or ("查不到" in answer and len(answer) < 40):
        answer = format_inbox_summary_text(result)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_inbox_summary_text(result)
    trace("final", quiet, intent="inbox", tools=tools_used, answer=answer[:500])
    return {"intent": "inbox", "risk": "none", "tools_used": tools_used, "answer": answer}


# ---------------------------------------------------------------- 缺陷检索（只读）
DEFECTS_LEVEL_MAP = [("严重", "serious"), ("重大", "major"), ("主要", "major"),
                     ("次要", "minor"), ("轻微", "minor"), ("一般", "minor")]
DEFECTS_ALLOWED_ARGS = {"company_name", "company_id", "level", "law",
                        "content_keywords", "start_date", "end_date", "limit", "offset"}
_DEFECTS_VERBS = ("请问", "帮我", "我想", "想知道", "查询", "查找", "查一下", "查看",
                  "检索", "搜索", "统计", "列出", "看看", "了解", "关于", "查", "看")
_DEFECTS_NAME_SUFFIX_RX = re.compile(r"[甲乙丙丁戊己庚辛壬癸0-9A-Za-z一二三四五六七八九十]{1,2}")


def _defects_clean_company(name):
    """去掉开头的查询动词，保留实体名；过短则视为未提取到。"""
    for v in _DEFECTS_VERBS:
        if name.startswith(v):
            name = name[len(v):]
            break
    return name if len(name) >= 3 else None


def map_defects_level(text):
    """中文级别 → serious/major/minor；无数值级别返回 None。"""
    for zh, en in DEFECTS_LEVEL_MAP:
        if zh in (text or ""):
            return en
    return None


def parse_defects_filters(question):
    """确定性解析企业名/级别/关键词/日期区间；保守提取，宁缺毋滥。"""
    global UNDERSTANDING
    _s1 = UNDERSTANDING if isinstance(UNDERSTANDING, dict) else None
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
            if _DEFECTS_NAME_SUFFIX_RX.fullmatch(nxt):
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
    raw = json.loads(DEFECTS_FIXTURE.read_text(encoding="utf-8"))
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
    if DEFECTS_AUTH:
        headers["Authorization"] = DEFECTS_AUTH
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "defects_search_defects", "arguments": arguments}}
    req = urllib.request.Request(DEFECTS_URL, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=DEFECTS_TIMEOUT) as resp:
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
    filters = {k: v for k, v in (filters or {}).items() if k in DEFECTS_ALLOWED_ARGS}
    if DEFECTS_FIXTURE and DEFECTS_FIXTURE.is_file():
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
    if not DEFECTS_URL:
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
    if not ((DEFECTS_FIXTURE and DEFECTS_FIXTURE.is_file()) or DEFECTS_URL):
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
        json_mode=False, max_tokens=max(MAX_TOKENS, 500),
        timeout=max(TIMEOUT, DEFECTS_TIMEOUT))
    answer = (content or "").strip()
    ground = summary_json + "\n" + question
    checked = re.sub(r"(?m)^\s*\d+[.、)]\s", "", answer)
    if not answer or ("查不到" in answer and len(answer) < 40):
        answer = format_defects_summary_text(summary)
    elif any(n not in ground for n in re.findall(r"\d+(?:\.\d+)?", checked)):
        answer = format_defects_summary_text(summary)
    trace("final", quiet, intent="defects", tools=tools_used, answer=answer[:500])
    return {"intent": "defects", "risk": "none", "tools_used": tools_used, "answer": answer}


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
    global UNDERSTANDING
    UNDERSTANDING = None
    danger = match_danger(question)
    if danger:  # 危险词直接拦截，不进模型
        rule_intent = classify_rules(question)
        intent = rule_intent or "out_of_scope"
        trace("danger", quiet, intent=intent, word=danger)
        return {"intent": intent, "risk": "danger", "tools_used": [],
                "answer": DANGER_RESPONSE}

    # Hybrid: 高置信规则快路径 → 否则 System One 主理解 → 软兜底
    hc = high_confidence_rule(question)
    s1_ms = 0
    if ROUTER_MODE == "rule":
        intent = classify_rules(question) or soft_intent_fallback(question) or "chat"
        trace("intent_rules", quiet, intent=intent, mode="rule")
    elif hc and ROUTER_MODE in ("hybrid", "rule"):
        intent, reason = hc
        trace("intent_rules", quiet, intent=intent, reason=reason, high_confidence=True)
    else:
        # System One primary (hybrid/systemone)
        understanding, s1_ms = (None, 0)
        if ROUTER_MODE in ("hybrid", "systemone"):
            understanding, s1_ms = system_one_understand(question, quiet)
        if understanding:
            UNDERSTANDING = understanding
            intent = understanding["intent"]
        else:
            intent = classify_rules(question) or soft_intent_fallback(question)
            if intent:
                trace("intent_fallback", quiet, intent=intent, after_s1_ms=s1_ms)
            elif (intent := laya_predict(question)) is not None:
                trace("intent_laya", quiet, intent=intent)
            elif kb_fallback(question):
                intent = "kb"
                trace("intent_kb_fallback", quiet, intent=intent)
            else:
                intent = "chat"
                trace("intent_chat_default", quiet, intent=intent, after_s1_ms=s1_ms)

    if intent == "kb":  # 确定性流程：模型不参与工具选择
        return run_kb(question, quiet)
    if intent == "work":
        return run_work(question, quiet)
    if intent == "inbox":
        return run_inbox(question, quiet)
    if intent == "defects":
        return run_defects(question, quiet)

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
