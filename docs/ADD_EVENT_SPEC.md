# add_event：创建工作事项

依据：`six-unified-tools.json` → `db_create_task`（与 `db_query_tasks` / query_work_* 同一事项库）。

## 工具
- **仅** `db_create_task`（禁止 update/delete / insert_record / custom SQL）
- 必填：`title`；常用：`task_type`（默认 `other`）、`start`/`end`（ISO-8601 +08:00）、`all_day`、`create_by`、`person`（可选 env）

## 确认
- 先预览完整 arguments JSON
- REPL：显式 `y`；CLI 非交互：`--yes`；否则不写
