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

import click

from patchflow.utils import logger


@click.group(invoke_without_command=True)
@click.version_option(version="0.2.0", prog_name="patchflow")
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


# 兼容 REPL 用户的斜杠命令习惯（patchflow /help、patchflow /chat）
@main.command(name="/help", hidden=True)
@click.pass_context
def slash_help(ctx: click.Context):
    """显示帮助信息（支持 /help 和 patchflow --help）"""
    click.echo(ctx.parent.get_help())


@main.command(name="/chat", hidden=True)
@click.option("--model", "-m", default=None, help="使用的 LLM 模型")
def slash_chat(model: str | None):
    """进入 REPL（支持 /chat）"""
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
@click.option(
    "--research/--no-research",
    default=True,
    help="是否在生成前搜索网络文档（默认开启）",
)
def build(task: str, model: str | None, max_retries: int | None, work_dir: str, research: bool):
    """从任务描述生成可运行的代码（一次性模式）

    \b
    示例:
      patchflow build "创建一个 FastAPI 登录 API"
      patchflow build "写一个 Python 爬虫抓取网页标题" -m claude-sonnet-4-20250514
      patchflow build "实现快速排序" --no-research
    """
    from patchflow.core.config import get_config
    from patchflow.core.orchestrator import Orchestrator

    cfg = get_config()

    if model is None:
        model = cfg["model"]
    if max_retries is None:
        max_retries = cfg["max_retries"]

    logger.info(f"任务: {task}")
    logger.info(f"模型: {model}")

    # Web 搜索增强
    web_ctx = ""
    if research:
        try:
            from patchflow.core.web.web_search import web_search, format_search_context
            logger.info("搜索相关文档...")
            results = web_search(task, limit=5)
            if results:
                web_ctx = format_search_context(results, max_chars=2500)
                logger.info(f"找到 {len(results)} 条参考结果")
        except Exception as e:
            logger.debug(f"Web 搜索跳过: {e}")

    orch = Orchestrator(model=model, max_retries=max_retries, work_dir=work_dir)
    success = orch.run(task, web_context=web_ctx)

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
    from rich.console import Console
    from rich.table import Table

    from patchflow.core.config import get_config
    from patchflow.core.fix.validator import validate
    from patchflow.core.planner import PlanExecutor

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
            logger.warn(f"最终验证未通过: {result.message or '验证失败'}")
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

    valid_top = ("api_key", "model", "max_retries", "provider", "api_base", "token_budget", "image_model")
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
        logger.info("配置文件位置: ~/.patchflow/config.json")
    except ValueError as e:
        logger.error(str(e))


@config.command("show")
def config_show():
    """查看当前配置"""
    from patchflow.core.config import get_config, list_models

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
            click.echo("  多 Agent 角色映射:")
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
    from patchflow.core.config import PROVIDER_DEFAULTS, _load_json, _save_json, _user_config_dir

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
    click.echo("  [green]配置完成![/green]")
    click.echo(f"    服务商: {provider}")
    click.echo(f"    模型:   [{alias}] {model_name}")
    click.echo(f"    API 地址: {api_base or '（默认）'}")
    click.echo(f"    最大重试: {max_retries} 次")
    click.echo()
    click.echo(f"  三个 Agent（分析/修复/审查）默认都使用 [{alias}] 模型")
    click.echo("  如需为不同 Agent 指定不同模型：")
    click.echo("    [cyan]patchflow config set agents.analyzer <别名>[/cyan]")
    click.echo("    [cyan]patchflow config set agents.fixer <别名>[/cyan]")
    click.echo("    [cyan]patchflow config set agents.reviewer <别名>[/cyan]")
    click.echo()
    click.echo("  运行 [cyan]patchflow config show[/cyan] 查看配置")
    click.echo("  运行 [cyan]patchflow[/cyan] 开始使用")


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
    from patchflow.core.config import get_config, list_models

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


@model.command("rename")
@click.argument("old_alias", type=str)
@click.argument("new_alias", type=str)
def model_rename(old_alias: str, new_alias: str):
    """重命名模型别名

    \b
    示例:
      patchflow model rename openai gpt-5.5
    """
    from patchflow.core.config import rename_model

    if rename_model(old_alias, new_alias):
        logger.success(f"已将 [{old_alias}] 重命名为 [{new_alias}]")
    else:
        logger.error(f"重命名失败: [{old_alias}] 不存在或 [{new_alias}] 已被占用")


@model.command("edit")
@click.argument("alias", type=str)
@click.option("--provider", "-p", default="", help="厂商 (openai/anthropic/deepseek)")
@click.option("--model", "-m", default="", help="模型名")
@click.option("--key", "-k", default="", help="API Key")
@click.option("--base", "-b", default="", help="API Base URL")
def model_edit(alias: str, provider: str, model: str, key: str, base: str):
    """编辑已有模型配置（只修改指定的字段）

    \b
    示例:
      patchflow model edit my-gpt --model gpt-5.5
      patchflow model edit my-gpt -m gpt-5.5 -p openai -b https://api.openai.com/v1
    """
    from patchflow.core.config import edit_model

    if not any([provider, model, key, base]):
        logger.error("至少指定一个要修改的字段: --provider, --model, --key, --base")
        return

    if edit_model(alias, provider=provider, model=model,
                   api_key=key, api_base=base):
        logger.success(f"已更新模型 [{alias}]")
        if model:
            logger.info(f"  模型名: {model}")
        if provider:
            logger.info(f"  厂商: {provider}")
        if key:
            logger.info(f"  Key:  {key[:10]}...")
        if base:
            logger.info(f"  Base: {base}")
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
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    from patchflow.core.project.context_collector import ContextCollector

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

    console = Console()
    wd = Path(work_dir)
    patchflow_dir = wd / ".patchflow"

    lines = []
    lines.append(f"[bold]Work Dir:[/bold] {wd.resolve()}")

    context_file = patchflow_dir / "context.json"
    if context_file.exists():
        import json
        data = json.loads(context_file.read_text(encoding="utf-8", errors="replace"))
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
    total = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


# ═══════════════════════════════════════════════════════════
# search 命令 — Web 搜索
# ═══════════════════════════════════════════════════════════

@main.command()
@click.argument("query", type=str)
@click.option("--limit", "-n", default=5, help="搜索结果数")
@click.option("--fetch", "-f", is_flag=True, default=False, help="同时抓取页面内容")
def search(query: str, limit: int, fetch: bool):
    """搜索网络并显示结果

    \b
    示例:
      patchflow search "python asyncio gather"
      patchflow search "flask sqlalchemy tutorial" -n 3 -f
    """
    from patchflow.core.web import web_search, web_fetch
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from urllib.parse import urlparse

    results = web_search(query, limit=limit)
    if not results:
        click.echo("  未找到结果")
        return

    console = Console()
    console.print()
    console.print(Panel(
        f"[bold]Search:[/bold] {query}  [dim]({len(results)} results)[/dim]",
        border_style="cyan",
    ))

    for i, r in enumerate(results, 1):
        # 提取域名，让 URL 显示更简洁
        try:
            domain = urlparse(r.url).netloc
        except Exception:
            domain = r.url[:60]

        # 标题
        console.print(f"  [bold cyan]{i}.[/bold cyan] [bold white]{r.title}[/bold white]")

        # URL — 截断过长
        url_display = r.url if len(r.url) <= 90 else r.url[:87] + "..."
        console.print(f"     [dim green]{url_display}[/dim green]")

        # 摘要 — 智能截断
        snippet = r.snippet.strip()
        if snippet:
            if len(snippet) > 200:
                snippet = snippet[:197] + "..."
            console.print(f"     [dim]{snippet}[/dim]")

        console.print()

        # 可选抓取
        if fetch and i <= 2:
            with console.status(f"[dim]Fetching {domain}...[/dim]"):
                fr = web_fetch(r.url)
            if fr.is_ok and fr.markdown:
                preview = fr.markdown[:300].replace("\n", " ")
                console.print(f"     [yellow]Preview:[/yellow] [dim]{preview}...[/dim]")
                console.print()


# ═══════════════════════════════════════════════════════════
# research 命令 — 深度调研
# ═══════════════════════════════════════════════════════════

@main.command()
@click.argument("question", type=str)
@click.option("--depth", "-d", default=3, help="搜索深度")
@click.option("--verify/--no-verify", default=True, help="是否交叉验证")
@click.option("--fast/--full", default=True, help="快速模式（跳过 LLM 合成，默认开启）")
@click.option("--sources", "-s", default=5, help="最大来源数（快速模式默认 5）")
@click.option("--output", "-o", default="", help="输出完整报告到文件")
def research(question: str, depth: int, verify: bool, fast: bool, sources: int, output: str):
    """深度调研一个问题，生成引用报告

    \b
    示例:
      patchflow research "FastAPI vs Flask 性能对比"
      patchflow research "Python async 最佳实践" --full -o report.md
      patchflow research "Django ORM 优化" --fast -s 3
    """
    from patchflow.core.web import deep_research
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # 快速模式参数
    if fast:
        max_sources = min(sources, 8)
        do_verify = False
        mode_label = "fast"
    else:
        max_sources = min(sources, 12)
        do_verify = verify
        mode_label = "full"

    console.print(Panel(
        f"[bold]Research:[/bold] {question}\n"
        f"[dim]mode={mode_label} | depth={depth} | sources≤{max_sources} | verify={do_verify}[/dim]",
        border_style="cyan",
    ))

    report = deep_research(
        question,
        search_depth=depth,
        verify=do_verify,
        max_sources=max_sources,
        fast=fast,
    )

    # 结果面板
    elapsed = f"{report.elapsed_ms / 1000:.1f}s"
    console.print()
    console.print(Panel(
        f"[bold green]Done![/bold green]  "
        f"Confidence: [bold]{report.confidence:.0%}[/bold] | "
        f"Sources: {len(report.sources)} | "
        f"Time: {elapsed}",
        border_style="green",
    ))

    # 摘要
    if report.summary:
        console.print(f"\n  [bold]Summary:[/bold] {report.summary}")

    # 关键发现表格
    if report.findings:
        console.print()
        table = Table(title="Key Findings", border_style="dim")
        table.add_column("#", style="dim", width=3)
        table.add_column("Claim", style="white")
        table.add_column("Sources", style="dim", width=10)

        for i, f in enumerate(report.findings[:8], 1):
            refs = f.get("references", [])
            ref_str = ", ".join(f"[{r}]" for r in refs) if refs else "—"
            claim = f.get("claim", "")
            if len(claim) > 120:
                claim = claim[:117] + "..."
            table.add_row(str(i), claim, ref_str)

        console.print(table)

    # 来源列表（紧凑）
    if report.sources:
        console.print()
        console.print(f"  [dim]Sources ({len(report.sources)}):[/dim]")
        for i, s in enumerate(report.sources[:10], 1):
            title = s.title[:70]
            domain = ""
            try:
                from urllib.parse import urlparse
                domain = urlparse(s.url).netloc
            except Exception:
                pass
            console.print(f"  [dim]{i}.[/dim] [bold]{title}[/bold] [dim green]{domain}[/dim green]")

    if output:
        from pathlib import Path
        Path(output).write_text(report.to_markdown(), encoding="utf-8")
        console.print(f"\n  [green]Report saved to: {output}[/green]")
    elif not fast:
        console.print(f"\n  [dim]Tip: use --output report.md to save full report[/dim]")


# ═══════════════════════════════════════════════════════════
# cron 命令组 — 定时任务
# ═══════════════════════════════════════════════════════════

@main.group()
def cron():
    """管理定时任务（cron 表达式）"""
    pass


@cron.command("add")
@click.argument("cron_expr", type=str)
@click.argument("prompt", type=str)
@click.option("--once", "-o", is_flag=True, default=False, help="一次性任务")
@click.option("--durable", "-d", is_flag=True, default=False, help="持久化到磁盘")
def cron_add(cron_expr: str, prompt: str, once: bool, durable: bool):
    """添加定时任务

    \b
    Cron 格式: 分 时 日 月 周
    示例:
      patchflow cron add "*/30 * * * *" "analyze"
      patchflow cron add "0 9 * * 1-5" "analyze" -d
      patchflow cron add "0 14 15 6 *" "build: release check" -o
    """
    from patchflow.core.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched.load()
    task = sched.add(
        cron_expr, prompt,
        recurring=not once,
        durable=durable,
    )
    if task:
        logger.success(f"任务已添加: {task.id}")
        logger.info(f"  表达式: {task.cron}")
        logger.info(f"  下次执行: {task.next_run_at}")


@cron.command("list")
def cron_list():
    """列出所有定时任务"""
    from patchflow.core.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched.load()

    tasks = sched.list_all()
    if not tasks:
        click.echo("  没有定时任务")
        return

    click.echo()
    for t in tasks:
        icon = "↻" if t.recurring else "→"
        enabled = "[green]on[/green]" if t.enabled else "[red]off[/red]"
        click.echo(
            f"  {icon} [{t.id}] {t.cron}  {enabled}  "
            f"(x{t.run_count})  {t.prompt[:60]}"
        )
    click.echo()


@cron.command("remove")
@click.argument("task_id", type=str)
def cron_remove(task_id: str):
    """删除定时任务"""
    from patchflow.core.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched.load()
    if sched.remove(task_id):
        logger.success(f"任务已删除: {task_id}")
    else:
        logger.error(f"任务不存在: {task_id}")


@cron.command("run")
def cron_run():
    """手动触发一次所有待执行的定时任务"""
    from patchflow.core.cron_scheduler import CronScheduler

    sched = CronScheduler()
    sched.load()
    count = sched.run_once()
    logger.success(f"执行了 {count} 个任务")


# ═══════════════════════════════════════════════════════════
# memory 命令组 — 跨会话记忆
# ═══════════════════════════════════════════════════════════

@main.group()
def memory():
    """管理跨会话记忆"""
    pass


@memory.command("add")
@click.argument("name", type=str)
@click.argument("content", type=str)
@click.option("--type", "-t", "mem_type", default="project",
              help="类型: user/feedback/project/reference")
def memory_add(name: str, content: str, mem_type: str):
    """添加一条记忆

    \b
    示例:
      patchflow memory add project-style "使用 snake_case 命名"
      patchflow memory add user-prefs "优先使用 FastAPI" -t user
    """
    from patchflow.core.memory import MemoryStore

    store = MemoryStore()
    store.remember(name, content, type=mem_type)
    logger.success(f"记忆已存储: {name}")


@memory.command("list")
@click.option("--type", "-t", "mem_type", default="",
              help="按类型过滤")
def memory_list(mem_type: str):
    """列出所有记忆"""
    from patchflow.core.memory import MemoryStore

    store = MemoryStore()

    if mem_type:
        memories = store.list_by_type(mem_type)
    else:
        memories = store.recall_all()

    if not memories:
        click.echo("  没有记忆")
        return

    click.echo()
    for m in memories:
        click.echo(f"  [{m.type}] [bold]{m.name}[/bold]")
        click.echo(f"  [dim]{m.description}[/dim]")
        if m.content:
            click.echo(f"  {m.content[:120]}")
        click.echo()


@memory.command("recall")
@click.argument("query", type=str)
def memory_recall(query: str):
    """搜索记忆"""
    from patchflow.core.memory import MemoryStore

    store = MemoryStore()
    results = store.recall(query)

    if not results:
        click.echo(f"  未找到匹配 '{query}' 的记忆")
        return

    click.echo()
    for m in results:
        click.echo(f"  [{m.type}] [bold]{m.name}[/bold]")
        click.echo(f"  [dim]{m.description}[/dim]")
        click.echo(f"  {m.content[:150]}")
        click.echo()


@memory.command("forget")
@click.argument("name", type=str)
def memory_forget(name: str):
    """删除记忆"""
    from patchflow.core.memory import MemoryStore

    store = MemoryStore()
    if store.forget(name):
        logger.success(f"记忆已删除: {name}")
    else:
        logger.error(f"记忆不存在: {name}")


# ═══════════════════════════════════════════════════════════
# workflow 命令组 — 高级编排模式
# ═══════════════════════════════════════════════════════════

@main.group()
def workflow():
    """高级多 Agent 编排模式"""
    pass


@workflow.command("judge-panel")
@click.argument("task", type=str)
@click.option("--generators", "-g", default=3, help="方案生成者数量")
@click.option("--judges", "-j", default=2, help="每个方案的评审人数")
@click.option("--work-dir", "-w", default=".", help="工作目录")
def workflow_judge_panel(task: str, generators: int, judges: int, work_dir: str):
    """Judge Panel: 多个 Fixer 独立生成方案 → 多个 Judge 评分 → 选最优

    \b
    示例:
      patchflow workflow judge-panel "修复 app.py 中的性能问题"
      patchflow workflow judge-panel "重构 auth 模块" -g 5 -j 3
    """
    from patchflow.agents.blackboard import Blackboard
    from patchflow.core.agent_orchestrator import AgentOrchestrator
    from patchflow.core.config import get_config
    from patchflow.core.project.context_collector import ContextCollector
    from patchflow.core.workflow import Workflow

    cfg = get_config()
    model = cfg["model"]

    # 构建 Blackboard
    from pathlib import Path
    wd = Path(work_dir)

    collector = ContextCollector(str(wd))
    ctx = collector.collect(use_cache=True)

    code = {}
    from patchflow.core.language_strategy import LanguageFactory
    factory = LanguageFactory()
    strategy = factory.detect(str(wd))
    exts = strategy.extensions if strategy else factory.all_extensions
    for ext in exts:
        for f in wd.rglob(f"*{ext}"):
            rel = str(f.relative_to(wd))
            if any(rel.startswith(p) for p in (".patchflow/", ".venv/", "node_modules/", "venv/", "__pycache__/")):
                continue
            try:
                code[rel] = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

    bb = Blackboard(task=task, context=ctx.to_dict(), code=code)

    logger.info(f"Judge Panel: {generators} generators × {judges} judges")
    best = Workflow.judge_panel(task, bb, generators=generators, judges=judges, model=model)

    if best:
        logger.success(f"最优方案: {best.get('summary', '')}")
        patches = best.get("patches", [])
        logger.info(f"共 {len(patches)} 个补丁")
        for p in patches:
            logger.info(f"  {p.get('file', '?')}: {p.get('description', p.get('reason', ''))[:80]}")
    else:
        logger.error("未能生成有效方案")


@workflow.command("verify-claim")
@click.argument("claim", type=str)
@click.option("--skeptics", "-s", default=3, help="质疑者数量")
@click.option("--threshold", "-t", default=2, help="反驳阈值（≥N人反驳=不通过）")
def workflow_verify_claim(claim: str, skeptics: int, threshold: int):
    """Adversarial Verify: 用多个独立质疑者验证一个结论

    \b
    示例:
      patchflow workflow verify-claim "auth.py 的登录逻辑是安全的"
      patchflow workflow verify-claim "修复后不会有性能问题" -s 5 -t 3
    """
    from patchflow.agents.blackboard import Blackboard
    from patchflow.core.config import get_config
    from patchflow.core.workflow import Workflow

    cfg = get_config()
    model = cfg["model"]

    bb = Blackboard()
    result = Workflow.adversarial_verify(
        claim, bb, skeptics=skeptics,
        refute_threshold=threshold, model=model,
    )

    if result["survives"]:
        logger.success(f"结论经得起质疑: {result['refute_count']}/{result['total_skeptics']} 人反驳")
    else:
        logger.error(f"结论被推翻: {result['refute_count']}/{result['total_skeptics']} 人反驳")

    for v in result["votes"]:
        icon = "[red]REFUTED[/red]" if v.get("refuted") else "[green]ACCEPTED[/green]"
        logger.info(f"  质疑者 #{v['index']}: {icon} — {v.get('reason', '')[:100]}")


@workflow.command("multi-sweep")
@click.argument("task", type=str)
@click.option("--work-dir", "-w", default=".", help="工作目录")
def workflow_multi_sweep(task: str, work_dir: str):
    """Multi-Modal Sweep: 从正确性/性能/安全/可维护性四个角度同时分析

    \b
    示例:
      patchflow workflow multi-sweep "审查 app.py"
    """
    from pathlib import Path

    from patchflow.agents.blackboard import Blackboard
    from patchflow.core.config import get_config
    from patchflow.core.project.context_collector import ContextCollector
    from patchflow.core.workflow import Workflow

    cfg = get_config()
    model = cfg["model"]

    wd = Path(work_dir)
    collector = ContextCollector(str(wd))
    ctx = collector.collect(use_cache=True)

    code = {}
    from patchflow.core.language_strategy import LanguageFactory
    factory = LanguageFactory()
    strategy = factory.detect(str(wd))
    exts = strategy.extensions if strategy else factory.all_extensions
    for ext in exts:
        for f in wd.rglob(f"*{ext}"):
            rel = str(f.relative_to(wd))
            if any(rel.startswith(p) for p in (".patchflow/", ".venv/", "node_modules/", "venv/", "__pycache__/")):
                continue
            try:
                code[rel] = f.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

    bb = Blackboard(task=task, context=ctx.to_dict(), code=code)

    result = Workflow.multi_modal_sweep(task, bb, model=model)

    logger.success(f"发现 {result['unique_count']} 个独特问题 (共 {result['total_count']} 个)")
    for lens, findings in result["per_lens"].items():
        logger.info(f"  [{lens}]: {len(findings)} 个发现")
    for f in result["findings"][:10]:
        logger.info(f"  - [{f['lens']}] {f.get('root_cause', '')[:100]}")
