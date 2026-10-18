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
from types import SimpleNamespace

import pytest

from app import schemas
from app.plugins.globalrankpro import GlobalRankPro
from app.plugins.globalrankpro.base import (
    RankRefreshResult,
    RankSnapshot,
    SOURCE_HONGGUO,
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


def hongguo_snapshot():
    """红果快照样本（结构与插件落盘一致）。"""
    return {
        "updated_at": "2026-10-18 09:00:00",
        "total": 3,
        "boards": [
            {
                "board": "红果热播榜", "type": "hot", "type_name": "热播榜",
                "category": "hot", "category_name": "热播榜",
                "url": "https://hongguoduanju.com/rank/hot-drama",
                "count": 2, "updated_at": "2026-10-18 09:00:00",
                "items": [
                    {
                        "rank": 1, "title": "开局签到十万亿功德",
                        "title_cn": "开局签到十万亿功德",
                        "series_id": "7501234567890123456",
                        "episode_cnt": 5, "heat": "热度 2145万",
                        "score": "评分9.0", "favorite": "55.6万收藏",
                        "likes": "137.2万点赞", "tags": "玄幻,逆袭",
                        "status_tags": "新剧,爆剧",
                        "preview_url": ",".join(
                            "https://hongguoduanju.com/video/public/"
                            "v0200fg{}/360p.mp4".format(i) for i in range(1, 4)),
                        "media_source": "", "media_id": "",
                        "poster": "https://p9-aio.ecombdimg.com/obj/x"
                                  "~tplv-shrink:1080:0.image",
                        "overview": "一朝穿越", "year": "", "release_date": "",
                    },
                    {
                        "rank": 2, "title": "我在天庭收废品",
                        "title_cn": "我在天庭收废品",
                        "series_id": "7501234567890123458",
                        "episode_cnt": 0, "heat": "热度 1200万",
                        "score": "", "favorite": "", "likes": "",
                        "tags": "", "status_tags": "新剧", "preview_url": "",
                        "media_source": "", "media_id": "", "poster": "",
                        "overview": "", "year": "", "release_date": "",
                    },
                ],
            },
            {
                "board": "红果漫剧热播榜", "type": "comic",
                "type_name": "漫剧热播榜", "category": "comic",
                "category_name": "漫剧热播榜",
                "url": "https://hongguoduanju.com/rank/hot-comic-drama",
                "count": 1, "updated_at": "2026-10-18 09:00:00",
                "items": [{
                    "rank": 1, "title": "一念神魔", "title_cn": "一念神魔",
                    "series_id": "7501234567890123999", "episode_cnt": 80,
                    "heat": "热度 900万", "score": "评分8.5",
                    "favorite": "12万收藏", "likes": "30万点赞",
                    "tags": "奇幻,战斗", "status_tags": "",
                    "preview_url": "", "media_source": "", "media_id": "",
                    "poster": "", "overview": "", "year": "",
                    "release_date": "",
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
    'hongguo_enabled': True,
    'hongguo_boards': ['hot'],
    'hongguo_limit': 20,
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
    """数据接口必须声明响应模型、bear 认证与信封包裹（V3 合同）。"""
    apis = {api["path"]: api for api in plugin.get_api()}

    # /poster 回的是图片字节，不套信封，单独在下一个用例里校验
    assert set(apis) == {"/rank", "/rank-list", "/refresh", "/poster"}
    for path in ("/rank", "/rank-list", "/refresh"):
        api = apis[path]
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


def test_poster_api_is_anonymous(plugin):
    """海报接口必须匿名：图片标签带不了 Authorization 头，鉴权就永远 401。"""
    poster = {api["path"]: api for api in plugin.get_api()}["/poster"]

    assert poster["allow_anonymous"] is True
    assert poster["methods"] == ["GET"]
    assert poster["endpoint"] == plugin.hongguo_poster
    assert "response_model" not in poster


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


def test_rank_resolves_unknown_source_as_all():
    """未知 source 按 all 处理，避免调用方拼错就什么都没拿到。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())

    result = instance.get_rank_snapshot(source="douban")
    assert result.data.sources == [
        SOURCE_MAOYAN, SOURCE_NETFLIX, SOURCE_HONGGUO]


def test_rank_returns_only_hongguo():
    """source=hongguo 时走红果分支，而不是落到 Netflix 的地区分支。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())

    result = instance.get_rank_snapshot(source=SOURCE_HONGGUO)

    assert result.data.sources == [SOURCE_HONGGUO]
    assert [b.board for b in result.data.boards] == [
        "红果热播榜", "红果漫剧热播榜"]
    assert result.data.total == 3
    assert result.data.updated_at == "2026-10-18 09:00:00"
    first = result.data.boards[0].items[0]
    assert first.series_id == "7501234567890123456"
    assert first.episode_cnt == 5
    assert first.score == "评分9.0"
    assert first.tags == "玄幻,逆袭"
    assert first.preview_url.count("http") == 3


def test_rank_hongguo_filters_board_codes():
    """红果的 board_type 是榜单代码，多值逐个取，非法代码返回空。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())

    one = instance.get_rank_snapshot(
        source=SOURCE_HONGGUO, board_type="comic")
    assert [b.board for b in one.data.boards] == ["红果漫剧热播榜"]

    # 逗号多值
    multi = instance.get_rank_snapshot(
        source=SOURCE_HONGGUO, board_type="hot,comic")
    assert len(multi.data.boards) == 2

    assert instance.get_rank_snapshot(
        source=SOURCE_HONGGUO, board_type="nope").data.boards == []


def test_rank_all_merges_three_sources():
    """source=all 时三源合并，各自按自己的语义解释 board_type。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())

    result = instance.get_rank_snapshot(source="all")
    assert result.data.sources == [
        SOURCE_MAOYAN, SOURCE_NETFLIX, SOURCE_HONGGUO]
    # 猫眼 2 榜 + Netflix 2 榜 + 红果 2 榜
    assert len(result.data.boards) == 6
    assert {b.source for b in result.data.boards} == {
        SOURCE_MAOYAN, SOURCE_NETFLIX, SOURCE_HONGGUO}


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


def test_rank_list_dispatches_to_hongguo(monkeypatch):
    """source=hongguo 时转发到红果数据源，短剧标题即中文名。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())
    # 这里只验分发与标题，海报转发另有专门用例；先关掉看官网原图地址
    monkeypatch.setattr(instance.sources[SOURCE_HONGGUO], "_poster_proxy", False)

    # 不传 board_type 时返回全部已抓取榜单
    result = instance.rank_list(source=SOURCE_HONGGUO)
    assert result.success is True
    assert [media.title for media in result.data] == [
        "开局签到十万亿功德", "我在天庭收废品", "一念神魔"]
    assert result.data[0].type == "电视剧"
    assert result.data[0].poster_path.endswith("~tplv-shrink:1080:0.image")

    one = instance.rank_list(source=SOURCE_HONGGUO, board_type="comic")
    assert [media.title for media in one.data] == ["一念神魔"]


def test_rank_list_proxies_hongguo_poster(monkeypatch):
    """默认把海报换成插件 /poster 接口：手机端不必直连字节 CDN。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())
    monkeypatch.setattr(
        "app.plugins.globalrankpro.hongguo.settings",
        SimpleNamespace(APP_DOMAIN="http://mp.example.com",
                        API_V1_STR="/api/v1", PROXY=None, TZ="Asia/Shanghai"))

    poster = instance.rank_list(source=SOURCE_HONGGUO).data[0].poster_path

    assert poster.startswith(
        "http://mp.example.com/api/v1/plugin/GlobalRankPro/poster?u=")
    # 原图地址整体 urlencode 进 u 参数，客户端不再直接连字节 CDN
    assert "ecombdimg.com" not in poster
    assert "~tplv-shrink%3A1080%3A0.image" in poster


def test_poster_url_falls_back_to_request_base(monkeypatch):
    """APP_DOMAIN 未配置时用请求地址拼，局域网 IP 访问也能出图。"""
    instance = make_plugin()
    instance.sources[SOURCE_HONGGUO].save_data("snapshot", hongguo_snapshot())
    monkeypatch.setattr(
        "app.plugins.globalrankpro.hongguo.settings",
        SimpleNamespace(APP_DOMAIN="", API_V1_STR="/api/v1",
                        PROXY=None, TZ="Asia/Shanghai"))
    request = SimpleNamespace(
        base_url="http://192.168.1.10:3000/",
        url=SimpleNamespace(hostname="192.168.1.10"))

    poster = instance.rank_list(
        source=SOURCE_HONGGUO, request=request).data[0].poster_path

    assert poster.startswith(
        "http://192.168.1.10:3000/api/v1/plugin/GlobalRankPro/poster?u=")


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
    """source=all 时逐个抓取并汇总条数（三源都算进去）。"""
    calls = []
    plugin.sources[SOURCE_MAOYAN].fetch = lambda: (
        calls.append("maoyan") or
        {"board_count": 2, "item_count": 4, "subscribed": 1})
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: (
        calls.append("netflix") or
        {"board_count": 6, "item_count": 8, "week": "2026-10-04"})
    plugin.sources[SOURCE_HONGGUO].fetch = lambda: (
        calls.append("hongguo") or
        {"board_count": 1, "item_count": 20})

    result = plugin.refresh_now()

    assert result.success is True
    assert isinstance(result.data, RankRefreshResult)
    assert calls == ["maoyan", "netflix", "hongguo"]
    assert (result.data.board_count, result.data.item_count,
            result.data.subscribed) == (9, 32, 1)
    assert [s.source for s in result.data.sources] == [
        SOURCE_MAOYAN, SOURCE_NETFLIX, SOURCE_HONGGUO]
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
    """单个数据源抛错被收敛成该源失败，其它源照常成功。"""
    def boom():
        raise RuntimeError("upstream down")

    plugin.sources[SOURCE_MAOYAN].fetch = boom
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: {
        "board_count": 6, "item_count": 8, "week": "2026-10-04"}
    plugin.sources[SOURCE_HONGGUO].fetch = lambda: {
        "board_count": 1, "item_count": 20}

    result = plugin.refresh_now()

    assert result.success is True  # 至少一个源成功
    assert result.data.board_count == 7      # Netflix 6 + 红果 1，猫眼失败不计
    maoyan_result = result.data.sources[0]
    assert maoyan_result.refreshed is False
    assert "upstream down" in maoyan_result.message


def test_refresh_all_failed_returns_business_error(plugin):
    """全部源失败时 success=False，但业务数据仍完整返回。"""
    plugin.sources[SOURCE_MAOYAN].fetch = lambda: {"board_count": 0}
    plugin.sources[SOURCE_NETFLIX].fetch = lambda: {"board_count": 0}
    plugin.sources[SOURCE_HONGGUO].fetch = lambda: {"board_count": 0}

    result = plugin.refresh_now()

    assert result.success is False
    assert result.data.refreshed is False
    assert len(result.data.sources) == 3


def test_refresh_when_nothing_enabled(plugin):
    """没有任何数据源启用时给出明确提示。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': False, 'netflix_enabled': False,
        'hongguo_enabled': False})

    result = plugin.refresh_now()

    assert result.success is False
    assert "未启用" in result.message


# ==================== 推荐页数据源 ====================

def _event():
    return type("Evt", (), {"event_data": RecommendSourceEventData()})()


def test_recommend_source_registers_all_sources(plugin):
    """三个数据源的推荐源合并注册，api_path 都指向本插件。"""
    event = _event()
    plugin.recommend_source(event)

    sources = event.event_data.extra_sources
    names = [s.name for s in sources]
    assert "猫眼电影票房" in names
    assert "Netflix 全球电影" in names
    assert "Netflix 中国香港剧集" in names
    assert "红果热播榜" in names           # 配置里只勾了 hot 榜
    assert all(s.api_path.startswith("plugin/GlobalRankPro/rank-list?source=")
               for s in sources)
    assert any(s.api_path.startswith(
        "plugin/GlobalRankPro/rank-list?source=maoyan") for s in sources)
    assert any(s.api_path.startswith(
        "plugin/GlobalRankPro/rank-list?source=netflix") for s in sources)
    assert any(s.api_path.startswith(
        "plugin/GlobalRankPro/rank-list?source=hongguo") for s in sources)


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
    """关掉 Netflix 与红果数据源后不再注册它们的推荐源。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'maoyan_type_movie': ['movie'],
        'maoyan_type_tv': [], 'netflix_enabled': False,
        'hongguo_enabled': False})

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
        'hongguo_enabled': True, 'hongguo_boards': ['comic', 'ai'],
        'hongguo_limit': 40,
        'enabled': True, 'cron': '0 8 * * *',
    })

    maoyan = plugin.sources[SOURCE_MAOYAN]
    assert maoyan.enabled is True
    assert maoyan._enabled is True          # 订阅开关
    assert maoyan._type == ["total", "zongyi"]
    netflix = plugin.sources[SOURCE_NETFLIX]
    assert netflix.enabled is False
    assert netflix._regions == ["tw"]
    hongguo = plugin.sources[SOURCE_HONGGUO]
    assert hongguo.enabled is True
    assert hongguo._boards == ["comic", "ai"]
    assert hongguo._limit == 40


def test_data_keys_are_isolated_between_sources(plugin):
    """三个源的快照与识别缓存互不覆盖（键带源前缀）。"""
    plugin.sources[SOURCE_MAOYAN].save_data("snapshot", {"marker": "m"})
    plugin.sources[SOURCE_NETFLIX].save_data("snapshot", {"marker": "n"})
    plugin.sources[SOURCE_HONGGUO].save_data("snapshot", {"marker": "h"})

    assert plugin.sources[SOURCE_MAOYAN].get_data("snapshot") == {"marker": "m"}
    assert plugin.sources[SOURCE_NETFLIX].get_data("snapshot") == {"marker": "n"}
    assert plugin.sources[SOURCE_HONGGUO].get_data("snapshot") == {"marker": "h"}
    # 直接看宿主侧真实键名
    assert plugin._data[f"{SOURCE_MAOYAN}_snapshot"] == {"marker": "m"}
    assert plugin._data[f"{SOURCE_NETFLIX}_snapshot"] == {"marker": "n"}
    assert plugin._data[f"{SOURCE_HONGGUO}_snapshot"] == {"marker": "h"}


def test_serialize_config_round_trips(plugin):
    """配置回写包含三个源的开关与默认值，且类型可控（None 收敛）。"""
    config = plugin.serialize_config()

    assert config["maoyan_enabled"] is True
    assert config["maoyan_type_movie"] == ["movie"]
    assert config["netflix_regions"] == ["global", "hk"]
    assert config["hongguo_enabled"] is True
    assert config["hongguo_boards"] == ["hot"]
    assert config["hongguo_limit"] == 20
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
                  "接入推荐页", "清理插件数据", "自动全网订阅", "自动平台榜订阅",
                  "榜单地区"):
        assert label in form_blob
    # 猫眼配置
    assert "猫眼榜单" in form_blob
    assert "启用猫眼数据源" in form_blob
    assert "电影榜单" in form_blob
    assert "剧集/综艺榜单" in form_blob
    for label in ("猫眼全网电影榜", "猫眼全网剧集榜", "猫眼全网综艺榜"):
        assert label in form_blob
    assert "全球年度" in form_blob
    # 平台细分榜：多选决定抓哪些平台并写进快照，条数只管订阅
    for label in ("平台专项榜", "腾讯视频", "爱奇艺", "芒果TV", "优酷"):
        assert label in form_blob
    assert "maoyan_platforms" in form_blob
    # 零散开关不再出现在表单上，避免「开了没反应」
    # （主榜抓不抓由「榜单类型」多选控制，*_rec_enabled 同样冗余）
    for dead in ("maoyan_sub_enabled", "maoyan_movie_rec_enabled",
                 "maoyan_tx_rec_enabled"):
        assert dead not in form_blob
    # Netflix 配置
    assert "Netflix 榜单" in form_blob
    assert "启用 Netflix 数据源" in form_blob
    assert "榜单地区" in form_blob

    assert defaults["maoyan_type_movie"] == ["movie"]
    assert defaults["netflix_regions"] == ["global", "hk"]
    assert defaults["maoyan_platforms"] == []  # 旧配置无 platforms 键 → 全不勾


def test_get_form_contains_hongguo_block(plugin):
    """设置页新增红果卡片：开关 + 榜单多选 + 每榜条数。"""
    form_items, defaults = plugin.get_form()
    form_blob = "".join(json.dumps(node, ensure_ascii=False)
                        for node in form_items)

    assert "红果短剧榜单" in form_blob
    assert "启用红果数据源" in form_blob
    assert "抓取榜单" in form_blob
    assert "每榜条数" in form_blob
    for model in ("hongguo_enabled", "hongguo_boards", "hongguo_limit"):
        assert model in form_blob
    # 四个官方热播榜都要能勾
    for name in ("热播榜", "真人剧热播榜", "漫剧热播榜", "AI剧热播榜"):
        assert name in form_blob

    assert defaults["hongguo_enabled"] is True
    assert defaults["hongguo_boards"] == ["hot"]
    assert defaults["hongguo_limit"] == 20


def test_get_form_reflects_saved_config(plugin):
    """表单默认值回显已保存配置，而不是写死的初值。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': False, 'maoyan_type_movie': ['total'],
        'netflix_enabled': True, 'netflix_regions': ['tw']})

    _, defaults = plugin.get_form()

    assert defaults["maoyan_enabled"] is False
    assert defaults["maoyan_type_movie"] == ["total"]
    assert defaults["netflix_regions"] == ["tw"]


def test_platform_multiselect_roundtrip(plugin):
    """platforms 多选 ↔ 各平台展示开关的双向转换，含旧开关配置的兼容读。"""
    from app.plugins.globalrankpro.base import SOURCE_MAOYAN

    picked = make_plugin({'maoyan_platforms': ['tx', 'mg']}, with_snapshots=False)
    maoyan = picked.sources[SOURCE_MAOYAN]
    assert maoyan._tx_rec_enabled is True and maoyan._tx_rec_num == 10
    assert maoyan._iqy_rec_enabled is False and maoyan._yk_rec_enabled is False
    # 表单回显勾选结果
    _, defaults = picked.get_form()
    assert defaults["maoyan_platforms"] == ["tx", "mg"]
    # 序列化只回传多选键，不再输出零散开关
    assert picked.serialize_config()["maoyan_platforms"] == ["tx", "mg"]
    assert "maoyan_tx_rec_enabled" not in picked.serialize_config()

    # 旧配置（无 platforms 键）按 *_rec_enabled 迁移
    legacy = make_plugin({'maoyan_tx_rec_enabled': True,
                          'maoyan_yk_rec_enabled': True}, with_snapshots=False)
    legacy_maoyan = legacy.sources[SOURCE_MAOYAN]
    assert legacy_maoyan._tx_rec_enabled is True
    assert legacy_maoyan._iqy_rec_enabled is False
    _, legacy_defaults = legacy.get_form()
    assert legacy_defaults["maoyan_platforms"] == ["tx", "yk"]


def test_get_page_shows_subscription_only(plugin):
    """详情页 = App 推荐 + 两源概览 + 订阅记录，不再平铺榜单条目。"""
    nodes = plugin.get_page()
    blob = json.dumps(nodes, ensure_ascii=False)

    # 顶部就是 App 推荐卡
    assert "MoviePilotApp" in json.dumps(nodes[0], ensure_ascii=False)
    # 概览保留两源统计与独立刷新入口
    assert "猫眼榜单" in blob and "Netflix 榜单" in blob
    assert '"source": "maoyan"' in blob and '"source": "netflix"' in blob
    # 榜单条目不再上页面（数据走 /rank、/rank-list 供推荐页与 App）
    assert "生化危机：爆发夜" not in blob
    assert "161.93万" not in blob
    assert "炸弹追缉令" not in blob
    # 订阅记录卡存在；测试夹具没有订阅数据，显示空态
    assert "自动订阅记录" in blob
    assert "暂无订阅记录" in blob


def test_get_page_history_table(plugin):
    """订阅记录表格渲染标题、平台、时间与 TMDB 跳转。"""
    plugin.sources[SOURCE_MAOYAN].save_data("history", [{
        "title": "许我耀眼", "type": "电视剧", "platformDesc": "腾讯视频",
        "releaseInfo": "上线3天", "time": "2026-10-17 08:00:00",
        "media_source": "themoviedb", "media_id": "241561",
        "poster": "https://image.tmdb.org/t/p/w500/a.jpg",
    }])

    blob = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "许我耀眼" in blob
    assert "腾讯视频" in blob
    assert "themoviedb.org" in blob  # 标题带 TMDB 链接
    assert "1 条" in blob


def test_get_page_warns_when_source_disabled(plugin):
    """关掉的数据源在概览卡标「未启用」，且不给刷新按钮。"""
    plugin._GlobalRankPro__apply_config({
        'maoyan_enabled': True, 'netflix_enabled': False})

    blob = json.dumps(plugin.get_page(), ensure_ascii=False)

    assert "Netflix 榜单" in blob
    assert "未启用" in blob
    assert "在设置里启用" in blob


def test_get_page_without_snapshot_shows_hint():
    """无快照时概览标「暂无快照」，页面不炸。"""
    blob = json.dumps(make_plugin(with_snapshots=False).get_page(),
                      ensure_ascii=False)
    assert "暂无快照" in blob
    assert "刷新" in blob


def test_page_overview_shows_source_stats(plugin):
    """概览卡（App 推荐卡之后）给出两源的榜数与条数。"""
    blob = json.dumps(plugin.get_page()[1], ensure_ascii=False)

    assert "猫眼榜单" in blob
    assert "Netflix 榜单" in blob
    assert "个榜" in blob
    assert "更新于" in blob


def test_netflix_region_choices_include_us(plugin):
    """Netflix 地区白名单包含美国，设置页多选跟着出现。"""
    netflix = plugin.sources[SOURCE_NETFLIX]
    assert netflix._REGION_NAMES.get("us") == "美国"

    form_blob = json.dumps(plugin.get_form()[0], ensure_ascii=False)
    assert "\"title\": \"美国\"" in form_blob
    assert "\"value\": \"us\"" in form_blob


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
