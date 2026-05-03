"""PatchFlow CLI — 用户交互入口

这是用户看到的"门面"。它只做三件事：
  1. 解析命令行参数（用 click 库）
  2. 显示帮助信息
  3. 把任务分派给对应的模块

设计原则（借鉴 Claude Code 的 cli.tsx）：
  - CLI 层尽量薄：只负责参数解析和路由
  - 默认进入 REPL 交互模式（像 Claude Code 那样）

使用方式：
  patchflow                    → 进入 REPL 交互对话
  patchflow chat               → 同上
  patchflow build "任务描述"    → 一次性生成+验证
  patchflow config set ...     → 管理配置
"""

import sys
import click

from patchflow.utils import logger


@click.group(invoke_without_command=True)
@click.version_option(version="0.1.0", prog_name="patchflow")
@click.option(
    "--model", "-m",
    default=None,
    help="使用的 LLM 模型",
)
@click.pass_context
def main(ctx: click.Context, model: str | None):
    """PatchFlow — AI 驱动的代码生成与自动修复工具

    \b
    直接运行进入交互对话模式（默认）:
      patchflow

    \b
    一次性生成代码:
      patchflow build "创建一个命令行计算器"

    \b
    配置管理:
      patchflow config set api_key <your-key>
    """
    # 如果用户没指定子命令 → 进入 REPL
    # invoke_without_command=True 让 click 在无子命令时不报错
    # ctx.invoked_subcommand 为 None 表示用户只敲了 patchflow
    if ctx.invoked_subcommand is None:
        # 延迟导入 —— 只在真正进入 REPL 时才加载
        from patchflow.core.repl import start_repl
        start_repl(model=model)


# ═══════════════════════════════════════════════════════════
# chat 命令 — 显式进入 REPL
# ═══════════════════════════════════════════════════════════

@main.command()
@click.option(
    "--model", "-m",
    default=None,
    help="使用的 LLM 模型",
)
def chat(model: str | None):
    """进入交互对话模式

    \b
    启动后你可以:
      - 直接输入问题或任务 → AI 回复
      - /help   → 查看帮助
      - /exit   → 退出
      - /clear  → 清空对话历史
      - /build  → 生成代码并自动验证
    """
    from patchflow.core.repl import start_repl
    start_repl(model=model)


# ═══════════════════════════════════════════════════════════
# build 命令 — 一次性生成
# ═══════════════════════════════════════════════════════════

@main.command()
@click.argument("task", type=str)
@click.option(
    "--model", "-m",
    default=None,
    help="使用的 LLM 模型（不指定则从配置文件读取）",
)
@click.option(
    "--max-retries", "-r",
    default=None,
    type=int,
    help="最大修复尝试次数（不指定则从配置文件读取）",
)
@click.option(
    "--work-dir", "-w",
    default=".",
    help="工作目录",
)
def build(task: str, model: str | None, max_retries: int | None, work_dir: str):
    """从任务描述生成可运行的代码（一次性模式）

    \b
    示例:
      patchflow build "创建一个 FastAPI 登录 API"
      patchflow build "写一个 Python 爬虫抓取网页标题" -m claude-sonnet-4-20250514
    """
    from patchflow.core.orchestrator import Orchestrator
    from patchflow.core.config import get_config

    cfg = get_config()

    if model is None:
        model = cfg["model"]
    if max_retries is None:
        max_retries = cfg["max_retries"]

    logger.info(f"任务: {task}")
    logger.info(f"模型: {model}")

    orch = Orchestrator(model=model, max_retries=max_retries, work_dir=work_dir)
    success = orch.run(task)

    if success:
        logger.success("任务完成！")
    else:
        logger.error("经过多次尝试仍无法完成，请手动检查。")


# ═══════════════════════════════════════════════════════════
# plan 命令 — 计划驱动分步骤生成
# ═══════════════════════════════════════════════════════════

@main.command()
@click.argument("task", type=str)
@click.option(
    "--model", "-m",
    default=None,
    help="使用的 LLM 模型（不指定则从配置文件读取）",
)
@click.option(
    "--work-dir", "-w",
    default=".",
    help="工作目录",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    default=False,
    help="跳过确认直接执行",
)
def plan(task: str, model: str | None, work_dir: str, yes: bool):
    """制定计划后分步骤生成代码

    AI 先分析任务并输出分步计划，用户确认后逐步骤执行。
    相比 build 命令，plan 更适用于大型或复杂任务。

    \b
    示例:
      patchflow plan "创建一个 FastAPI TODO 应用"
      patchflow plan "搭建 React+Express 全栈项目" -y
    """
    from patchflow.core.planner import PlanExecutor
    from patchflow.core.fix.validator import validate
    from rich.console import Console
    from rich.table import Table
    from patchflow.core.config import get_config

    cfg = get_config()
    model = model or cfg["model"]

    logger.info(f"任务: {task}")
    logger.info(f"模型: {model}")

    executor = PlanExecutor(model=model, work_dir=work_dir)

    plan = executor.generate_plan(task)
    if plan is None or not plan.steps:
        logger.error("计划生成失败")
        return

    console = Console()

    # 显示计划
    table = Table(title=f"Plan: {plan.summary}", title_style="bold cyan", border_style="cyan")
    table.add_column("#", style="dim", width=3)
    table.add_column("Step", style="bold", width=30)
    table.add_column("Description", style="dim", width=60)

    for s in plan.steps:
        files_hint = ", ".join(s.files_expected[:3])
        if len(s.files_expected) > 3:
            files_hint += "..."
        desc = f"{s.description} ({files_hint})" if files_hint else s.description
        table.add_row(str(s.step), s.title, desc)

    console.print()
    console.print(table)
    console.print()

    # 确认
    if not yes:
        console.print("[bold]是否按此计划执行? (y/n)[/bold] ", end="")
        try:
            confirm = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = "n"
        if confirm != "y" and confirm != "yes":
            logger.info("计划已取消")
            return

    logger.step("开始执行计划...")

    total = len(plan.steps)
    all_ok = True
    for i in range(total):
        ok = executor.execute_step(i)
        step = plan.steps[i]
        if ok:
            files_str = ", ".join(step.files_written[:3])
            extra = f" (+{len(step.files_written) - 3})" if len(step.files_written) > 3 else ""
            logger.success(f"步骤 {i + 1}/{total} [{step.title}] 完成: {files_str}{extra}")
        else:
            logger.error(f"步骤 {i + 1}/{total} [{step.title}] 失败: {step.error}")
            all_ok = False
            break

    if all_ok:
        logger.success(f"全部 {total} 步执行完成")
        result = validate(work_dir=work_dir)
        if result.ok:
            logger.success("最终验证通过")
        else:
            logger.warn(f"最终验证: {result.message or '未通过'}")
    else:
        logger.error(f"执行中断 (完成 {i + 1}/{total} 步)")


# ═══════════════════════════════════════════════════════════
# fix 命令 — 多 Agent 协作修复
# ═══════════════════════════════════════════════════════════

@main.command()
@click.argument("task", type=str)
@click.option(
    "--model", "-m",
    default=None,
    help="使用的 LLM 模型（不指定则从配置文件读取）",
)
@click.option(
    "--work-dir", "-w",
    default=".",
    help="工作目录",
)
def fix(task: str, model: str | None, work_dir: str):
    """使用多 Agent 协作模式修复代码问题

    启动 Analyzer → Fixer → Reviewer 三个 Agent 协作修复，
    相比 build 命令更适合修复已有代码的 bug。

    \b
    示例:
      patchflow fix "修复 app.py 中的语法错误"
      patchflow fix "解决类型错误" -m claude-sonnet-4-20250514
    """
    from patchflow.core.agent_orchestrator import AgentOrchestrator
    from patchflow.core.config import get_config

    cfg = get_config()
    if model is None:
        model = cfg["model"]

    logger.info(f"任务: {task}")
    logger.info(f"模型: {model}")
    logger.info("启动多 Agent 协作模式 (Analyzer → Fixer → Reviewer)")

    orch = AgentOrchestrator(model=model, work_dir=work_dir)
    success = orch.run_from_task(task, work_dir)

    if success:
        logger.success(f"多 Agent 协作修复完成! (共 {orch.turn_count} 步)")
    else:
        logger.error("修复失败，请手动检查。")


# ═══════════════════════════════════════════════════════════
# config 命令组
# ═══════════════════════════════════════════════════════════

@main.group()
def config():
    """管理配置（API Key、模型等）"""
    pass


@config.command("set")
@click.argument("key", type=str)
@click.argument("value", type=str)
def config_set(key: str, value: str):
    """设置配置项

    \b
    示例:
      patchflow config set api_key sk-ant-xxxxxxxxxxxxx
      patchflow config set model claude-sonnet-4-20250514
      patchflow config set max_retries 5
      patchflow config set agents.analyzer deepseek
      patchflow config set agents.fixer claude
      patchflow config set agents.reviewer deepseek
    """
    from patchflow.core.config import set_user_config

    valid_top = ("api_key", "model", "max_retries", "provider", "api_base", "token_budget")
    valid_agents = ("agents.analyzer", "agents.fixer", "agents.reviewer")
    if key not in valid_top and key not in valid_agents:
        logger.error(f"未知配置项: {key}")
        logger.info(f"可用配置项: {', '.join(valid_top)}")
        logger.info(f"Agent 映射: {', '.join(valid_agents)}")
        return

    display_value = value[:10] + "..." if key == "api_key" and len(value) > 10 else value

    try:
        set_user_config(key, value)
        logger.success(f"已设置 {key} = {display_value}")
        logger.info(f"配置文件位置: ~/.patchflow/config.json")
    except ValueError as e:
        logger.error(str(e))


@config.command("show")
def config_show():
    """查看当前配置"""
    from patchflow.core.config import list_models, get_config

    cfg = get_config()
    models = list_models()
    active = cfg["active"]

    click.echo()
    click.echo(f"  当前模型: [{active}]")
    click.echo(f"    provider:  {cfg['provider']}")
    click.echo(f"    model:     {cfg['model']}")
    key = cfg["api_key"]
    click.echo(f"    api_key:   {key[:10] + '...' if key else '(未设置)'}")
    if cfg["api_base"]:
        click.echo(f"    api_base:  {cfg['api_base']}")
    click.echo(f"    max_retries: {cfg['max_retries']}")
    click.echo()

    agents_cfg = cfg.get("agents", {})
    if agents_cfg:
        any_mapped = any(v for v in agents_cfg.values())
        if any_mapped:
            click.echo(f"  多 Agent 角色映射:")
            for role, alias in agents_cfg.items():
                if alias:
                    click.echo(f"    {role}: [{alias}]")
                else:
                    click.echo(f"    {role}: (使用默认模型 [{active}])")
        else:
            click.echo(f"  多 Agent: 全部使用默认模型 [{active}]")
    else:
        click.echo(f"  多 Agent: 全部使用默认模型 [{active}]")

    if models:
        click.echo(f"  已配置 {len(models)} 个模型:")
        for alias, mcfg in models.items():
            m = " [当前]" if alias == active else ""
            click.echo(f"    [{alias}]{m}: {mcfg['provider']}/{mcfg['model']}")
        click.echo()


@config.command("init")
def config_init():
    """交互式首次配置 — 设置模型和 Agent"""
    from patchflow.core.config import _user_config_dir, _save_json, _load_json, PROVIDER_DEFAULTS

    click.echo()
    click.echo("  PatchFlow 首次配置")
    click.echo(f"  配置文件: {_user_config_dir() / 'config.json'}")
    click.echo()

    # 1. 选择 provider
    click.echo("  选择 AI 服务商:")
    click.echo("    1. Anthropic Claude  (推荐)")
    click.echo("    2. OpenAI")
    click.echo("    3. DeepSeek")
    prov_map = {"1": "anthropic", "2": "openai", "3": "deepseek"}
    provider = prov_map.get(click.prompt("  请输入编号", default="1"), "anthropic")

    # 2. 模型别名（默认为 provider 名称）
    alias = click.prompt("  模型别名", default=provider).strip() or provider

    # 3. API Key
    key_hint = "sk-" if provider in ("deepseek", "openai") else "sk-ant-"
    api_key = click.prompt(f"  请输入 {provider.title()} API Key（{key_hint}...）", hide_input=True).strip()
    if not api_key:
        click.echo("  [red]API Key 不能为空[/red]")
        return

    # 4. 模型名称
    pdefaults = PROVIDER_DEFAULTS.get(provider, {})
    model_default = pdefaults.get("model", "")
    model_name = click.prompt("  模型名称", default=model_default).strip() or model_default

    # 5. API Base（可选，回车使用默认）
    api_base_default = pdefaults.get("api_base", "")
    api_base = click.prompt("  API Base URL（回车使用默认）", default=api_base_default).strip()

    # 6. 最大重试次数
    max_retries = click.prompt("  最大重试次数", default=3, type=int)

    # ── 写入新格式 ──
    user_path = _user_config_dir() / "config.json"
    cfg = _load_json(user_path)
    if "models" not in cfg:
        cfg["models"] = {}
    cfg["models"][alias] = {
        "provider": provider,
        "model": model_name,
        "api_key": api_key,
        "api_base": api_base,
    }
    cfg["active"] = alias
    cfg["max_retries"] = max_retries
    cfg["agents"] = {
        "analyzer": alias,
        "fixer": alias,
        "reviewer": alias,
    }
    _save_json(user_path, cfg)

    click.echo()
    click.echo(f"  [green]配置完成![/green]")
    click.echo(f"    服务商: {provider}")
    click.echo(f"    模型:   [{alias}] {model_name}")
    click.echo(f"    API 地址: {api_base or '（默认）'}")
    click.echo(f"    最大重试: {max_retries} 次")
    click.echo()
    click.echo(f"  三个 Agent（分析/修复/审查）默认都使用 [{alias}] 模型")
    click.echo(f"  如需为不同 Agent 指定不同模型：")
    click.echo(f"    [cyan]patchflow config set agents.analyzer <别名>[/cyan]")
    click.echo(f"    [cyan]patchflow config set agents.fixer <别名>[/cyan]")
    click.echo(f"    [cyan]patchflow config set agents.reviewer <别名>[/cyan]")
    click.echo()
    click.echo(f"  运行 [cyan]patchflow config show[/cyan] 查看配置")
    click.echo(f"  运行 [cyan]patchflow[/cyan] 开始使用")


# ═══════════════════════════════════════════════════════════
# model 命令组 — 多模型管理
# ═══════════════════════════════════════════════════════════

@main.group()
def model():
    """管理多个 AI 模型配置"""
    pass


@model.command("list")
def model_list():
    """列出所有已配置的模型"""
    from patchflow.core.config import list_models, get_config

    models = list_models()
    active = get_config()["active"]

    if not models:
        click.echo()
        click.echo("  还没有配置任何模型")
        click.echo()
        click.echo("  添加模型:")
        click.echo("    patchflow model add <别名> <厂商> <模型名> <api_key>")
        click.echo()
        click.echo("  示例:")
        click.echo("    patchflow model add my-ds deepseek deepseek-chat sk-xxx")
        click.echo("    patchflow model add my-claude anthropic claude-sonnet-4-20250514 sk-ant-xxx")
        return

    click.echo()
    for alias, cfg in models.items():
        marker = " [green](当前)[/green]" if alias == active else ""
        key_display = cfg["api_key"][:10] + "..." if cfg.get("api_key") else "(未设置)"
        click.echo(f"  [{alias}]{marker}")
        click.echo(f"    厂商: {cfg['provider']}  模型: {cfg['model']}")
        click.echo(f"    Key:  {key_display}")
        if cfg.get("api_base"):
            click.echo(f"    Base: {cfg['api_base']}")
    click.echo()


@model.command("add")
@click.argument("alias", type=str)
@click.argument("provider", type=str)
@click.argument("model_name", type=str)
@click.argument("api_key", type=str)
@click.option("--base", "-b", default="", help="自定义 API Base URL")
def model_add(alias: str, provider: str, model_name: str, api_key: str, base: str):
    """添加一个新的模型配置

    \b
    示例:
      patchflow model add my-ds deepseek deepseek-chat sk-xxx
      patchflow model add my-claude anthropic claude-sonnet-4-20250514 sk-ant-xxx
      patchflow model add my-custom openai gpt-4o sk-xxx -b https://my-proxy.com/v1
    """
    from patchflow.core.config import add_model

    add_model(alias, provider, model_name, api_key, base)

    key_display = api_key[:10] + "..." if len(api_key) > 10 else api_key
    logger.success(f"已添加模型 [{alias}]: {provider}/{model_name}")
    logger.success(f"Key: {key_display}")
    if base:
        logger.info(f"Base: {base}")
    logger.info(f"使用: patchflow model use {alias}")


@model.command("use")
@click.argument("alias", type=str)
def model_use(alias: str):
    """切换到指定的模型

    \b
    示例:
      patchflow model use deepseek
      patchflow model use my-claude
    """
    from patchflow.core.config import set_active_model

    if set_active_model(alias):
        logger.success(f"已切换到模型 [{alias}]")
    else:
        logger.error(f"模型 [{alias}] 不存在，先用 model add 添加")


@model.command("remove")
@click.argument("alias", type=str)
def model_remove(alias: str):
    """删除一个模型配置

    \b
    示例:
      patchflow model remove my-claude
    """
    from patchflow.core.config import remove_model

    if remove_model(alias):
        logger.success(f"已删除模型 [{alias}]")
    else:
        logger.error(f"模型 [{alias}] 不存在")


# ═══════════════════════════════════════════════════════════
# analyze / status
# ═══════════════════════════════════════════════════════════

@main.command()
@click.option("--work-dir", "-w", default=".", help="工作目录")
@click.option("--module", "-m", default=None, help="深入查看指定模块（可选）")
def analyze(work_dir: str, module: str | None):
    """分析当前项目结构

    显示项目概览：
      - 语言/框架/包管理
      - 模块列表 + 文件数
      - 运行时依赖列表
      - 代码风格约定

    可选添加 --module 深入查看指定模块详情。

    \b
    示例:
      patchflow analyze
      patchflow analyze --module auth
    """
    from patchflow.core.project.context_collector import ContextCollector, build_context_prompt
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console()
    collector = ContextCollector(work_dir)
    ctx = collector.collect(use_cache=False)

    p = ctx.project
    info_lines = [
        f"[bold]Language:[/bold] {p['language']}",
    ]
    if p.get("name"):
        info_lines.append(f"[bold]Project:[/bold] {p['name']}")
    if p.get("framework"):
        info_lines.append(f"[bold]Framework:[/bold] {p['framework']}")
    if p.get("package_manager"):
        info_lines.append(f"[bold]Package Manager:[/bold] {p['package_manager']}")
    if p.get("python_version"):
        info_lines.append(f"[bold]Python:[/bold] {p['python_version']}")

    console.print(Panel("\n".join(info_lines), title="Project Info", border_style="cyan"))
    console.print()

    s = ctx.structure
    if s.get("modules"):
        table = Table(title=f"Modules ({s['total_files']} files in {s['total_dirs']} dirs)")
        table.add_column("Module", style="cyan")
        table.add_column("Role", style="green")
        for mod in s["modules"][:15]:
            table.add_row(mod + "/", "project module")
        if len(s["modules"]) > 15:
            table.add_row(f"... ({len(s['modules']) - 15} more)", "dim")
        console.print(table)
        console.print()

    deps = ctx.dependencies.get("runtime", [])
    if deps:
        dep_str = ", ".join(deps[:20])
        if len(deps) > 20:
            dep_str += f" ... (+{len(deps) - 20} more)"
        console.print(f"[bold]Dependencies:[/bold] {dep_str}")

    cs = ctx.code_style
    indent_str = "tab" if cs.get("indent") == 0 else f"{cs.get('indent', 4)} spaces"
    console.print(f"[bold]Style:[/bold] {indent_str}, {cs.get('naming', '?')}, {cs.get('import_style', '?')} imports")
    console.print()


@main.command()
@click.option("--work-dir", "-w", default=".", help="工作目录")
def status(work_dir: str):
    """查看当前项目的缓存状态

    显示：
      - 上下文缓存状态
      - 快照数量
      - 项目结构是否变化

    \b
    示例:
      patchflow status
    """
    from pathlib import Path
    from rich.console import Console
    from rich.panel import Panel
    from datetime import datetime

    console = Console()
    wd = Path(work_dir)
    patchflow_dir = wd / ".patchflow"

    lines = []
    lines.append(f"[bold]Work Dir:[/bold] {wd.resolve()}")

    context_file = patchflow_dir / "context.json"
    if context_file.exists():
        import json
        data = json.loads(context_file.read_text(encoding="utf-8"))
        cached_at = data.get("_cached_at", "unknown")
        lines.append(f"[green]Context:[/green] cached at {cached_at}")
    else:
        lines.append("[yellow]Context:[/yellow] not built (run 'patchflow analyze')")

    snapshots_dir = patchflow_dir / "snapshots"
    if snapshots_dir.exists():
        snap_count = len([d for d in snapshots_dir.iterdir() if d.is_dir()])
        lines.append(f"[green]Snapshots:[/green] {snap_count} snapshot(s)")
    else:
        lines.append("[dim]Snapshots:[/dim] none")

    lines.append(f"[dim].patchflow size: {_dir_size(patchflow_dir) / 1024:.1f} KB[/dim]")

    console.print(Panel("\n".join(lines), title="PatchFlow Status", border_style="cyan"))


def _dir_size(path) -> int:
    from pathlib import Path
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total
