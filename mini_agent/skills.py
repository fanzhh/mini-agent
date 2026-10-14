"""mini_agent.skills：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
from .trace import trace
from datetime import datetime
import json
import re

def wishlist_path():
    if config.WISHLIST_PATH is not None:
        return config.WISHLIST_PATH
    return (config.SANDBOX / "wishlist.jsonl").resolve()


def append_wishlist(question, intent=None, confidence=None):
    """追加一行 wishlist（ts, request, intent, confidence）。"""
    path = wishlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "request": (question or "").strip(),
        "intent": intent,
        "confidence": confidence,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def list_wishlist(limit=50):
    path = wishlist_path()
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"raw": line})
    return out


def looks_like_missing_skill(question, intent):
    """可行动但当前无匹配工具（如天气）。天气等硬缺失技能优先于 kb 误命中。"""
    q = (question or "").strip()
    if not q:
        return False
    # Hard missing skills: never fake via kb/chat/web
    if config._MISSING_SKILL_RX.search(q):
        if intent == "web" and re.search(r"https?://", q):
            return False
        return True
    if intent == "out_of_scope":
        return True
    return False


def run_missing_skill(question, quiet, intent=None):
    """诚实回复无此技能，并写入 wishlist。"""
    conf = None
    if state.UNDERSTANDING and isinstance(state.UNDERSTANDING, dict):
        intent = intent or state.UNDERSTANDING.get("intent")
        conf = state.UNDERSTANDING.get("confidence")
    rec = append_wishlist(question, intent=intent, confidence=conf)
    answer = (
        f"我还没有这个技能，无法回答「{question.strip()}」。"
        f"已记入愿望清单（wishlist），路径由 MINI_AGENT_WISHLIST 配置（默认沙箱 wishlist.jsonl）。"
    )
    trace("wishlist_append", quiet, intent=intent or "missing_skill", request=question[:200])
    trace("final", quiet, intent=intent or "out_of_scope", tools=[], answer=answer[:500])
    return {"intent": intent or "out_of_scope", "risk": "none", "tools_used": [],
            "answer": answer, "wishlist": rec}


def format_wishlist_listing(rows):
    if not rows:
        return "愿望清单为空。"
    lines = [f"愿望清单共 {len(rows)} 条（{wishlist_path().name}）："]
    for i, r in enumerate(rows, 1):
        if "raw" in r and len(r) == 1:
            lines.append(f"{i}. {r['raw']}")
            continue
        lines.append(
            f"{i}. [{r.get('ts','')}] intent={r.get('intent')} "
            f"conf={r.get('confidence')} | {r.get('request','')}"
        )
    return "\n".join(lines)
