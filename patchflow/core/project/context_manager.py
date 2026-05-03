"""上下文管理器 — 取代 [-40:] 硬截断

三层压缩策略：
  1. recent_layer — 最近 N 个逻辑回合，保持完整
  2. medium_layer — 较早回合，压缩工具结果（源码 → 摘要）
  3. old_layer — 最早回合，整组合并摘要

不再丢信息！老的内容被压缩为一句话但不会消失。
"""

from patchflow.core.config import get_token_budget


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：英语 1 token ≈ 4 chars，中文 1 token ≈ 1.5 chars"""
    if not text:
        return 0
    return max(1, len(text) // 3)


def estimate_message_tokens(msg: dict) -> int:
    """估算单条消息的 token 数（含 role 开销 4 token）"""
    tokens = 4
    content = msg.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    tokens += estimate_tokens(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tokens += 20
                    inp = block.get("input", {})
                    tokens += estimate_tokens(str(inp))
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, list):
                        for cb in c:
                            tokens += estimate_tokens(cb.get("text", ""))
                    else:
                        tokens += estimate_tokens(str(c))
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            tokens += 20 + estimate_tokens(tc["function"].get("arguments", ""))
    return tokens


def _compress_tool_result(msg: dict) -> dict | None:
    """将 read_file 等大工具结果压缩为一行摘要"""
    content = msg.get("content", "")

    if isinstance(content, str):
        if content.startswith("ERROR:"):
            return {"role": "tool", "tool_call_id": msg.get("tool_call_id", ""),
                    "content": f"[tool error: {content[:80]}]"}
        if content.startswith("[lines "):
            lines_total = ""
            for part in content.split("\n", 1):
                lines_total += part + " "
            return {"role": "tool", "tool_call_id": msg.get("tool_call_id", ""),
                    "content": f"[read_file: {lines_total.strip()[:120]}]"}
        if len(content) > 500:
            return {"role": "tool", "tool_call_id": msg.get("tool_call_id", ""),
                    "content": f"[tool output: {len(content)} chars — compressed]"}
        return msg

    if isinstance(content, list):
        total_text = ""
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                total_text += block.get("text", "")
        if len(total_text) > 500:
            return {"role": "user", "content": [
                {"type": "tool_result",
                 "tool_use_id": msg.get("tool_call_id", ""),
                 "content": [{"type": "text", "text": f"[tool output: {len(total_text)} chars — compressed]"}]}
            ]}

    return msg


def _compress_assistant_text(msg: dict) -> dict:
    """压缩 assistant 纯文本回复"""
    content = msg.get("content", "")
    if isinstance(content, str) and len(content) > 400:
        return {"role": "assistant", "content": content[:200] + "\n[...]"}

    if isinstance(content, list):
        new_blocks = []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                if len(text) > 300:
                    new_blocks.append({"type": "text", "text": text[:150] + "\n[...]"})
                else:
                    new_blocks.append(block)
            else:
                new_blocks.append(block)
        return {"role": "assistant", "content": new_blocks}

    return msg


def compress(messages: list[dict], budget: int | None = None,
             keep_turns: int = 3) -> list[dict]:
    """压缩消息列表，确保总 token 不超预算

    Args:
        messages: 原始消息列表
        budget: token 预算上限，默认从配置读取
        keep_turns: 在 recent_layer 中保留多少个逻辑回合

    分层逻辑：
      recent_layer: 最后 keep_turns 个 user 消息及之后的所有消息 → 完整保留
      medium_layer: 之前的内容中，压缩工具结果
      old_layer: 最老的内容，连 AI 回复也压缩
    """
    if not messages:
        return messages

    budget = budget or get_token_budget()

    total_tokens = sum(estimate_message_tokens(m) for m in messages)
    if total_tokens <= budget * 0.7:
        return list(messages)

    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    recent_start = user_indices[-keep_turns] if len(user_indices) >= keep_turns else 0

    recent = messages[recent_start:]
    recent_tokens = sum(estimate_message_tokens(m) for m in recent)

    if recent_tokens <= budget * 0.8:
        return _compress_medium(messages, recent_start, budget)

    return _compress_heavy(messages, recent_start, budget)


def _compress_all_tool_results(result: list[dict]) -> None:
    """压缩所有过大的工具结果——无论在哪一层"""
    for i, msg in enumerate(result):
        if msg.get("role") == "tool":
            compressed = _compress_tool_result(msg)
            if compressed and compressed is not msg:
                result[i] = compressed


def _compress_medium(messages: list[dict], recent_start: int, budget: int) -> list[dict]:
    """压缩所有工具结果，recent 层内的用户/assistant 文本不动"""
    result = list(messages)
    _compress_all_tool_results(result)
    return _trim_to_budget(result, budget)


def _compress_heavy(messages: list[dict], recent_start: int, budget: int) -> list[dict]:
    """激进压缩：old_layer 整组合并 + 所有工具结果 + 老 AI 回复"""
    result = list(messages)

    medium_start = max(0, recent_start - 10)
    old_count = medium_start

    if old_count > 0:
        old_summary_parts = []
        for i in range(old_count):
            msg = messages[i]
            content = msg.get("content", "")
            if isinstance(content, str) and msg.get("role") == "user":
                old_summary_parts.append(content[:80])

        if old_summary_parts:
            summary = "[earlier: " + "; ".join(old_summary_parts[:3]) + "]"
            result = [{"role": "user", "content": summary}] + result[medium_start:]
            recent_start = recent_start - medium_start + 1
        else:
            result = list(messages[medium_start:])
            recent_start = recent_start - medium_start

    _compress_all_tool_results(result)

    for i in range(recent_start):
        msg = result[i]
        if msg.get("role") == "assistant" and not msg.get("tool_calls"):
            result[i] = _compress_assistant_text(msg)

    return _trim_to_budget(result, budget)


def _trim_to_budget(messages: list[dict], budget: int) -> list[dict]:
    """从头部丢弃旧消息，直到总 token 不超预算"""
    while len(messages) > 1:
        total = sum(estimate_message_tokens(m) for m in messages)
        if total <= budget:
            return messages

        if messages[0].get("role") == "system":
            messages.pop(0)
            continue

        first_role = messages[0].get("role")
        if first_role == "user":
            content = messages[0].get("content", "")
            if isinstance(content, str) and len(content) > 5:
                messages[0] = {"role": "user", "content": "[...]"}
            else:
                messages.pop(0)
        elif first_role in ("assistant", "tool"):
            messages.pop(0)
        else:
            messages.pop(0)

    return messages
