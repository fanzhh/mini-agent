# wee：inbox 意图（本地 Inbox 待办/超期）实现规格

依据：`/workspace/extra-pi-review/src/inbox.ts`、`src/tools.ts`（query_inbox_tasks）。
目标：在 `/workspace/mini-agent` 增加确定性只读 `inbox` 意图；程序解析 Markdown 任务并过滤；模型只据结果措辞；空 →「查不到」。

## 数据源（禁止硬编码私有路径）

| 变量 | 作用 |
|---|---|
| `MINI_AGENT_INBOX` | Inbox.md 文件路径（优先）。测试用 fixture。 |
| `MINI_AGENT_KB` | 若未设 INBOX：在 KB 下按候选相对路径查找（与 extra-pi 一致） |
| `MINI_AGENT_INBOX_TODAY` | 可选。固定“今天”YYYY-MM-DD，便于测超期；默认本地日期。 |

候选相对路径（仅当用 KB 时）：
1. `01_AREAS/Inbox.md`
2. `01_AREAS/tasks/🔥 Inbox.md`
3. `Inbox.md`

两者皆不可用 → 「查不到：未配置 Inbox（请设置 MINI_AGENT_INBOX 或 MINI_AGENT_KB）。」
只读：绝不写入 Inbox。返回摘要的 `file` 只用 basename，不要绝对路径。

## 解析（对齐 inbox.ts）

匹配行：`^([-*]|\d+\.)\s*\[([ xX/])\]\s*(.*)$`
- `[x]`/`[X]` → completed；`[ ]`/`[/]` → pending
- `✅ YYYY-MM-DD` → doneDate；`📅 YYYY-MM-DD` → planDate
- `#tag` → tags；isWork：tags 含 work/six/case/GMP
- overdue：pending 且 planDate < today
- cleanTitle：去掉日期标记、#tag、优先级符号

## 过滤（问题确定性解析）

| 信号 | 选项 |
|---|---|
| 未完成/待办/pending | status=pending |
| 已完成/completed | status=completed |
| 超期/逾期/过期 | overdue_only=true |
| 个人/生活/非工作 | personal_only=true |
| 收件箱+工作标签语境 | work_only=true（勿抢 work 意图的「工作事项/工作汇总」） |
| 日期区间 / 本周/上周/今天 | start/end |

默认 status=all。与 work 冲突：「工作事项/工作汇总」→ work；「收件箱/待办/超期/Inbox」→ inbox。

## 输出摘要

file(basename), totalFound, filters, today, summary{completed,pending,work,personal,overdue}, items≤20（无 rawText）, note。
totalFound==0 → 查不到。

## 实现要点

- INTENTS 加 inbox；config keywords/regex；run_inbox 类似 run_work；tools_used=`inbox_query`
- tests/fixtures/inbox.md 虚构数据；MINI_AGENT_INBOX_TODAY=2026-10-12
- 用例 n/o/p/q + 纯函数单测；原 a–m 仍 PASS
- 更新 README；不 git commit；跑通 tests/run_tests.py
