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
from openai import OpenAI
from anthropic import Anthropic

from patchflow.core.config import get_config
from patchflow.utils import logger


def call_llm(
    system_prompt: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 4096,
    model_alias: str | None = None,
) -> dict | None:
    """调用 LLM API（自动根据 provider 选择底层）

    内置指数退避重试：
      - 网络/限流错：最多 3 次（2s, 4s, 8s 等待）
      - JSON 解析错：最多 2 次
      - 认证错：不重试

    Args:
        system_prompt: 系统提示词
        user_message:  用户消息
        model:         模型名称（不传则从配置读取）
        max_tokens:    最大输出 token 数
        model_alias:   模型别名（如 "deepseek"、"claude"），指定后覆盖 provider/api_key/api_base

    Returns:
        dict: 解析后的 JSON 结果，或 None
    """
    # 如果指定了别名，从别名配置读取 provider/api_key/api_base
    if model_alias:
        from patchflow.core.config import _user_config_dir, _load_json
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

    last_error = None
    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            if provider in ("deepseek", "openai"):
                result = _call_openai_compat(system_prompt, user_message, model, max_tokens, api_key, api_base, provider)
            else:
                result = _call_anthropic(system_prompt, user_message, model, max_tokens, api_key)

            if result is not None:
                return result

            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                logger.warn(f"LLM 返回空结果，{wait}s 后重试 ({attempt + 1}/{max_attempts})...")
                time.sleep(wait)

        except Exception as e:
            last_error = e
            error_str = str(e).lower()

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


def _call_anthropic(system_prompt, user_message, model, max_tokens, api_key) -> dict | None:
    """通过 Anthropic 原生 SDK 调用"""
    client = Anthropic(api_key=api_key)

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
    """从 LLM 回复中提取 JSON，失败时尝试修复截断"""
    if not text or not text.strip():
        logger.warn("LLM 返回空内容")
        return None

    # 先尝试提取代码块
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

    # ── 尝试修复截断的 JSON ──
    repaired = _repair_truncated_json(text)
    if repaired is not None:
        return repaired

    logger.error(f"LLM 返回的不是有效 JSON")
    logger.error(f"原始返回: {text[:500]}")
    return None


def _repair_truncated_json(text: str) -> dict | None:
    """尝试修复被截断的 JSON"""
    stripped = text.rstrip().rstrip(",")

    # 尝试补全 }
    for closer in ("\n}", "}", "]}"):
        test = stripped + closer
        try:
            return json.loads(test)
        except json.JSONDecodeError:
            continue

    # 尝试补全 "}
    for closer in ('"\n}', '"}', '"\n]}'):
        test = stripped + closer
        try:
            return json.loads(test)
        except json.JSONDecodeError:
            continue

    return None
