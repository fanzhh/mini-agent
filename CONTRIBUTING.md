# 贡献指南

感谢你想让 mini-agent 变更好！以下几条能让你的 PR 更容易被接受。

## 开发环境

只需要 Python（≥ 3.9）；本项目的核心特性之一是**零第三方依赖**——只用标准库。
不建议引入任何运行时 `pip` 包；确有需要请在 issue 里先讨论。

```bash
git clone https://github.com/fanzhh/mini-agent
cd mini-agent
python3 -m mini_agent --help          # 冒烟
python3 tests/run_tests.py            # 61 个测试全绿
```

## 目录结构（速览）

```
mini_agent/
  config.py        # 环境变量 + config.json：import 期读取，reload(this) 刷新
  state.py         # 跨函数可变状态（熔断开关 + 理解缓存）
  trace.py         # stderr + sandbox/trace.jsonl 事件流
  jsonutil.py      # extract_json / valid_step / feedback_for
  model.py         # chat_completion（OpenAI 兼容 /chat/completions）
  router.py        # 危险词 + 意图判定（规则 / System One / 兜底）
  tools.py         # read_file / search_files / run_shell / web_fetch / web_search / note_append / kb_*
  knowledge.py     # kb_search / kb_read / run_kb
  work.py          # 工作事项汇总（MCP/REST + fixture）
  inbox.py         # Inbox.md 待办解析
  defects.py       # 缺陷检索（six-unified MCP + fixture）
  events.py        # add_event 写入（仅 db_create_task；带确认）
  skills.py        # wishlist 与「无此技能」回复
  agent.py         # run_agent 主编排 + run_web 确定性网页搜索
  cli.py           # main / repl（readline）
  __main__.py      # python -m mini_agent 入口
  config.json      # 意图关键词/正则、危险词、shell 白名单、max_steps/max_tokens
```

依赖方向：`config/state/trace/jsonutil` ← `model/router/tools` ← 能力模块 ← `agent` ← `cli`。

## 修改原则

- **不改行为，除非明确要改行为**。`run_kb` / `run_web` / `run_defects` 等确定性流程是
  「防胡说」的兜底：小模型摆烂时结果仍要呈现，别把清单换成一句「查不到」。
- **跨函数可变状态**（`model_available`、`UNDERSTANDING`）通过 `state.x` 属性读写；
  `from .state import x` 会拷贝值，赋值不回写——这是踩坑点。
- **配置读取**通过 `config.X` 属性访问；`from .config import X` 会拷贝，`reload(config)`
  后看不到新值。测试里改 env 后请 `importlib.reload(mini_agent.config)` 再 `reload(mini_agent)`。
- **monkeypatch 目标**要指向函数**定义所在模块**（如 `router.call_systemone`），
  不是包顶层再导出的别名。
- 新增工具：加入 `tools.py` 里的 `TOOLS` 字典；若给模型用，同步在 `config.json` 对应意图的
  `tools` 数组里声明。

## 测试

```bash
python3 tests/run_tests.py
```

61 个用例覆盖：意图路由、危险词拦截、防胡说校验、kb/work/inbox/defects 确定性流程、
System One mock、add_event 预览/落盘、REPL 输入容错等。新增功能请补测；改动导致既有
用例失败，要么改代码要么改测试——**但不要静默删掉断言**。

## 代码风格

- 中文 docstring / 关键注释保留（"为什么"，非"做了什么"）。
- `pathlib.Path`、`urllib` 优先；避免 `os.path` 字符串拼接。
- 单文件 2500+ 行才拆成包，日常改动请保持模块边界；一个函数搬错模块比留在原地更坏。

## 提交与 PR

- Commit 标题：`type(scope): 简述`（`feat`/`fix`/`refactor`/`docs`/`test`/`chore`）。
- PR 描述里说清：动机、行为变化（若有）、如何验证。
- 破坏性变更（CLI 参数、config.json schema、意图分类语义）请开 issue 讨论后再动手。

## 许可

提交即视为你同意以 MIT 许可证授权你的贡献。
