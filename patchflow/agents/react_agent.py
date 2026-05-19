"""ReAct Agent — 结构化 Think → Act → Observe 循环

ReAct = Reasoning + Acting。不同于 ChatClient 的隐式推理，
ReActAgent 强制 LLM 在每个步骤显式输出它的思考过程，
然后选择工具调用、观察结果、再思考，形成完整的认知循环。

格式：
  Thought: <推理当前状态，分析下一步该做什么>
  Action: <工具名>[<参数>]
  Observation: <工具返回的结果>
  ... (循环直到任务完成)

设计要点：
  1. 每个循环严格遵循 T→A→O 格式
  2. 思考内容对用户可见（可折叠）
  3. 有 step 预算限制，防止无限循环
  4. 支持最终回答（Finish action），不需要工具时退出
"""

import json
import re
from pathlib import Path

from patchflow.utils import logger

# ReAct 系统提示词 —— 教模型如何以 ReAct 模式工作
REACT_SYSTEM_PROMPT = """\
You are PatchFlow ReAct Agent — an AI that reasons step by step and uses tools to accomplish tasks.

IMPORTANT: You MUST follow this exact format for EVERY response:

Thought: <your step-by-step reasoning about what you need to do next>
Action: <tool_name>[<arguments>]

OR, if you have completed the task:

Thought: <reasoning about why the task is done>
Finish: <summary of what was accomplished>

RULES:
1. ALWAYS start with "Thought:" — explain your reasoning before acting
2. Use EXACTLY one Action per response
3. Read tool results carefully — they appear as "Observation:" in the next turn
4. Never hallucinate observations — only use what you actually see
5. If a tool result is empty or shows an error, think about WHY before acting again
6. Be concise but thorough in your reasoning
7. When the task is complete, use "Finish:" with a summary

Available tools:
- read[files] — read file(s), single path or array, offset/limit for pagination
- write_file[filename] — create or overwrite a file (content must be provided in your response after the Action line)
- list[path] — list directory structure
- search[query] — search code (regex auto-detected) or find files by concept
- run_code[command] — run a shell command
- review_code[filepath] — perform code review
- delete_file[filename] — delete a file
- rename_file[source, dest] — move or rename a file

Example session:

User: Fix the syntax error in app.py

Thought: I need to find and fix a syntax error in app.py. First, I should read the file to understand what's wrong.
Action: read[app.py]

Observation: [content of app.py showing a missing colon on line 42]

Thought: I can see the issue — line 42 is missing a colon after the function definition. I'll write the corrected file.
Action: write_file[app.py]

Observation: OK: wrote 520 chars to app.py

Thought: The syntax error has been fixed. The missing colon was added to line 42. The task is complete.
Finish: Fixed syntax error in app.py — added missing colon on line 42.
"""


class ReActAgent:
    """ReAct Agent — 结构化推理+行动循环

    使用方式：
        agent = ReActAgent(work_dir=".")
        agent.run("Fix the bug in UserService.java")

    Step 预算：
        max_steps 限制 LLM 调用次数，防止无限循环。默认 15。
    """

    def __init__(self, model: str | None = None, work_dir: str = ".",
                 max_steps: int = 15, thinking_budget: int = 2000):
        from patchflow.core.chat_client import ChatClient
        from patchflow.core.config import get_model

        self.model = model or get_model()
        self.work_dir = Path(work_dir).resolve()
        self.max_steps = max_steps
        self.thinking_budget = thinking_budget

        # 内部复用 ChatClient 的工具执行能力，但不走它的对话循环
        self._client = ChatClient(
            model=self.model,
            work_dir=str(work_dir),
            memory_enabled=False,
            thinking_budget=thinking_budget,
        )

        # ReAct 用到的工具执行器（和 chat_client 共用）
        self._tool_executor = None  # 延迟从 chat_client 导入

    def _get_executor(self):
        if self._tool_executor is None:
            from patchflow.core.chat_client import _execute_tool
            self._tool_executor = _execute_tool
        return self._tool_executor

    def _parse_action(self, text: str) -> tuple[str, dict, str]:
        """从 LLM 输出中解析 Action 行

        支持的格式：
          Action: tool_name[arg1=val1, arg2=val2]
          Action: tool_name[filename]
          Action: tool_name[filename, content]
          Action: tool_name[]
          Finish: <summary>

        Returns:
            (tool_name, args_dict, finish_summary)
            - tool_name 非空 → 正常工具调用
            - finish_summary 非空 → 任务完成
        """
        # 找 Finish 行
        finish_match = re.search(r'(?:^|\n)\s*Finish\s*:\s*(.*?)(?:\n|$)', text, re.IGNORECASE)
        if finish_match:
            return ("", {}, finish_match.group(1).strip())

        # 找 Action 行
        action_match = re.search(r'(?:^|\n)\s*Action\s*:\s*(\w+)\s*\[(.*?)\]', text, re.IGNORECASE)
        if not action_match:
            return ("", {}, "")

        tool_name = action_match.group(1).strip()
        raw_args = action_match.group(2).strip()

        # 解析参数 — 支持多种格式
        args: dict = {}
        if raw_args:
            # 先尝试 JSON 格式
            if raw_args.startswith("{"):
                try:
                    args = json.loads(raw_args)
                    return (tool_name, args, "")
                except json.JSONDecodeError:
                    pass

            # key=value 格式
            for part in self._split_args(raw_args):
                if "=" in part:
                    k, v = part.split("=", 1)
                    k = k.strip().strip('"').strip("'")
                    v = v.strip().strip('"').strip("'")
                    args[k] = v
                else:
                    # 位置参数 → 按工具名映射
                    value = part.strip().strip('"').strip("'")
                    if tool_name in ("read", "write_file", "review_code",
                                     "delete_file"):
                        args["files" if tool_name == "read" else "filename"] = value
                    elif tool_name == "list":
                        args["path"] = value
                    elif tool_name == "run_code":
                        args["command"] = value
                    elif tool_name == "search":
                        args["query"] = value

            # 如果 write_file 没有 content，尝试从 LLM 回复的代码块中提取
            if tool_name == "write_file" and "content" not in args:
                content = self._extract_content_from_response(text)
                if content:
                    args["content"] = content

        return (tool_name, args, "")

    @staticmethod
    def _split_args(raw: str) -> list[str]:
        """按逗号分割参数字符串，正确处理嵌套括号和引号"""
        parts = []
        current = []
        depth = 0
        in_quote = False
        quote_char = ""

        for ch in raw:
            if ch in ('"', "'") and not in_quote:
                in_quote = True
                quote_char = ch
            elif ch == quote_char and in_quote:
                in_quote = False
                quote_char = ""
            elif ch in ("(", "[", "{") and not in_quote:
                depth += 1
            elif ch in (")", "]", "}") and not in_quote:
                depth -= 1
            elif ch == "," and depth == 0 and not in_quote:
                parts.append("".join(current).strip())
                current = []
                continue
            current.append(ch)

        if current:
            parts.append("".join(current).strip())
        return [p for p in parts if p]

    @staticmethod
    def _extract_content_from_response(text: str) -> str | None:
        """从 LLM 回复中提取 write_file 的 content（代码块）"""
        # 找 Markdown 代码块
        pattern = r'```(?:\w+)?\n(.*?)```'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 找 Action 行之后的每行缩进内容
        action_match = re.search(r'Action:\s*write_file\[.*?\]', text, re.IGNORECASE)
        if action_match:
            after = text[action_match.end():]
            lines = after.strip().split("\n")
            # 收集续行（非 Thought/Finish/Observation/Action 开头的行）
            # 实际上 write_file 的 content 通常在下一行开始
            content_lines = []
            for line in lines:
                if re.match(r'^(Thought|Finish|Action|Observation)\s*[:[]', line, re.IGNORECASE):
                    break
                content_lines.append(line)
            if content_lines:
                return "\n".join(content_lines).strip()

        return None

    def _extract_thought(self, text: str) -> str:
        """从 LLM 输出中提取 Thought 部分"""
        match = re.search(r'(?:^|\n)\s*Thought\s*:\s*(.*?)(?:\n\s*(?:Action|Finish)\s*[:\[]|\Z)',
                          text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    def run(self, task: str, on_event=None) -> str:
        """执行 ReAct 循环

        Args:
            task: 用户任务描述
            on_event: 可选回调 on_event(event_type, data)，用于实时输出

        Returns:
            最终回答文本（Finish 的摘要内容）
        """
        from patchflow.core.config import get_normalized_provider

        _provider = get_normalized_provider()  # 验证 provider 可用

        # 构建初始对话
        system = REACT_SYSTEM_PROMPT
        messages = [{"role": "user", "content": task}]

        step_count = 0
        observations: list[str] = []

        while step_count < self.max_steps:
            step_count += 1

            if on_event:
                on_event("step", {"step": step_count, "max": self.max_steps})

            # 调用 LLM（带扩展思考如果启用）
            from patchflow.core.llm_client import call_llm
            raw_result = call_llm(
                system_prompt=system,
                user_message=self._build_user_message(messages, observations),
                model=self.model,
                max_tokens=2048,
            )

            if raw_result is None:
                logger.error(f"[ReAct] LLM 调用失败 (step {step_count})")
                if on_event:
                    on_event("error", "LLM call failed")
                return ""

            # 从 LLM 响应中提取文本（call_llm 可能返回 dict 或 str）
            if isinstance(raw_result, dict):
                text = raw_result.get("content", "") or str(raw_result)
            else:
                text = str(raw_result)

            # 提取 Thought
            thought = self._extract_thought(text)
            if thought and on_event:
                on_event("thought", thought)

            # 解析 Action / Finish
            tool_name, args, finish = self._parse_action(text)

            if finish:
                if on_event:
                    on_event("finish", finish)
                logger.info(f"[ReAct] 任务完成 ({step_count} steps): {finish[:100]}")
                return finish

            if not tool_name:
                logger.warn(f"[ReAct] 未解析到 Action (step {step_count})")
                logger.debug(f"  LLM 输出: {text[:300]}")
                if on_event:
                    on_event("error", "No valid Action found in LLM response")
                # 给一次重试的机会
                messages.append({"role": "user", "content": (
                    "You MUST output exactly: Thought: <reasoning>\\nAction: <tool>[<args>]\\n"
                    "or Thought: <reasoning>\\nFinish: <summary>"
                )})
                continue

            # 执行工具
            if on_event:
                on_event("action", {"tool": tool_name, "args": args})

            executor = self._get_executor()
            try:
                result = executor(tool_name, args)
            except Exception as e:
                result = f"ERROR: tool execution failed: {e}"
                logger.error(f"[ReAct] 工具执行异常: {tool_name} → {e}")

            # 截断过长的结果
            if len(result) > 3000:
                result = result[:1500] + f"\n... [truncated, {len(result)} chars total] ...\n" + result[-500:]

            logger.info(f"[ReAct] step {step_count}: {tool_name} → {len(result)} chars")

            # 记录 Observation
            obs = f"Observation[{step_count}] ({tool_name}):\n{result}"
            observations.append(obs)

            if on_event:
                on_event("observation", result)

        logger.warn(f"[ReAct] 达到最大步数 ({self.max_steps})")
        if on_event:
            on_event("error", f"Max steps ({self.max_steps}) reached")
        return ""

    def _build_user_message(self, messages: list[dict],
                            observations: list[str]) -> str:
        """构建发送给 LLM 的用户消息（包含历史 observation）"""
        if observations:
            obs_text = "\n\n".join(observations[-10:])  # 只保留最近 10 条
            return (
                f"{messages[0]['content']}\n\n--- Previous observations ---\n"
                f"{obs_text}\n\nContinue using Thought/Action/Finish format."
            )
        return messages[0]["content"]
