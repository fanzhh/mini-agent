# mini-agent

最小智能体：单文件 `mini_agent.py`，仅 Python 标准库，经 OpenAI 兼容接口做工具调用。

## 配置

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINI_AGENT_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容接口地址 |
| `MINI_AGENT_MODEL` | 空 | 模型名（空则不传 model 字段） |
| `MINI_AGENT_API_KEY` | 空 | API key（可空；不会被打印） |
| `MINI_AGENT_SANDBOX` | `./sandbox` | 沙箱目录（工具与笔记都在其中） |
| `MINI_AGENT_LAYA_URL` | 空 | 可选意图预测服务，规则不命中时 POST `/predict`（失败忽略） |
| `MINI_AGENT_KB` | 空 | 只读知识库目录（空 = 关闭；例 `~/knowledge`） |
| `MINI_AGENT_TIMEOUT` | `10` | HTTP 超时秒数 |

行为参数在 `config.json`：意图关键词/正则、危险词、shell 白名单、`max_steps`、`max_tokens`。

## 用法

```bash
python3 mini_agent.py "info.txt 里写的预算是多少"   # 问答
python3 mini_agent.py --json "你好"                # 输出 {"intent":...,"risk":...,"tools_used":[...],"answer":...}
python3 mini_agent.py --quiet "列出目录下的文件"    # 关闭 stderr trace
python3 mini_agent.py                              # 无参数进入 REPL
```

## 流程

1. **System 1 前置路由**：先查危险词（`rm`、`sudo`、`mkfs`、`dd`、`shutdown`、`curl|sh`、重定向 `>`、`chmod` 等），命中直接返回「已拦截：不可逆操作」；否则按 `config.json` 关键词/正则判意图（chat / file / shell 只读 / web / note / kb 知识库 / out_of_scope 拒答；`笔记里/我的笔记/知识库` 等 kb 关键词与 note 同分时优先归 kb，`记一下：…` 仍归 note）；规则不命中时，若设了 `MINI_AGENT_LAYA_URL` 则 POST `/predict`（失败忽略），再把提到「我/我的」的事实性问题兜底归 kb（需配置知识库），最后用一次极短模型分类（只输出类别名，不在白名单则 chat）。
2. **工具循环**（最多 `max_steps` 步）：每个意图只暴露 1–3 个工具——`read_file`、`search_files`（限沙箱内，防 `..` 逃逸）、`run_shell`（白名单 `ls cat wc head date pwd echo grep`，`shlex` 解析，禁管道/重定向，`cwd=沙箱`）、`web_fetch`（截断 4000 字）、`note_append`（追加 `sandbox/notes.md`）。模型每步只输出 JSON `{"tool":..,"args":{..}}` 或 `{"answer":..,"sources":[..]}`；请求附 `response_format: json_object`（服务器报 4xx 则去掉重试）；JSON 提取去代码块并校验，失败带错误反馈重试 1 次。
3. **防胡说**：系统提示要求只依据工具结果回答，查不到答「不知道」；规则校验器——答案里的数字必须出现在工具结果或问题中，否则改为「查不到可靠信息」；非 chat 意图没有任何工具结果且答案不含「不知道/查不到」，则改为「查不到相关信息」。
4. **降级**：模型接口不可用时按意图走规则兜底（直接读文件、执行白名单命令、追加笔记等），保证基本可用。
5. **trace**：每一步打到 stderr（`--quiet` 关闭），并追加写入 `sandbox/trace.jsonl`。

## 知识库

设置 `MINI_AGENT_KB`（默认空 = 关闭）指向知识库目录：

```bash
export MINI_AGENT_KB=~/knowledge
wee "知识库里关于周报模板写了什么"
```

- **只读**：全程只检索/读取，绝不写入或删除知识库内任何文件；`kb_read` 只接受库内相对路径（拒绝绝对路径与 `..`，解析后校验仍在库内）。未配置时答「查不到：未配置知识库」。
- **触发词**：`知识库`、`我写过`、`我的文章`、`记过`、`我的笔记`、`笔记里`、`我记的`、`手册`、`文档里`；规则未命中时，提到「我/我的」的事实性问题也会落入 kb（危险词与闲聊除外）。
- **流程**：`kb_search` 用 `rg -i -c --glob '*.md' -g '!.obsidian' -g '!.git' -g '!node_modules' -g '!attachments' -F -e …`（无 `rg` 时用 `os.walk` 跳相同目录，>2MB 跳过）按「命中数 + 命中词数 + 文件名/标题命中 + 近期修改」打分取前 5；`kb_read` 返回标题行 + 关键词最密集区域（≤3000 字）。随后把前 1–2 个文件的摘录（带 `[路径]` 标签）交给模型，要求只用摘录、中文简洁回答并引用路径；答案数字必须出现在摘录中，否则改为摘录 + 「来源」兜底；答案缺路径时追加 `来源：path1, path2`。无结果答「查不到：知识库里没有相关笔记。」

## 测试

```bash
python3 tests/run_tests.py
```

建临时 sandbox 放入 `info.txt`（含「项目代号：青松，预算 37 万元」）与临时知识库（蓝莓基地报告、pi agent 笔记、必须跳过的 `.obsidian/x.md`），子进程调 `mini_agent.py --json --quiet`，跑 9 例打印 PASS/FAIL 表：预算查询、未知事实拒答、危险命令拦截、闲聊零工具、列目录、记笔记、知识库亩产查询（`820`/`blueberry`）、知识库无结果拒答、知识库负责人查询。
