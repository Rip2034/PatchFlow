"""多模态支持 — 图片自动识别、剪贴板、Content Block 构建

零命令设计：用户不需要学任何语法。
  - 文件路径：输入中提到存在的图片文件 → 自动加载
  - 剪贴板：每次输入自动检查系统剪贴板，有图片就附加
"""

import base64
import re
from io import BytesIO
from pathlib import Path
from patchflow.utils import logger

# ── 常量 ──────────────────────────────────────────────

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

MAX_IMAGE_BYTES = 20 * 1024 * 1024    # 20MB
MAX_IMAGE_PIXELS = 8_000 * 8_000      # 64MP


# 魔术字节 → media_type 映射
_MAGIC_SIGNATURES: list[tuple[bytes, int, str]] = [
    (b"\x89PNG\r\n\x1a\n", 8, "image/png"),
    (b"\xff\xd8\xff",      3, "image/jpeg"),
    (b"GIF87a",            6, "image/gif"),
    (b"GIF89a",            6, "image/gif"),
    (b"RIFF",              4, "image/webp"),   # RIFF....WEBP
    (b"BM",                2, "image/bmp"),
]


# ── 数据结构 ──────────────────────────────────────────

class ImageContent:
    """已加载的图片内容"""
    __slots__ = ("data", "media_type")

    def __init__(self, data: bytes, media_type: str):
        self.data = data
        self.media_type = media_type


# ── 文件加载 ──────────────────────────────────────────

def _detect_media_type(data: bytes) -> str | None:
    """通过魔术字节检测图片类型，不信任扩展名"""
    for magic, length, mime in _MAGIC_SIGNATURES:
        if data[:length] == magic:
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue  # RIFF 也可能是 AVI/WAV，排除
            if mime == "image/bmp":
                # BMP 进一步确认：头部有 "BM" 且文件大小字段匹配
                if len(data) < 14:
                    continue
            return mime
    return None


def load_image_file(path: str | Path) -> ImageContent:
    """加载图片文件，内置安全检查

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 格式不支持 / 文件过大
    """
    p = Path(path).resolve()

    if not p.is_file():
        raise FileNotFoundError(f"图片不存在: {p}")

    size = p.stat().st_size
    if size == 0:
        raise ValueError(f"图片为空: {p}")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(
            f"图片过大 ({size / 1024 / 1024:.1f}MB)，上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB"
        )

    data = p.read_bytes()

    media_type = _detect_media_type(data)
    if media_type is None:
        raise ValueError(f"不支持的图片格式: {p} (无法识别的文件类型)")

    # 像素限制 + 自动缩放（需要 Pillow）
    try:
        from PIL import Image
        img = Image.open(BytesIO(data))
        w, h = img.size
        if w * h > MAX_IMAGE_PIXELS:
            scale = (MAX_IMAGE_PIXELS / (w * h)) ** 0.5
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.LANCZOS)
            buf = BytesIO()
            save_fmt = media_type.split("/")[-1]
            if save_fmt == "jpeg":
                img = img.convert("RGB")
            img.save(buf, format=save_fmt)
            data = buf.getvalue()
            logger.info(f"图片缩放: {w}x{h} → {new_size[0]}x{new_size[1]}")
    except ImportError:
        pass

    return ImageContent(data=data, media_type=media_type)


# ── 剪贴板 ────────────────────────────────────────────

MIN_CLIPBOARD_PIXELS = 200 * 200  # 跳过小于此尺寸的剪贴板图片（大概率是残留数据）

# 剪贴板去重：同一张图不再重复发送
_last_clipboard_hash: str | None = None
# 用户已主动忽略的图片 hash（/drop 命令），直到剪贴板出现新图才清除
_dismissed_clipboard_hash: str | None = None


def _reset_clipboard_cache() -> None:
    """重置剪贴板缓存（切换对话后调用）"""
    global _last_clipboard_hash, _dismissed_clipboard_hash
    _last_clipboard_hash = None
    _dismissed_clipboard_hash = None


def _dismiss_clipboard() -> None:
    """忽略当前剪贴板图片（/drop 命令），新截图会重新检测"""
    global _dismissed_clipboard_hash
    import hashlib
    from PIL import Image, ImageGrab
    try:
        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="PNG")
            _dismissed_clipboard_hash = hashlib.sha256(buf.getvalue()).hexdigest()
    except Exception as e:
        logger.debug(f"[multimodal] dismiss clipboard failed: {e}")
        _dismissed_clipboard_hash = "__drop__"


def _has_clipboard_image() -> bool:
    """检查剪贴板是否有未被忽略的图片"""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return False
    try:
        img = ImageGrab.grabclipboard()
        if not isinstance(img, Image.Image):
            return False
        w, h = img.size
        if w * h < MIN_CLIPBOARD_PIXELS:
            return False
        # 检查是否为用户已忽略的图片
        global _dismissed_clipboard_hash
        if _dismissed_clipboard_hash is not None:
            import hashlib
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="PNG")
            cur_hash = hashlib.sha256(buf.getvalue()).hexdigest()
            if cur_hash == _dismissed_clipboard_hash:
                return False
            # 剪贴板内容已变化，清除忽略标记
            _dismissed_clipboard_hash = None
        return True
    except Exception as e:
        logger.debug(f"[multimodal] _has_clipboard_image failed: {e}")
        return False


def _grab_clipboard_image() -> ImageContent | None:
    """从系统剪贴板获取图片。不支持时返回 None。"""
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        logger.info("[multimodal] Pillow 未安装，剪贴板功能不可用")
        return None

    try:
        img = ImageGrab.grabclipboard()
    except Exception as e:
        logger.debug(f"[multimodal] grabclipboard() 异常: {e}")
        return None

    if img is None:
        logger.info("[multimodal] 剪贴板无图片数据 (grabclipboard 返回 None)")
        return None
    if not isinstance(img, Image.Image):
        logger.info(f"[multimodal] 剪贴板内容非图片 ({type(img).__name__})")
        return None

    w, h = img.size
    if w * h < MIN_CLIPBOARD_PIXELS:
        logger.info(f"[multimodal] 忽略剪贴板小图 ({w}x{h}px，< {MIN_CLIPBOARD_PIXELS}px)")
        return None

    if w * h > MAX_IMAGE_PIXELS:
        scale = (MAX_IMAGE_PIXELS / (w * h)) ** 0.5
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()

    if len(data) > MAX_IMAGE_BYTES:
        logger.warn("[multimodal] 剪贴板图片过大，已跳过")
        return None

    # 去重：和上次一样的图片不重复发送（防止剪贴板残留反复上传）
    global _last_clipboard_hash
    import hashlib
    img_hash = hashlib.sha256(data).hexdigest()
    if img_hash == _last_clipboard_hash:
        logger.info(f"[multimodal] 剪贴板图片未变化，跳过 ({w}x{h}，hash 去重)")
        return None
    _last_clipboard_hash = img_hash

    logger.info(f"[multimodal] 从剪贴板获取图片: {w}x{h}, {len(data) // 1024}KB")
    return ImageContent(data=data, media_type="image/png")


# ── 输入解析 ──────────────────────────────────────────


def _extract_images(text: str, work_dir: Path) -> tuple[str, list[ImageContent]]:
    """从用户输入中自动识别并加载图片。

    来源 1 — 文件路径：扫描 token，找到存在的图片文件则加载
    来源 2 — 剪贴板：自动检查系统剪贴板，有图片就附加

    Returns:
        (清理后的文本, 图片列表)
    """
    images: list[ImageContent] = []
    cleaned = text

    # ── 来源 1：文件路径 ──
    quoted_paths = re.findall(r'"([^"]+)"', cleaned)
    for path_str in quoted_paths:
        _try_load_path(path_str, work_dir, images)

    cleaned_no_quotes = re.sub(r'"[^"]+"', "", cleaned)

    for token in cleaned_no_quotes.split():
        token = token.strip("(),:;'")
        _try_load_path(token, work_dir, images)

    # ── 来源 2：剪贴板（自动检测）──
    img = _grab_clipboard_image()
    if img:
        images.append(img)

    if images:
        logger.info(f"[multimodal] 共加载 {len(images)} 张图片，将附加到输入中")
    else:
        logger.info("[multimodal] 未检测到图片")

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, images


def _try_load_path(token: str, work_dir: Path, images: list[ImageContent]) -> None:
    """尝试将一个 token 作为图片路径加载，成功则追加到 images"""
    # 快速检查：扩展名对得上吗？
    ext = Path(token).suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        return

    # 解析路径
    p = Path(token)
    if not p.is_absolute():
        p = (work_dir / token).resolve()
    else:
        p = p.resolve()

    if not p.is_file():
        return  # 文件不存在 → 用户只是在谈论文件名

    try:
        img = load_image_file(p)
        images.append(img)
        logger.info(f"[multimodal] 自动加载图片: {p.name} ({len(img.data) // 1024}KB)")
    except (FileNotFoundError, ValueError) as e:
        logger.debug(f"[multimodal] 跳过 {token}: {e}")


# ── Content Block 构建 ────────────────────────────────

def _build_user_content(
    text: str,
    images: list[ImageContent],
    provider: str,
) -> str | list[dict]:
    """将文本 + 图片转换为 provider 特定的 content 格式。

    纯文本时返回 str（向后兼容），带图片时返回 content block 数组。

    Anthropic:
      [{"type": "text", "text": "..."}, {"type": "image", "source": {...}}]

    OpenAI / DeepSeek:
      [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {...}}]
    """
    if not images:
        return text

    blocks: list[dict] = [{"type": "text", "text": text}]

    for img in images:
        b64 = base64.b64encode(img.data).decode("ascii")
        if provider == "anthropic":
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": b64,
                },
            })
        else:
            blocks.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{img.media_type};base64,{b64}",
                    "detail": "auto",
                },
            })

    return blocks


# ── 模型能力检测 ──────────────────────────────────────

# 已知支持 vision 的模型（忽略大小写，子串匹配）
_VISION_MODEL_PATTERNS = [
    # Anthropic — Claude 3+ 全系支持图片
    "claude-3", "claude-4",
    # OpenAI — 支持 vision 的模型
    "gpt-4o", "gpt-4-turbo", "gpt-4-vision", "gpt-4.1",
    "gpt-5", "o1", "o3", "o4",
    # Google
    "gemini",
    # 其他已知支持 vision 的
    "gpt-image", "dall-e",
]


def _supports_vision(model: str) -> bool:
    """检查当前模型是否支持图片/多模态输入。

    DeepSeek 系列（v3/r1/v4）不支持 image_url 类型，
    发送图片会导致 API 400 错误。
    """
    lowered = model.lower()
    for pattern in _VISION_MODEL_PATTERNS:
        if pattern in lowered:
            return True
    return False


def _strip_images_with_warning(text: str, images: list[ImageContent]) -> str:
    """不支持视觉的模型：剥离图片，在文本前插入提示"""
    n = len(images)
    total_kb = sum(len(img.data) for img in images) // 1024
    warning = (
        f"[当前模型不支持图片输入，已自动移除 {n} 张图片"
        f"（共 {total_kb}KB）。建议切换至 Claude 4 或 GPT-4o 以启用视觉分析]\n\n"
    )
    return warning + text


def _strip_image_blocks_from_history(messages: list[dict]) -> list[dict]:
    """清洗对话历史：移除所有消息中的 image_url block。

    用于非 vision 模型（DeepSeek 等），防止历史中的旧图片 block 导致 API 400 错误。
    """
    cleaned: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            # content block 数组 — 只保留 text 类型
            text_blocks = [b for b in content if b.get("type") == "text"]
            if text_blocks:
                new_msg = dict(msg)
                new_msg["content"] = text_blocks
                cleaned.append(new_msg)
            # 全被过滤 → 跳过这条消息
        else:
            cleaned.append(msg)
    return cleaned
