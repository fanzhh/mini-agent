"""mini_agent.tools：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from .knowledge import kb_read, kb_search
from datetime import datetime
from pathlib import Path
import json
import re
import shlex
import subprocess
import urllib.parse
import urllib.request

def t_kb_search(args):
    return json.dumps(kb_search(args.get("query") or args.get("q") or ""),
                      ensure_ascii=False)


def t_kb_read(args):
    return kb_read(args.get("path") or "", args.get("query") or args.get("q") or "")


def _web_query(q):
    """从提问里剥掉搜索口令词，留下真正要搜的关键词。"""
    t = re.sub(r"(?i)搜(一下|索)?|查一下|帮我查|帮我搜|一下|look ?up|search(?:\s+for)?",
               " ", str(q or ""))
    t = t.strip("：:，,。.?？ \t")
    return t


def safe_resolve(path):
    """把路径解析到沙箱内；越权（含 .. 逃逸）抛 PermissionError。"""
    p = Path(path)
    p = p.resolve() if p.is_absolute() else (config.SANDBOX / p).resolve()
    if p != config.SANDBOX and config.SANDBOX not in p.parents:
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
            hits.append(str(p.relative_to(config.SANDBOX)))
    return "\n".join(hits[:50]) or "（未找到匹配文件）"


def t_run_shell(args):
    command = args.get("command") or args.get("cmd") or ""
    if not command or "\n" in command:
        raise ValueError("非法命令")
    for ch in "|;&<>`$":  # 禁止管道、重定向、后台、命令替换
        if ch in command:
            raise ValueError("禁止管道/重定向/控制字符")
    parts = shlex.split(command)
    if not parts or parts[0] not in config.SHELL_WHITELIST:
        raise ValueError("命令不在白名单内")
    for arg in parts[1:]:
        safe_resolve(arg)  # 参数也必须在沙箱内（防 .. 逃逸与绝对路径越权）
    proc = subprocess.run(parts, cwd=str(config.SANDBOX), capture_output=True,
                          text=True, timeout=10)
    out = (proc.stdout + proc.stderr).strip()
    return out[:4000] or "(无输出)"


def t_web_fetch(args):
    url = args.get("url") or args.get("u") or ""
    if not url.startswith(("http://", "https://")):
        raise ValueError("仅支持 http(s) URL")
    req = urllib.request.Request(url, headers={"User-Agent": "mini-agent/1.0"})
    with urllib.request.urlopen(req, timeout=config.TIMEOUT) as resp:
        raw = resp.read(200000)
    text = raw.decode("utf-8", "ignore")
    return text[:4000] + ("...[截断]" if len(text) > 4000 else "")


def _clean_url(url):
    """去掉跟踪/重定向 query（夸克 page.sm.cn、百度跳转等），保留可读的 host+path。"""
    url = (url or "").strip()
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
        # 只对已知聚合/跟踪域清 query；普通站点保留 query（很多是真内容路径参数）
        hosts = ("page.sm.cn", "m.baidu.com", "www.baidu.com", "link.zhihu.com")
        if p.netloc in hosts and p.path.count("/") > 1:
            # 这类 URL 真实目标常在 query 里（h=/url=），优先抽出来
            q = urllib.parse.parse_qs(p.query)
            for k in ("url", "h", "target", "u"):
                if q.get(k):
                    tgt = q[k][0]
                    if tgt.startswith("http"):
                        return _clean_url(tgt)
            return p.scheme + "://" + p.netloc + p.path
        if p.query and len(p.query) > 120:
            return p.scheme + "://" + p.netloc + p.path
    except Exception:
        pass
    return url


def _searxng_search(query):
    """调 SearXNG，返回解析后的 JSON dict；网络/解析错误向上抛。"""
    url = f"{config.SEARXNG_URL}/search?q={urllib.parse.quote(query)}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "mini-agent/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=config.SEARXNG_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _render_searxng(data):
    """把 SearXNG JSON 压成小模型读得动的纯文本。返回 (命中条数, 文本)。"""
    results = data.get("results") or []
    lines, seen = [], set()
    for r in results:
        if not isinstance(r, dict):
            continue
        url = _clean_url(r.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = (r.get("title") or "").strip() or "（无标题）"
        snippet = re.sub(r"\s+", " ", (r.get("content") or "")).strip()
        line = f"{len(lines) + 1}. {title}\n   {url}"
        if snippet:
            line += f"\n   {snippet[:240]}"
        lines.append(line)
        if len(lines) >= config.SEARXNG_MAX_RESULTS:
            break
    found = len(lines)
    if found:
        return found, "\n".join(lines)
    sug = data.get("suggestions") or []
    unresp = data.get("unresponsive_engines") or []
    if unresp:
        return 0, "搜索引擎暂时都没返回结果（多为上游被验证码/限流所致）。" + (
            "相关搜索建议：" + "、".join(str(s) for s in sug[:6]) if sug else "换个更完整的关键词再试。")
    if sug:
        return 0, "没有找到网页结果。相关搜索建议：" + "、".join(str(s) for s in sug[:6])
    return 0, "没有找到网页结果。"


def t_web_search(args):
    """通过 HK VPS 上的 SearXNG 搜索网页，返回压缩后的纯文本结果（小模型友好）。"""
    query = (args.get("query") or args.get("q") or args.get("keyword") or "").strip()
    if not query:
        raise ValueError("缺少搜索关键词")
    if not config.SEARXNG_URL:
        raise ValueError("未配置搜索服务（MINI_AGENT_SEARXNG_URL）")
    _found, text = _render_searxng(_searxng_search(query))
    return text


def t_note_append(args):
    content = args.get("content") or args.get("note") or args.get("text")
    if not content:
        raise ValueError("缺少笔记内容")
    config.SANDBOX.mkdir(parents=True, exist_ok=True)
    with open(config.SANDBOX / "notes.md", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {content}\n")
    return "已追加到 notes.md"

TOOLS = {"read_file": t_read_file, "search_files": t_search_files,
         "run_shell": t_run_shell, "web_fetch": t_web_fetch, "web_search": t_web_search,
         "note_append": t_note_append, "kb_search": t_kb_search,
         "kb_read": t_kb_read}

