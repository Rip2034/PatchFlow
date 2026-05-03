# PatchFlow

> AI-powered code generation and auto-fix CLI tool

---

## Overview

PatchFlow is an AI-powered CLI tool that closes the loop from "task description" to "working code output." Its core philosophy: **automate the tedious cycle of "write code → run → fail → analyze → fix → re-run."**

Unlike Cursor or Claude Code, PatchFlow distinguishes itself by using **deterministic algorithms to constrain LLM behavior** — AST parsing, dependency graph analysis, and strategy selectors precisely limit what files the LLM can modify, rather than letting AI act freely.

## Features

- **Interactive REPL** — Chat-driven programming with tool-calling
- **`/build` One-shot generation** — Generate runnable code from description with auto-verification
- **`/fix` Multi-agent collaboration** — Analyzer → Fixer → Reviewer pipeline; assign different models per role
- **Multi-model management** — Configure multiple LLMs (Anthropic / OpenAI / DeepSeek), switch at runtime
- **Automatic snapshots & rollback** — Snapshot files before each fix, auto-rollback on failure
- **Hard-constrained fixing** — Strategy Selector algorithmically determines editable file scope; LLM cannot touch out-of-scope files
- **Project-aware** — AST-based dependency graph and function signature map for precise impact analysis
- **Cross-session memory** — Auto-compresses conversation history into summaries; restores context on next launch
- **Cross-platform** — Windows / macOS / Linux

## Quick Start

```bash
# Install
pip install patchflow

# First-time setup (interactive)
patchflow config init

# Start interactive session
patchflow

# One-shot generation
patchflow build "create a CLI calculator"

# Multi-agent fix
patchflow fix "fix syntax errors in app.py"
```

## Configuration

Configuration lives at `~/.patchflow/config.json`, supporting multiple model profiles:

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
# CLI configuration
patchflow config set api_key sk-ant-xxx
patchflow config set model claude-sonnet-4-20250514

# Multi-model management
patchflow model add my-ds deepseek deepseek-chat sk-xxx
patchflow model use my-ds
patchflow model list
```

## Commands

### CLI Commands

| Command | Description |
|---------|-------------|
| `patchflow` | Enter interactive REPL mode |
| `patchflow chat` | Same as above |
| `patchflow build <task>` | One-shot code generation with auto-fix |
| `patchflow fix <task>` | Multi-agent collaborative code repair |
| `patchflow analyze` | Analyze current project structure |
| `patchflow status` | View cache and snapshot status |
| `patchflow config set/show/init` | Configuration management |
| `patchflow model add/use/list/remove` | Multi-model management |

### REPL Built-in Commands

| Command | Description |
|---------|-------------|
| `/help` | Display help |
| `/exit` or `/quit` | Exit REPL |
| `/clear` | Clear conversation history |
| `/history` | Show conversation stats |
| `/memory` | Show memory status (cross-session) |
| `/model` | List/switch models |
| `/build` | Generate code with auto-verification |
| `/fix` | Multi-agent collaborative fix |
| `/context` | View context statistics |
| `/init` | Create project-level rules file |
| `/stop <pid>` | Stop background process |
| `/ps` | List background processes |

## Multi-Agent Architecture

PatchFlow employs the **Blackboard pattern** for multi-agent collaboration. Three agents communicate **indirectly** through a shared Blackboard data structure:

```
┌──────────────────────────────────────────────┐
│                 Blackboard                     │
│  ┌──────────┬──────────┬──────────┐           │
│  │ analysis │ fix_plan │  review  │           │
│  │ (written:│ (written:│ (written:│           │
│  │ Analyzer)│  Fixer)  │ Reviewer)│           │
│  └──────────┴──────────┴──────────┘           │
└──────────────────────────────────────────────┘
        ▲            ▲            ▲
        │            │            │
   ┌────┴───┐   ┌───┴────┐   ┌──┴──────┐
   │Analyzer│ → │ Fixer  │ → │Reviewer │
   │ Analyze│   │ Execute│   │  Review │
   └────────┘   └────────┘   └─────────┘
```

- **Analyzer** — Identifies the problem without suggesting fixes. Single responsibility prevents contamination
- **Fixer** — Executes fixes within the algorithmically-determined scope
- **Reviewer** — Independently reviews the fix; can reject and request redo (max one retry)

Each agent's output follows a fixed schema (defined in `schema.py`) with a mandatory `summary` field (≤150 chars) for efficient cross-agent reading. Different models can be assigned to different roles (e.g., cheap DeepSeek for analysis, strong Claude for fixing).

## Architecture Overview

```
patchflow/
├── cli.py                    # CLI entry point (click)
├── core/
│   ├── orchestrator.py       # Single-agent orchestrator (generate→verify→fix)
│   ├── agent_orchestrator.py # Multi-agent Blackboard orchestrator
│   ├── repl.py               # Interactive REPL loop
│   ├── chat_client.py        # LLM chat client (streaming + tool calls)
│   ├── generator.py          # Code generation
│   ├── validator.py          # Compilation/running/testing validation
│   ├── fixer.py              # Auto-fix execution
│   ├── error_parser.py       # Regex-based error extraction
│   ├── error_analyzer.py     # Precision error analysis (traceback + call chain)
│   ├── context_collector.py  # Project context collection (deterministic scan)
│   ├── context_manager.py    # Context compression (3-layer strategy)
│   ├── strategy_selector.py  # Fix strategy selection (hard constraints)
│   ├── scope_calculator.py   # Code dependency graph + impact analysis
│   ├── snapshot_manager.py   # Snapshot/rollback management
│   ├── conflict_detector.py  # Lazy diff conflict detection
│   ├── agent_sandbox.py      # Agent isolation sandbox
│   ├── breaker.py            # Fix loop circuit breaker
│   ├── llm_client.py         # LLM API call wrapper
│   ├── codebase_index.py     # Code index (AST + vector embeddings)
│   └── config.py             # Configuration system (multi-model)
├── agents/
│   ├── analyzer.py           # Problem analysis Agent
│   ├── fixer_agent.py        # Fix execution Agent
│   ├── reviewer.py           # Code review Agent
│   ├── blackboard.py         # Shared Blackboard data structure (with activity tracking)
│   └── schema.py             # Agent output contract validation
└── utils/
    ├── runner.py             # subprocess wrapper
    ├── logger.py             # Logging system
    ├── diff.py               # Code diff utility
    └── agent_display.py      # Multi-agent pipeline visualization panel
```

## Tech Stack

| Module | Technology |
|--------|-----------|
| CLI | click |
| LLM | OpenAI / Anthropic / DeepSeek |
| Execution | subprocess |
| Terminal Rendering | rich |
| Project Analysis | Python AST (stdlib) |
| Vector Embeddings | numpy |
| Configuration | JSON (multi-layer merge) |

## Development

```bash
# Clone
git clone https://github.com/your-org/patchflow.git
cd patchflow

# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest
```

---

[MIT License](LICENSE)
