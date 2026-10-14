"""mini_agent.agent：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
from .defects import run_defects
from .events import run_add_event
from .inbox import run_inbox
from .jsonutil import extract_json, feedback_for, valid_step
from .knowledge import run_kb
from .model import chat_completion
from .router import classify_rules, high_confidence_rule, kb_fallback, laya_predict, match_danger, soft_intent_fallback, system_one_understand
from .skills import looks_like_missing_skill, run_missing_skill
from .tools import _render_searxng, _searxng_search, _web_query, safe_resolve, t_note_append, t_run_shell, t_web_fetch, t_web_search
from .trace import trace
from .work import run_work
import json
import re

def run_web(question, quiet):
    """web 意图确定性流程（模型不参与工具选择）：有网址→抓原文；否则 SearXNG 搜索。

    关键：搜到结果就**绝不**以「查不到」收尾——模型总结失败就直接把清单呈现给检查员。
    （实测把结果交给通用工具循环里的 tev1:0.8b，它会无视有效结果直接答「查不到」。）
    """
    q = str(question or "").strip()
    m = re.search(r"https?://\S+", q)
    if m:  # 用户给了具体网址 → 抓该页原文
        try:
            raw = t_web_fetch({"url": m.group(0)})
            trace("tool_result", quiet, step=1, tool="web_fetch", result=raw[:1000])
            return {"intent": "web", "risk": "none", "tools_used": ["web_fetch"],
                    "answer": "网页内容：\n" + raw}
        except Exception as exc:
            trace("tool_error", quiet, step=1, tool="web_fetch",
                  result=f"{type(exc).__name__}: {exc}")
            return {"intent": "web", "risk": "none", "tools_used": ["web_fetch"],
                    "answer": f"抓网页失败：{type(exc).__name__}: {exc}"}

    kw = _web_query(q)
    if not kw:
        kw = q
    if not config.SEARXNG_URL:
        return {"intent": "web", "risk": "none", "tools_used": [],
                "answer": "查不到：未配置搜索服务（请设置 MINI_AGENT_SEARXNG_URL）。"}
    try:
        data = _searxng_search(kw)
    except Exception as exc:
        trace("tool_error", quiet, step=1, tool="web_search",
              result=f"{type(exc).__name__}: {exc}")
        return {"intent": "web", "risk": "none", "tools_used": ["web_search"],
                "answer": f"搜索服务不可达：{type(exc).__name__}: {exc}"}
    found, text = _render_searxng(data)
    trace("tool_result", quiet, step=1, tool="web_search", result=text[:1000])
    if found == 0:
        return {"intent": "web", "risk": "none", "tools_used": ["web_search"],
                "answer": text}

    # 搜到了：尝试让模型据结果总结；总结为空/仍说查不到 → 直接呈现清单（不因小模型偷懒而丢结果）
    sys_prompt = ("你在做网页搜索问答。只根据给出的搜索结果回答，简洁中文，"
                  "可附来源链接；结果里没有才说查不到，不要编造。")
    user_msg = (f"搜索结果如下（编号列表，含标题/链接/摘要）：\n{text}\n\n"
                f"问题：{question}\n请只用上面的结果简洁回答。")
    content = chat_completion(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_msg}], json_mode=False,
        max_tokens=max(config.MAX_TOKENS, 400), timeout=max(config.TIMEOUT, config.SEARXNG_TIMEOUT))
    ans = (content or "").strip()
    # 只在"小模型摆烂"时兜底：完全没输出、或光秃秃一句"查不到/不知道"（不含任何结果的短语）。
    # 长答案即便出现"没有找到"也不误伤（run_kb 里同款判据）。
    is_lazily_negative = (
        not ans or
        (len(ans) < 40 and re.search(r"查不到|不知道|没有找到|未找到", ans))
    )
    if is_lazily_negative:
        ans = f"为你找到以下结果：\n{text}"
    trace("final", quiet, intent="web", tools=["web_search"], answer=ans[:500])
    return {"intent": "web", "risk": "none", "tools_used": ["web_search"], "answer": ans}


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
            for p in sorted(config.SANDBOX.rglob("*")):
                if p.is_file() and any(t.lower() in p.name.lower() for t in tokens):
                    cand = p
                    break
        if cand is not None:
            content = cand.read_text(encoding="utf-8", errors="replace")[:2000]
            evidence = f"read_file({cand.name}) = {content}"
            return f"根据文件 {cand.name} 的内容：{content}", evidence
        return "查不到相关信息", ""
    if intent == "shell":
        m = re.search(r"\b(" + "|".join(config.SHELL_WHITELIST) + r")\b", q)
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
        # 没给具体网址 → 当作关键词搜索（服务端压缩结果，规则兜底也能给出条目）
        kw = _web_query(q) or q
        if kw:
            try:
                out = t_web_search({"query": kw})
                return "搜索结果：\n" + out, f"web_search({kw}) = {out}"
            except Exception:
                pass
        return "查不到相关信息", ""
    if intent == "note":
        text = q
        for kw in config.CFG["intents"]["note"].get("keywords", []):
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
    if any(kw.lower() in q.lower() for kw in config.CFG["intents"]["chat"].get("keywords", [])):
        return "你好！有什么可以帮你的吗？", ""
    return "不知道，我无法回答这个问题。", ""


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


def tool_msg(tool, result, question):
    """工具结果用普通 user 消息回传：很多本地服务（如 Ollama）不认没有 tool_call_id 的 tool 角色。"""
    return (f"工具 {tool} 返回的内容如下：\n<<<\n{result}\n>>>\n"
            f"请只根据上面的内容回答原问题：{question}\n内容里确实没有答案才答“查不到”。")


def run_agent(question, quiet=False, yes=False, confirm_fn=None):
    state.UNDERSTANDING = None
    danger = match_danger(question)
    if danger:  # 危险词直接拦截，不进模型
        rule_intent = classify_rules(question)
        intent = rule_intent or "out_of_scope"
        trace("danger", quiet, intent=intent, word=danger)
        return {"intent": intent, "risk": "danger", "tools_used": [],
                "answer": config.DANGER_RESPONSE}

    # Hybrid: 高置信规则快路径 → 否则 System One 主理解 → 软兜底
    hc = high_confidence_rule(question)
    s1_ms = 0
    if config.ROUTER_MODE == "rule":
        intent = classify_rules(question) or soft_intent_fallback(question) or "chat"
        trace("intent_rules", quiet, intent=intent, mode="rule")
    elif hc and config.ROUTER_MODE in ("hybrid", "rule"):
        intent, reason = hc
        trace("intent_rules", quiet, intent=intent, reason=reason, high_confidence=True)
    else:
        # System One primary (hybrid/systemone)
        understanding, s1_ms = (None, 0)
        if config.ROUTER_MODE in ("hybrid", "systemone"):
            understanding, s1_ms = system_one_understand(question, quiet)
        if understanding:
            state.UNDERSTANDING = understanding
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

    # 可行动但无工具：诚实说没有技能 + wishlist（如天气；优先于 kb 误命中）
    if looks_like_missing_skill(question, intent):
        return run_missing_skill(question, quiet, intent=intent)

    if intent == "kb":  # 确定性流程：模型不参与工具选择
        return run_kb(question, quiet)
    if intent == "work":
        return run_work(question, quiet)
    if intent == "inbox":
        return run_inbox(question, quiet)
    if intent == "defects":
        return run_defects(question, quiet)
    if intent == "add_event":
        return run_add_event(question, quiet, yes=yes, confirm_fn=confirm_fn)
    if intent == "web":
        return run_web(question, quiet)

    if intent not in config.CFG.get("intents", {}):
        return run_missing_skill(question, quiet, intent=intent)

    spec = config.CFG["intents"][intent]
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

    for step in range(1, config.MAX_STEPS + 1):
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
            result = config.DANGER_RESPONSE
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
