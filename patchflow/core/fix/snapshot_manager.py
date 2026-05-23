"""Project-scoped snapshot manager used for safe rollback."""

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path

from patchflow.core.fs import relative_path
from patchflow.utils import logger


class SnapshotManager:
    MAX_SNAPSHOTS = 5
    MAX_TOTAL_SIZE = 50 * 1024 * 1024  # 50MB

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.patchflow_dir = self.work_dir / ".patchflow"
        self.snapshot_dir = self.patchflow_dir / "snapshots"
        self._lock = threading.Lock()

    def save(self, files: list[str]) -> str:
        """Save the current state of files, preserving project-relative paths."""
        with self._lock:
            self._gc()

            snapshot_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            snapshot_path = self.snapshot_dir / snapshot_id
            files_root = snapshot_path / "files"
            snapshot_path.mkdir(parents=True, exist_ok=True)

            saved = []
            for file_path in files:
                if not file_path or str(file_path).strip() in ("", "."):
                    continue
                try:
                    rel = relative_path(self.work_dir, file_path)
                except Exception as e:
                    logger.warn(f"Snapshot skipped unsafe path: {file_path} ({e})")
                    continue

                src = self.work_dir / rel
                if src.is_dir():
                    logger.warn(f"Snapshot skipped directory: {file_path}")
                    continue

                entry = {"path": rel, "existed": src.exists()}
                if src.exists():
                    dst = files_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dst))
                saved.append(entry)

            meta = {"time": snapshot_id, "files": saved}
            with open(snapshot_path / "meta.json", "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)

            logger.info(f"Snapshot saved: {len(saved)} file(s) -> {snapshot_path}")
            return snapshot_id

    def rollback(self, snapshot_id: str):
        """Restore all files from a snapshot, then delete the snapshot."""
        with self._lock:
            snapshot_path = self.snapshot_dir / snapshot_id
            if not snapshot_path.exists():
                logger.warn(f"Snapshot not found, skip rollback: {snapshot_id}")
                return

            meta_file = snapshot_path / "meta.json"
            with open(meta_file, encoding="utf-8") as f:
                meta = json.load(f)

            for entry in meta.get("files", []):
                rel, existed, snapshot_file = self._entry_info(snapshot_path, entry)
                target = self.work_dir / rel

                if existed and snapshot_file.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(snapshot_file), str(target))
                    logger.info(f"Rolled back: {rel}")
                elif not existed and target.exists() and target.is_file():
                    target.unlink()
                    logger.info(f"Removed rollback-created file: {rel}")

            shutil.rmtree(snapshot_path, ignore_errors=True)
            logger.info("Snapshot deleted")

    def commit(self, snapshot_id: str):
        """Delete a snapshot after successful changes."""
        with self._lock:
            snapshot_path = self.snapshot_dir / snapshot_id
            if snapshot_path.exists():
                shutil.rmtree(snapshot_path, ignore_errors=True)
                logger.info("Snapshot committed (deleted)")

    def _entry_info(self, snapshot_path: Path, entry) -> tuple[str, bool, Path]:
        if isinstance(entry, str):
            rel = relative_path(self.work_dir, entry)
            return rel, True, snapshot_path / Path(rel).name

        rel = relative_path(self.work_dir, entry.get("path", ""))
        return rel, bool(entry.get("existed", True)), snapshot_path / "files" / rel

    def _gc(self):
        """Remove oldest snapshots when count or total size exceeds the limits."""
        if not self.snapshot_dir.exists():
            return

        snapshots = sorted(
            [d for d in self.snapshot_dir.iterdir() if d.is_dir()],
            key=lambda d: d.name,
        )

        while len(snapshots) > self.MAX_SNAPSHOTS:
            oldest = snapshots.pop(0)
            shutil.rmtree(oldest, ignore_errors=True)
            logger.info(f"GC: removed old snapshot {oldest.name}")

        total_size = sum(
            sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            for d in snapshots
        )
        while total_size > self.MAX_TOTAL_SIZE and snapshots:
            oldest = snapshots.pop(0)
            size = sum(f.stat().st_size for f in oldest.rglob("*") if f.is_file())
            shutil.rmtree(oldest, ignore_errors=True)
            total_size -= size
            logger.info(f"GC: removed old snapshot {oldest.name} ({size // 1024}KB)")

    def list_snapshots(self) -> list[dict]:
        """List all snapshots and their metadata."""
        with self._lock:
            if not self.snapshot_dir.exists():
                return []
            snapshots = []
            for d in sorted(self.snapshot_dir.iterdir()):
                if not d.is_dir():
                    continue
                meta_file = d / "meta.json"
                if not meta_file.exists():
                    continue
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
                    files = [
                        f.get("path", "") if isinstance(f, dict) else f
                        for f in meta.get("files", [])
                    ]
                    snapshots.append({
                        "id": d.name,
                        "time": meta.get("time", d.name),
                        "files": files,
                        "file_count": len(files),
                    })
                except (json.JSONDecodeError, OSError):
                    snapshots.append({"id": d.name, "time": d.name, "files": [], "file_count": 0})
            return snapshots

    def count(self) -> int:
        return len(self.list_snapshots())
