"""Agent Pipeline Display — 多 Agent 协作的终端进度显示"""

import time

from patchflow.utils import logger

STEP_NAMES = {"analyzer": "Analyzer", "fixer": "Fixer", "reviewer": "Reviewer"}


def _get_model_display(alias: str | None, fallback: str) -> str:
    if not alias:
        return fallback
    from patchflow.core.config import list_models
    models = list_models()
    cfg = models.get(alias)
    if cfg:
        return cfg.get("model", alias)
    return alias


class AgentPipelineDisplay:
    """多 Agent 流水线进度显示（纯 console 输出，避免 Rich Live 在 Windows 卡死）"""

    def __init__(self, blackboard=None):
        self.steps: list[dict] = []
        self._start_time = time.time()
        self._blackboard = blackboard

    def add_step(self, role: str, model: str, detail: str = ""):
        self.steps.append({
            "role": role,
            "label": STEP_NAMES.get(role, role.title()),
            "model": model,
            "status": "pending",
            "summary": "",
            "detail": detail,
            "start_time": None,
            "duration": 0.0,
            "retry": 0,
        })

    def start(self):
        self._start_time = time.time()
        labels = " → ".join(s["label"] for s in self.steps)
        models = ", ".join(f"{s['label']}: {s['model']}" for s in self.steps)
        logger.info(f"[Pipeline] 启动多 Agent 流水线: {labels}")
        logger.info(f"[Pipeline] 模型分配: {models}")

    def set_running(self, step_index: int):
        step = self.steps[step_index]
        step["status"] = "running"
        step["start_time"] = time.time()
        logger.info(f"[Pipeline] [{step['label']}] 开始运行...")

    def set_completed(self, step_index: int, summary: str = ""):
        step = self.steps[step_index]
        step["status"] = "completed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        logger.info(f"[Pipeline] [{step['label']}] 完成 ({step['duration']:.1f}s){' — ' + summary if summary else ''}")

    def set_failed(self, step_index: int, reason: str = ""):
        step = self.steps[step_index]
        step["status"] = "failed"
        if step["start_time"]:
            step["duration"] = time.time() - step["start_time"]
        logger.error(f"[Pipeline] [{step['label']}] 失败{': ' + reason if reason else ''}")

    def set_retry(self, step_index: int):
        step = self.steps[step_index]
        step["retry"] += 1
        step["status"] = "running"
        step["start_time"] = time.time()
        step["summary"] = ""
        logger.info(f"[Pipeline] [{step['label']}] 重试 #{step['retry']}...")

    def set_detail(self, step_index: int, text: str, color: str = ""):
        step = self.steps[step_index] if step_index < len(self.steps) else None
        if step:
            step["detail"] = text

    def finish(self, success: bool):
        elapsed = time.time() - self._start_time
        result = "成功" if success else "失败"
        logger.info(f"[Pipeline] 流水线{result} (总耗时 {elapsed:.1f}s)")
