# PatchFlow

> AI-powered code generation and auto-fix CLI tool

---

## Overview

PatchFlow is an AI-powered CLI tool that closes the loop from "task description" to "working code output." Its core philosophy: **automate the tedious cycle of "write code → run → fail → analyze → fix → re-run."**

Unlike Cursor or Claude Code, PatchFlow distinguishes itself by using **deterministic algorithms to constrain LLM behavior** — AST parsing, dependency graph analysis, and strategy selectors precisely limit what files the LLM can modify, rather than letting AI act freely.

## Features

- **Interactive REPL** — Chat-driven programming with tool-calling
- **`/plan` Plan-driven generation** — AI outputs a structured step-by-step plan; user confirms before execution with visible progress
- **`/build` One-shot generation** — Generate runnable code from description with auto-verification
- **`/fix` Multi-agent collaboration** — Analyzer → Fixer → Reviewer pipeline; assign different models per role
- **Multi-model management** — Configure multiple LLMs (Anthropic / OpenAI / DeepSeek), switch at runtime
- **Automatic snapshots & rollback** — Snapshot files before each fix, auto-rollback on failure
- **Hard-constrained fixing** — Strategy Selector algorithmically determines editable file scope; LLM cannot touch out-of-scope files
- **Project-aware** — AST-based dependency graph and function signature map for precise impact analysis
- **Cross-session memory** — Auto-compresses conversation history into summaries; restores context on next launch
- **Multi-language support** — LanguageRegistry abstraction supports Python / JavaScript / TypeScript / Java / Go / Rust
- **Cross-platform** — Windows / macOS / Linux

## Quick Start

```bash
# Install
pip install patchflow

# First-time setup (interactive)
patchflow config init

# Start interactive session
patchflow

# Plan-driven generation (recommended for larger projects)
patchflow plan "create a FastAPI TODO app"

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
| `patchflow plan <task>` | Plan-driven step-by-step code generation |
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
| `/plan` | Plan-driven step-by-step generation |
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

- **Analyzer** — Identifies the problem without suggesting fixes. LLM auto-detects the project language
- **Fixer** — Executes fixes within the algorithmically-determined scope
- **Reviewer** — Independently reviews the fix; can reject and request redo (max one retry)

Each agent's output follows a fixed schema (defined in `schema.py`) with mandatory `summary` (≤150 chars) and `language` (auto-detected by LLM) fields. Different models can be assigned to different roles (e.g., cheap DeepSeek for analysis, strong Claude for fixing). The CLI panel shows each agent's Blackboard read/write activity in real time.

## Language Support

PatchFlow supports multiple languages through the `LanguageRegistry` abstraction layer, automatically matching each language's traceback format and error classification:

| Language | Error Recognition | Project Config | Dep Parser | Run/Compile |
|----------|-----------------|---------------|-----------|-------------|
| Python | SyntaxError, TypeError, ImportError... | pyproject.toml, setup.py | AST | python |
| JavaScript | TypeError, ReferenceError... | package.json | Regex | node |
| TypeScript | TS errors, TypeError... | tsconfig.json | Regex | tsc + node |
| Java | NullPointerException, ClassNotFoundException... | pom.xml, build.gradle | — | javac + java |
| Go | nil pointer, index out of range... | go.mod | — | go build |
| Rust | error[E0425], panic... | Cargo.toml | — | cargo build |

Language detection is done by the Analyzer Agent's LLM based on the error text and code — no manual specification needed.

## Architecture Overview

```
patchflow/
├── cli.py                    # CLI entry point (click)
│
├── core/                     # Core orchestration + infrastructure
│   ├── orchestrator.py       # Single-agent orchestrator (generate→verify→fix)
│   ├── agent_orchestrator.py # Multi-agent Blackboard orchestrator
│   ├── planner.py            # Plan generation and step-by-step execution
│   ├── repl.py               # Interactive REPL loop
│   ├── chat_client.py        # LLM chat client (streaming + tool calls + cross-session memory)
│   ├── config.py             # Configuration system (multi-model)
│   ├── llm_client.py         # LLM API call wrapper (exponential backoff)
│   ├── agent_sandbox.py      # Agent isolation sandbox
│   └── language_registry.py  # Multi-language registry (6 languages)
│
├── core/analysis/            # Error analysis module
│   ├── error_parser.py       # Multi-language error text parsing
│   ├── error_analyzer.py     # Precision error analysis (traceback + call chain)
│   └── strategy_selector.py  # Fix strategy selection (12 error type mappings)
│
├── core/fix/                 # Fix execution module
│   ├── fixer.py              # Auto-fix execution (hard constraints)
│   ├── generator.py          # Code generation
│   ├── validator.py          # Multi-language compile/run/test validation
│   ├── scope_calculator.py   # Code dependency graph + impact analysis
│   ├── snapshot_manager.py   # Snapshot/rollback management
│   ├── breaker.py            # Fix loop circuit breaker
│   └── conflict_detector.py  # Lazy diff conflict detection
│
├── core/project/             # Project understanding module
│   ├── context_collector.py  # Multi-language project context collection
│   ├── context_manager.py    # Context compression (3-layer strategy)
│   └── codebase_index.py     # Code index (AST + vector embeddings)
│
├── agents/                   # Multi-agent definitions
│   ├── analyzer.py           # Problem analysis Agent (LLM auto-detects language)
│   ├── fixer_agent.py        # Fix execution Agent
│   ├── reviewer.py           # Code review Agent
│   ├── blackboard.py         # Shared Blackboard data structure (with activity tracking)
│   └── schema.py             # Agent output contract validation
│
└── utils/
    ├── runner.py             # subprocess wrapper
    ├── logger.py             # Logging system
    ├── diff.py               # Code diff utility
    ├── agent_display.py      # Multi-agent pipeline visualization (with read/write tracking)
    └── code_reviewer.py      # Code review tool
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
| Language Abstraction | LanguageRegistry (registry pattern) |

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
