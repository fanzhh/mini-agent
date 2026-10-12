# wee：work 意图（月度/区间工作事项汇总）实现规格

依据：`/workspace/extra-pi-review/src/work-mcp.ts`、`src/tools.ts`（query_work_month / query_work_range）。
目标：在 `/workspace/mini-agent` 增加确定性只读 `work` 意图，程序做分页/去重/统计，模型只根据摘要措辞；空结果答「查不到」。

## 行为（对齐 extra-pi）

### 输入解析（确定性，不靠模型选工具）
从用户问题提取：
- **月份**：`YYYY年M月` / `YYYY-MM` / `YYYY/M` → year+month → 转为该月首尾日
- **区间**：`YYYY-MM-DD` 至 `YYYY-MM-DD`（含「到/至/-」）
- 「本月/这个月」→ 用本地日期（Asia/Shanghai 可用 `datetime.now()`，box 已是该时区）算当月
- 「上月/上个月」→ 上月
- 解析失败 → 答「查不到：请说明月份（如 2026年9月）或日期区间（YYYY-MM-DD 至 YYYY-MM-DD）。」

### 数据获取（只读）
环境变量（**禁止**硬编码 `/Users/...`、内网 IP、用户名、密钥）：
| 变量 | 作用 |
|---|---|
| `MINI_AGENT_WORK_FIXTURE` | 若设置：读本地 JSON（任务数组或 `{"tasks":[...]}`），在内存里按日期过滤并模拟分页。测试用。 |
| `MINI_AGENT_WORK_URL` | 若无 fixture 且设置：HTTP 调用工作数据源（见下） |
| `MINI_AGENT_WORK_AUTH` | 可选。完整 Authorization 头值，如 `Bearer xxx`（勿打印、勿写入仓库） |
| `MINI_AGENT_WORK_TIMEOUT` | 可选，默认 30 |

两者皆空 → 「查不到：未配置工作数据源（请设置 MINI_AGENT_WORK_FIXTURE 或 MINI_AGENT_WORK_URL）。」

**HTTP 协议（与 extra-pi 的 db_query_tasks 对齐）**：
对 `MINI_AGENT_WORK_URL` POST JSON：
```json
{"start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","limit":100,"offset":0}
```
期望响应：
```json
{"success":true,"data":[{"id":...,"start_date":"...","task_type":"...","assignee":"...","task_name":"..."}, ...]}
```
也接受外层 MCP 风格：`{"content":[{"type":"text","text":"<上述 JSON 字符串>"}]}`。
分页：offset 0,100,200… 直到 `len(data)<100` 或累计 ≥2000。
每行：`start_date` 前 10 字符必须在 [start,end] 内，否则报错；必须有 `id` 去重。

### 汇总（纯函数，可单测）
与 work-mcp.ts 一致：
```
summary = {
  start_date, end_date,
  scope: "按事项开始日期筛选（…至…）；包含所有接口可见负责人",
  complete: bool,          # 未触 2000 上限
  fetchedCount: int,
  byType: {type: count},
  byAssignee: {assignee: count},
  examples: rows[:8],      # 代表事项（字段保留 id/task_name/task_type/assignee/start_date/end_date 若有）
  note: complete ? "以分页结束及唯一ID计数，不使用接口total作为总数；事项不等于已完成工作"
                 : "达到2000条上限，仅为部分结果，不能声称总数"
}
```
空 `fetchedCount==0` → 最终答「查不到」（可加说明该区间无事项）。

### 意图与路由
- `INTENTS` 增加 `"work"`
- `config.json` 增加 work：keywords 含 `工作事项` `工作任务` `工作汇总` `工作摘要` `月度工作` `工作安排`；regex 含月份/区间模式
- **优先级**：与 kb 类似，工作类关键词命中时优先 work（避免被 note/file 抢走）
- `run_agent`：`intent=="work"` 走 `run_work()` 确定性流程（类似 `run_kb`），**不要**让模型选工具

### run_work 流程
1. 解析日期；失败 → 查不到提示
2. `query_work_range` / month → summary（工具名记入 tools_used：`work_summary`）
3. 空 → 「查不到：该时间范围内没有工作事项。」
4. 把 **summary JSON**（不要 full rows 全量，examples 已够）交给模型，system：「你是工作事项汇总助手。只根据给出的统计摘要用简洁中文复述；不要编造未出现的数字或人名；摘要为空或 fetchedCount=0 则答查不到；必须说明统计口径（按开始日期）且事项≠已完成。」
5. 校验：答案中的数字必须出现在 summary JSON 或问题中；否则用规则模板兜底输出（fetchedCount、byType、byAssignee 前几项、examples 标题列表）
6. 模型不可用时同样用规则模板兜底

### 测试（tests/run_tests.py + fixtures）
在 `tests/fixtures/work_tasks.json` 放虚构数据（**禁止真实企业/人名/真实任务**），例如：
- id 1–5：2026-09 内不同 assignee/type
- id 6：2026-08（应被 9 月查询排除）
- id 7：2026-10

用例至少：
- j: 「查询2026年9月的工作事项」→ intent=work，答案含正确 fetchedCount（5）、出现某 assignee 或 type，tools_used 含 work_summary
- k: 「2026-08-01 至 2026-08-31 的工作汇总」→ count=1
- l: 「查询2099年1月的工作事项」→ 查不到
- m: 未配置 WORK_FIXTURE/URL 时 → 查不到：未配置…

原有 a–i 仍须全部 PASS。测试里 `env["MINI_AGENT_WORK_FIXTURE"]=fixture_path`。

另加**纯函数单测**（可在 run_tests.py 内或 tests/test_work_summary.py）：直接 import `summarize_work_rows` / `parse_work_range`，不启模型：去重、越界日期报错、月份末日、2000 上限 complete=False。

### README
补充 `work` 意图与上述环境变量；示例用占位路径，不写真实私有路径。

### 禁止
- 不读 `~/.hermes/config.yaml`（除非将来另开任务）；凭据只从 env
- 不开放写/删/自定义 SQL
- 不把 fixture 写成真实监管数据
- 不打印 AUTH

完成后：`python3 tests/run_tests.py` 全绿。
