"""mini-agent：最小智能体（工具调用 + 防胡说）。曾为单文件，现按职责拆分为多模块。"""

from . import config, state, trace, jsonutil, model, router, tools, knowledge, work, inbox, defects, events, skills, agent, cli
from .agent import run_agent, run_web, fallback_answer, validate_answer
from .cli import main, repl
from .model import chat_completion
from .router import (classify_rules, high_confidence_rule, system_one_understand, call_systemone, relative_date_range)
from .jsonutil import extract_json
from .trace import trace
from .tools import safe_resolve
from .knowledge import run_kb
from .work import parse_work_range, _month_bounds, summarize_work_rows, query_work_range, run_work
from .inbox import resolve_inbox_path, parse_inbox_line, query_inbox_tasks, run_inbox
from .defects import (map_defects_level, parse_defects_filters, dedupe_defect_rows, query_defects, summarize_defects, run_defects)
from .events import parse_add_event, build_create_task_args, run_add_event
from .skills import looks_like_missing_skill, run_missing_skill, list_wishlist, append_wishlist
