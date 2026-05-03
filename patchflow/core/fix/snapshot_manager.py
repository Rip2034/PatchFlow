"""快照管理器 — 修复前的安全网

每次修复代码之前，保存受影响文件的原始内容。
如果修复后验证失败 → 自动恢复原始文件。

设计要点（借鉴 Claude Code Agent 系统）：
  1. 临时安全网：快照存活时间 = 一个修复循环（几秒到几分钟）
  2. 只存原版：不存 diff，不存多版本 —— 回滚只需要原版直接覆盖
  3. 空间保护：最多 5 个快照，总计 50MB 上限，超限自动清除最旧的
  4. 修完就删：成功 commit() 或失败 rollback() 后立即删除快照

类比：
  git stash 存的是你改的东西，快照存的是"改之前"的东西。
  修坏了 → rollback → 回到修之前的状态。
"""

import os
import shutil
import json
from datetime import datetime
from pathlib import Path

from patchflow.utils import logger


class SnapshotManager:
    MAX_SNAPSHOTS = 5
    MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50MB

    def __init__(self, work_dir: str = "."):
        # .patchflow/ 目录存放所有运行时数据
        self.patchflow_dir = Path(work_dir) / ".patchflow"
        self.snapshot_dir = self.patchflow_dir / "snapshots"

    def save(self, files: list[str]) -> str:
        """保存指定文件的原始内容到快照目录

        每个文件只存一份"修复前的原始版本"。
        回滚时直接复制回去即可。

        Returns:
            snapshot_id: 快照标识（时间戳）
        """
        self._gc()  # 清理过期快照

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_path = self.snapshot_dir / timestamp
        snapshot_path.mkdir(parents=True, exist_ok=True)

        saved = []
        for file_path in files:
            if not file_path or file_path.strip() in ("", "."):
                continue
            src = Path(file_path)
            if not src.exists() or src.is_dir():
                logger.warn(f"快照跳过（不存在或目录）: {file_path}")
                continue
            dst = snapshot_path / src.name
            shutil.copy2(str(src), str(dst))
            saved.append(file_path)

        # 记录快照元数据
        meta = {
            "time": timestamp,
            "files": saved,
        }
        with open(snapshot_path / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

        logger.info(f"快照已保存: {len(saved)} 个文件 → {snapshot_path}")
        return timestamp

    def rollback(self, snapshot_id: str):
        """从快照恢复所有文件，然后删除快照"""
        snapshot_path = self.snapshot_dir / snapshot_id
        if not snapshot_path.exists():
            logger.warn(f"快照不存在，跳过回滚: {snapshot_id}")
            return

        # 读取元数据
        meta_file = snapshot_path / "meta.json"
        with open(meta_file, "r", encoding="utf-8") as f:
            meta = json.load(f)

        for file_path in meta["files"]:
            snapshot_file = snapshot_path / Path(file_path).name
            if snapshot_file.exists():
                shutil.copy2(str(snapshot_file), file_path)
                logger.info(f"已回滚: {file_path}")

        # 删除快照
        shutil.rmtree(snapshot_path, ignore_errors=True)
        logger.info("快照已删除")

    def commit(self, snapshot_id: str):
        """修复成功 → 删除快照（不需要回滚）"""
        snapshot_path = self.snapshot_dir / snapshot_id
        if snapshot_path.exists():
            shutil.rmtree(snapshot_path, ignore_errors=True)
            logger.info("快照已提交（删除）")

    def _gc(self):
        """垃圾回收：超上限自动清除最旧的快照"""
        if not self.snapshot_dir.exists():
            return

        snapshots = sorted(
            [d for d in self.snapshot_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )

        # 数量超过上限
        while len(snapshots) > self.MAX_SNAPSHOTS:
            oldest = snapshots.pop(0)
            shutil.rmtree(oldest, ignore_errors=True)
            logger.info(f"GC: 清除过期快照 {oldest.name}")

        # 总大小超过上限
        total_size = sum(
            sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            for d in snapshots
        )
        while total_size > self.MAX_TOTAL_SIZE and snapshots:
            oldest = snapshots.pop(0)
            size = sum(f.stat().st_size for f in oldest.rglob("*") if f.is_file())
            shutil.rmtree(oldest, ignore_errors=True)
            total_size -= size
            logger.info(f"GC: 清除过期快照 {oldest.name} ({size // 1024}KB)")

    def list_snapshots(self) -> list[dict]:
        """列出所有快照及其信息"""
        if not self.snapshot_dir.exists():
            return []
        snapshots = []
        for d in sorted(self.snapshot_dir.iterdir()):
            if d.is_dir():
                meta_file = d / "meta.json"
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        snapshots.append({
                            "id": d.name,
                            "time": meta.get("time", d.name),
                            "files": meta.get("files", []),
                            "file_count": len(meta.get("files", [])),
                        })
                    except (json.JSONDecodeError, OSError):
                        snapshots.append({"id": d.name, "time": d.name, "files": [], "file_count": 0})
        return snapshots

    def count(self) -> int:
        """返回当前快照数量"""
        return len(self.list_snapshots())
