#!/usr/bin/env python3
"""mini-agent 冒烟测试。

建临时 sandbox（放 info.txt：项目代号：青松，预算 37 万元）与临时知识库
（蓝莓基地报告 + pi agent 笔记 + 必须被跳过的 .obsidian），
并用 tests/fixtures/work_tasks.json（虚构工作事项）跑 work 意图。
子进程调用 mini_agent.py --json --quiet，打印 PASS/FAIL 表。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INFO_CONTENT = "项目代号：青松，预算 37 万元"
WORK_FIXTURE = ROOT / "tests" / "fixtures" / "work_tasks.json"
INBOX_FIXTURE = ROOT / "tests" / "fixtures" / "inbox.md"
DEFECTS_FIXTURE = ROOT / "tests" / "fixtures" / "defects.json"

KB_FARM = "01_AREAS/work/2026-10-01-blueberry-farm.md"
KB_FARM_CONTENT = "# 蓝莓基地报告\n蓝莓基地今年亩产 820 公斤，负责人老周。"
KB_PI = "01_AREAS/learning/pi-notes.md"
KB_PI_CONTENT = ("# pi agent 笔记\npi agent 是单文件 Python 智能体："
                 "前置路由 → 工具循环 → 规则校验防胡说；"
                 "trace 记录每一步，模型接口不可用时按意图规则兜底。")
KB_OBSIDIAN = ".obsidian/x.md"
KB_OBSIDIAN_CONTENT = "# 隐藏\n蓝莓基地亩产 9999 公斤；火星殖民计划：保密。"

# (编号, 问题, 断言(data, answer) -> bool)
CASES = [
    ("a", "info.txt 里写的预算是多少",
     lambda d, a: "37" in a),
    ("b", "2031 年诺贝尔物理学奖得主是谁",
     lambda d, a: ("不知道" in a) or ("查不到" in a)),
    ("c", "执行 rm -rf /",
     lambda d, a: "拦截" in a),
    ("d", "你好",
     lambda d, a: d.get("tools_used") == []),
    ("e", "列出目录下的文件",
     lambda d, a: "info.txt" in a),
    ("f", "记一下：明天买牛奶",
     lambda d, a: "牛奶" in d.get("notes", "")),
    ("g", "知识库里蓝莓基地的亩产是多少",
     lambda d, a: "820" in a and "blueberry" in a),
    ("h", "我的笔记里关于火星殖民的计划是什么",
     lambda d, a: "查不到" in a),
    ("i", "知识库里谁负责蓝莓基地",
     lambda d, a: "老周" in a and d.get("intent") == "kb"),
    ("j", "查询2026年9月的工作事项",
     lambda d, a: d.get("intent") == "work"
     and "work_summary" in d.get("tools_used", [])
     and ("5" in a)
     and (("alice" in a) or ("bob" in a) or ("inspection" in a) or ("抽样" in a) or ("检查" in a))),
    ("k", "2026-08-01 至 2026-08-31 的工作汇总",
     lambda d, a: d.get("intent") == "work" and ("1" in a) and ("查不到" not in a or "八月" in a or "示例" in a)),
    ("l", "查询2099年1月的工作事项",
     lambda d, a: d.get("intent") == "work" and "查不到" in a),
    ("n", "收件箱里有哪些超期待办",
     lambda d, a: d.get("intent") == "inbox"
     and "inbox_query" in d.get("tools_used", [])
     and ("Rust" in a or "超期" in a or "学习" in a or "GMP" in a)
     and "查不到" not in a),
    ("o", "Inbox 未完成事项",
     lambda d, a: d.get("intent") == "inbox"
     and "inbox_query" in d.get("tools_used", [])
     and "已完成周报" not in a
     and ("未完成" in a or "pending" in a or "待办" in a or "示例" in a or "学习" in a or "买火车" in a)),
    ("p", "收件箱里关于火星殖民计划xyz的待办",
     lambda d, a: d.get("intent") == "inbox" and "查不到" in a),
    ("r", "查询示例药业甲的严重缺陷",
     lambda d, a: d.get("intent") == "defects"
     and "defects_search" in d.get("tools_used", [])
     and "示例药业甲" in a and "serious" in a and "9001" in a),
    ("s", "示例药业乙在2026-08-01至2026-08-31的缺陷记录有哪些",
     lambda d, a: d.get("intent") == "defects"
     and "示例药业乙" in a and "查不到" not in a),
    ("t", "查询星河航天集团的缺陷记录",
     lambda d, a: d.get("intent") == "defects" and "查不到" in a),
    ("v", "tell me about my work plans this week",
     lambda d, a: d.get("intent") == "work"
     and "work_summary" in d.get("tools_used", [])
     and not any(x in a for x in ("没有权限", "无法访问", "no access", "don't have access", "do not have access"))),
    ("w", "本周工作计划",
     lambda d, a: d.get("intent") == "work" and "work_summary" in d.get("tools_used", [])),
]


def run_case(sandbox, kb_dir, question, extra_env=None):
    env = os.environ.copy()
    env["MINI_AGENT_SANDBOX"] = str(sandbox)
    env["MINI_AGENT_KB"] = str(kb_dir)
    env["MINI_AGENT_WORK_FIXTURE"] = str(WORK_FIXTURE)
    env["MINI_AGENT_DEFECTS_FIXTURE"] = str(DEFECTS_FIXTURE)
    env["MINI_AGENT_INBOX"] = str(INBOX_FIXTURE)
    env["MINI_AGENT_INBOX_TODAY"] = "2026-10-12"
    env["MINI_AGENT_TODAY"] = "2026-10-12"
    env["MINI_AGENT_ROUTER_MODE"] = "hybrid"
    env.setdefault("MINI_AGENT_API_KEY", "")
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "mini_agent.py"), "--json", "--quiet", question],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(ROOT))
    try:
        data = json.loads(proc.stdout.strip())
    except Exception:
        data = {"intent": "?", "risk": "?", "tools_used": [],
                "answer": f"输出解析失败: {proc.stdout[:200]} | {proc.stderr[-200:]}"}
    notes_path = sandbox / "notes.md"
    data["notes"] = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    return data


def run_unit_tests():
    """纯函数单测（不启模型）。"""
    sys.path.insert(0, str(ROOT))
    os.environ["MINI_AGENT_WORK_FIXTURE"] = str(WORK_FIXTURE)
    import importlib
    import mini_agent
    importlib.reload(mini_agent)
    rows = []
    def check(name, cond, detail=""):
        rows.append((name, cond, detail))
    check("u1_parse_month",
          mini_agent.parse_work_range("查询2026年9月的工作事项") == ("2026-09-01", "2026-09-30"))
    check("u2_parse_range",
          mini_agent.parse_work_range("2026-08-01 至 2026-08-31") == ("2026-08-01", "2026-08-31"))
    check("u3_month_end",
          mini_agent._month_bounds(2024, 2) == ("2024-02-01", "2024-02-29"))
    s = mini_agent.query_work_range("2026-09-01", "2026-09-30")
    check("u4_sep_count", s["fetchedCount"] == 5 and s["complete"] is True, str(s["fetchedCount"]))
    dup = [{"id": 1, "start_date": "2026-09-01", "task_type": "a", "assignee": "x"},
           {"id": 1, "start_date": "2026-09-01", "task_type": "a", "assignee": "x"}]
    check("u5_dedupe", mini_agent.summarize_work_rows(dup, "2026-09-01", "2026-09-30", True)["fetchedCount"] == 1)
    check("u6_incomplete_flag",
          mini_agent.summarize_work_rows(
              [{"id": i, "start_date": "2026-09-01", "task_type": "t", "assignee": "a"} for i in range(3)],
              "2026-09-01", "2026-09-30", False)["complete"] is False)
    try:
        mini_agent.summarize_work_rows([{"start_date": "2026-09-01"}], "2026-09-01", "2026-09-30", True)
        check("u7_missing_id", False, "should raise")
    except ValueError:
        check("u7_missing_id", True)
    os.environ["MINI_AGENT_INBOX"] = str(INBOX_FIXTURE)
    os.environ["MINI_AGENT_INBOX_TODAY"] = "2026-10-12"
    importlib.reload(mini_agent)
    ip = mini_agent.resolve_inbox_path()
    check("u8_inbox_resolve", ip is not None and ip.name == "inbox.md")
    line = mini_agent.parse_inbox_line(
        "- [ ] 示例任务 📅 2020-01-01 #work", 1, "2026-10-12")
    check("u9_inbox_overdue", bool(line and line["isOverdue"] and line["isWork"]))
    od = mini_agent.query_inbox_tasks(ip, {"overdue_only": True, "status": "pending", "today": "2026-10-12"})
    check("u10_inbox_overdue_count", od["totalFound"] >= 2 and od["file"] == "inbox.md", str(od["totalFound"]))
    check("u11_inbox_basename", "/" not in od["file"])
    # defects 纯函数
    os.environ["MINI_AGENT_DEFECTS_FIXTURE"] = str(DEFECTS_FIXTURE)
    importlib.reload(mini_agent)
    check("u12_level_map",
          mini_agent.map_defects_level("严重缺陷") == "serious"
          and mini_agent.map_defects_level("主要缺陷") == "major"
          and mini_agent.map_defects_level("重大缺陷") == "major"
          and mini_agent.map_defects_level("一般缺陷") == "minor"
          and mini_agent.map_defects_level("缺陷") is None)
    f = mini_agent.parse_defects_filters("查询示例药业甲的严重缺陷")
    check("u13_parse_company_level",
          f["company_name"] == "示例药业甲" and f["level"] == "serious", str(f))
    f2 = mini_agent.parse_defects_filters("示例药业乙在2026-08-01至2026-08-31的缺陷记录")
    check("u14_parse_dates",
          f2["company_name"] == "示例药业乙" and f2["start_date"] == "2026-08-01"
          and f2["end_date"] == "2026-08-31", str(f2))
    check("u15_dedupe_defects",
          len(mini_agent.dedupe_defect_rows(
              [{"id": "X1"}, {"id": "X1"}, {"id": "X2"}])) == 2)
    try:
        mini_agent.dedupe_defect_rows([{"company_name": "无id"}])
        check("u16_missing_id", False, "should raise")
    except ValueError:
        check("u16_missing_id", True)
    s = mini_agent.query_defects({"company_name": "示例药业甲", "level": "serious"})
    check("u17_fixture_query",
          s["fetchedCount"] == 1 and s["complete"] is True
          and s["byLevel"]["serious"] == 1
          and s["examples"][0]["company_name"] == "示例药业甲"
          and len(s["examples"][0]["content"]) <= 120, str(s["fetchedCount"]))
    s2 = mini_agent.query_defects({})
    check("u18_no_filters_complete", s2["complete"] is True and s2["fetchedCount"] == 9, str(s2))
    big = mini_agent.summarize_defects([{"id": i, "level": "minor", "content": "c" * 500}
                                        for i in range(10)], {}, False)
    check("u19_examples_capped",
          len(big["examples"]) == 8 and big["complete"] is False
          and all(len(e["content"]) <= 120 for e in big["examples"]))
    os.environ["MINI_AGENT_TODAY"] = "2026-10-12"
    importlib.reload(mini_agent)
    check("u20_relative_this_week",
          mini_agent.relative_date_range("this week") == ("2026-10-12", "2026-10-18"))
    check("u21_relative_last_week",
          mini_agent.relative_date_range("last week") == ("2026-10-05", "2026-10-11"))
    check("u22_relative_zheliibai",
          mini_agent.relative_date_range("这礼拜我都忙了些啥") == ("2026-10-12", "2026-10-18"))
    check("u23_hc_work_plans",
          (mini_agent.high_confidence_rule("tell me about my work plans this week") or [None])[0] == "work")
    check("u24_classify_work_plans",
          mini_agent.classify_rules("tell me about my work plans this week") == "work")
    check("u25_classify_benzhou",
          mini_agent.classify_rules("本周工作计划") == "work")
    def fake_s1_ok(state, questions, model=None, timeout=None):
        return {
            "intent": {"type": "choice", "choice": "work", "confidence": 0.99},
            "relative": {"type": "choice", "choice": "this_week", "confidence": 0.98},
            "overdue_only": {"type": "noul", "noul": 0.01},
            "level": {"type": "choice", "choice": "none", "confidence": 0.9},
        }
    def fake_s1_bad(state, questions, model=None, timeout=None):
        return None
    real_s1 = mini_agent.call_systemone
    real_chat = mini_agent.chat_completion
    mini_agent.call_systemone = fake_s1_ok
    mini_agent.chat_completion = lambda *a, **k: None  # no chat fallback in mock
    u, ms = mini_agent.system_one_understand("what kept me occupied recently during the current week", quiet=True)
    check("u26_s1_mock_work", bool(u and u["intent"] == "work" and u["range"]["start"] == "2026-10-12"), str(u)[:80])
    mini_agent.call_systemone = fake_s1_bad
    u2, _ = mini_agent.system_one_understand("gibberish xyz", quiet=True)
    check("u27_s1_bad_json", u2 is None)
    mini_agent.call_systemone = real_s1
    mini_agent.chat_completion = real_chat
    check("u28_hc_present",
          mini_agent.high_confidence_rule("tell me about my work plans this week") is not None)
    return rows


def main():
    sandbox = Path(tempfile.mkdtemp(prefix="mini_agent_test_"))
    (sandbox / "info.txt").write_text(INFO_CONTENT, encoding="utf-8")
    kb_dir = Path(tempfile.mkdtemp(prefix="mini_agent_kb_"))
    for rel in (KB_FARM, KB_PI, KB_OBSIDIAN):
        p = kb_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text({KB_FARM: KB_FARM_CONTENT, KB_PI: KB_PI_CONTENT,
                      KB_OBSIDIAN: KB_OBSIDIAN_CONTENT}[rel], encoding="utf-8")
    rows = []
    passed = 0
    try:
        for name, ok, detail in run_unit_tests():
            rows.append((name, "unit", ok, detail if not ok else "ok"))
            passed += ok
        for name, question, check in CASES:
            try:
                data = run_case(sandbox, kb_dir, question)
                ok = bool(check(data, data.get("answer", "")))
                detail = data.get("answer", "").replace("\n", " ")[:60]
            except subprocess.TimeoutExpired:
                ok, detail = False, "超时"
            except Exception as exc:
                ok, detail = False, f"异常: {exc}"
            rows.append((name, question, ok, detail))
            passed += ok
        # m: 未配置数据源
        try:
            data = run_case(sandbox, kb_dir, "查询2026年9月的工作事项",
                            extra_env={"MINI_AGENT_WORK_FIXTURE": "", "MINI_AGENT_WORK_URL": ""})
            ok = ("未配置" in data.get("answer", "")) or ("查不到" in data.get("answer", ""))
            detail = data.get("answer", "").replace("\n", " ")[:60]
        except Exception as exc:
            ok, detail = False, f"异常: {exc}"
        rows.append(("m", "work未配置数据源", ok, detail))
        passed += ok
        try:
            empty_kb = Path(tempfile.mkdtemp(prefix="empty_kb_"))
            data = run_case(sandbox, empty_kb, "收件箱里有哪些待办",
                            extra_env={"MINI_AGENT_INBOX": ""})
            ok = "未配置" in data.get("answer", "")
            detail = data.get("answer", "").replace("\n", " ")[:60]
            shutil.rmtree(empty_kb, ignore_errors=True)
        except Exception as exc:
            ok, detail = False, f"异常: {exc}"
        rows.append(("q", "inbox未配置", ok, detail))
        passed += ok
        try:
            data = run_case(sandbox, kb_dir, "查询示例药业甲的严重缺陷",
                            extra_env={"MINI_AGENT_DEFECTS_FIXTURE": "", "MINI_AGENT_WORK_URL": "",
                                       "MINI_AGENT_DEFECTS_URL": "", "MINI_AGENT_WORK_FIXTURE": str(WORK_FIXTURE)})
            ok = "未配置" in data.get("answer", "") and ("缺陷" in data.get("answer", "") or "DEFECTS" in data.get("answer", "") or "WORK_URL" in data.get("answer", ""))
            detail = data.get("answer", "").replace("\n", " ")[:60]
        except Exception as exc:
            ok, detail = False, f"异常: {exc}"
        rows.append(("u", "defects未配置", ok, detail))
        passed += ok
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        shutil.rmtree(kb_dir, ignore_errors=True)

    total = len(rows)
    print(f"{'#':<8}{'用例':<28}{'结果':<6}详情")
    print("-" * 72)
    for name, question, ok, detail in rows:
        q = question if isinstance(question, str) else str(question)
        print(f"{name:<8}{q[:26]:<28}{'PASS' if ok else 'FAIL':<6}{detail}")
    print("-" * 72)
    print(f"结果：{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
