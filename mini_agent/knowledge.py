"""mini_agent.knowledge：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from .model import chat_completion
from .trace import trace
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import time

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
        if len(tok) >= 2 and tok not in config.KB_STOP_EN:
            add(tok)
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        for part in config._KB_STOP_RX.split(run):
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
        proc = subprocess.run(cmd, cwd=str(config.KB), capture_output=True,
                              text=True, timeout=config.TIMEOUT)
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
    for dirpath, dirnames, filenames in os.walk(config.KB):
        dirnames[:] = [d for d in dirnames if d not in config.KB_SKIP_DIRS]
        for fn in sorted(filenames):
            if fn.lower().endswith(".md"):
                rels.append(str((Path(dirpath) / fn).relative_to(config.KB)))
    return rels


def _kb_score(rel, kws):
    """单文件打分：命中数(封顶 5) + 3×命中词数 + 文件名/标题命中各 5 + mtime 新鲜度。"""
    full = config.KB / rel
    try:
        if full.stat().st_size > config.KB_MAX_FILE:
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
    if not config.KB or not config.KB.is_dir():
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
    if not config.KB:
        raise PermissionError("未配置知识库")
    p = Path(path)
    if p.is_absolute() or ".." in p.parts:
        raise PermissionError("非法路径（仅限知识库内相对路径）")
    full = (config.KB / p).resolve()
    if full != config.KB and config.KB not in full.parents:
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


def run_kb(question, quiet):
    """kb 意图确定性流程（模型不做工具选择）：kb_search → kb_read top 1-2 → 模型据摘录作答并校验。"""
    tools_used = ["kb_search"]
    if not config.KB:
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
        max_tokens=max(config.MAX_TOKENS, 400), timeout=max(config.TIMEOUT, config.KB_TIMEOUT))
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
