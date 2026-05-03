# PatchFlow

> AI 驱动的代码生成与自动修复 CLI 工具

---

## 简介

PatchFlow 是一个 AI 驱动的 CLI 工具，实现了从「需求描述」到「可用代码输出」的完整闭环。它的核心理念是：**自动化「写代码 → 运行 → 报错 → 分析 → 修复 → 再运行」这个枯燥的迭代循环**。

与 Cursor / Claude Code 等同类工具相比，PatchFlow 的核心差异在于**用确定性算法控制 LLM 的行为**——通过 AST 解析、依赖图分析和策略选择器，精确限定 LLM 的修改范围，而不是让 AI 自由发挥。

## 特性

- **交互式 REPL** — 像 Claude Code 一样对话式编程，支持工具调用
- **`/plan` 计划驱动生成** — AI 先输出结构化分步计划，用户确认后逐步骤执行，全程可见进度
- **`/build` 一次性生成** — 从需求描述直接生成可运行代码，自动验证+修复
- **`/fix` 多 Agent 协作** — Analyzer → Fixer → Reviewer 三 Agent 流水线，可为每个角色指定不同模型
- **多模型管理** — 同时配置多个 LLM（Anthropic / OpenAI / DeepSeek），一键切换
- **自动快照与回滚** — 每次修复前自动保存快照，失败自动回滚
- **硬约束修复** — Strategy Selector 根据错误类型算法圈定可修改的文件范围，LLM 无法接触圈外文件
- **项目感知** — 通过 AST 解析构建模块依赖图和函数签名图谱，精确计算修复影响面
- **跨会话记忆** — 自动压缩历史对话为摘要，下次启动时恢复上下文
- **多语言支持** — 通过 LanguageRegistry 抽象层支持 Python / JavaScript / TypeScript / Java / Go / Rust
- **跨平台** — Windows / macOS / Linux

## 快速开始

```bash
# 安装
pip install patchflow

# 首次配置（交互式）
patchflow config init

# 启动交互式对话
patchflow

# 计划驱动分步骤生成（推荐大项目）
patchflow plan "创建一个 FastAPI TODO 应用"

# 一次性生成
patchflow build "创建一个命令行计算器"

# 多 Agent 修复
patchflow fix "修复 app.py 中的语法错误"
```

## 配置

配置文件位于 `~/.patchflow/config.json`，支持多模型配置：

```json
{
  "active": "deepseek",
  "models": {
    "deepseek": {
      "provider": "deepseek",
      "model": "deepseek-chat",
      "api_key": "sk-xxx",
      "api_base": "https://api.deepseek.com"
    },
    "claude": {
      "provider": "anthropic",
      "model": "claude-sonnet-4-20250514",
      "api_key": "sk-ant-xxx"
    },
    "gpt4": {
      "provider": "openai",
      "model": "gpt-4o",
      "api_key": "sk-xxx"
    }
  },
  "max_retries": 3,
  "agents": {
    "analyzer": "deepseek",
    "fixer": "claude",
    "reviewer": "deepseek"
  }
}
```

```bash
# 命令行配置
patchflow config set api_key sk-ant-xxx
patchflow config set model claude-sonnet-4-20250514

# 多模型管理
patchflow model add my-ds deepseek deepseek-chat sk-xxx
patchflow model use my-ds
patchflow model list
```

## 命令

### CLI 命令

| 命令 | 说明 |
|------|------|
| `patchflow` | 进入交互式 REPL 模式 |
| `patchflow chat` | 同上 |
| `patchflow plan <任务>` | 计划驱动分步骤生成代码（推荐大型/复杂任务） |
| `patchflow build <任务>` | 一次性生成代码并自动验证修复 |
| `patchflow fix <任务>` | 多 Agent 协作修复代码问题 |
| `patchflow analyze` | 分析当前项目结构 |
| `patchflow status` | 查看缓存状态 |
| `patchflow config set/show/init` | 配置管理 |
| `patchflow model add/use/list/remove` | 多模型管理 |

### REPL 内部命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/exit` 或 `/quit` | 退出 |
| `/clear` | 清空对话历史 |
| `/history` | 显示对话统计 |
| `/memory` | 显示记忆状态（跨会话记忆） |
| `/model` | 列出/切换可用模型 |
| `/plan` | 制定计划后分步骤生成代码 |
| `/build` | 生成代码并自动验证 |
| `/fix` | 多 Agent 协作修复 |
| `/context` | 查看上下文统计 |
| `/init` | 创建项目级规则文件 |
| `/stop <pid>` | 停止后台进程 |
| `/ps` | 查看后台进程 |

## 多 Agent 协作架构

PatchFlow 使用 **Blackboard（黑板）模式** 实现多 Agent 协作。三个 Agent **不直接通信**，而是通过共享的 Blackboard 数据结构交换信息：

```
┌──────────────────────────────────────────────┐
│                 Blackboard                     │
│  ┌──────────┬──────────┬──────────┐           │
│  │ analysis │ fix_plan │  review  │           │
│  │ (写入:   │ (写入:   │ (写入:   │           │
│  │ Analyzer)│  Fixer)  │ Reviewer)│           │
│  └──────────┴──────────┴──────────┘           │
└──────────────────────────────────────────────┘
        ▲            ▲            ▲
        │            │            │
   ┌────┴───┐   ┌───┴────┐   ┌──┴──────┐
   │Analyzer│ → │ Fixer  │ → │Reviewer │
   │ 定位问题│   │ 执行修复│   │ 审查方案│
   └────────┘   └────────┘   └─────────┘
```

- **Analyzer** — 只说问题在哪，不提修复方案。LLM 自动识别项目语言
- **Fixer** — 根据分析结果和策略约束执行修复，只能在圈定范围内行动
- **Reviewer** — 独立审查修复方案，可以驳回让 Fixer 重做（最多一次重试）

每个 Agent 的输出格式固定（定义在 `schema.py`），包含 `summary`（≤150 字符）、`language`（LLM 自动识别）等字段。支持为不同角色配置不同模型（如 Analyzer 用便宜的 DeepSeek，Fixer 用更强的 Claude）。CLI 面板实时显示每个 Agent 对 Blackboard 的读写活动。

## 语言支持

PatchFlow 通过 `LanguageRegistry` 抽象层支持多语言，自动匹配各语言的 traceback 格式和错误类型体系：

| 语言 | 错误识别 | 项目配置 | 依赖解析 | 运行/编译 |
|------|---------|---------|---------|----------|
| Python | SyntaxError, TypeError, ImportError... | pyproject.toml, setup.py | AST 解析 | python |
| JavaScript | TypeError, ReferenceError... | package.json | 正则解析 | node |
| TypeScript | TS errors, TypeError... | tsconfig.json | 正则解析 | tsc + node |
| Java | NullPointerException, ClassNotFoundException... | pom.xml, build.gradle | — | javac + java |
| Go | nil pointer, index out of range... | go.mod | — | go build |
| Rust | error[E0425], panic... | Cargo.toml | — | cargo build |

语言检测由 Analyzer Agent 的 LLM 根据错误文本和代码自动识别，无需手动指定。

## 架构概览

```
patchflow/
├── cli.py                    # CLI 入口（click 框架）
│
├── core/                     # 核心调度 + 基础设施
│   ├── orchestrator.py       # 单 Agent 调度器（生成→验证→修复）
│   ├── agent_orchestrator.py # 多 Agent Blackboard 调度器
│   ├── planner.py            # Plan 计划生成与分步执行
│   ├── repl.py               # 交互式 REPL 循环
│   ├── chat_client.py        # LLM 对话客户端（流式+工具调用+跨会话记忆）
│   ├── config.py             # 配置系统（多模型管理）
│   ├── llm_client.py         # LLM API 调用封装（指数退避重试）
│   ├── agent_sandbox.py      # Agent 隔离沙箱
│   └── language_registry.py  # 多语言注册中心（6种语言）
│
├── core/analysis/            # 错误分析模块
│   ├── error_parser.py       # 多语言错误文本解析
│   ├── error_analyzer.py     # 精准错误分析（traceback 解析+调用链）
│   └── strategy_selector.py  # 修复策略选择（12种错误类型映射）
│
├── core/fix/                 # 修复执行模块
│   ├── fixer.py              # 自动修复执行（硬约束）
│   ├── generator.py          # 代码生成
│   ├── validator.py          # 多语言编译/运行/测试验证
│   ├── scope_calculator.py   # 代码依赖图 + 影响面计算
│   ├── snapshot_manager.py   # 快照回滚管理
│   ├── breaker.py            # 修复循环熔断器
│   └── conflict_detector.py  # Lazy Diff 冲突检测
│
├── core/project/             # 项目理解模块
│   ├── context_collector.py  # 多语言项目上下文收集
│   ├── context_manager.py    # 上下文压缩（三层压缩策略）
│   └── codebase_index.py     # 代码索引（AST + 向量嵌入）
│
├── agents/                   # 多 Agent 定义
│   ├── analyzer.py           # 问题定位 Agent（LLM 自动识别语言）
│   ├── fixer_agent.py        # 修复执行 Agent
│   ├── reviewer.py           # 审查 Agent
│   ├── blackboard.py         # 共享黑板数据结构（含活动追踪）
│   └── schema.py             # Agent 输出合约校验
│
└── utils/
    ├── runner.py             # subprocess 封装
    ├── logger.py             # 日志系统
    ├── diff.py               # 代码 diff 工具
    ├── agent_display.py      # 多 Agent 流水线可视化面板（含读写追踪）
    └── code_reviewer.py      # 代码审查工具
```

## 技术栈

| 模块 | 技术 |
|------|------|
| CLI | click |
| LLM | OpenAI / Anthropic / DeepSeek |
| 执行 | subprocess |
| 终端渲染 | rich |
| 项目分析 | Python AST（标准库）|
| 向量嵌入 | numpy |
| 配置 | JSON（多层合并）|
| 语言抽象 | LanguageRegistry（注册器模式）|

## 开发

```bash
# 克隆
git clone https://github.com/your-org/patchflow.git
cd patchflow

# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest
```

---

[MIT License](LICENSE)
