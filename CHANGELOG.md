# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

## [0.1.0] - 2026-10-14

### 新增
- 从单文件 `mini_agent.py` 重构为规范 Python 包 `mini_agent/`，按职责拆分为
  config / state / trace / jsonutil / model / router / tools / knowledge / work /
  inbox / defects / events / skills / agent / cli 模块（行为逐字保留，回归测试 61/61 通过）。
- `pyproject.toml`：可 `pip install`，提供 `mini-agent` 命令行入口；纯标准库、无第三方依赖。
- MIT 许可证、CONTRIBUTING、CHANGELOG。
- SearXNG 网页搜索工具 `web_search`（经 HK VPS 上的 SearXNG 实例，服务端把结果压缩成
  「标题 / 链接 / 摘要」纯文本再喂给小模型；`MINI_AGENT_SEARXNG_URL` 可覆盖）。
- `web` 意图改为确定性流程：搜到结果即呈现，不再让小模型决定要不要复述（避免拿到有效结果仍答「查不到」）。
- REPL 接入 `readline`：↑↓ 翻历史、←→ 移光标；历史持久化到 `sandbox/.mini-agent_history`。
- REPL 输入容错：终端截断字节不再崩溃（stdin/stdout 以 `errors="replace"` 重配）。

### 变更
- 入口从 `python3 mini_agent.py` 改为 `python3 -m mini_agent`（或安装后 `mini-agent`）。
- 跟踪/重定向长 URL（夸克 page.sm.cn、百度跳转等）在搜索结果里被清洗为可读的 host+path。

### 修复
- 搜索服务上游引擎被验证码/限流导致空结果时，如实提示并给换词建议，不再伪装「查不到」。

[Unreleased]: https://github.com/fanzhh/mini-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/fanzhh/mini-agent/releases/tag/v0.1.0
