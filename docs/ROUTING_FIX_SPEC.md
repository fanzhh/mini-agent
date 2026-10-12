# wee：System One 主解析 + 规则快路径（Hybrid）

参考：extra-pi M3 Hybrid（`src/router/index.ts` + `systemone.ts`）：高置信规则 0ms 直通，否则 System-1 语义理解，失败平滑降级。

## Bug
`tell me about my work plans this week` → chat。用户要求：别只靠关键词，**充分用 System One 理解请求**。

## 架构（默认 hybrid）

```
danger → 拦截
→ 高置信规则快路径（仅强匹配，可跳过模型）
→ System One 理解层（主路径）：一次受限模型调用 → 校验 JSON
→ 失败则规则软兜底 / chat
→ 下游工具仍确定性执行（work/inbox/defects/kb）
```

环境：
- `MINI_AGENT_ROUTER_MODE=hybrid|rule|systemone`（默认 hybrid）
- `MINI_AGENT_SYSTEMONE_URL` 可选原生 `/v1/systemone`；**若未设，用 OpenAI 兼容 chat/completions 做同等 JSON 理解**（同一 BASE_URL，可用 `MINI_AGENT_SYSTEMONE_MODEL` 指定更小模型，默认当前 MODEL）
- `MINI_AGENT_INTENT_TIMEOUT` 默认 8s
- `MINI_AGENT_TODAY` 固定今天测相对日期

## System One JSON schema（强制校验）

模型只输出：
```json
{
  "intent": "kb|work|inbox|defects|chat|note|file|shell|web|out_of_scope",
  "relative": "this_week|last_week|next_week|this_month|last_month|next_month|today|yesterday|tomorrow|null",
  "range": {"start":"YYYY-MM-DD","end":"YYYY-MM-DD"} | null,
  "entities": {
    "company": null,
    "level": "serious|major|minor|null",
    "keywords": [],
    "status": "pending|completed|all|null",
    "overdue_only": false
  }
}
```
校验失败 → 当 S1 失败处理。成功 `trace("intent_systemone", ...)` 含 duration_ms。
relative 用 `relative_date_range` 解析为具体日期；range 优先于 relative。
将解析后的 start/end/entities 传给 run_work/run_inbox/run_defects（可用模块级 HINT 或改 run_* 签名接收 understanding dict）。

criteria 文案（写入 prompt，对齐 extra-pi 精神）：
- work: 工作安排/计划/本周忙什么/schedule/plans
- inbox: 待办/超期/todo/overdue
- defects: 缺陷/findings/不符合
- kb: 笔记/knowledge/notes about X
- chat: 纯闲聊问候

## 规则层（快路径 + 相对日期，仍要做）

1. config.json 补英文关键词（work plans, schedule, todo, findings, my notes…）
2. `relative_date_range(q, today=None)`：EN/ZH 本周/这礼拜/this week…；周=周一–周日；`MINI_AGENT_TODAY`
3. `high_confidence_rule(q)`：仅强匹配返回 intent（如精确「工作事项汇总」、含「超期待办」、`rm` 已在 danger）。**弱匹配不要 highConfidence**，交给 System One。
4. hybrid：highConfidence → 用规则；否则 **先** System One；S1 失败再用软规则/chat。
5. `tell me about my work plans this week` / `本周工作计划` / `这礼拜我都忙了些啥` → work + this_week。

## 测试
- relative_date_range + MINI_AGENT_TODAY=2026-10-12 → this week 10-12..10-18
- classify/route：英文句、本周工作计划 → work（规则或 S1 mock）
- e2e fixture：英文句 intent=work, tools 含 work_summary
- **mock System One / chat_completion**：
  - paraphrase 无关键词 → S1 返回 work+this_week → intent_systemone
  - 非法 JSON → 降级
  - highConfidence 规则句 → 不调用模型（可计数）
- 原用例全绿

## README
Hybrid / System One 主理解、规则快路径、相对日期、英文触发；`intent_systemone` trace。

不要 git commit；勿读密钥。完成后 `python3 tests/run_tests.py` 全绿。
参考文件（只读）：`/tmp/wee-routing-fix/extra-pi-systemone.ts`（若存在）。

## S1 小模型（M6 已确认）
Ollama 有 `tev1:0.8b`、`qwen2.5:0.5b`。System One 阶段用：
- `MINI_AGENT_S1_MODEL` 默认 `tev1:0.8b`
- 答题仍用 `MINI_AGENT_MODEL`（qwen2.5-coder:14b）
- 同一 `MINI_AGENT_BASE_URL`（Ollama /v1）
