"""代码生成器 — 把自然语言需求变成可运行的代码

这是 PatchFlow 流程的起点。用户说"创建一个登录 API"，
Generator 调用 LLM 生成代码文件。

设计要点（借鉴文档中的 Prompt 设计）：
  1. 注入任务需求
  2. 注入项目上下文（Phase 3）— 让生成的代码匹配项目技术栈和风格
  3. 限制文件数量（<=3）— 避免生成一堆无关文件
  4. 强制 JSON 输出 — 程序需要结构化地知道生成了哪些文件
  5. 输出格式：{"files": [{"file": "xxx.py", "content": "..."}]}
"""

from pathlib import Path

from patchflow.core.llm_client import call_llm
from patchflow.utils import logger

GENERATE_SYSTEM_PROMPT = """You are a coding agent. Your task is to generate code that fits into an existing project.

RULES:
- Output ONLY valid JSON, no other text
- Generate at most 3 files
- The code MUST be runnable
- Match the project's existing framework, dependencies, and code style
- Use existing dependencies from the project, do NOT add new ones unless necessary
- Fit into the existing project structure
- If the project has an entry point (app.py/main.py), integrate with it

OUTPUT FORMAT:
{
  "files": [
    {"file": "app.py", "content": "import ...\\n\\ndef main():..."}
  ]
}"""


def generate(task: str, model: str | None = None,
             project_context: str | None = None) -> list[dict] | None:
    """根据任务描述生成代码文件列表

    Args:
        task: 用户的自然语言需求
        model: 使用的 LLM 模型
        project_context: 项目上下文文本（Phase 3）

    Returns:
        list[dict] 或 None
    """
    logger.step("Generator: 正在生成代码...")

    context_block = ""
    if project_context:
        context_block = f"{project_context}\n"

    user_message = f"{context_block}Task: {task}\n\nGenerate the code now."

    result = call_llm(
        system_prompt=GENERATE_SYSTEM_PROMPT,
        user_message=user_message,
        model=model,
    )

    if result is None:
        logger.error("Generator: LLM 调用失败")
        return None

    files = result.get("files", [])
    if not isinstance(files, list) or len(files) == 0:
        logger.error("Generator: LLM 返回的文件列表为空")
        return None

    if len(files) > 3:
        logger.warn(f"Generator: LLM 返回了 {len(files)} 个文件，截取前 3 个")
        files = files[:3]

    valid_files = []
    for f in files:
        if "file" in f and "content" in f:
            valid_files.append(f)
        else:
            logger.warn(f"Generator: 跳过无效文件条目: {f}")

    logger.success(f"Generator: 生成了 {len(valid_files)} 个文件")
    return valid_files


def write_files(files: list[dict], work_dir: str = ".") -> list[str]:
    """把 Generator 生成的文件写入磁盘

    Args:
        files:    [{"file": "app.py", "content": "..."}, ...]
        work_dir: 工作目录

    Returns:
        list[str]: 写入的文件路径列表
    """
    written = []
    wd = Path(work_dir)

    for f in files:
        file_path = wd / f["file"]
        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)

        content = f["content"]
        file_path.write_text(content, encoding="utf-8")
        logger.info(f"写入文件: {file_path} ({len(content)} 字符)")
        written.append(str(file_path))

    return written
