"""mini_agent.jsonutil：由单文件版机械拆分而来（函数体逐字保留）。"""
import json
import re

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
