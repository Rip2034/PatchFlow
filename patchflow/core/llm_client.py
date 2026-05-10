"""LLM 客户端 — 多厂商统一调用接口

Generator 和 Fixer 都需要调用 LLM API。
这个模块提供统一的调用接口，自动根据 provider 选择底层 SDK。

支持的 provider:
  anthropic → Anthropic Claude API（原生 SDK）
  deepseek  → DeepSeek API（OpenAI 兼容 SDK）
  openai    → OpenAI API + 其他兼容接口（如 ollama、vllm 等）

API Key 读取：
  从 ~/.patchflow/config.json 中 models -> active 配置读取

V0.4 增强：指数退避重试机制
  - 网络错误 / 限流错误 → 重试最多 3 次，每次等待 2^attempt 秒
  - API Key 无效 / 格式错误 → 不重试（立即返回）
  - JSON 解析失败 → 重试最多 2 次
"""

import json
import time

from anthropic import Anthropic
from openai import OpenAI

from patchflow.core.config import get_config
from patchflow.utils import logger


def call_llm(
    system_prompt: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 4096,
    model_alias: str | None = None,
    budget=None,
) -> dict | None:
    """调用 LLM API（自动根据 provider 选择底层 SDK）

    这是 Generator / Fixer / Planner 等模块的统一 LLM 入口。
    调用方只需提供 prompt，不需要关心底层是 OpenAI 还是 Anthropic。

    内置指数退避重试机制（借鉴分布式系统的容错设计）：
      - 网络/限流错（如 429 Too Many Requests）：最多重试 3 次
        等待时间：2s → 4s → 8s（2^attempt 秒）
      - JSON 解析错：最多重试 2 次
      - 认证错（API Key 无效等）：不重试，立即返回

    Args:
        system_prompt: 系统提示词，定义 AI 的角色和行为规则
        user_message:  用户消息，即具体的任务描述
        model:         模型名称（不传则从配置读取当前活跃模型）
        max_tokens:    最大输出 token 数
        model_alias:   模型别名（如 "deepseek"、"claude"），
                       指定后覆盖 provider/api_key/api_base
                       用于多 Agent 场景：不同角色用不同模型
        budget:        可选的 TokenBudget 实例，用于追踪和限制 token 消耗

    Returns:
        dict: 解析后的 JSON 结果，或 None（所有重试都失败时）
    """
    # 如果指定了别名，从别名配置读取 provider/api_key/api_base
    if model_alias:
        from patchflow.core.config import _load_json, _user_config_dir
        user_cfg = _load_json(_user_config_dir() / "config.json")
        alias_cfg = user_cfg.get("models", {}).get(model_alias, {})
        provider = alias_cfg.get("provider", "")
        api_key = alias_cfg.get("api_key", "")
        api_base = alias_cfg.get("api_base", "")
        if model is None:
            model = alias_cfg.get("model", "")
    else:
        cfg = get_config()
        provider = cfg["provider"]
        api_key = cfg["api_key"]
        api_base = cfg["api_base"]
        if model is None:
            model = cfg["model"]

    if not api_key:
        logger.error("未找到 API Key")
        logger.error("设置方式: patchflow config set api_key <your-key>")
        return None

    # Token 预算检查 — 优先使用传入的 budget，否则检查会话级全局预算
    if budget is None:
        try:
            from patchflow.core.fix.budget import get_session_budget
            budget = get_session_budget()
        except Exception:
            pass
    agent_name = model_alias or "llm"
    if budget is not None:
        estimated_input = (len(system_prompt) + len(user_message)) // 4 + max_tokens
        blocked = budget.check(estimated_input)
        if blocked:
            logger.error(f"[TokenBudget] 调用被预算拦截: {blocked}")
            return None

    # Prompt 注入防御 — 扫描 user_message
    try:
        from patchflow.core.fix.prompt_guard import scan as scan_injection
        injection = scan_injection(user_message, source="llm_user_message")
        if injection.blocked:
            logger.error(f"[PromptGuard] 用户消息被拦截: {injection.reason}")
            return None
        if injection.suspicious and injection.sanitized:
            logger.warn(f"[PromptGuard] 用户消息已清洗: {injection.reason}")
            user_message = injection.sanitized
    except Exception:
        pass

    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            if provider in ("deepseek", "openai"):
                result = _call_openai_compat(
                    system_prompt, user_message, model, max_tokens, api_key, api_base, provider
                )
            else:
                result = _call_anthropic(system_prompt, user_message, model, max_tokens, api_key, api_base)

            if result is not None:
                if budget is not None:
                    estimated_input = (len(system_prompt) + len(user_message)) // 4
                    estimated_output = len(str(result)) // 4
                    budget.track_call(agent_name, estimated_input,
                                     estimated_output, model or "")
                return result

            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                logger.warn(f"LLM 返回空结果，{wait}s 后重试 ({attempt + 1}/{max_attempts})...")
                time.sleep(wait)

        except Exception as e:
            error_str = str(e).lower()

            # 认证类错误不重试（API Key 无效、未授权等），立即返回
            if "api_key" in error_str or "unauthorized" in error_str or "invalid" in error_str or "auth" in error_str:
                logger.error(f"LLM 认证失败（不重试）: {e}")
                return None

            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                logger.warn(f"LLM 调用失败 ({e})，{wait}s 后重试 ({attempt + 1}/{max_attempts})...")
                time.sleep(wait)
            else:
                logger.error(f"LLM 调用最终失败: {e}")

    return None


def _call_anthropic(system_prompt, user_message, model, max_tokens, api_key, api_base="") -> dict | None:
    """通过 Anthropic 原生 SDK 调用"""
    base_url = api_base or None
    if base_url:
        base_url = base_url.rstrip("/")
        if base_url.endswith("/v1/messages"):
            base_url = base_url[:-len("/v1/messages")]
        elif base_url.endswith("/v1"):
            base_url = base_url[:-len("/v1")]
    client = Anthropic(api_key=api_key, base_url=base_url)

    try:
        logger.llm(f"[anthropic] 调用 {model}...")

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )

        text = response.content[0].text
        return _parse_json(text)

    except Exception as e:
        logger.error(f"[anthropic] 调用失败: {e}")
        return None


def _call_openai_compat(system_prompt, user_message, model, max_tokens, api_key, api_base, provider) -> dict | None:
    """通过 OpenAI 兼容 SDK 调用（DeepSeek、OpenAI、vllm 等都用这个）"""

    # DeepSeek 的 key 命名格式
    if not api_base:
        api_base = "https://api.deepseek.com" if provider == "deepseek" else "https://api.openai.com/v1"

    client = OpenAI(api_key=api_key, base_url=api_base, timeout=120)

    # 构造消息：OpenAI 格式没有 system 字段，system 也是一条 message
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        logger.llm(f"[{provider}] 调用 {model} ({api_base})...")

        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.7,
            # DeepSeek 新版 API 需要这个参数来启用思考链
            # 如果不需要可以不加
        )

        text = response.choices[0].message.content
        return _parse_json(text)

    except Exception as e:
        logger.error(f"[{provider}] 调用失败: {e}")
        return None


def _parse_json(text: str) -> dict | None:
    """从 LLM 回复中提取 JSON，失败时尝试修复截断

    LLM 输出 JSON 时经常出现的两个问题：
      1. 用 ```json ... ``` 代码块包裹（Markdown 格式）
      2. JSON 被截断（输出达到 max_tokens 限制）

    Returns:
        dict 或 None
    """
    if not text or not text.strip():
        logger.warn("LLM 返回空内容")
        return None

    # 第一步：先尝试提取 markdown 代码块中的 JSON
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    if not text:
        logger.warn("提取代码块后内容为空")
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 第二步：如果标准 JSON 解析失败，尝试修复截断
    repaired = _repair_truncated_json(text)
    if repaired is not None:
        return repaired

    logger.error("LLM 返回的不是有效 JSON")
    logger.error(f"原始返回: {text[:500]}")
    return None


def _repair_truncated_json(text: str) -> dict | None:
    """尝试修复被截断的 JSON

    截断通常发生在：
      - LLM 输出达到 max_tokens 上限
      - 网络传输中断

    修复策略：依次尝试追加各种闭合符号，看哪个能解析成功。
    从最简单的 } 开始，逐步尝试更复杂的闭合组合。
    """
    stripped = text.rstrip().rstrip(",")

    for closer in ("\n}", "}", "]}"):
        test = stripped + closer
        try:
            return json.loads(test)
        except json.JSONDecodeError:
            continue

    for closer in ('"\n}', '"}', '"\n]}'):
        test = stripped + closer
        try:
            return json.loads(test)
        except json.JSONDecodeError:
            continue

    return None
