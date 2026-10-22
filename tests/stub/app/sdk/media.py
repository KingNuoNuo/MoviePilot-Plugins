"""宿主 app.sdk.media 垫片：转发 schemas 中的媒体模型并补媒体工具函数。"""

from app.schemas import MediaInfo, MediaSource, MediaType


class MetaInfo:
    """标题解析结果的替身，只保留插件用到的字段。"""

    def __init__(self, title: str = ""):
        self.name = title
        self.year = None
        self.begin_season = None
        self.type = None


def normalize_media_source(value):
    """把来源归一化成 MediaSource，识别不出时返回 None。"""
    if value is None:
        return None
    text = str(getattr(value, "value", value)).strip().lower()
    if not text:
        return None
    if text in {"themoviedb", "tmdb"}:
        return MediaSource.TMDB
    if text == "douban":
        return MediaSource.Douban
    return value


def resolve_media_identity(media_source=None, media_id=None):
    """返回规范化的 (来源, ID)；任一缺失即视为无效身份。"""
    source = normalize_media_source(media_source)
    if not source:
        return None, None
    if media_id is None or str(media_id).strip() in {"", "0"}:
        return None, None
    return source, str(media_id).strip()


__all__ = [
    "MediaInfo",
    "MediaSource",
    "MediaType",
    "MetaInfo",
    "normalize_media_source",
    "resolve_media_identity",
]