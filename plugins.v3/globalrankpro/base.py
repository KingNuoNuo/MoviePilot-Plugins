"""全球影视音乐榜插件的数据源基类与统一响应模型。

合并后的插件由主类 :class:`GlobalRankPro` 承担宿主生命周期（配置、调度、
页面与接口注册），猫眼、Netflix 与红果短剧三套抓取解析逻辑各自封装成一个
:class:`RankSource` 子类。数据源不继承 ``_PluginBase``，而是持有主插件实例，
把 ``get_data``/``save_data``/``chain``/``systemmessage`` 等宿主能力代理回去，
因此上游数据源代码可以几乎原样复用。

三个数据源共用同一份插件数据区（``plugin_config_prefix`` 相同），所以代理层
统一给数据键加 ``<source_id>_`` 前缀，各源的 ``snapshot``/``history``/
``identity_cache`` 互不覆盖。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from pydantic import BaseModel, Field

# 数据源标识：对外接口的 source 参数取值，也是数据键前缀
SOURCE_MAOYAN = "maoyan"
SOURCE_NETFLIX = "netflix"
SOURCE_HONGGUO = "hongguo"
SOURCE_MUSIC = "music"
# 对外接口 source 参数支持的全部取值（all 表示合并返回）
SOURCE_ALL = "all"
# ⚠️ 音乐榜**不进** SOURCE_CHOICES：`all` 会遍历这份清单，九个音乐分榜
# 每个 300~600KB，混进去会让一次 /rank?source=all 多抓十几秒。
# 音乐榜必须显式 `source=music` 才会被取到（见 __resolve_sources 的特例）。
SOURCE_CHOICES = (SOURCE_MAOYAN, SOURCE_NETFLIX, SOURCE_HONGGUO)
# 可显式请求的数据源（含不参与 all 的音乐榜）
SOURCE_ANY = SOURCE_CHOICES + (SOURCE_MUSIC,)


class RankItem(BaseModel):
    """统一榜单单条快照：两个数据源字段的并集。"""

    rank: int = Field(0, description="名次，从 1 开始")
    title: str = Field("", description="榜单原始标题")
    title_cn: str = Field("", description="中文片名（识别结果）")
    media_source: str = Field("", description="主媒体来源，未识别时为空")
    media_id: str = Field("", description="主媒体 ID，未识别时为空")
    media_type: str = Field(
        "",
        description="媒体类型 movie / tv，未识别或该源不做识别时为空。"
                    "客户端拿 media_id 反查宿主详情时必须带对 type_name（电影 / 电视剧），"
                    "传错类型宿主不报错、静默返回空 data",
    )
    poster: str = Field("", description="海报地址，未识别时为空")
    overview: str = Field("", description="剧情简介，未识别时为空")
    year: str = Field("", description="年份，未识别时为空")
    vote_average: float = Field(
        0.0,
        description="豆瓣评分（插件侧检索补全，猫眼/Netflix 等本身不带评分的数据源用），0 表示暂无",
    )

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

    # ---- 红果短剧专有 ----
    series_id: str = Field("", description="红果剧集 ID")
    episode_cnt: int = Field(0, description="总集数")
    score: str = Field("", description="评分文本，如「评分9.0」")
    favorite: str = Field("", description="收藏数文本，如「55.6万收藏」")
    likes: str = Field("", description="点赞数文本，如「137.2万点赞」")
    tags: str = Field("", description="题材标签，逗号分隔，如「玄幻,逆袭,修真」")
    status_tags: str = Field("", description="状态标签，逗号分隔，如「新剧,爆剧」")
    preview_url: str = Field("", description="前 3 集的公开 360p 直链，多集用逗号分隔")

    # ---- 音乐榜单专有 ----
    artist: str = Field("", description="歌手，多位用「 / 」分隔")
    album: str = Field("", description="专辑名")
    duration: int = Field(0, description="时长（秒），0 表示未知")
    last_rank: int = Field(
        0, description="上期名次，0 表示新入榜或该平台不提供（QQ/Apple 恒为 0）")
    genre: str = Field("", description="曲风，仅 Apple 提供")
    song_id: str = Field("", description="平台原生单曲 ID：网易云数字 id / QQ songmid / Apple trackId")
    song_url: str = Field("", description="该单曲在对应平台的详情页地址，App 端用于跳转播放")


class RankBoard(BaseModel):
    """统一榜单快照：三个数据源榜单元数据字段的并集。"""

    source: str = Field("", description="数据源标识：maoyan / netflix / hongguo / music")
    board: str = Field(..., description="榜单名称")
    board_type: str = Field("", description="统一榜单类型标识：猫眼取 type，Netflix 取地区代码")
    type: str = Field("", description="猫眼原生榜单类型标识")
    type_name: str = Field("", description="榜单类型中文名")
    platform: str = Field("", description="猫眼平台名称；音乐榜为此处取平台代码（netease/qq/apple）")
    platform_type: str = Field("", description="猫眼 platformType 参数值，全网榜为空")
    platform_name: str = Field("", description="音乐榜平台展示名（网易云 / QQ音乐 / Apple Music）")
    board_name: str = Field("", description="音乐榜分榜短名（热歌榜 / 新歌榜…，不含平台前缀）")
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
    stale: bool = Field(False, description="本轮抓取失败，该榜沿用上一份快照（数据本身仍有效）")
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


class DoubanRatingResult(BaseModel):
    """按标题批量查豆瓣评分的结果（搜索结果页用）。"""

    rates: Dict[str, str] = Field(
        default_factory=dict, description="标题 → 豆瓣评分，如 8.7；查不到的标题不出现在这里")
    cached: int = Field(0, description="命中持久缓存的条数（不发请求）")
    queried: int = Field(0, description="本次实际请求豆瓣的条数（每条 2 个 HTTP）")
    blocked: bool = Field(False, description="豆瓣正在限流冷却，剩余条目下次再查")


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
    # 快照进程内缓存时长（秒）。一次 /rank 会按榜单 / 地区反复调 boards()：
    # Netflix 全地区一轮 8 次、猫眼多榜单类型一轮 N 次，每次都是「读整份快照
    # JSON（100~250KB）+ 反序列化」。同一请求内复用一份即可，写快照时立刻失效，
    # 所以不存在跨请求读到旧数据的窗口（2s 只兜住同一轮读取）。
    _SNAPSHOT_TTL = 2.0

    def __init__(self, host: Any):
        # 用 object.__setattr__ 写入，避免触发子类可能定义的 __setattr__
        object.__setattr__(self, "_host", host)
        # 最近一次抓取到的榜单元数据，供上层落快照（不持久化）
        object.__setattr__(self, "_last_boards", [])
        # 数据源自己的运行态配置（由主类 apply_config 注入）
        object.__setattr__(self, "_config", {})
        # 数据源开关：关闭后不抓取、接口也不返回该源数据
        object.__setattr__(self, "enabled", True)
        # 快照缓存：(monotonic 时间戳, 快照字典)
        object.__setattr__(self, "_snapshot_memo", None)

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
        if key == "snapshot":
            # 自己刚写的，缓存必须立刻失效，不能让后续 boards() 读到旧快照
            object.__setattr__(self, "_snapshot_memo", None)

    def del_data(self, key: str = None) -> None:
        """删除本数据源的数据键（自动加源前缀）。"""
        if not key:
            return
        self._host.del_data(f"{self._data_prefix}{key}")
        if key == "snapshot":
            object.__setattr__(self, "_snapshot_memo", None)

    # ------------------------------------------------------------------
    # 快照读取（带进程内短缓存）
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        """读本数据源的榜单快照（带 [_SNAPSHOT_TTL] 进程内缓存）。"""
        memo = self._snapshot_memo
        now = time.monotonic()
        if memo is not None and now - memo[0] < self._SNAPSHOT_TTL:
            return memo[1]
        raw = self._host.get_data(f"{self._data_prefix}snapshot")
        snapshot = raw if isinstance(raw, dict) else {}
        object.__setattr__(self, "_snapshot_memo", (now, snapshot))
        return snapshot

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
        boards = self.snapshot().get("boards")
        return [b for b in boards if isinstance(b, dict)] if isinstance(boards, list) else []

    def snapshot_meta(self) -> Dict[str, Any]:
        """返回快照的元信息（updated_at / week 等）。"""
        return self.snapshot()

    def rank_list(self, **kwargs: Any) -> list:
        """返回宿主的 MediaInfo 列表（供推荐页/浏览页消费）。"""
        return []

    def recommend_sources(self) -> list:
        """返回本数据源要注册到推荐页的数据源描述（宿主 schema 对象）。"""
        return []
