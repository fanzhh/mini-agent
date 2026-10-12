# wee：defects 意图（只读缺陷检索）实现规格

依据：six-unified MCP 工具 `defects_search_defects`；对齐 extra-pi 窄包装（只读，禁止写/删/自定义 SQL）。

## 目标
在 mini-agent 增加确定性只读 `defects` 意图：
- 程序解析问题 → 调 defects_search_defects（分页/去重）→ 摘要
- 模型只据摘要措辞；空 →「查不到」
- 禁止硬编码私有路径/IP/密钥；勿打印 AUTH

## 环境变量
| 变量 | 作用 |
|---|---|
| `MINI_AGENT_DEFECTS_FIXTURE` | 本地 JSON fixture（测试）；数组或 `{"data":[...]}` |
| `MINI_AGENT_WORK_URL` | MCP URL（与 work 共用 six-unified；也可用 `MINI_AGENT_DEFECTS_URL` 覆盖） |
| `MINI_AGENT_WORK_AUTH` | Authorization 完整值（或 `MINI_AGENT_DEFECTS_AUTH`） |
| `MINI_AGENT_WORK_TIMEOUT` | 超时（默认 30；defects 可复用） |

未配置 fixture 且无 URL →「查不到：未配置缺陷数据源（请设置 MINI_AGENT_DEFECTS_FIXTURE 或 MINI_AGENT_WORK_URL）。」

## MCP 调用
JSON-RPC `tools/call` name=`defects_search_defects`，参数仅允许：
`company_name, company_id, level, law, content_keywords, start_date, end_date, limit, offset`
- level 枚举：serious | major | minor（中文「严重/主要|重大/一般|次要」→ 映射）
- 分页：limit 默认 50（≤50），offset 步进直到页长 <limit 或累计 ≥200；按 `id` 去重
- 响应：`{success, data:[...]}` 或 MCP content text 包一层；行字段含 id, company_name, check_date, level, content, law, clause 等
- **只读**：绝不调用 insert/update/delete/execute_custom_query

## 问题解析（确定性）
- 企业名：`关于X的缺陷` / `X公司` / `企业X`（保守提取，宁可不提 company_name）
- 级别：严重→serious，主要/重大→major，一般/次要/轻微→minor
- 关键词：清洁验证/偏差 等；或「关于…的」
- 日期区间 YYYY-MM-DD 至 YYYY-MM-DD
- 若几乎无过滤条件且只有「缺陷」一字：仍可查（limit 小），但测试用例应带过滤

## 摘要（给模型）
```
{
  "fetchedCount": N,
  "complete": bool,
  "filters": {...},  # 不含密钥
  "byLevel": {"serious":n,"major":n,"minor":n},
  "examples": [最多8条：{id, company_name, check_date, level, content截断120字, law}],
  "note": "分页结果不能当作全库总数；级别枚举 serious/major/minor"
}
```
不要把完整 content 长文全塞进 examples（截断）。

## 路由（勿与 work/inbox 冲突）
- keywords：缺陷、defect、不符合项、缺陷记录、严重缺陷、主要缺陷
- 「工作事项/工作汇总」→ work；「收件箱/待办/超期」→ inbox；「缺陷/不符合项」→ defects
- inbox/work 优先规则保持；defects 在命中缺陷词时优先于 kb/note

## 实现
- INTENTS 加 `defects`；`run_defects` 类似 `run_work`；tools_used=`defects_search`
- tests/fixtures/defects.json 虚构数据（假公司名：示例药业甲/乙，假 id）
- 用例：严重缺陷检索、无匹配查不到、未配置数据源；纯函数：级别映射、去重、分页 complete
- 更新 README、config.json、tests/run_tests.py
- 原有测试必须全绿

完成后在项目根运行：`python3 tests/run_tests.py`，打印结果。不要 git commit。不要读取/打印 ~/.hermes 密钥。
