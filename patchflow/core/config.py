"""配置系统 — 多模型管理

PatchFlow 的配置系统借鉴了 Claude Code 的 Settings 多层配置模式（override 模式）。

核心概念：
  models:  { "别名": {provider, model, api_key, api_base}, ... }
  active:  "别名" — 当前使用哪个模型别名

配置来源（优先级从高到低）：
  1. 项目级: .patchflow/config.json（只覆盖当前项目）
  2. 用户级: ~/.patchflow/config.json（全局配置，所有项目共享）
  3. 默认值（内置兜底）

多模型策略：
  - 可以为不同 Agent 角色（analyzer/fixer/reviewer）配置不同模型
  - 分析用便宜模型（如 deepseek-chat），修复用强模型（如 claude）

示例 ~/.patchflow/config.json：
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
    }
  },
  "max_retries": 3,
  "agents": {
    "analyzer": "deepseek",
    "fixer": "claude",
    "reviewer": "deepseek"
  }
}
"""

import json
from pathlib import Path

PROVIDER_DEFAULTS = {
    "deepseek": {
        "model": "deepseek-chat",
        "api_base": "https://api.deepseek.com",
    },
    "anthropic": {
        "model": "claude-sonnet-4-20250514",
        "api_base": "",
    },
    "claude": {
        "model": "claude-sonnet-4-20250514",
        "api_base": "",
    },
    "openai": {
        "model": "gpt-4o",
        "api_base": "https://api.openai.com/v1",
    },
}

PROVIDER_ALIASES = {"claude": "anthropic"}


def _user_config_dir() -> Path:
    """用户级配置目录：~/.patchflow/（全局生效）"""
    return Path.home() / ".patchflow"


def _project_config_dir() -> Path:
    """项目级配置目录：当前项目下的 .patchflow/（仅当前项目生效）"""
    return Path.cwd() / ".patchflow"


def _load_json(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════
# 模型配置 CRUD
# ═══════════════════════════════════════════════════════════

def list_models() -> dict[str, dict]:
    """列出所有已配置的模型别名"""
    user_cfg = _load_json(_user_config_dir() / "config.json")
    return user_cfg.get("models", {})


def add_model(alias: str, provider: str, model: str, api_key: str, api_base: str = ""):
    """添加一个新模型配置"""
    user_path = _user_config_dir() / "config.json"
    user_cfg = _load_json(user_path)

    if "models" not in user_cfg:
        user_cfg["models"] = {}

    if not api_base:
        pdefaults = PROVIDER_DEFAULTS.get(provider, {})
        api_base = pdefaults.get("api_base", "")

    user_cfg["models"][alias] = {
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "api_base": api_base,
    }

    if "active" not in user_cfg:
        user_cfg["active"] = alias

    _save_json(user_path, user_cfg)


def remove_model(alias: str) -> bool:
    """删除一个模型配置"""
    user_path = _user_config_dir() / "config.json"
    user_cfg = _load_json(user_path)

    models = user_cfg.get("models", {})
    if alias not in models:
        return False

    del models[alias]
    user_cfg["models"] = models

    if user_cfg.get("active") == alias:
        user_cfg["active"] = next(iter(models), "") if models else ""

    _save_json(user_path, user_cfg)
    return True


def set_active_model(alias: str) -> bool:
    """切换当前使用的模型"""
    user_path = _user_config_dir() / "config.json"
    user_cfg = _load_json(user_path)

    models = user_cfg.get("models", {})
    if alias not in models:
        return False

    user_cfg["active"] = alias
    _save_json(user_path, user_cfg)
    return True


def set_user_config(key: str, value: str):
    """设置用户配置项（支持 agents.<role> = <model_alias> 语法）

    Args:
        key: 配置键名，如 "max_retries"，或 "agents.analyzer"（点分格式）
        value: 配置值
    """
    user_path = _user_config_dir() / "config.json"
    user_cfg = _load_json(user_path)

    if key.startswith("agents."):
        role = key.split(".", 1)[1]
        if role not in ("analyzer", "fixer", "reviewer"):
            raise ValueError(f"未知 Agent 角色: {role}，可用: analyzer, fixer, reviewer")
        if "agents" not in user_cfg:
            user_cfg["agents"] = {}
        user_cfg["agents"][role] = value
    else:
        if key not in ("api_key", "model", "max_retries", "provider", "api_base",
                       "token_budget"):
            raise ValueError(f"未知配置项: {key}")
        if key in ("max_retries", "token_budget"):
            value = int(value) if value else 0
        user_cfg[key] = value

    _save_json(user_path, user_cfg)


# ═══════════════════════════════════════════════════════════
# 解析配置（合并多层来源 → 最终生效值）
# ═══════════════════════════════════════════════════════════

def get_config() -> dict:
    """获取合并后的当前活跃配置

    合并顺序（后覆盖前）：
      provider_defaults → env → user models[active] → project

    这意味着：
      1. 先加载内置的 provider 默认值
      2. 用 ~/.patchflow/config.json 中的 active 模型配置覆盖
      3. 最后用项目级 .patchflow/config.json 覆盖（最高优先级）
    """
    user_raw = _load_json(_user_config_dir() / "config.json")
    project_raw = _load_json(_project_config_dir() / "config.json")
    models = user_raw.get("models", {})
    # 确定当前使用的模型别名：项目级 > 用户级 > 第一个可用模型
    active_alias = (
        project_raw.get("active") or
        user_raw.get("active") or
        next(iter(models), "") if models else ""
    )

    # 内置默认值（兜底）：避免任何配置缺失时崩溃
    config = {
        "active": active_alias or "default",
        "provider": "anthropic",
        "api_key": "",
        "model": "claude-sonnet-4-20250514",
        "api_base": "",
        "max_retries": 3,
    }

    # 从活跃模型别名读取完整配置（provider / api_key / model / api_base）
    model_cfg = models.get(active_alias, {}) if active_alias else {}

    for key in ("provider", "api_key", "model", "api_base"):
        if key in model_cfg:
            config[key] = model_cfg[key]

    # 兼容旧版：如果用户在 config.json 顶层直接写了 api_key 等字段
    for key in ("api_key", "provider", "model", "api_base"):
        if key in user_raw and user_raw[key]:
            config[key] = user_raw[key]

    if "max_retries" in user_raw:
        config["max_retries"] = user_raw["max_retries"]

    # token 预算：控制 LLM 上下文窗口大小
    token_budget = user_raw.get("token_budget", 0) or project_raw.get("token_budget", 0)
    config["token_budget"] = int(token_budget) if token_budget else 80000

    # 项目级配置覆盖（最高优先级，但不能覆盖 models / active / provider）
    config.update({k: v for k, v in project_raw.items() if v and k not in ("models", "active", "provider")})

    # embedding 配置（语义搜索用，默认关闭）
    embed_defaults = {"provider": "none", "model": "", "api_key": "", "api_base": ""}
    embed_cfg = dict(embed_defaults)

    embed_raw = user_raw.get("embedding", {}) or project_raw.get("embedding", {})
    embed_cfg.update(embed_raw)

    config["embedding"] = embed_cfg

    # 多 Agent 角色-模型映射（analyzer / fixer / reviewer 可选不同模型）
    agents_raw = user_raw.get("agents", {}) or project_raw.get("agents", {})
    config["agents"] = dict(agents_raw)

    return config


# ═══════════════════════════════════════════════════════════
# 便利函数
# ═══════════════════════════════════════════════════════════

def get_api_key() -> str:
    return get_config()["api_key"]


def get_model() -> str:
    return get_config()["model"]


def get_provider() -> str:
    return get_config()["provider"]


def get_normalized_provider() -> str:
    raw = get_config()["provider"]
    return PROVIDER_ALIASES.get(raw, raw)


def get_api_base() -> str:
    return get_config()["api_base"]


def get_token_budget() -> int:
    return get_config()["token_budget"]


def get_agent_model_config(role: str) -> dict:
    """获取指定 Agent 角色的模型配置

    解析顺序：
      1. 检查 config.json 中 agents -> {role} 是否映射到某个模型别名
      2. 从 models 中查找该别名的完整配置（provider / api_key / model / api_base）
      3. 如果角色未配置，回退到当前的活跃模型

    这样设计的好处：
      - 分析用便宜模型（deepseek），修复用强模型（claude）
      - 不配置则所有角色共用活跃模型，简化上手

    Returns:
        dict: {"provider": "...", "model": "...", "api_key": "...", "api_base": "..."}
    """
    cfg = get_config()
    models = _load_json(_user_config_dir() / "config.json").get("models", {})

    role_alias = cfg.get("agents", {}).get(role)
    if role_alias and role_alias in models:
        m = models[role_alias]
        return {
            "provider": m.get("provider", cfg["provider"]),
            "model": m.get("model", cfg["model"]),
            "api_key": m.get("api_key", cfg["api_key"]),
            "api_base": m.get("api_base", cfg["api_base"]),
        }

    return {
        "provider": cfg["provider"],
        "model": cfg["model"],
        "api_key": cfg["api_key"],
        "api_base": cfg["api_base"],
    }


def init_user_config():
    config_path = _user_config_dir() / "config.json"
    if not config_path.exists():
        _save_json(config_path, {
            "active": "",
            "models": {},
            "max_retries": 3,
            "agents": {
                "analyzer": "",
                "fixer": "",
                "reviewer": "",
            },
        })
