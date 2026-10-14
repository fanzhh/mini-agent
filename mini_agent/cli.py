"""mini_agent.cli：由单文件版机械拆分而来（函数体逐字保留）。"""
from . import config
from .agent import run_agent
from .skills import format_wishlist_listing, list_wishlist
import argparse
import json
import sys

def repl(quiet=False, yes=False):
    print("mini-agent REPL（输入 exit 退出）", file=sys.stderr)
    # 终端偶发送来被截断的多字节字符（如粘贴打断），strict 解码会抛 UnicodeDecodeError
    # 直接崩掉整个 REPL。把 stdin/stdout 重配成 replace，坏字节替换成 U+FFFD 而不中断。
    for _stream in (sys.stdin, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    # 方向键（↑↓ 翻历史、←→ 移光标）不是 mini-agent 的能力，是 readline 提供的。
    # 裸 input() 把 ANSI 转义（\x1b[A）当字符读，屏幕就会看到 ^[[A 之类的乱码。
    # 导入即生效（mac 上是 libedit 兼容层）；Windows/精简环境可能没有，降级继续跑。
    hist_path = None
    try:
        import readline as _readline  # noqa: F401
        try:
            hist_path = config.SANDBOX / ".mini-agent_history"
            hist_path.parent.mkdir(parents=True, exist_ok=True)
            if hist_path.is_file():
                _readline.read_history_file(str(hist_path))
            _readline.set_history_length(200)
        except (OSError, ValueError):
            hist_path = None
    except ImportError:
        _readline = None
    while True:
        try:
            line = input("mini-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        except UnicodeDecodeError:
            print("（输入编码异常，已跳过这一行）", file=sys.stderr)
            continue
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            break
        print(run_agent(line, quiet, yes=yes,
                        confirm_fn=(None if yes else input))["answer"])
    if _readline is not None and hist_path is not None:
        try:
            _readline.write_history_file(str(hist_path))
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="最小智能体（OpenAI 兼容接口 + 工具循环）")
    ap.add_argument("question", nargs="?", help='问题；缺省进入 REPL')
    ap.add_argument("--json", action="store_true", help='输出最终 JSON：{intent,risk,tools_used,answer}')
    ap.add_argument("--quiet", action="store_true", help="关闭 stderr trace")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="add_event 非交互确认写入（否则只预览）")
    ap.add_argument("--wishlist", action="store_true", help="列出本地愿望清单后退出")
    args = ap.parse_args()
    config.SANDBOX.mkdir(parents=True, exist_ok=True)
    if args.wishlist:
        text = format_wishlist_listing(list_wishlist())
        print(text)
        return
    if args.question is None:
        repl(args.quiet, yes=args.yes)
        return
    result = run_agent(args.question, args.quiet, yes=args.yes,
                       confirm_fn=None)  # 非交互：无 --yes 则只预览
    if args.json:
        out = {"intent": result["intent"], "risk": result["risk"],
               "tools_used": result["tools_used"],
               "answer": result["answer"]}
        if result.get("preview") is not None:
            out["preview"] = result["preview"]
        print(json.dumps(out, ensure_ascii=False))
    else:
        print(result["answer"])
