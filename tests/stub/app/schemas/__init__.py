"""宿主 schemas 垫片：提供插件导入与响应模型所需的符号。"""

from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field


DataT = TypeVar("DataT")


class Response(BaseModel, Generic[DataT]):
    """宿主统一响应结构（与 app.schemas.response.Response 一致的三段式）。"""

    success: bool
    message: str = ""
    data: Optional[DataT] = None


class MediaSource(str, Enum):
    """媒体来源枚举的最小替身，取值与宿主一致。"""

    TMDB = "themoviedb"
    Douban = "douban"


class MediaType(Enum):
    """媒体类型枚举的最小替身，取值与宿主一致。"""

    MOVIE = '电影'
    TV = '电视剧'
    MUSIC = '音乐'
    COLLECTION = '系列'
    UNKNOWN = '未知'


class MediaInfo(BaseModel):
    """媒体信息（仅保留插件用到的字段）。"""

    media_source: Optional[str] = None
    media_id: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    title_year: Optional[str] = None
    season: Optional[int] = None
    poster_path: Optional[str] = None
    overview: Optional[str] = None
    vote_average: Optional[float] = 0.0
    release_date: Optional[str] = None
    first_air_date: Optional[str] = None
    seasons: Optional[dict] = Field(default_factory=dict)
    number_of_seasons: Optional[int] = 0

    def get_poster_image(self, default: Optional[bool] = None) -> str:
        """与宿主一致：把 original 尺寸替换为 w500。"""
        if self.poster_path:
            return self.poster_path.replace("original", "w500")
        return default or ""


class RecommendMediaSource(BaseModel):
    """推荐数据源。"""

    name: str
    api_path: str
    type: str


class RecommendSourceEventData(BaseModel):
    """推荐源事件数据。"""

    extra_sources: List[RecommendMediaSource] = Field(default_factory=list)


class DiscoverMediaSource(BaseModel):
    """探索数据源（占位，插件未使用）。"""

    name: str = ""
    media_source: Optional[str] = None
    api_path: str = ""
    filter_params: dict = Field(default_factory=dict)


class ChainEventData(BaseModel):
    """链式事件数据基类。"""


class Event:
    """宿主事件对象。"""

    def __init__(self, event_data: Any = None):
        self.event_data = event_data