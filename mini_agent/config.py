"""配置与模块级常量：import 期从环境变量与 config.json 读取（config.json 已随包内置，
可用 MINI_AGENT_CONFIG 覆盖路径）。重算请 reload(this) —— 其余模块一律以 config.NAME 属性式
访问，reload 后立即可见（勿 from .config import NAME，那会拷贝值、脱离 reload）。
"""
import json
import os
import re
import time
import calendar
from datetime import datetime
from pathlib import Path

_env_cfg = os.environ.get("MINI_AGENT_CONFIG", "").strip()
CONFIG_PATH = Path(_env_cfg).resolve() if _env_cfg else Path(__file__).with_name("config.json")
INTENTS = ["chat", "file", "shell", "web", "note", "kb", "work", "inbox", "defects", "add_event", "out_of_scope"]
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)
BASE_URL = os.environ.get("MINI_AGENT_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/")
MODEL = os.environ.get("MINI_AGENT_MODEL", "")
API_KEY = os.environ.get("MINI_AGENT_API_KEY", "")  # 可空；绝不打印
SANDBOX = Path(os.environ.get("MINI_AGENT_SANDBOX", "./sandbox")).resolve()
LAYA_URL = os.environ.get("MINI_AGENT_LAYA_URL", "")
TIMEOUT = float(os.environ.get("MINI_AGENT_TIMEOUT", "10"))
MAX_STEPS = int(CFG.get("max_steps", 4))
SEARXNG_URL = os.environ.get("MINI_AGENT_SEARXNG_URL", "http://100.73.52.74:8088").strip().rstrip("/")
SEARXNG_TIMEOUT = float(os.environ.get("MINI_AGENT_SEARXNG_TIMEOUT", "12"))
SEARXNG_MAX_RESULTS = int(os.environ.get("MINI_AGENT_SEARXNG_MAX_RESULTS", "6"))
MAX_TOKENS = int(CFG.get("max_tokens", 256))
DANGER_WORDS = CFG.get("danger_words", [])
SHELL_WHITELIST = CFG.get("shell_whitelist", [])
DANGER_RESPONSE = CFG.get("danger_response", "已拦截：不可逆操作")
TRACE_PATH = SANDBOX / "trace.jsonl"
_KB_ENV = os.environ.get("MINI_AGENT_KB", "").strip()
KB = Path(_KB_ENV).expanduser().resolve() if _KB_ENV else None
KB_MAX_FILE = 2 * 1024 * 1024
KB_TIMEOUT = float(os.environ.get("MINI_AGENT_KB_TIMEOUT", "120"))
_WORK_FIXTURE = os.environ.get("MINI_AGENT_WORK_FIXTURE", "").strip()
WORK_FIXTURE = Path(_WORK_FIXTURE).expanduser().resolve() if _WORK_FIXTURE else None
WORK_URL = os.environ.get("MINI_AGENT_WORK_URL", "").strip()
WORK_AUTH = os.environ.get("MINI_AGENT_WORK_AUTH", "").strip()  # 完整 Authorization 值；绝不打印
WORK_TIMEOUT = float(os.environ.get("MINI_AGENT_WORK_TIMEOUT", "30"))
WORK_CREATE_SINK = os.environ.get("MINI_AGENT_WORK_CREATE_SINK", "").strip()
WORK_PERSON = os.environ.get("MINI_AGENT_WORK_PERSON", "").strip()  # 可选负责人
ADD_EVENT_TASK_TYPE = os.environ.get("MINI_AGENT_ADD_EVENT_TASK_TYPE", "other").strip() or "other"
ADD_EVENT_CREATE_BY = os.environ.get("MINI_AGENT_ADD_EVENT_CREATE_BY", "wee").strip() or "wee"
_WISHLIST_ENV = os.environ.get("MINI_AGENT_WISHLIST", "").strip()
WISHLIST_PATH = Path(_WISHLIST_ENV).expanduser().resolve() if _WISHLIST_ENV else None  # resolved later vs SANDBOX
ROUTER_MODE = os.environ.get("MINI_AGENT_ROUTER_MODE", "hybrid").strip().lower() or "hybrid"
S1_MODEL = os.environ.get("MINI_AGENT_S1_MODEL", "tev1:0.8b").strip() or "tev1:0.8b"
S1_TIMEOUT = float(os.environ.get("MINI_AGENT_INTENT_TIMEOUT", "8"))
_s1_ep = os.environ.get("MINI_AGENT_S1_ENDPOINT", "").strip()
if _s1_ep:
    S1_ENDPOINT = _s1_ep.rstrip("/")
else:
    # BASE_URL like http://host:11434/v1 -> http://host:11434/v1/systemone
    S1_ENDPOINT = (BASE_URL.rstrip("/") + "/systemone") if BASE_URL else ""
TODAY_OVERRIDE = os.environ.get("MINI_AGENT_TODAY", "").strip()  # YYYY-MM-DD 测相对日期
_DEFECTS_FIXTURE_ENV = os.environ.get("MINI_AGENT_DEFECTS_FIXTURE", "").strip()
DEFECTS_FIXTURE = Path(_DEFECTS_FIXTURE_ENV).expanduser().resolve() if _DEFECTS_FIXTURE_ENV else None
DEFECTS_URL = (os.environ.get("MINI_AGENT_DEFECTS_URL", "").strip() or WORK_URL)
DEFECTS_AUTH = (os.environ.get("MINI_AGENT_DEFECTS_AUTH", "").strip() or WORK_AUTH)  # 绝不打印
DEFECTS_TIMEOUT = WORK_TIMEOUT
_INBOX_ENV = os.environ.get("MINI_AGENT_INBOX", "").strip()
INBOX_PATH = Path(_INBOX_ENV).expanduser().resolve() if _INBOX_ENV else None
INBOX_TODAY = os.environ.get("MINI_AGENT_INBOX_TODAY", "").strip()  # YYYY-MM-DD 可选固定今天
INBOX_CANDIDATES = ["01_AREAS/Inbox.md", "01_AREAS/tasks/🔥 Inbox.md", "Inbox.md"]
KB_SKIP_DIRS = {".obsidian", ".git", "node_modules", "attachments"}
KB_STOP_EN = {"a", "an", "the", "is", "are", "was", "were", "be", "been",
              "of", "in", "on", "at", "to", "for", "with", "and", "or",
              "not", "no", "what", "how", "who", "why", "when", "where",
              "my", "i", "you", "it", "we", "they", "do", "did", "does"}
KB_STOP_ZH = ("什么", "怎么", "怎样", "我", "的", "吗", "呢", "了", "是",
              "在", "有", "哪些", "哪个", "多少", "知识库", "笔记", "里", "中",
              "写过", "记过", "关于", "一下", "请", "告诉", "我的", "文章",
              "和", "与", "么", "如何", "为什么")
_KB_STOP_RX = re.compile("|".join(sorted(set(KB_STOP_ZH), key=len, reverse=True)))
S1_INTENTS = {"kb", "work", "inbox", "defects", "add_event", "chat", "note", "file", "shell", "web", "out_of_scope"}
S1_RELATIVE = {"this_week", "last_week", "next_week", "this_month", "last_month",
               "next_month", "today", "yesterday", "tomorrow", None, "null"}
S1_QUESTIONS = {
    "intent": {
        "type": "choice",
        "instructions": "Which intent best matches the user request?",
        "criteria": {
            "work": "Query/list job schedule, work plans, what I was busy with (read-only)",
            "add_event": "Create/add a new work event or task into the six database (write)",
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
_TASK_LINE_RX = re.compile(r"^([-*]|\d+\.)\s*\[([ xX/])\]\s*(.*)$")
_TAG_RX = re.compile(r"#([\w\u4e00-\u9fff-]+)", re.UNICODE)
_WORK_TAGS = {"work", "six", "case", "gmp"}
DEFECTS_LEVEL_MAP = [("严重", "serious"), ("重大", "major"), ("主要", "major"),
                     ("次要", "minor"), ("轻微", "minor"), ("一般", "minor")]
DEFECTS_ALLOWED_ARGS = {"company_name", "company_id", "level", "law",
                        "content_keywords", "start_date", "end_date", "limit", "offset"}
_DEFECTS_VERBS = ("请问", "帮我", "我想", "想知道", "查询", "查找", "查一下", "查看",
                  "检索", "搜索", "统计", "列出", "看看", "了解", "关于", "查", "看")
_DEFECTS_NAME_SUFFIX_RX = re.compile(r"[甲乙丙丁戊己庚辛壬癸0-9A-Za-z一二三四五六七八九十]{1,2}")
_ADD_EVENT_TASK_TYPES = {
    "0check", "1case", "2sample", "3action", "4help", "5file",
    "6company", "7complaint", "8wlearn", "9info", "other",
}
_MISSING_SKILL_RX = re.compile(
    r"(?i)天气|气温|天气预报|weather|温度怎么样|"
    r"股价|股票|汇率|航班|订票|快递单号|彩票|"
    r"\b(stock\s*price|forecast|uber|taxi)\b"
)
if __name__ == "__main__":
    main()
