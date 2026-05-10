"""项目语义索引 — embedding + 符号提取 + 语义搜索

为 PatchFlow 提供大项目的"理解"能力。
当项目有上百个文件时，AI 需要快速找到相关代码。

核心功能：
  1. 启动时扫描项目 → 提取符号（类名、函数名）→ 生成 embedding → 持久化
  2. search_files(query) → 语义搜索（embedding 相似度），返回最相关的文件
  3. search_code(pattern) → 正则搜索代码内容，返回匹配行及行号
  4. get_file_meta(path) → 快速获取文件摘要（类名、方法签名）

设计要点：
  - embedding 可用则用语义搜索，不可用自动降级为关键词匹配
  - embedding API 失败后，当前 session 不再重试（_mark_embed_unavailable）
  - 符号提取用正则（纯 Python 实现，不依赖语言服务器）
  - 索引持久化到 .patchflow/index/ 目录，下次启动直接加载
"""

import json
import re
import time
from pathlib import Path

from patchflow.core.config import get_config
from patchflow.utils import logger

# ═══════════════════════════════════════════════════════════
# Embedding 可用性缓存 — 失败后 session 内不再重试
# ═══════════════════════════════════════════════════════════

_embed_available: bool | None = None

def _is_embed_available() -> bool:
    """检查 embedding 是否可用（缓存结果，失败后不再重试）"""
    global _embed_available
    if _embed_available is not None:
        return _embed_available
    cfg = get_config()["embedding"]
    if cfg["provider"] in ("", "none"):
        _embed_available = False
        return False
    if not cfg["api_key"]:
        _embed_available = False
        return False
    _embed_available = True
    return True

def _mark_embed_unavailable():
    """标记 embedding 不可用（API 调用失败后调用）"""
    global _embed_available
    _embed_available = False

# ═══════════════════════════════════════════════════════════
# 忽略规则（与 chat_client 的 list_files 保持一致）
# ═══════════════════════════════════════════════════════════

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".idea", ".vscode",
    ".venv", "venv", ".env", "build", "dist", ".next", ".nuxt",
    ".turbo", "target", ".tox", ".eggs", "*.egg-info",
    ".patchflow",
}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".exe", ".dll", ".so", ".dylib", ".bin",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".ttf", ".woff", ".woff2", ".eot",
    ".jar", ".war", ".ear", ".class",
    ".pyc", ".pyo", ".pyd",
    ".lock", ".sum",
    ".map", ".chunk",
}

TEXT_EXT = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".scala",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    ".rs", ".go", ".rb", ".php",
    ".swift", ".m", ".mm",
    ".cs", ".fs", ".vb",
    ".vue", ".svelte", ".astro",
    ".html", ".htm", ".xml", ".xhtml",
    ".css", ".scss", ".sass", ".less",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".mdx", ".rst", ".txt",
    ".sql", ".sh", ".bash", ".zsh", ".bat", ".ps1",
    ".proto", ".graphql", ".gql",
    ".env", ".gitignore", ".gitattributes", ".dockerignore",
}


def _should_ignore(name: str, is_dir: bool) -> bool:
    if name.startswith(".") and name not in (".env", ".gitignore", ".gitattributes"):
        return True
    if is_dir and name in IGNORE_DIRS:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# 符号提取 — 按语言用正则抓取 class/function/interface 等
# ═══════════════════════════════════════════════════════════

_RE_CLASS_PY = re.compile(r"^\s*class\s+(\w+)", re.MULTILINE)
_RE_DEF_PY = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.MULTILINE)
_RE_IMPORT_FROM = re.compile(r"^\s*from\s+[\w.]+\s+import\s+", re.MULTILINE)
_RE_IMPORT = re.compile(r"^\s*import\s+", re.MULTILINE)

_RE_CLASS_JAVA = re.compile(
    r"^\s*(?:public|protected|private)?\s*(?:abstract|final)?\s*(?:class|interface|enum)\s+(\w+)",
    re.MULTILINE,
)
_RE_METHOD_JAVA = re.compile(
    r"^\s*(?:public|protected|private)?\s*(?:static|abstract|final|synchronized)?\s*(?:<\w+>\s*)?(?:\w+(?:\[\])?\s+)?(\w+)\s*\([^)]*\)",
    re.MULTILINE,
)

_RE_CLASS_TS = re.compile(
    r"^\s*(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum)\s+(\w+)",
    re.MULTILINE,
)
_RE_FUNC_TS = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|const|let|var)\s+(\w+)",
    re.MULTILINE,
)

_RE_MODULE_GO = re.compile(r"^package\s+(\w+)", re.MULTILINE)
_RE_FUNC_GO = re.compile(r"^\s*func\s+(?:\([^)]*\)\s+)?(\w+)", re.MULTILINE)
_RE_TYPE_GO = re.compile(r"^\s*type\s+(\w+)", re.MULTILINE)

_RE_FN_RUST = re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)", re.MULTILINE)
_RE_STRUCT_RUST = re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)", re.MULTILINE)

_RE_DEF_RB = re.compile(r"^\s*def\s+(?:self\.)?(\w+)", re.MULTILINE)
_RE_MODULE_RB = re.compile(r"^\s*(?:class|module)\s+(\w+)", re.MULTILINE)

_RE_FUNC_PHP = re.compile(
    r"^\s*(?:public|protected|private)?\s*(?:static)?\s*function\s+(\w+)",
    re.MULTILINE,
)
_RE_CLASS_PHP = re.compile(r"^\s*(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)


def _extract_symbols(path: Path, content: str) -> list[str]:
    """根据文件扩展名提取类名、函数名等符号"""
    ext = path.suffix.lower()
    symbols = []

    if ext == ".py":
        symbols.extend(_RE_CLASS_PY.findall(content))
        symbols.extend(_RE_DEF_PY.findall(content))
    elif ext in (".java", ".kt", ".kts"):
        symbols.extend(_RE_CLASS_JAVA.findall(content))
        symbols.extend(_RE_METHOD_JAVA.findall(content))
    elif ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        symbols.extend(_RE_CLASS_TS.findall(content))
        symbols.extend(_RE_FUNC_TS.findall(content))
    elif ext == ".go":
        symbols.extend(_RE_FUNC_GO.findall(content))
        symbols.extend(_RE_TYPE_GO.findall(content))
    elif ext == ".rs":
        symbols.extend(_RE_FN_RUST.findall(content))
        symbols.extend(_RE_STRUCT_RUST.findall(content))
    elif ext == ".rb":
        symbols.extend(_RE_DEF_RB.findall(content))
        symbols.extend(_RE_MODULE_RB.findall(content))
    elif ext == ".php":
        symbols.extend(_RE_FUNC_PHP.findall(content))
        symbols.extend(_RE_CLASS_PHP.findall(content))
    elif ext in (".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"):
        symbols.extend(re.findall(r"^\s*(?:\w+(?:\s*\*)?\s+)?(\w+)\s*\([^)]*\)\s*(?:\{|;)", content, re.MULTILINE))

    seen = set()
    unique = []
    for s in symbols:
        if s not in seen and not s.startswith("_") and len(s) > 1:
            seen.add(s)
            unique.append(s)
    return unique[:40]


def _make_summary(path: Path, content: str, symbols: list[str]) -> str:
    """生成一行文件摘要"""
    ext = path.suffix.lower()
    tags = []

    if ext == ".py":
        classes = [s for s in symbols if s[0].isupper()]
        funcs = [s for s in symbols if s[0].islower()]
        if classes:
            tags.append(f"classes: {', '.join(classes[:5])}")
        if funcs:
            tags.append(f"funcs: {', '.join(funcs[:5])}")
    elif ext in (".java", ".kt"):
        types = [s for s in symbols if s[0].isupper()]
        methods = [s for s in symbols if s[0].islower()]
        if types:
            tags.append(f"types: {', '.join(types[:5])}")
        if methods:
            tags.append(f"methods: {', '.join(methods[:5])}")
    elif ext in (".ts", ".tsx", ".jsx"):
        comps = [s for s in symbols if "/" not in s and s[0].isupper()]
        fns = [s for s in symbols if s[0].islower()]
        if comps:
            tags.append(f"components: {', '.join(comps[:5])}")
        if fns:
            tags.append(f"funcs: {', '.join(fns[:5])}")
    elif ext == ".go":
        pkg = _RE_MODULE_GO.findall(content)
        fns = [s for s in symbols if s[0].islower()]
        types = [s for s in symbols if s[0].isupper()]
        if pkg:
            tags.append(f"package: {pkg[0]}")
        if types:
            tags.append(f"types: {', '.join(types[:3])}")
        if fns:
            tags.append(f"funcs: {', '.join(fns[:3])}")
    elif ext == ".rs":
        fns = [s for s in symbols if s[0].islower()]
        types = [s for s in symbols if s[0].isupper()]
        if types:
            tags.append(f"types: {', '.join(types[:3])}")
        if fns:
            tags.append(f"funcs: {', '.join(fns[:3])}")
    else:
        if symbols:
            tags.append(f"symbols: {', '.join(symbols[:10])}")

    summary = f"[{ext}] {path.name}"
    if tags:
        summary += " | " + " | ".join(tags)
    return summary


# ═══════════════════════════════════════════════════════════
# 文件扫描
# ═══════════════════════════════════════════════════════════

def _scan_files(work_dir: str) -> list[Path]:
    """扫描项目下所有可用文本文件"""
    root = Path(work_dir).resolve()
    files = []

    def _walk(d: Path):
        try:
            entries = sorted(d.iterdir())
        except PermissionError:
            return
        for entry in entries:
            if _should_ignore(entry.name, entry.is_dir()):
                continue
            if entry.is_dir():
                _walk(entry)
            elif entry.suffix.lower() in TEXT_EXT or entry.suffix.lower() not in BINARY_EXT:
                files.append(entry.relative_to(root))

    _walk(root)
    return files


# ═══════════════════════════════════════════════════════════
# Embedding 调用
# ═══════════════════════════════════════════════════════════

def _batch_embed(texts: list[str]) -> list[list[float]] | None:
    """调用 embedding API 生成向量"""
    if not _is_embed_available():
        return None

    cfg = get_config()["embedding"]
    from openai import OpenAI

    try:
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["api_base"])
        resp = client.embeddings.create(model=cfg["model"], input=texts)
        return [d.embedding for d in resp.data]
    except Exception as e:
        _mark_embed_unavailable()
        logger.info(f"embedding 不可用，将使用关键词匹配: {e}")
        return None


# ═══════════════════════════════════════════════════════════
# 余弦相似度（用 numpy 或纯 Python）
# ═══════════════════════════════════════════════════════════

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    try:
        import numpy as np
        va = np.array(a)
        vb = np.array(b)
        return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))
    except ImportError:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb) if na and nb else 0.0


# ═══════════════════════════════════════════════════════════
# Git 自动忽略
# ═══════════════════════════════════════════════════════════

def _ensure_gitignore(work_dir: str = "."):
    """如果项目使用 Git，确保 .patchflow/ 被 .gitignore 忽略"""
    wd = Path(work_dir).resolve()
    git_dir = wd / ".git"
    gitignore = wd / ".gitignore"
    if not git_dir.exists():
        return
    if not gitignore.exists():
        gitignore.write_text(".patchflow/\n", encoding="utf-8")
        logger.info("已创建 .gitignore 并添加 .patchflow/")
        return
    content = gitignore.read_text(encoding="utf-8")
    for line in content.split("\n"):
        if line.strip() == ".patchflow/" or line.strip() == ".patchflow":
            return
    with open(gitignore, "a", encoding="utf-8") as f:
        f.write("\n.patchflow/\n")
    logger.info("已添加 .patchflow/ 到 .gitignore")


# ═══════════════════════════════════════════════════════════
# CodebaseIndex
# ═══════════════════════════════════════════════════════════

class CodebaseIndex:
    """项目语义索引"""

    def __init__(self, work_dir: str = "."):
        self.work_dir = Path(work_dir).resolve()
        self.index_dir = Path(work_dir) / ".patchflow" / "index"
        self.index_path = self.index_dir / "files.json"
        self._entries: dict[str, dict] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._built = False

    def is_built(self) -> bool:
        if self._built:
            return True
        return self.index_path.exists()

    def _embed_path(self) -> Path:
        return self.index_dir / "embeddings.npy"

    def _embed_paths_path(self) -> Path:
        return self.index_dir / "embed_paths.json"

    def _save_embeddings(self, file_paths: list[str], embeddings_list: list[list[float]]):
        """保存 embedding 到二进制文件"""
        if not embeddings_list:
            return
        self.index_dir.mkdir(parents=True, exist_ok=True)
        import numpy as np
        arr = np.array(embeddings_list, dtype=np.float32)
        np.save(str(self._embed_path()), arr)
        self._embed_paths_path().write_text(
            json.dumps(file_paths, ensure_ascii=False), encoding="utf-8"
        )
        logger.info(f"保存 embedding: {arr.shape} → {self._embed_path().name}")

    def _load_embeddings(self) -> dict[str, list[float]]:
        """从二进制文件加载 embedding"""
        npy = self._embed_path()
        pjson = self._embed_paths_path()
        if not npy.exists() or not pjson.exists():
            return {}
        try:
            import numpy as np
            arr = np.load(str(npy))
            paths: list[str] = json.loads(pjson.read_text(encoding="utf-8"))
            return {p: arr[i].tolist() for i, p in enumerate(paths) if i < len(arr)}
        except Exception as e:
            logger.warn(f"加载 embedding 失败: {e}")
            return {}

    def load(self) -> bool:
        if not self.index_path.exists():
            return False
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._entries = data.get("files", {})
            self._embeddings = self._load_embeddings()
            self._built = True
            return True
        except (json.JSONDecodeError, OSError):
            return False

    def build(self, force: bool = False) -> int:
        """扫描项目 → 提取符号 → 生成 embedding → 持久化

        返回索引的文件数量。如果 embedding 不可用，仍保存符号信息。
        """
        if not force and self.is_built():
            self.load()
            return len(self._entries)

        logger.info("正在构建项目索引...")
        t0 = time.time()

        files = _scan_files(str(self.work_dir))
        logger.info(f"发现 {len(files)} 个文件")

        entries: dict[str, dict] = {}
        summaries: list[str] = []
        file_paths: list[str] = []

        for fp in files:
            key = str(fp).replace("\\", "/")
            try:
                content = Path(self.work_dir / fp).read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

            symbols = _extract_symbols(fp, content)
            summary = _make_summary(fp, content, symbols)

            entries[key] = {
                "path": key,
                "size": len(content),
                "symbols": symbols,
                "summary": summary,
            }
            summaries.append(summary)
            file_paths.append(key)

        logger.info(f"提取 {len(entries)} 个文件摘要")

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_gitignore(str(self.work_dir))

        if summaries:
            embeddings_list = _batch_embed(summaries)
            if embeddings_list:
                self._embeddings = dict(zip(file_paths, embeddings_list))
                self._save_embeddings(file_paths, embeddings_list)
                logger.info(f"生成 {len(embeddings_list)} 个 embedding")

        self._entries = entries
        self._built = True

        # files.json 只存文本元数据，不含 embedding 向量
        data = {
            "files": {k: {kk: vv for kk, vv in v.items() if kk != "embedding"} for k, v in entries.items()},
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "file_count": len(entries),
        }
        self.index_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        elapsed = time.time() - t0
        logger.info(f"索引构建完成: {len(entries)} 文件, 耗时 {elapsed:.1f}s")
        return len(entries)

    def search_files(self, query: str, top_k: int = 10) -> list[dict]:
        """语义搜索：返回最相关的文件列表"""
        if not self._entries:
            if not self.load():
                return []

        has_embeddings = _is_embed_available() and bool(self._embeddings)

        if has_embeddings:
            emb_list = _batch_embed([query])
            if emb_list:
                query_vec = emb_list[0]
                scored = []
                for key, entry in self._entries.items():
                    emb = self._embeddings.get(key)
                    if not emb:
                        continue
                    score = _cosine_similarity(query_vec, emb)
                    scored.append((score, entry))
                scored.sort(key=lambda x: x[0], reverse=True)
                results = [entry for _, entry in scored[:top_k]]
                return results

        query_lower = query.lower()
        fallback = []
        for key, entry in self._entries.items():
            text = (entry["summary"] + " " + " ".join(entry.get("symbols", []))).lower()
            if any(term in text for term in query_lower.split()) or query_lower in entry["path"].lower():
                fallback.append(entry)
        return fallback[:top_k]

    def get_file_meta(self, filepath: str) -> dict | None:
        key = filepath.replace("\\", "/")
        if not self._entries:
            self.load()
        return self._entries.get(key)

    def search_code(self, pattern: str, path_filter: str = "", max_matches: int = 30) -> str:
        """正则搜索代码文件，返回匹配行及其行号"""
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"ERROR: invalid regex: {e}"

        results: list[str] = []
        count = 0
        root = self.work_dir

        target_files = list(self._entries.keys()) if self._entries else []
        if not target_files:
            for fp in _scan_files(str(self.work_dir)):
                target_files.append(str(fp).replace("\\", "/"))

        for key in target_files:
            if path_filter and path_filter not in key:
                continue
            if count >= max_matches:
                break

            try:
                content = (root / key).read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue

            lines = content.split("\n")
            for i, line in enumerate(lines):
                if count >= max_matches:
                    break
                if regex.search(line):
                    results.append(f"{key}:{i + 1}: {line.strip()[:200]}")
                    count += 1

        if not results:
            return f"(no matches found for pattern '{pattern}')"
        return "\n".join(results)
