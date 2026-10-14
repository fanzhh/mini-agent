"""mini_agent.model：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from . import state
import json
import os
import urllib.error
import urllib.request

def chat_completion(messages, json_mode=True, max_tokens=None, timeout=None, model=None):
    """调 OpenAI 兼容 /chat/completions（temperature=0，小 max_tokens）。
    附 response_format=json_object；服务器报 4xx 则去掉后重试一次。连接层失败标记模型不可用。
    model= 可覆盖全局 MODEL（System One 用 S1_MODEL）。"""
    if not state.model_available:
        return None
    use_model = (model or os.environ.get("MINI_AGENT_MODEL_OVERRIDE_S1") or config.MODEL or "").strip()
    body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens or config.MAX_TOKENS}
    if use_model:
        body["model"] = use_model
    headers = {"Content-Type": "application/json"}
    if config.API_KEY:
        headers["Authorization"] = "Bearer " + config.API_KEY
    # Per-call timeout: S1 parse should not wait for answer-model TIMEOUT
    req_timeout = config.TIMEOUT if timeout is None else timeout
    use_rf = json_mode
    while True:
        if use_rf:
            body["response_format"] = {"type": "json_object"}
        else:
            body.pop("response_format", None)
        req = urllib.request.Request(config.BASE_URL + "/chat/completions",
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
                state.model_available = False
            return None
