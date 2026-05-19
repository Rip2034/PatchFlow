"""语言注册中心 — 多语言支持抽象层

这是 PatchFlow 多语言支持的基石。所有语言相关的数据来自 LanguageStrategy，
本模块提供 LanguageDescriptor（薄封装）和 LanguageRegistry（注册表 + detect 缓存）。

新增语言只需在 language_strategy.py 中添加一个 Strategy 子类即可。
"""

from pathlib import Path

from patchflow.core.language_strategy import LanguageFactory, LanguageStrategy


class LanguageDescriptor:
    """语言描述符 — 从 LanguageStrategy 复制数据，提供 traceback/error 解析"""

    def __init__(self, strategy: LanguageStrategy):
        self.name = strategy.name
        self.extensions = strategy.extensions
        self.project_files = strategy.project_files
        self.traceback_patterns = strategy.traceback_patterns
        self.error_classifiers = strategy.error_classifiers
        self.comment_syntax = strategy.comment_syntax
        self.run_command = strategy.run_command
        self.compile_command = strategy.compile_command
        self.type_search_patterns = strategy.type_search_patterns
        self._strategy = strategy

    def match_file(self, filepath: str) -> bool:
        ext = Path(filepath).suffix.lower()
        return ext in self.extensions

    def parse_traceback(self, error_text: str) -> list[dict] | None:
        """用本语言的 traceback 模式解析错误文本，返回栈帧列表"""
        for pattern in self.traceback_patterns:
            frames = []
            for line in error_text.split("\n"):
                m = pattern.search(line)
                if m:
                    frames.append({
                        "file": m.group(1),
                        "line": int(m.group(2)),
                        "function": m.group(3) if m.lastindex and m.lastindex >= 3 else "",
                    })
            if frames:
                return frames
        return None

    def classify_error(self, error_text: str) -> tuple[str, str]:
        """从错误文本识别错误类型 → (error_type, root_cause)"""
        for keyword, etype in self.error_classifiers.items():
            if keyword in error_text:
                for line in reversed(error_text.strip().split("\n")):
                    if keyword in line:
                        return (etype, line.strip()[:200])
                return (etype, error_text.strip().split("\n")[-1][:200])
        return ("unknown", error_text.strip().split("\n")[-1][:200])


def _parse_generic_traceback(frames: list[dict]) -> list[dict] | None:
    """对通用解析结果赋予角色（entry / propagator / crash_site）"""
    if not frames:
        return None
    for i, frame in enumerate(frames):
        if i == len(frames) - 1:
            frame["role"] = "crash_site"
        elif i == 0:
            frame["role"] = "entry"
        else:
            frame["role"] = "propagator"
    return frames


class LanguageRegistry:
    """语言注册中心 — 单例，管理所有已注册语言

    所有语言数据来自 LanguageStrategy。使用 detect() 自动检测项目语言。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            factory = LanguageFactory()
            cls._instance._languages: dict[str, LanguageDescriptor] = {
                s.name: LanguageDescriptor(s) for s in factory.all_strategies()
            }
            cls._instance._factory = factory
            cls._instance._detect_cache: dict[str, LanguageDescriptor | None] = {}
        return cls._instance

    def register(self, lang: LanguageDescriptor):
        self._languages[lang.name] = lang

    def get(self, name: str) -> LanguageDescriptor | None:
        return self._languages.get(name)

    def all(self) -> dict[str, LanguageDescriptor]:
        return dict(self._languages)

    def detect(self, work_dir: str = ".") -> LanguageDescriptor | None:
        """自动检测项目语言"""
        wd = str(Path(work_dir).resolve())
        if wd in self._detect_cache:
            return self._detect_cache[wd]

        strategy = self._factory.detect(work_dir)
        result = self._languages.get(strategy.name) if strategy else None
        self._detect_cache[wd] = result
        return result

    def clear_detect_cache(self):
        self._detect_cache.clear()

    def detect_from_files(self, files: list[str]) -> LanguageDescriptor | None:
        """从文件列表检测语言"""
        ext_count: dict[str, int] = {}
        for f in files:
            ext = Path(f).suffix.lower()
            if ext:
                ext_count[ext] = ext_count.get(ext, 0) + 1
        if ext_count:
            best_ext = max(ext_count, key=ext_count.get)
            strategy = self._factory.detect_by_extension(best_ext)
            if strategy:
                return self._languages.get(strategy.name)
        return None

    def parse_traceback(self, error_text: str, lang: LanguageDescriptor | None = None) -> list[dict] | None:
        """用语言感知方式解析错误 traceback"""
        if lang:
            frames = lang.parse_traceback(error_text)
            if frames:
                return _parse_generic_traceback(frames)

        skip_name = lang.name if lang else None
        for other_lang in self._languages.values():
            if skip_name and other_lang.name == skip_name:
                continue
            frames = other_lang.parse_traceback(error_text)
            if frames:
                return _parse_generic_traceback(frames)
        return None

    def classify_error(self, error_text: str, lang: LanguageDescriptor | None = None) -> tuple[str, str]:
        """用语言感知方式分类错误"""
        if lang:
            etype, msg = lang.classify_error(error_text)
            if etype != "unknown":
                return etype, msg
        skip_name = lang.name if lang else None
        for other_lang in self._languages.values():
            if skip_name and other_lang.name == skip_name:
                continue
            etype, msg = other_lang.classify_error(error_text)
            if etype != "unknown":
                return etype, msg
        return "unknown", error_text.strip().split("\n")[-1][:200]

    def get_import_parser(self, lang: LanguageDescriptor | None):
        """获取对应语言的 import 解析函数 → callable(filepath, work_dir) -> list[str]"""
        if lang is None:
            from patchflow.core.language_strategy import PythonStrategy
            s = PythonStrategy()
            return s.parse_imports
        return lang._strategy.parse_imports
