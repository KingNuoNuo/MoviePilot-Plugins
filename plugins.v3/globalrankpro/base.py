"""全球影视榜单插件的数据源基类与统一响应模型。

合并后的插件由主类 :class:`GlobalRankPro` 承担宿主生命周期（配置、调度、
页面与接口注册），猫眼与 Netflix 两套抓取解析逻辑各自封装成一个
:class:`RankSource` 子类。数据源不继承 ``_PluginBase``，而是持有主插件实例，
把 ``get_data``/``save_data``/``chain``/``systemmessage`` 等宿主能力代理回去，
因此上游数据源代码可以几乎原样复用。

两个数据源共用同一份插件数据区（``plugin_config_prefix`` 相同），所以代理层
统一给数据键加 ``<source_id>_`` 前缀，两个源的 ``snapshot``/``history``/
``identity_cache`` 互不覆盖。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# 数据源标识：对外接口的 source 参数取值，也是数据键前缀
SOURCE_MAOYAN = "maoyan"
SOURCE_NETFLIX = "netflix"
# 对外接口 source 参数支持的全部取值（all 表示合并返回）
SOURCE_ALL = "all"
SOURCE_CHOICES = (SOURCE_MAOYAN, SOURCE_NETFLIX)


class RankItem(BaseModel):
    """统一榜单单条快照：两个数据源字段的并集。"""

    rank: int = Field(0, description="名次，从 1 开始")
    title: str = Field("", description="榜单原始标题")
    title_cn: str = Field("", description="中文片名（识别结果）")
    media_source: str = Field("", description="主媒体来源，未识别时为空")
    media_id: str = Field("", description="主媒体 ID，未识别时为空")
    poster: str = Field("", description="海报地址，未识别时为空")
    overview: str = Field("", description="剧情简介，未识别时为空")
    year: str = Field("", description="年份，未识别时为空")

    # ---- 猫眼专有 ----
    type: str = Field("", description="猫眼榜单类型：movie/web-movie/web-all/zongyi")
    type_name: str = Field("", description="猫眼榜单类型中文名")
    platform: str = Field("", description="猫眼平台名称，如全网、腾讯视频")
    release_info: str = Field("", description="猫眼上线信息，如「上映 11 天」")
    show_date: str = Field("", description="猫眼榜单日期 YYYY-MM-DD")
    box_office: str = Field("", description="当日票房或热度值等榜单量值")
    total_box_office: str = Field("", description="累计票房，仅电影票房榜提供")
    heat: str = Field("", description="热度/票房占比等榜单热度描述")

    # ---- Netflix 专有 ----
    season_title: str = Field("", description="季标题，N/A 表示非剧集季度")
    hours_viewed: int = Field(0, description="周观看小时数，仅全球榜提供")
    views: int = Field(0, description="周观看次数，仅全球榜近期提供")
    runtime: float = Field(0.0, description="片长（小时），仅全球电影榜提供")
    weeks_in_top10: int = Field(0, description="累计在榜周数")
    release_date: str = Field("", description="发行日期 YYYY-MM-DD，未识别时为空")


class RankBoard(BaseModel):
    """统一榜单快照：两个数据源榜单元数据字段的并集。"""

    source: str = Field("", description="数据源标识：maoyan / netflix")
    board: str = Field(..., description="榜单名称")
    board_type: str = Field("", description="统一榜单类型标识：猫眼取 type，Netflix 取地区代码")
    type: str = Field("", description="猫眼原生榜单类型标识")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="猫眼平台名称")
    platform_type: str = Field("", description="猫眼 platformType 参数值，全网榜为空")
    show_date: str = Field("", description="猫眼榜单日期")
    region: str = Field("", description="Netflix 地区代码，全球榜为空")
    region_name: str = Field("", description="Netflix 地区中文名")
    kind: str = Field("", description="Netflix 内容类型：film / tv")
    category: str = Field("", description="Netflix 数据源原始类目名")
    category_name: str = Field("", description="Netflix 类目中文名")
    week: str = Field("", description="Netflix 榜单周期（周二所在周）")
    url: str = Field("", description="榜单来源地址")
    count: int = Field(0, description="本次返回的条目数（受 limit 截断）")
    updated_at: str = Field("", description="快照时间")
    items: List[RankItem] = Field(default_factory=list, description="榜单条目")


class RankSnapshot(BaseModel):
    """统一榜单快照汇总数据。"""

    updated_at: str = Field("", description="最近一次刷新的快照时间")
    week: str = Field("", description="Netflix 最近一次抓取的榜单周期")
    total: int = Field(0, description="返回的条目总数")
    sources: List[str] = Field(default_factory=list, description="本次响应包含的数据源")
    boards: List[RankBoard] = Field(default_factory=list, description="榜单列表")


class RankSourceRefresh(BaseModel):
    """单个数据源的刷新结果。"""

    source: str = Field("", description="数据源标识")
    refreshed: bool = Field(False, description="该数据源是否成功完成一次抓取")
    board_count: int = Field(0, description="该数据源抓取到的榜单数量")
    item_count: int = Field(0, description="该数据源抓取到的条目数量")
    subscribed: int = Field(0, description="该数据源本次新增订阅数量")
    week: str = Field("", description="该数据源的榜单周期")
    message: str = Field("", description="结果说明")


class RankRefreshResult(BaseModel):
    """统一刷新接口的业务数据。"""

    refreshed: bool = Field(False, description="是否至少有一个数据源完成抓取")
    board_count: int = Field(0, description="抓取到的榜单数量合计")
    item_count: int = Field(0, description="抓取到的条目数量合计")
    subscribed: int = Field(0, description="本次新增订阅数量合计")
    message: str = Field("", description="结果说明")
    sources: List[RankSourceRefresh] = Field(
        default_factory=list, description="按数据源拆分的刷新结果")


def as_text(value: Any) -> str:
    """把任意值收敛成字符串，None 一律转成空串。"""
    return "" if value is None else str(value)


class RankSource:
    """数据源基类：代理宿主插件能力，使上游抓取解析逻辑可原样复用。

    子类需要声明 :attr:`source_id` 与 :attr:`source_name`，并实现
    :meth:`apply_config` / :meth:`fetch` / :meth:`boards`。
    """

    # 数据源标识，同时也是数据键前缀与对外接口 source 参数取值
    source_id: str = ""
    # 数据源展示名，用于配置项与详情页标题
    source_name: str = ""

    def __init__(self, host: Any):
        # 用 object.__setattr__ 写入，避免触发子类可能定义的 __setattr__
        object.__setattr__(self, "_host", host)
        # 最近一次抓取到的榜单元数据，供上层落快照（不持久化）
        object.__setattr__(self, "_last_boards", [])
        # 数据源自己的运行态配置（由主类 apply_config 注入）
        object.__setattr__(self, "_config", {})
        # 数据源开关：关闭后不抓取、接口也不返回该源数据
        object.__setattr__(self, "enabled", True)

    # ------------------------------------------------------------------
    # 宿主能力代理
    # ------------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        """未定义的属性一律回落到主插件实例（chain、systemmessage 等）。"""
        # 注意：_host 本身通过 object.__setattr__ 写入实例字典，正常不会走到这里；
        # 这里仍然显式处理，避免初始化中途访问时无限递归。
        if name == "_host":
            raise AttributeError(name)
        return getattr(self._host, name)

    @property
    def _data_prefix(self) -> str:
        return f"{self.source_id}_" if self.source_id else ""

    def get_data(self, key: str = None, default: Any = None) -> Any:
        """读取本数据源的数据键（自动加源前缀，两个源互不覆盖）。"""
        if not key:
            return default
        data = self._host.get_data(f"{self._data_prefix}{key}")
        return default if data is None else data

    def save_data(self, key: str, value: Any) -> None:
        """写入本数据源的数据键（自动加源前缀）。"""
        if not key:
            return
        self._host.save_data(f"{self._data_prefix}{key}", value)

    def del_data(self, key: str = None) -> None:
        """删除本数据源的数据键（自动加源前缀）。"""
        if not key:
            return
        self._host.del_data(f"{self._data_prefix}{key}")

    # ------------------------------------------------------------------
    # 子类接口
    # ------------------------------------------------------------------
    def apply_config(self, config: Dict[str, Any]) -> None:
        """接收主类从插件配置里剥离出来的本数据源配置。"""
        object.__setattr__(self, "_config", dict(config or {}))
        object.__setattr__(
            self, "enabled", bool((config or {}).get("enabled", True)))

    def fetch(self) -> Dict[str, Any]:
        """执行一次抓取，返回含 board_count/item_count 等字段的摘要字典。"""
        raise NotImplementedError

    def boards(self) -> List[dict]:
        """返回最近一次快照的榜单列表（原始 dict，供主类组装统一模型）。"""
        snapshot = self.get_data("snapshot") or {}
        if not isinstance(snapshot, dict):
            return []
        boards = snapshot.get("boards")
        return [b for b in boards if isinstance(b, dict)] if isinstance(boards, list) else []

    def snapshot_meta(self) -> Dict[str, Any]:
        """返回快照的元信息（updated_at / week 等）。"""
        snapshot = self.get_data("snapshot") or {}
        return snapshot if isinstance(snapshot, dict) else {}

    def rank_list(self, **kwargs: Any) -> list:
        """返回宿主的 MediaInfo 列表（供推荐页/浏览页消费）。"""
        return []

    def recommend_sources(self) -> list:
        """返回本数据源要注册到推荐页的数据源描述（宿主 schema 对象）。"""
        return []
