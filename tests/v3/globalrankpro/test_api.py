"""GlobalRankPro 主插件测试：统一接口分发、聚合、推荐源、表单与详情页。

数据源级的抓取解析逻辑在 ``test_maoyan_source.py`` / ``test_netflix_source.py``
里覆盖；这里只测「一个插件、一套接口」这层契约：

1. 三个接口的响应模型与认证符合宿主 v3 合同（信封防漂移）；
2. ``source`` 参数在 maoyan / netflix / all 之间正确分发与聚合；
3. 数据源未启用时不返回数据但也不报错；
4. 推荐源按两个数据源合并注册、开关与叠加语义；
5. 设置表单与详情页把两套配置/榜单放在同一页；
6. 配置前缀剥离与数据键隔离（两源快照互不覆盖）。
"""

import json
from pathlib import Path

import pytest

from app import schemas
from app.plugins.globalrankpro import GlobalRankPro
from app.plugins.globalrankpro.base import (
    RankRefreshResult,
    RankSnapshot,
    SOURCE_MAOYAN,
    SOURCE_NETFLIX,
)
from app.schemas import RecommendMediaSource, RecommendSourceEventData


def maoyan_snapshot():
    """猫眼快照样本（结构与插件落盘一致）。"""
    return {
        "updated_at": "2026-10-17 08:00:00",
        "total": 2,
        "boards": [
            {
                "board": "电影票房榜单", "type": "movie", "type_name": "电影票房",
                "platform": "", "platform_type": "", "show_date": "2026-10-17",
                "url": "https://piaofang.maoyan.com/dashboard-ajax/movie",
                "count": 1, "updated_at": "2026-10-17 08:00:00",
                "items": [{
                    "rank": 1, "title": "生化危机：爆发夜", "board_type": "movie",
                    "type_name": "电影票房", "platformDesc": "院线",
                    "releaseInfo": "上映11天", "board": "电影票房榜单",
                    "show_date": "2026-10-17", "box_office": "161.93万",
                    "heat": "28.1%", "media_source": "themoviedb",
                    "media_id": "1586199",
                    "poster": "https://image.tmdb.org/t/p/w500/a.jpg",
                    "overview": "简介", "year": "2026",
                }],
            },
            {
                "board": "剧集综合榜单", "type": "web-all", "type_name": "剧集综合",
                "platform": "", "platform_type": "", "show_date": "2026-10-17",
                "url": "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData",
                "count": 1, "updated_at": "2026-10-17 08:00:00",
                "items": [{
                    "rank": 1, "title": "许我耀眼", "board_type": "web-all",
                    "type_name": "剧集综合", "platformDesc": "全网",
                    "releaseInfo": "上线3天", "board": "剧集综合榜单",
                    "show_date": "2026-10-17",
                    "media_source": "themoviedb", "media_id": "241561",
                    "poster": "", "overview": "", "year": "2026",
                }],
            },
        ],
    }


def netflix_snapshot():
    """Netflix 快照样本（结构与插件落盘一致）。"""
    return {
        "updated_at": "2026-10-16 20:00:00",
        "week": "2026-10-04",
        "total": 2,
        "boards": [
            {
                "board": "Netflix 全球 · 电影（英语）", "type": "global",
                "region": "", "region_name": "全球", "kind": "film",
                "category": "Films (English)", "category_name": "电影（英语）",
                "week": "2026-10-04",
                "url": "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv",
                "count": 1, "updated_at": "2026-10-16 20:00:00",
                "items": [{
                    "rank": 1, "title": "UNABOMBER", "title_cn": "炸弹追缉令",
                    "season_title": "N/A", "hours_viewed": 41500000,
                    "views": 24900000, "runtime": 1.6667, "weeks_in_top10": 2,
                    "media_source": "themoviedb", "media_id": "1586199",
                    "poster": "https://image.tmdb.org/t/p/w500/u.jpg",
                    "overview": "", "year": "2026", "release_date": "2026-09-25",
                }],
            },
            {
                "board": "Netflix 中国香港 · 剧集", "type": "region", "region": "hk",
                "region_name": "中国香港", "kind": "tv", "category": "TV",
                "category_name": "剧集", "week": "2026-10-04",
                "url": "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv",
                "count": 1, "updated_at": "2026-10-16 20:00:00",
                "items": [{
                    "rank": 1, "title": "Wednesday", "title_cn": "星期三",
                    "season_title": "Wednesday: Season 2", "hours_viewed": 0,
                    "views": 0, "runtime": 0.0, "weeks_in_top10": 2,
                    "media_source": "themoviedb", "media_id": "119460",
                    "poster": "", "overview": "", "year": "2025",
                    "release_date": "2025-09-03",
                }],
            },
        ],
    }


BASE_CONFIG = {
    'maoyan_enabled': True,
    'maoyan_sub_enabled': False,
    'maoyan_type_movie': ['movie'],
    'maoyan_type_tv': ['web-all'],
    'netflix_enabled': True,
    'netflix_regions': ['global', 'hk'],
}


def make_plugin(config=None, with_snapshots=True):
    """构造主插件并注入快照（绕过网络与调度）。"""
    instance = GlobalRankPro()
    instance._enabled = True
    instance._api_enabled = True
    instance._recommend_enabled = True
    instance._GlobalRankPro__apply_config(dict(config or BASE_CONFIG))
    if with_snapshots:
        instance.sources[SOURCE_MAOYAN].save_data("snapshot", maoyan_snapshot())
        instance.sources[SOURCE_NETFLIX].save_data("snapshot", netflix_snapshot())
    return instance


@pytest.fixture
def plugin():
    return make_plugin()


# ==================== 接口合同 ====================

def test_get_api_declares_enveloped_typed_responses(plugin):
    """三个接口必须声明响应模型、bear 认证与信封包裹（V3 合同）。"""
    apis = {api["path"]: api for api in plugin.get_api()}

    assert set(apis) == {"/rank", "/rank-list", "/refresh"}
    for path, api in apis.items():
        assert api["response_model"] is not None, f"{path} 未声明响应模型"
        assert api["auth"] == "bear", f"{path} 未声明 bear 认证"
        assert api["summary"]
        # 防漂移：必须用宿主信封，否则普通数据客户端判「无效响应」
        metadata = getattr(api["response_model"],
                           "__pydantic_generic_metadata__", None) or {}
        origin = metadata.get("origin") or getattr(
            api["response_model"], "__origin__", None)
        assert origin is schemas.Response, f"{path} 未用 schemas.Response 信封"
    assert apis["/refresh"]["methods"] == ["GET", "POST"]
    # endpoint 必须是主类自己的方法，source 参数由宿主从 query 注入
    assert apis["/rank"]["endpoint"] == plugin.get_rank_snapshot
    assert apis["/rank-list"]["endpoint"] == plugin.rank_list


def test_api_survives_disabled_sources(plugin):
    """data source 关掉接口仍可用，只是不返回该源数据并给出提示。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': False, 'netflix_enabled': True,
        'netflix_regions': ['global'],
    })
    result = plugin.get_rank_snapshot(source=SOURCE_MAOYAN)
    assert result.success is True
    assert result.data.boards == []
    assert "未启用" in result.message


# ==================== /rank 分发与聚合 ====================

def test_rank_returns_only_maoyan(plugin):
    """source=maoyan 时只返回猫眼榜单，字段沿用猫眼原生语义。"""
    result = plugin.get_rank_snapshot(source=SOURCE_MAOYAN)

    assert result.success is True
    assert isinstance(result.data, RankSnapshot)
    assert result.data.sources == [SOURCE_MAOYAN]
    assert [b.board for b in result.data.boards] == ["电影票房榜单", "剧集综合榜单"]
    assert result.data.total == 2
    assert result.data.updated_at == "2026-10-17 08:00:00"
    assert all(b.source == SOURCE_MAOYAN for b in result.data.boards)
    assert result.data.boards[0].items[0].release_info == "上映11天"


def test_rank_returns_only_netflix(plugin):
    """source=netflix 时只返回 Netflix 榜单，week 与观看量字段可用。"""
    result = plugin.get_rank_snapshot(source=SOURCE_NETFLIX)

    assert result.data.sources == [SOURCE_NETFLIX]
    assert result.data.week == "2026-10-04"
    assert len(result.data.boards) == 2
    assert all(b.source == SOURCE_NETFLIX for b in result.data.boards)
    first = result.data.boards[0].items[0]
    assert (first.title, first.title_cn) == ("UNABOMBER", "炸弹追缉令")
    assert first.hours_viewed == 41500000


def test_rank_all_merges_both_sources(plugin):
    """source=all 时合并两个数据源，updated_at 取较晚的一个。"""
    result = plugin.get_rank_snapshot()

    assert result.data.sources == [SOURCE_MAOYAN, SOURCE_NETFLIX]
    assert len(result.data.boards) == 4
    assert result.data.total == 4
    # 猫眼 08:00 晚于 Netflix 前一天 20:00
    assert result.data.updated_at == "2026-10-17 08:00:00"
    assert {b.source for b in result.data.boards} == {SOURCE_MAOYAN, SOURCE_NETFLIX}


def test_rank_filters_board_type_per_source(plugin):
    """board_type 按各源语义过滤：猫眼取 type，Netflix 取地区代码。"""
    maoyan = plugin.get_rank_snapshot(source=SOURCE_MAOYAN, board_type="movie")
    assert [b.board for b in maoyan.data.boards] == ["电影票房榜单"]

    netflix = plugin.get_rank_snapshot(source=SOURCE_NETFLIX, board_type="hk")
    assert [b.region for b in netflix.data.boards] == ["hk"]

    # 多值逗号分隔
    both = plugin.get_rank_snapshot(source=SOURCE_MAOYAN,
                                    board_type="movie,web-all")
    assert len(both.data.boards) == 2


def test_rank_filters_kind_and_limit(plugin):
    """Netflix 的 kind 与两源共用的 limit 生效。"""
    films = plugin.get_rank_snapshot(source=SOURCE_NETFLIX, kind="film")
    assert [b.kind for b in films.data.boards] == ["film"]

    limited = plugin.get_rank_snapshot(source=SOURCE_NETFLIX, limit=1)
    assert all(len(b.items) == 1 for b in limited.data.boards)

    tv = plugin.get_rank_snapshot(source=SOURCE_NETFLIX, kind="tv")
    assert [b.region for b in tv.data.boards] == ["hk"]


def test_rank_resolves_unknown_source_as_all(plugin):
    """未知 source 按 all 处理，避免调用方拼错就什么都没拿到。"""
    result = plugin.get_rank_snapshot(source="douban")
    assert result.data.sources == [SOURCE_MAOYAN, SOURCE_NETFLIX]


# ==================== /rank-list 分发 ====================

def test_rank_list_defaults_to_maoyan(plugin):
    """不传 source 时沿用原猫眼接口语义，返回宿主 MediaInfo。"""
    result = plugin.rank_list(board_type="movie")

    assert result.success is True
    assert [media.title for media in result.data] == ["生化危机：爆发夜"]
    # 猫眼侧沿用榜单类型中文名作为 MediaInfo.type（原接口行为）
    assert result.data[0].type == "电影票房"
    assert result.data[0].title_year == "生化危机：爆发夜 (2026)"


def test_rank_list_dispatches_to_netflix(plugin):
    """source=netflix 时转发到 Netflix 数据源，中文片名优先。"""
    result = plugin.rank_list(source=SOURCE_NETFLIX, board_type="global")

    assert result.success is True
    assert [media.title for media in result.data] == ["炸弹追缉令"]
    assert result.data[0].type == "电影"


def test_rank_list_rejects_unknown_source(plugin):
    """未知数据源明确报错，而不是静默返回空。"""
    result = plugin.rank_list(source="douban")
    assert result.success is False
    assert "未知数据源" in result.message
    assert result.data == []


def test_rank_list_rejects_disabled_source(plugin):
    """数据源关闭时 rank-list 也不返回数据。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'netflix_enabled': False})
    result = plugin.rank_list(source=SOURCE_NETFLIX)
    assert result.success is False
    assert "未启用" in result.message


# ==================== /refresh 聚合 ====================

def test_refresh_aggregates_sources(plugin, monkeypatch):
    """source=all 时逐个抓取并汇总条数。"""
    calls = []
    plugin.sources[SOURCE_MAOYAN].fetch = lambda: (
        calls.append("maoyan") or
        {"board_count": 2, "item_count": 4, "subscribed": 1})
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: (
        calls.append("netflix") or
        {"board_count": 6, "item_count": 8, "week": "2026-10-04"})

    result = plugin.refresh_now()

    assert result.success is True
    assert isinstance(result.data, RankRefreshResult)
    assert calls == ["maoyan", "netflix"]
    assert (result.data.board_count, result.data.item_count,
            result.data.subscribed) == (8, 12, 1)
    assert [s.source for s in result.data.sources] == [SOURCE_MAOYAN, SOURCE_NETFLIX]
    assert result.data.sources[1].week == "2026-10-04"


def test_refresh_single_source_only_touches_it(plugin):
    """source=maoyan 时不去抓 Netflix。"""
    called = []
    plugin.sources[SOURCE_MAOYAN].fetch = lambda: (
        called.append("maoyan") or {"board_count": 1, "item_count": 2})
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: called.append("netflix") or {}

    result = plugin.refresh_now(source=SOURCE_MAOYAN)

    assert called == ["maoyan"]
    assert result.success is True
    assert [s.source for s in result.data.sources] == [SOURCE_MAOYAN]


def test_refresh_reports_source_errors_without_raising(plugin):
    """单个数据源抛错被收敛成该源失败，另一源照常成功。"""
    def boom():
        raise RuntimeError("upstream down")

    plugin.sources[SOURCE_MAOYAN].fetch = boom
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: {
        "board_count": 6, "item_count": 8, "week": "2026-10-04"}

    result = plugin.refresh_now()

    assert result.success is True  # 至少一个源成功
    assert result.data.board_count == 6
    maoyan_result = result.data.sources[0]
    assert maoyan_result.refreshed is False
    assert "upstream down" in maoyan_result.message


def test_refresh_all_failed_returns_business_error(plugin):
    """全部源失败时 success=False，但业务数据仍完整返回。"""
    plugin.sources[SOURCE_MAOYAN].fetch = lambda: {"board_count": 0}
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: {"board_count": 0}

    result = plugin.refresh_now()

    assert result.success is False
    assert result.data.refreshed is False
    assert len(result.data.sources) == 2


def test_refresh_when_nothing_enabled(plugin):
    """没有任何数据源启用时给出明确提示。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': False, 'netflix_enabled': False})

    result = plugin.refresh_now()

    assert result.success is False
    assert "未启用" in result.message


# ==================== 推荐页数据源 ====================

def _event():
    return type("Evt", (), {"event_data": RecommendSourceEventData()})()


def test_recommend_source_registers_both_sources(plugin):
    """两个数据源的推荐源合并注册，api_path 都指向本插件。"""
    event = _event()
    plugin.recommend_source(event)

    sources = event.event_data.extra_sources
    names = [s.name for s in sources]
    assert "猫眼电影票房" in names
    assert "Netflix 全球电影" in names
    assert "Netflix 中国香港剧集" in names
    assert all(s.api_path.startswith("plugin/GlobalRankPro/rank-list?source=")
               for s in sources)
    assert any(s.api_path.startswith(
        "plugin/GlobalRankPro/rank-list?source=maoyan") for s in sources)
    assert any(s.api_path.startswith(
        "plugin/GlobalRankPro/rank-list?source=netflix") for s in sources)


def test_recommend_source_respects_switches(plugin):
    """推荐或接口开关关闭时不注册；订阅开关不影响推荐源。"""
    plugin._recommend_enabled = False
    event = _event()
    plugin.recommend_source(event)
    assert not event.event_data.extra_sources

    plugin._recommend_enabled = True
    plugin._api_enabled = False
    event = _event()
    plugin.recommend_source(event)
    assert not event.event_data.extra_sources

    plugin._api_enabled = True
    event = _event()
    plugin.recommend_source(event)
    assert event.event_data.extra_sources


def test_recommend_source_skips_disabled_source(plugin):
    """关掉 Netflix 数据源后不再注册它的推荐源。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'maoyan_type_movie': ['movie'],
        'maoyan_type_tv': [], 'netflix_enabled': False})

    event = _event()
    plugin.recommend_source(event)

    names = [s.name for s in event.event_data.extra_sources]
    assert names == ["猫眼电影票房"]


def test_recommend_source_appends_to_existing(plugin):
    """已有其它插件的源时叠加而非替换。"""
    other = RecommendMediaSource(name="其它插件源",
                                 api_path="plugin/Other/rank-list", type="电影")
    event = type("Evt", (), {"event_data": RecommendSourceEventData(
        extra_sources=[other])})()

    plugin.recommend_source(event)

    names = [s.name for s in event.event_data.extra_sources]
    assert names[0] == "其它插件源"
    assert "猫眼电影票房" in names


# ==================== 配置 ====================

def test_config_prefixes_are_split_per_source(plugin):
    """扁平配置按前缀剥离后分给对应数据源。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'maoyan_sub_enabled': True,
        'maoyan_type_movie': ['total'], 'maoyan_type_tv': ['zongyi'],
        'netflix_enabled': False, 'netflix_regions': ['tw'],
        'enabled': True, 'cron': '0 8 * * *',
    })

    maoyan = plugin.sources[SOURCE_MAOYAN]
    assert maoyan.enabled is True
    assert maoyan._enabled is True          # 订阅开关
    assert maoyan._type == ["total", "zongyi"]
    netflix = plugin.sources[SOURCE_NETFLIX]
    assert netflix.enabled is False
    assert netflix._regions == ["tw"]


def test_data_keys_are_isolated_between_sources(plugin):
    """两个源的快照与识别缓存互不覆盖（键带源前缀）。"""
    plugin.sources[SOURCE_MAOYAN].save_data("snapshot", {"marker": "m"})
    plugin.sources[SOURCE_NETFLIX].save_data("snapshot", {"marker": "n"})

    assert plugin.sources[SOURCE_MAOYAN].get_data("snapshot") == {"marker": "m"}
    assert plugin.sources[SOURCE_NETFLIX].get_data("snapshot") == {"marker": "n"}
    # 直接看宿主侧真实键名
    assert plugin._data[f"{SOURCE_MAOYAN}_snapshot"] == {"marker": "m"}
    assert plugin._data[f"{SOURCE_NETFLIX}_snapshot"] == {"marker": "n"}


def test_serialize_config_round_trips(plugin):
    """配置回写包含两个源的开关与默认值，且类型可控（None 收敛）。"""
    config = plugin.serialize_config()

    assert config["maoyan_enabled"] is True
    assert config["maoyan_type_movie"] == ["movie"]
    assert config["netflix_regions"] == ["global", "hk"]
    assert config["cron"] == ""
    assert config["onlyonce"] is False
    assert isinstance(config["enabled"], bool)


# ==================== 表单与详情页 ====================

def test_get_form_contains_both_sources(plugin):
    """设置页把两套配置放在同一表单，推荐卡在最前。"""
    form_items, defaults = plugin.get_form()

    blobs = [json.dumps(node, ensure_ascii=False) for node in form_items]
    assert "MoviePilotApp" in blobs[0], "推荐卡应为设置页第一个节点"
    assert any("VForm" in blob for blob in blobs)
    form_blob = "".join(blobs)
    for label in ("启用插件", "立即运行一次", "执行周期", "推荐页取数授权",
                  "接入推荐页", "清理插件数据", "自动订阅条数", "榜单地区"):
        assert label in form_blob
    # 猫眼配置
    assert "猫眼榜单" in form_blob
    assert "启用猫眼数据源" in form_blob
    assert "自动订阅新上榜" in form_blob
    assert "电影榜单类型" in form_blob
    assert "剧集榜单类型" in form_blob
    assert "全球年度票房榜" in form_blob
    # Netflix 配置
    assert "Netflix 榜单" in form_blob
    assert "启用 Netflix 数据源" in form_blob
    assert "榜单地区" in form_blob

    assert defaults["maoyan_type_movie"] == ["movie"]
    assert defaults["netflix_regions"] == ["global", "hk"]
    assert defaults["maoyan_movie_rec_enabled"] is True


def test_get_form_reflects_saved_config(plugin):
    """表单默认值回显已保存配置，而不是写死的初值。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': False, 'maoyan_type_movie': ['total'],
        'netflix_enabled': True, 'netflix_regions': ['tw']})

    _, defaults = plugin.get_form()

    assert defaults["maoyan_enabled"] is False
    assert defaults["maoyan_type_movie"] == ["total"]
    assert defaults["netflix_regions"] == ["tw"]


def test_get_page_renders_both_sources(plugin):
    """详情页同时渲染两个数据源的榜单分区。"""
    nodes = plugin.get_page()
    blob = json.dumps(nodes, ensure_ascii=False)

    assert "MoviePilotApp" in blob
    assert "猫眼榜单" in blob
    assert "Netflix 榜单" in blob
    # 两源各自的刷新按钮，params 带 source
    assert "\"source\": \"maoyan\"" in blob
    assert "\"source\": \"netflix\"" in blob
    # 猫眼条目与量值
    assert "生化危机：爆发夜" in blob
    assert "161.93万" in blob
    # Netflix 条目、中文片名与观看量
    assert "炸弹追缉令" in blob
    assert "4150 万小时" in blob
    assert "2490 万次观看" in blob
    # 订阅历史区（猫眼专有）
    assert "订阅历史" in blob


def test_get_page_platform_boards_show_main_board_only(plugin):
    """同一 type 有多份平台细分榜时，详情页只展示全网主榜。"""
    snapshot = maoyan_snapshot()
    platform_board = json.loads(json.dumps(snapshot["boards"][1]))
    platform_board["board"] = "剧集综合榜单（腾讯视频）"
    platform_board["platform"] = "腾讯视频"
    platform_board["platform_type"] = "3"
    snapshot["boards"].append(platform_board)
    plugin.sources[SOURCE_MAOYAN].save_data("snapshot", snapshot)

    blob = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert blob.count("剧集综合") == 1 + blob.count("platform_type")
    assert "剧集综合榜单（腾讯视频）" not in blob


def test_get_page_warns_when_source_disabled(plugin):
    """关掉的数据源在详情页给出启用提示而不是空白。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'netflix_enabled': False})

    blob = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "Netflix 榜单数据源未启用" in blob
    assert "Netflix 全球 · 电影（英语）" not in blob


def test_get_page_without_snapshot_shows_hint():
    """无快照时给出刷新提示，不炸。"""
    blob = json.dumps(make_plugin(with_snapshots=False).get_page(),
                      ensure_ascii=False)
    assert "暂无榜单快照" in blob
    assert "刷新" in blob


def test_get_page_rank_badge_uses_node_text(plugin):
    """名次角标必须把数字放在节点 text 上。

    放进 props 时宿主不认（当成未知属性丢掉），海报左上角只剩一个空色块。
    """
    def walk(node):
        if isinstance(node, list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            yield node
            yield from walk(node.get("content") or [])

    badges = [
        node for node in walk(plugin.get_page())
        if node.get("component") == "span"
        and (node.get("props") or {}).get("style", {}).get("position") == "absolute"
        and "border-radius" in (node.get("props") or {}).get("style", {})
    ]

    assert badges, "详情页应渲染名次角标"
    for badge in badges:
        assert "text" not in badge["props"], "text 不能放在 props 里"
        assert str(badge.get("text") or "").strip(), "角标缺名次数字"


def test_get_page_netflix_board_titles_use_board_name(plugin):
    """Netflix 分区标题取榜单名，不能只按地区+类型拼（全球榜会重名）。"""
    snapshot = netflix_snapshot()
    second = json.loads(json.dumps(snapshot["boards"][0]))
    second["board"] = "Netflix 全球 · 电影（非英语）"
    second["category"] = "Films (Non-English)"
    second["category_name"] = "电影（非英语）"
    snapshot["boards"].append(second)
    plugin.sources[SOURCE_NETFLIX].save_data("snapshot", snapshot)

    blob = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "全球 · 电影（英语）" in blob
    assert "全球 · 电影（非英语）" in blob
    # 旧写法会把两份都渲染成「全球电影」
    assert "全球电影" not in blob


# ==================== 生命周期 ====================

def test_stop_service_does_not_block_on_running_job(plugin):
    """抓取进行中保存配置时，stop_service 不能阻塞等待任务结束。

    单轮抓取要跑几分钟（识别 + 补图 + 订阅），``shutdown()`` 默认 wait=True 会让
    宿主保存配置的请求一直转圈。
    """
    calls = []

    class DummyScheduler:
        running = True

        def remove_all_jobs(self):
            calls.append(("remove_all_jobs",))

        def shutdown(self, wait=True):
            calls.append(("shutdown", wait))

    plugin._scheduler = DummyScheduler()
    plugin.stop_service()

    assert ("shutdown", False) in calls, "必须显式 wait=False"
    assert plugin._scheduler is None


# ==================== 版本 ====================

def test_plugin_version_matches_package_metadata():
    """plugin_version 必须与 package.v3.json 登记版本一致。

    宿主市场用两者比较判断「有更新」，类属性漏升会让市场在已装最新版时仍误报更新。
    """
    package = json.loads(
        (Path(__file__).resolve().parents[3] / "package.v3.json")
        .read_text(encoding="utf-8"))
    assert GlobalRankPro.plugin_version == package["GlobalRankPro"]["version"]
