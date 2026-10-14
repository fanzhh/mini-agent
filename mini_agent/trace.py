"""mini_agent.trace：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from datetime import datetime
import json
import sys

def trace(event, quiet, **data):
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event}
    rec.update(data)
    line = json.dumps(rec, ensure_ascii=False)
    if not quiet:
        print(line, file=sys.stderr)
    try:
        config.SANDBOX.mkdir(parents=True, exist_ok=True)
        with open(config.TRACE_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
