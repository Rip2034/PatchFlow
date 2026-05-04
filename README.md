# PatchFlow

> AI 驱动的代码生成与自动修复 CLI 工具
>
> AI-powered code generation and auto-fix CLI tool

[中文文档](README.zh.md) · [English Documentation](README.en.md)

***

## 核心理念 / Core Philosophy

**让工具代替开发者做最枯燥的事，而不是替代开发者本身。**

PatchFlow 的出发点不是"让 AI 写代码"，而是"让 AI 自动执行『写代码 → 运行 → 报错 → 分析 → 修复 → 再运行』这个循环"。开发者只需要描述需求、审查结果、做关键决策。

与 Cursor / Claude Code / GitHub Copilot 等工具相比，PatchFlow 走了一条完全不同的路——**用确定性算法控制 LLM，而不是让 LLM 自由发挥**。

***

## 核心差异化 / Key Differentiators

### 1. 算法驱动，而非 LLM 驱动

![与传统编程工具核心差异化](A:\Fixly\patchflow\staticResource\与传统编程工具核心差异化.png)

关键区别：**PatchFlow 不信任 LLM 的判断力，而是用算法搭建护栏。**

### 2. 三层硬约束体系

PatchFlow 构建了三层互不信任的检查机制：

![三层硬约束体系架构](A:\Fixly\patchflow\staticResource\三层硬约束体系架构.png)

**LLM 的任务是"在圈定的范围内写代码"，而不是"判断应该改什么"。**

### 3. Blackboard 多 Agent 协作

三个职责单一的 Agent 通过共享黑板数据交换（彼此不直接通信）：

```
          ┌──────────────────────────┐
          │      Blackboard           │
          │  ┌─────────┬───────────┐  │
          │  │ analysis│ fix_plan  │  │
          │  └─────────┴───────────┘  │
          │  ┌──────────────────────┐ │
          │  │  review / feedback  │ │
          │  └──────────────────────┘ │
          └──────────────────────────┘
                ▲          ▲
        写入    │          │  写入
      ┌────────┴──┐    ┌──┴────────┐
      │ Analyzer  │ → │   Fixer   │ → Reviewer →
      │ 只分析不提  │    │ 只修复不评估│   独立审查
      │ 修复方案   │    │           │   可驳回重做
      └───────────┘    └───────────┘
```

- **Analyzer**：只说问题在哪，不提修复方案。职责单一防止"自我审查放水"
- **Fixer**：根据分析结果和策略约束执行修复，只能在算法圈定范围内行动
- **Reviewer**：独立审查修复方案，可以驳回让 Fixer 重做（最多一次重试）

每个 Agent 的输出格式固定（`schema.py`），包含 `summary`（≤150 字符）、`language`（LLM 自动识别）等字段。支持为不同角色配置不同模型。CLI 面板实时显示每个 Agent 对 Blackboard 的读写活动。

### 4. 计划驱动（Plan Mode）

用户输入任务 → AI 输出结构化分步计划 → 用户确认 → 逐步骤执行 → 每步可见进度：

```
$ patchflow plan "创建一个 FastAPI TODO 应用"
┌────────────────────────────────────────────────────┐
│ Plan: FastAPI TODO 应用                             │
├────┬────────────────────┬──────────────────────────┤
│ #  │ Step               │ Description               │
├────┼────────────────────┼──────────────────────────┤
│ 1  │ 项目初始化         │ 创建项目骨架 (pyproject.toml, app.py) │
│ 2  │ 数据库模型         │ 创建 SQLAlchemy 模型 (models.py)    │
│ 3  │ API 路由           │ 创建 CRUD 路由 (routes.py)          │
│ 4  │ 入口整合           │ 整合所有模块 (main.py)               │
└────┴────────────────────┴──────────────────────────┘
是否按此计划执行? (y/n) > y
[1/4] ✓ 项目初始化 → pyproject.toml, app.py
[2/4] ✓ 数据库模型 → models.py
[3/4] ✓ API 路由   → routes.py
[4/4] ✓ 入口整合   → main.py
✓ 验证通过 | 成功完成! (4 步)
```

### 5. 多语言抽象层

通过 `LanguageRegistry` 注册器模式统一管理语言特质，新增语言只需注册一个 `LanguageDescriptor`：

```python
# 语言注册：一行声明该语言的全部特质
_BUILTINS["python"] = LanguageDescriptor(
    name="python",
    extensions={".py"},
    project_files=["pyproject.toml", "setup.py"],
    traceback_patterns=[re.compile(r'File "(.+?)", line (\d+)')],
    error_classifiers={"SyntaxError": "syntax", "TypeError": "type", ...},
    run_command="python",
)
```

各模块通过注册中心委派工作，不再硬编码 Python 格式：

| 模块                 | 之前（硬编码 Python）                | 之后（委派给 LanguageRegistry）             |
| ------------------ | ----------------------------- | ------------------------------------ |
| error\_analyzer    | 正则匹配 `File "xxx.py"`          | 按语言选择 traceback 模式                   |
| scope\_calculator  | `rglob("*.py")` + `ast.parse` | 按语言选择 import 解析器                     |
| context\_collector | 只认 `pyproject.toml`           | 覆盖 6 种语言的包配置                         |
| validator          | 只跑 `python xxx.py`            | 按语言选 `node` / `javac` / `go build` 等 |

### 6. 跨会话智能记忆

历史对话不原样存储，而是压缩为结构化摘要：

```
存磁盘：                       下次启动 LLM 看到：
{                              === 之前会话摘要 ===
  "summary": [                 - 帮我写一个登录模块, 用JWT认证
    "帮我写一个登录模块...",      - 文件: auth.py, app.py
    "修复数据库查询慢...",       - 修复数据库查询慢; 建议加索引
    "为文章列表添加分页..."      - 为文章列表添加分页; 文件: views.py
  ],                             === new session ===
  "messages": [...]              (继续新的对话...)
}
```

- 过程中的读写、报错、重试全部丢弃，只保留"做了什么 + 结果"
- 500KB 自动裁剪，最近 3 轮保持完整原始消息

***

## 架构 / Architecture

```
patchflow/
├── cli.py
├── core/                           # 核心调度 + 基础设施
│   ├── orchestrator.py             # 单 Agent 调度器
│   ├── agent_orchestrator.py       # 多 Agent Blackboard 调度器
│   ├── planner.py                  # Plan 计划驱动生成
│   ├── repl.py                     # 交互式 REPL
│   ├── chat_client.py              # LLM 对话客户端（跨会话记忆）
│   ├── config.py                   # 多模型配置
│   ├── llm_client.py               # LLM 调用封装
│   └── language_registry.py        # 多语言注册中心
├── core/analysis/                  # 错误分析
│   ├── error_parser.py             # 多语言错误解析
│   ├── error_analyzer.py           # 精准分析（traceback + 调用链）
│   └── strategy_selector.py        # 12种错误 → 策略映射
├── core/fix/                       # 修复执行
│   ├── fixer.py / generator.py / validator.py
│   ├── scope_calculator.py         # 依赖图 + 影响面计算
│   ├── snapshot_manager.py         # 快照回滚
│   └── breaker.py                  # 熔断器
├── core/project/                   # 项目理解
│   ├── context_collector.py        # 多语言上下文收集
│   ├── context_manager.py          # 三层压缩
│   └── codebase_index.py           # 代码索引
├── agents/                         # 多 Agent
│   ├── analyzer.py / fixer_agent.py / reviewer.py
│   ├── blackboard.py               # 黑板数据结构（活动追踪）
│   └── schema.py                   # 通信合约
└── utils/
    ├── runner.py / logger.py / diff.py
    ├── agent_display.py            # 流水线可视化
    └── code_reviewer.py
```

***

## 技术栈 / Tech Stack

| 模块   | 技术                            |
| ---- | ----------------------------- |
| CLI  | click                         |
| LLM  | OpenAI / Anthropic / DeepSeek |
| 终端渲染 | rich                          |
| 项目分析 | Python AST                    |
| 语言抽象 | LanguageRegistry（注册器模式）       |
| 配置   | JSON 多层合并                     |

***

[MIT License](LICENSE)
