"""猫眼榜单对外接口与推荐源注册的回归测试。

覆盖范围：
1. `/rank` 快照的名次、量值、媒体身份、筛选、条数限制与兜底；
2. 旧快照字段兼容（升级后不会让接口报错）；
3. `/rank-list` 产出宿主推荐页所需的 MediaInfo 并正确终止分页；
4. `get_api()` 的响应模型与认证声明符合宿主 V3 合同；
5. 推荐源事件按配置叠加数据源；
6. 刷新入口落快照、回填媒体身份、失败时保留旧快照；
7. 猫眼现网字段（boxSplitUnit / currHeatDesc）的量值解析；
8. 既有订阅行为（抓取参数顺序、年号解析）未被破坏。

测试放在 `tests/v3/<plugin_id>/` 下（与上游仓库约定一致，不能放进插件目录，
否则会被整目录下发到运行时副本）。
"""

import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.plugins import maoyanpro
from app.plugins.maoyanpro import (
    MaoyanPro,
    MaoyanProSnapshot,
    MaoyanRefreshResult,
)
from app.schemas import (
    MediaInfo,
    MediaType,
    RecommendMediaSource,
    RecommendSourceEventData,
)
from app.schemas.types import ChainEventType


@pytest.fixture
def plugin(monkeypatch):
    """绕过宿主初始化，并隔离网络与浏览器。"""
    instance = object.__new__(MaoyanPro)
    instance._data = {}
    instance._type_movie = ['movie', 'web-movie']
    instance._type_tv = ['web-all', 'zongyi']
    instance._type_zongyi = ['zongyi']
    instance._type = ['movie', 'web-movie', 'web-all', 'zongyi']
    instance._enabled = True
    instance._recommend_enabled = True
    instance._api_enabled = True
    instance._movie_sub_num = 10
    instance._movie_rec_num = 20
    instance._tv_sub_num = 10
    instance._tv_rec_num = 20
    instance._zongyi_sub_num = 10
    instance._zongyi_rec_num = 20
    instance._tx_sub_num = 0
    instance._iqy_sub_num = 0
    instance._mg_sub_num = 0
    instance._yk_sub_num = 0
    instance._tx_rec_num = 0
    instance._iqy_rec_num = 0
    instance._mg_rec_num = 0
    instance._yk_rec_num = 0
    monkeypatch.setattr(instance, "_MaoyanPro__with_browser", lambda callback: None)
    monkeypatch.setattr(maoyanpro, "RequestUtils", lambda *a, **k: SimpleNamespace(
        get_res=lambda *a, **k: None
    ))
    return instance


def snapshot_boards():
    """一份快照样本，字段与插件落盘结构一致。"""
    return [
        {
            "board": "电影票房榜单", "type": "movie", "type_name": "电影票房",
            "platform": "", "platform_type": "", "show_date": "2026-10-15",
            "url": "https://piaofang.maoyan.com/dashboard-ajax/movie",
            "count": 2, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "生化危机：爆发夜", "board_type": "movie",
                 "type_name": "电影票房", "platformDesc": "院线", "releaseInfo": "上映11天",
                 "board": "电影票房榜单", "show_date": "2026-10-15",
                 "box_office": "161.93万", "total_box_office": "1.87亿", "heat": "28.1%",
                 "title_cn": "",
                 "media_source": "themoviedb", "media_id": "1586199",
                 "poster": "https://image.tmdb.org/t/p/w500/a.jpg",
                 "overview": "简介", "year": "2026"},
                {"rank": 2, "title": "神探之痕迹", "board_type": "movie",
                 "type_name": "电影票房", "platformDesc": "院线", "releaseInfo": "上映15天",
                 "board": "电影票房榜单", "show_date": "2026-10-15",
                 "box_office": "138.06万", "heat": "23.9%",
                 "media_source": "", "media_id": "", "poster": "",
                 "overview": "", "year": ""},
            ],
        },
        {
            "board": "剧集综合榜单（腾讯视频）", "type": "web-all", "type_name": "剧集综合",
            "platform": "腾讯视频", "platform_type": "3", "show_date": "2026-10-15",
            "url": ("https://piaofang.maoyan.com/i/api/encrypt/dashboard/"
                    "webHeatData?seriesType=0&platformType=3"),
            "count": 1, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "沉默的荣耀", "board_type": "web-all",
                 "type_name": "剧集综合", "platformDesc": "腾讯视频",
                 "releaseInfo": "上线28天", "board": "剧集综合榜单（腾讯视频）",
                 "show_date": "2026-10-15", "box_office": "", "heat": "9820",
                 "media_source": "themoviedb", "media_id": "219920",
                 "poster": "https://image.tmdb.org/t/p/w500/b.jpg",
                 "overview": "简介", "year": "2025"},
            ],
        },
    ]


def test_rank_endpoint_returns_snapshot_with_metrics(plugin):
    """`/rank` 返回带名次与量值的榜单快照，未识别条目也保留。"""
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-15 12:00:00", "total": 3, "boards": snapshot_boards(),
    })

    result = plugin.get_rank_snapshot()

    assert result.success is True
    assert isinstance(result.data, MaoyanProSnapshot)
    assert result.data.updated_at == "2026-10-15 12:00:00"
    assert result.data.total == 3
    assert [board.board for board in result.data.boards] == [
        "电影票房榜单", "剧集综合榜单（腾讯视频）",
    ]
    first = result.data.boards[0].items[0]
    assert (first.rank, first.title, first.box_office, first.heat) == (
        1, "生化危机：爆发夜", "161.93万", "28.1%",
    )
    assert first.total_box_office == "1.87亿"
    assert first.title_cn == ""
    assert (first.media_source, first.media_id) == ("themoviedb", "1586199")
    # 上榜但未识别到媒体的条目仍要保留，只是身份为空
    assert result.data.boards[0].items[1].title == "神探之痕迹"
    assert result.data.boards[0].items[1].media_id == ""


def test_rank_endpoint_filters_by_type_board_and_limit(plugin):
    """`/rank` 支持按类型、榜单名筛选与条数限制。"""
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-15 12:00:00", "total": 3, "boards": snapshot_boards(),
    })

    only_heat = plugin.get_rank_snapshot(board_type="web-all")
    assert len(only_heat.data.boards) == 1
    assert only_heat.data.boards[0].type == "web-all"

    limited = plugin.get_rank_snapshot(limit=1)
    assert limited.data.total == 2
    assert [len(board.items) for board in limited.data.boards] == [1, 1]

    by_board = plugin.get_rank_snapshot(board="腾讯")
    assert len(by_board.data.boards) == 1
    assert by_board.data.boards[0].platform == "腾讯视频"

    by_platform = plugin.get_rank_snapshot(platform="3")
    assert len(by_platform.data.boards) == 1
    assert by_platform.data.boards[0].platform_type == "3"

    missing = plugin.get_rank_snapshot(board_type="zongyi")
    assert missing.data.boards == []
    assert missing.data.total == 0


def test_rank_endpoint_falls_back_to_history(plugin):
    """快照未生成时用订阅流水兜底并提示，空历史也不报错。"""
    plugin.save_data("history", [
        {"title": "旧条目", "type": MediaType.TV.value, "platformDesc": "爱奇艺",
         "releaseInfo": "上线3天", "media_source": "themoviedb", "media_id": "1",
         "poster": "", "overview": "", "year": "2026", "time": "2026-10-14 09:00:00"},
    ])

    result = plugin.get_rank_snapshot()

    assert result.success is True
    assert "订阅流水" in result.message
    assert result.data.total == 1
    assert result.data.boards[0].items[0].title == "旧条目"

    plugin._data = {}
    assert plugin.get_rank_snapshot().data.boards == []


def test_rank_endpoint_tolerates_legacy_snapshot_fields(plugin):
    """旧快照缺字段或带未知字段时仍能序列化。"""
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-14 09:00:00",
        "boards": [{"board": "猫眼订阅流水", "type": "", "show_date": None,
                    "unknown_field": "legacy",
                    "items": [{"title": "旧格式条目", "tmdbid": 123, "rank": None}]}],
    })

    result = plugin.get_rank_snapshot()

    assert result.success is True
    board = result.data.boards[0]
    assert board.show_date == ""
    item = board.items[0]
    assert (item.title, item.rank, item.media_source) == ("旧格式条目", 0, "")


def test_rank_list_produces_host_media_info(plugin):
    """`/rank-list` 产出宿主推荐页可直接渲染的媒体信息。"""
    plugin.save_data("snapshot", {"boards": snapshot_boards()})

    result = plugin.rank_list(board_type="movie")

    assert result.success is True
    assert [item.title for item in result.data] == ["生化危机：爆发夜", "神探之痕迹"]
    first = result.data[0]
    assert isinstance(first, MediaInfo)
    assert (first.media_source, first.media_id) == ("themoviedb", "1586199")
    assert first.year == "2026"
    assert first.title_year == "生化危机：爆发夜 (2026)"
    assert first.poster_path.endswith("a.jpg")
    # 未识别身份的条目仍返回，只是身份为空
    assert result.data[1].media_source is None
    assert result.data[1].media_id is None


def test_rank_list_terminates_pagination(plugin):
    """列表视图翻到第二页立即返回空，避免无限翻页。"""
    plugin.save_data("snapshot", {"boards": snapshot_boards()})

    assert plugin.rank_list(board_type="movie", page=2).data == []
    assert len(plugin.rank_list(board_type="movie", page=1).data) == 2


def test_rank_list_filters_by_platform(plugin):
    """`/rank-list` 带 platform 返回对应平台细分榜。"""
    plugin.save_data("snapshot", {"boards": snapshot_boards()})

    platform = plugin.rank_list(board_type="web-all", platform=3)
    assert [item.title for item in platform.data] == ["沉默的荣耀"]

    # 平台对不上时返回空列表
    assert plugin.rank_list(board_type="web-all", platform=2).data == []


def test_rank_list_paging_carries_platform_boards(plugin):
    """「更多」页分页：第 1 页全网榜，后续页依次为有数据的平台榜（标题带平台后缀）。"""
    boards = snapshot_boards()
    boards += [
        {
            "board": "剧集综合榜单", "type": "web-all", "type_name": "剧集综合",
            "platform": "", "platform_type": "", "show_date": "2026-10-15",
            "url": "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData",
            "count": 1, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "雁回时", "board_type": "web-all",
                 "type_name": "剧集综合", "platformDesc": "全网",
                 "releaseInfo": "上线10天", "board": "剧集综合榜单",
                 "show_date": "2026-10-15", "heat": "9100",
                 "media_source": "themoviedb", "media_id": "260000",
                 "poster": "https://image.tmdb.org/t/p/w500/c.jpg",
                 "overview": "", "year": "2026"},
            ],
        },
        {
            "board": "剧集综合榜单（爱奇艺）", "type": "web-all", "type_name": "剧集综合",
            "platform": "爱奇艺", "platform_type": "2", "show_date": "2026-10-15",
            "url": ("https://piaofang.maoyan.com/i/api/encrypt/dashboard/"
                    "webHeatData?platformType=2"),
            "count": 1, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "玉簟秋", "board_type": "web-all",
                 "type_name": "剧集综合", "platformDesc": "爱奇艺",
                 "releaseInfo": "上线5天", "board": "剧集综合榜单（爱奇艺）",
                 "show_date": "2026-10-15", "heat": "8800",
                 "media_source": "themoviedb", "media_id": "260001",
                 "poster": "https://image.tmdb.org/t/p/w500/d.jpg",
                 "overview": "", "year": "2026"},
            ],
        },
    ]
    plugin.save_data("snapshot", {"boards": boards})
    plugin._tx_rec_num = 10
    plugin._iqy_rec_num = 10

    page1 = plugin.rank_list(board_type="web-all", page=1)
    assert [item.title for item in page1.data] == ["雁回时"]
    # 推荐条数为 0 的平台即使快照里有数据也不进页序
    page2 = plugin.rank_list(board_type="web-all", page=2)
    assert [item.title for item in page2.data] == ["沉默的荣耀（腾讯视频）"]
    assert page2.data[0].title_year == "沉默的荣耀（腾讯视频） (2025)"
    page3 = plugin.rank_list(board_type="web-all", page=3)
    assert [item.title for item in page3.data] == ["玉簟秋（爱奇艺）"]
    # 超出页序返回空列表终止翻页
    assert plugin.rank_list(board_type="web-all", page=4).data == []


def test_rank_list_handles_unknown_board_type(plugin):
    """快照中不存在的榜单类型返回空列表而不是报错。"""
    plugin.save_data("snapshot", {"boards": snapshot_boards()})

    result = plugin.rank_list(board_type="zongyi")

    assert result.success is True
    assert result.data == []


def test_get_api_declares_typed_responses(plugin):
    """接口清单须声明响应模型与认证方式，符合宿主 V3 合同。"""
    apis = {api["path"]: api for api in plugin.get_api()}

    assert set(apis) == {"/rank", "/rank-list", "/refresh"}
    assert apis["/rank"]["response_model"] is not None
    assert apis["/rank"]["methods"] == ["GET"]
    # 推荐页走宿主普通数据客户端，必须用网页登录态并返回统一响应
    assert apis["/rank-list"]["auth"] == "bear"
    assert apis["/rank-list"]["response_model"] is not None
    assert apis["/refresh"]["methods"] == ["GET", "POST"]
    assert apis["/refresh"]["response_model"] is MaoyanRefreshResult
    assert all(api.get("summary") for api in apis.values())


def test_recommend_source_registers_configured_boards(plugin):
    """推荐源按已配置榜单类型叠加数据源，不覆盖其它插件的数据。"""
    other = RecommendMediaSource(name="已有来源", api_path="plugin/Other/x", type="榜单")
    event_data = RecommendSourceEventData(extra_sources=[other])

    plugin.recommend_source(SimpleNamespace(event_data=event_data))

    names = [source.name for source in event_data.extra_sources]
    assert names[0] == "已有来源"
    for expected in ["猫眼电影票房", "猫眼剧集综合热度", "猫眼综艺热度"]:
        assert expected in names
    for source in event_data.extra_sources[1:]:
        assert source.api_path.startswith("plugin/MaoyanPro/rank-list?board_type=")
    movie_source = next(s for s in event_data.extra_sources if s.name == "猫眼电影票房")
    assert movie_source.type == "电影"


def test_recommend_source_keeps_platform_boards_off_main_page(plugin):
    """平台推荐条数 > 0 也不注册平台源，主页面只保留全网源保持简洁。"""
    plugin._tx_rec_num = 10
    event_data = RecommendSourceEventData()

    plugin.recommend_source(SimpleNamespace(event_data=event_data))

    names = [source.name for source in event_data.extra_sources]
    assert names == ["猫眼电影票房", "猫眼剧集综合热度", "猫眼综艺热度"]
    assert not any("腾讯" in name or "爱奇艺" in name or "芒果" in name or "优酷" in name
                   for name in names)


def test_recommend_source_skips_when_disabled(plugin):
    """未开启推荐源或对外接口停用时都不注册数据源；订阅开关不影响推荐源。"""
    plugin._recommend_enabled = False
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources == []

    plugin._recommend_enabled = True
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources

    # 订阅总开关关闭时，推荐页数据源照常注册（v1.1.0 起功能解耦）
    plugin._enabled = False
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources

    plugin._api_enabled = False
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources == []


def test_recommend_source_only_includes_selected_boards(plugin):
    """只配置电影时不注册剧集类数据源。"""
    plugin._type = ["movie"]
    event_data = RecommendSourceEventData()

    plugin.recommend_source(SimpleNamespace(event_data=event_data))

    assert [source.name for source in event_data.extra_sources] == ["猫眼电影票房"]


def test_recommend_source_registers_combined_tv_board(plugin):
    """配置剧集综合榜时注册对应的推荐源。"""
    plugin._type = ["web-all"]
    event_data = RecommendSourceEventData()

    plugin.recommend_source(SimpleNamespace(event_data=event_data))

    assert [source.name for source in event_data.extra_sources] == ["猫眼剧集综合热度"]
    assert event_data.extra_sources[0].api_path.endswith("board_type=web-all")


def test_refresh_now_reports_counts(plugin, monkeypatch):
    """手动刷新返回抓取与订阅统计。"""
    monkeypatch.setattr(plugin, "_MaoyanPro__refresh_maoyan", lambda: {
        "board_count": 2, "item_count": 4, "subscribed": 2,
    })

    result = plugin.refresh_now()

    assert isinstance(result, MaoyanRefreshResult)
    assert result.refreshed is True
    assert (result.board_count, result.item_count, result.subscribed) == (2, 4, 2)


def test_refresh_now_handles_missing_config_and_errors(plugin, monkeypatch):
    """未配置榜单或刷新抛错时返回明确的业务失败，不向上抛异常。"""
    plugin._type = []
    empty = plugin.refresh_now()
    assert empty.refreshed is False
    assert "未配置" in empty.message

    plugin._type = ["movie"]

    def boom():
        raise RuntimeError("upstream down")

    monkeypatch.setattr(plugin, "_MaoyanPro__refresh_maoyan", boom)
    failed = plugin.refresh_now()
    assert failed.refreshed is False
    assert "刷新失败" in failed.message


def test_refresh_builds_board_snapshot_with_identity(plugin, monkeypatch):
    """刷新入口落成带媒体身份的持久化快照。"""
    plugin._type = ['movie', 'web-all']
    saved = {}
    monkeypatch.setattr(plugin, "get_data", lambda key: [] if key == "history" else None)
    monkeypatch.setattr(plugin, "save_data",
                        lambda key, value: saved.__setitem__(key, value))

    def fetch(movie_url, tv_urls, web_movie_url, num):
        """模拟一次抓取：设置榜单元数据并返回待订阅条目。"""
        plugin._last_boards = [{
            "board": "电影票房榜单", "type": "movie", "type_name": "电影票房",
            "platform": "", "platform_type": "", "show_date": "2026-10-15", "url": movie_url, "count": 1,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [{"rank": 1, "title": "院线电影", "board_type": "movie",
                       "type_name": "", "platformDesc": "院线", "releaseInfo": "上映3天",
                       "board": "电影票房榜单", "show_date": "2026-10-15",
                       "box_office": "100万", "heat": "20%"}],
        }]
        return [{"title": "院线电影", "releaseInfo": "上映3天"}], [], \
            [{"title": "院线电影", "releaseInfo": "上映3天"}], []

    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)

    def recognize(meta, mtype, cache):
        """返回带身份的识别结果，用于验证回填。"""
        return SimpleNamespace(
            title=meta.name, year="2026", type=mtype,
            media_source=SimpleNamespace(value="themoviedb"), media_id="42",
            get_poster_image=lambda: "https://image.tmdb.org/t/p/w500/p.jpg",
            overview="剧情简介",
        )

    plugin.chain = SimpleNamespace(recognize_media=recognize)
    plugin.downloadchain = SimpleNamespace(
        get_no_exists_info=lambda meta, mediainfo: (False, {}))
    plugin.subscribechain = SimpleNamespace(
        exists=lambda mediainfo, meta: False, add=lambda **kwargs: None)
    monkeypatch.setattr(plugin, "_MaoyanPro__resolve_tv_subscribe_season",
                        lambda **kwargs: 1)

    stats = plugin._MaoyanPro__refresh_maoyan()

    assert stats["board_count"] == 1
    assert stats["subscribed"] == 1
    item = saved["snapshot"]["boards"][0]["items"][0]
    assert item["media_source"] == "themoviedb"
    assert item["media_id"] == "42"
    assert item["poster"].endswith("p.jpg")
    assert item["year"] == "2026"
    assert item["type_name"] == "电影票房"


def test_refresh_keeps_previous_snapshot_when_fetch_fails(plugin, monkeypatch):
    """抓取全失败时保留上一次快照，避免接口在异常时段返回空数据。"""
    plugin._type = ['movie']
    previous = {"updated_at": "2026-10-14 09:00:00", "total": 1,
                "boards": snapshot_boards()}
    saved = {}
    monkeypatch.setattr(plugin, "get_data",
                        lambda key: previous if key == "snapshot" else [])
    monkeypatch.setattr(plugin, "save_data",
                        lambda key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info",
                        lambda *args, **kwargs: ([], [], [], []))

    plugin._MaoyanPro__refresh_maoyan()

    assert "snapshot" not in saved
    assert plugin.get_data("snapshot") == previous


def test_refresh_skips_subscribe_but_keeps_snapshot_when_disabled(plugin, monkeypatch):
    """订阅总开关关闭时完全不订阅，但榜单快照照常更新（推荐页数据不受影响）。"""
    plugin._type = ['movie']
    plugin._enabled = False
    plugin._recommend_enabled = True
    saved = {}
    monkeypatch.setattr(plugin, "get_data", lambda key: [] if key == "history" else None)
    monkeypatch.setattr(plugin, "save_data",
                        lambda key, value: saved.__setitem__(key, value))

    def fetch(movie_url, tv_urls, web_movie_url, num):
        """模拟一次抓取：返回待订阅条目，验证关闭订阅后不会被消费。"""
        plugin._last_boards = [{
            "board": "电影票房榜单", "type": "movie", "type_name": "电影票房",
            "platform": "", "platform_type": "", "show_date": "2026-10-15", "url": movie_url, "count": 1,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [{"rank": 1, "title": "院线电影", "board_type": "movie",
                       "type_name": "", "platformDesc": "院线", "releaseInfo": "上映3天",
                       "board": "电影票房榜单", "show_date": "2026-10-15"}],
        }]
        return [{"title": "院线电影"}], [], [{"title": "院线电影"}], []

    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)
    set_sub = Mock()
    monkeypatch.setattr(MaoyanPro, "set_sub", set_sub)

    stats = plugin._MaoyanPro__refresh_maoyan()

    set_sub.assert_not_called()
    assert "snapshot" in saved
    assert saved["snapshot"]["boards"][0]["items"][0]["title"] == "院线电影"


def test_rank_metrics_reads_current_maoyan_fields(plugin):
    """量值解析覆盖现网字段：票房用 boxSplitUnit，热度用 currHeatDesc。"""
    metrics = plugin._MaoyanPro__rank_metrics(
        {"boxSplitUnit": {"num": "161.93", "unit": "万"}, "boxRate": "28.1%",
         "sumBoxDesc": "3.30亿", "showCount": 98919},
        {"releaseInfo": "上映11天", "platformDesc": "院线"},
    )
    assert metrics["box_office"] == "161.93万"
    assert metrics["total_box_office"] == "3.30亿"
    assert metrics["heat"] == "28.1%"
    assert metrics["release_info"] == "上映11天"

    heat = plugin._MaoyanPro__rank_metrics(
        {"currHeatDesc": "9820", "barValue": "9820"}, {"platformDesc": "腾讯视频"})
    assert heat["heat"] == "9820"
    assert heat["platform"] == "腾讯视频"
    assert heat["total_box_office"] == ""

    empty = plugin._MaoyanPro__rank_metrics({}, {})
    assert (empty["box_office"], empty["total_box_office"], empty["heat"]) == ("", "", "")


def test_parse_rank_items_keeps_rank_and_board(plugin):
    """条目解析保留名次与榜单归属，并跳过坏条目。"""
    data = [
        {"movieInfo": {"movieName": "第一条", "releaseInfo": "上映3天"}},
        None,
        {"movieInfo": {"movieName": ""}},
        {"movieInfo": {"movieName": "第四条"}},
    ]
    items = plugin._MaoyanPro__parse_rank_items(
        data, 4, "movieInfo", "movieName", "电影票房榜单",
        meta={"board": "电影票房榜单", "board_type": "movie",
              "url": "u", "show_date": "2026-10-15"},
    )

    assert [(item["rank"], item["title"]) for item in items] == [(1, "第一条"), (4, "第四条")]
    assert items[0]["board"] == "电影票房榜单"
    assert items[0]["board_type"] == "movie"
    assert items[0]["releaseInfo"] == "上映3天"


TOTAL_BOARD_HTML = """
<ul class="row" data-com="hrefTo,href:'/movie/3446690'">
    <li class="col0">1</li>
    <li class="col1">
        <p class="first-line">哪吒之魔童闹海</p>
        <p class="second-line">2025-01-29 上映</p>
    </li>
    <li class="col2 tr">1544614</li>
    <li class="col3 tr">47.639984</li>
    <li class="col4 tr">24</li>
</ul>
<ul class="row" data-com="hrefTo,href:'/movie/1200471'">
    <li class="col0">2</li>
    <li class="col1">
        <p class="first-line">长津湖</p>
        <p class="second-line">2021-09-30 上映</p>
    </li>
    <li class="col2 tr">577534</li>
    <li class="col3 tr">46.383892</li>
    <li class="col4 tr">22</li>
</ul>
<ul class="row" data-com="hrefTo,href:'/movie/0'">
    <li class="col0">3</li>
    <li class="col1"><p class="first-line"></p></li>
    <li class="col2 tr">100</li>
</ul>
"""

GLOBAL_BOARD_HTML = """
<tr>
    <th>Rank</th><th>Release Group</th><th>Worldwide</th>
    <th>Domestic</th><th>%</th><th>Foreign</th><th>%</th>
</tr>
<tr>
    <td class="a-text-right mojo-field-type-rank">1</td>
    <td class="a-text-left mojo-field-type-release">
        <a href="/release/rl1234/">Spider-Man: Brand New Day</a></td>
    <td class="a-text-right mojo-field-type-money">$2,513,503,453</td>
    <td class="a-text-right mojo-field-type-money">$957,590,528</td>
    <td class="a-text-right mojo-field-type-percent">38.1%</td>
    <td class="a-text-right mojo-field-type-money">$1,555,912,925</td>
    <td class="a-text-right mojo-field-type-percent">61.9%</td>
</tr>
<tr>
    <td class="a-text-right mojo-field-type-rank">2</td>
    <td class="a-text-left mojo-field-type-release"><a href="/release/rl5678/">The Odyssey</a></td>
    <td class="a-text-right mojo-field-type-money">$1,783,073,069</td>
    <td class="a-text-right mojo-field-type-money">$624,348,825</td>
    <td class="a-text-right mojo-field-type-percent">35%</td>
    <td class="a-text-right mojo-field-type-money">$1,158,724,244</td>
    <td class="a-text-right mojo-field-type-percent">65%</td>
</tr>
<tr>
    <td class="a-text-right mojo-field-type-rank">3</td>
    <td class="a-text-left mojo-field-type-release"></td>
    <td class="a-text-right mojo-field-type-money">n/a</td>
</tr>
"""


def test_format_wan_amount():
    """猫眼票房数值（万元）按量级转成亿/万文本，非法值返回空串。"""
    fmt = MaoyanPro._MaoyanPro__format_wan_amount
    assert fmt("1544614") == "154.46亿"
    assert fmt("577534") == "57.75亿"
    assert fmt("9999") == "9999万"
    assert fmt("") == ""
    assert fmt("abc") == ""


def test_format_usd_amount():
    """Box Office Mojo 美元数值按量级转成亿美元/万美元文本，非法值返回空串。"""
    fmt = MaoyanPro._MaoyanPro__format_usd_amount
    assert fmt("$2,513,503,453") == "25.14亿美元"
    assert fmt("$1,783,073,069") == "17.83亿美元"
    assert fmt("$95,759") == "9.6万美元"
    assert fmt("$999") == "999美元"
    assert fmt("") == ""
    assert fmt("n/a") == ""


def test_fetch_global_board_parses_ssr_html(plugin, monkeypatch):
    """全球年度票房榜解析 Box Office Mojo SSR 表格：英文片名与美元票房，坏条目跳过。"""
    monkeypatch.setattr(maoyanpro, "RequestUtils", lambda *a, **k: SimpleNamespace(
        get_res=lambda *a, **k: SimpleNamespace(
            text=GLOBAL_BOARD_HTML, raise_for_status=lambda: None)))

    board = plugin._MaoyanPro__fetch_global_board()

    assert board["type"] == "global"
    assert board["type_name"] == "全球年度票房"
    assert board["count"] == 2
    first = board["items"][0]
    assert (first["rank"], first["title"]) == (1, "Spider-Man: Brand New Day")
    # releaseInfo 放榜单年份，供媒体识别做年份线索
    assert first["releaseInfo"] == str(datetime.datetime.now().year)
    assert first["box_office"] == "25.14亿美元"
    assert first["total_box_office"] == "25.14亿美元"
    assert board["items"][1]["title"] == "The Odyssey"
    assert board["items"][1]["box_office"] == "17.83亿美元"


def test_global_board_flows_to_recommend_source_and_rank_list(plugin, monkeypatch):
    """勾选全球年度票房榜后：推荐页注册独立源，rank-list 可按 board_type=global 取数。"""
    monkeypatch.setattr(maoyanpro, "RequestUtils", lambda *a, **k: SimpleNamespace(
        get_res=lambda *a, **k: SimpleNamespace(
            text=GLOBAL_BOARD_HTML, raise_for_status=lambda: None)))
    board = plugin._MaoyanPro__fetch_global_board()
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-17 08:00:00", "total": 1, "boards": [board],
    })
    plugin._type = ["movie", "global"]

    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    global_source = next(
        s for s in event_data.extra_sources if s.name == "全球年度票房")
    assert global_source.api_path == "plugin/MaoyanPro/rank-list?board_type=global"
    assert global_source.type == "电影"

    resp = plugin.rank_list(board_type="global")
    assert resp.success is True
    assert [media.title for media in resp.data] == [
        "Spider-Man: Brand New Day", "The Odyssey"]


def test_fetch_year_board_uses_year_param(plugin, monkeypatch):
    """国内年度票房榜与影史榜共用解析，按 ?year= 当前年份抓取。"""
    captured = {}

    def fake_utils(*args, **kwargs):
        return SimpleNamespace(get_res=lambda url, **kw: (
            captured.setdefault("url", url),
            SimpleNamespace(text=TOTAL_BOARD_HTML, raise_for_status=lambda: None),
        )[1])

    monkeypatch.setattr(maoyanpro, "RequestUtils", fake_utils)

    board = plugin._MaoyanPro__fetch_total_board("2026-10-17", "year")

    assert board["type"] == "year"
    assert board["type_name"] == "国内年度票房"
    assert captured["url"].startswith("https://piaofang.maoyan.com/rankings/year?year=")
    assert captured["url"].endswith(str(datetime.datetime.now().year))
    assert board["items"][0]["title"] == "哪吒之魔童闹海"


def test_recognized_items_carry_chinese_title(plugin, monkeypatch):
    """识别链路把 TMDB 中文名写进快照条目，rank-list 优先展示中文名。"""
    recognize = Mock(return_value=SimpleNamespace(
        title="蜘蛛侠：崭新一日", year="2026", type=MediaType.MOVIE,
        media_source=SimpleNamespace(value="themoviedb"), media_id="1",
        get_poster_image=lambda: "http://img/poster.jpg", overview="简介"))
    plugin.chain = SimpleNamespace(recognize_media=recognize)

    plugin._MaoyanPro__resolve_board_identities([
        {"type": "global", "items": [
            {"title": "Spider-Man: Brand New Day", "releaseInfo": "2026",
             "board_type": "global"},
        ]},
    ])

    identity = plugin._MaoyanPro__resolve_board_identities  # noqa: F841
    snapshot_items = [{"title": "Spider-Man: Brand New Day", "releaseInfo": "2026",
                       "board_type": "global", "rank": 1}]
    # 从 _resolved_identity 取身份写入快照的路径与 __save_snapshot 一致
    identity_map = plugin._resolved_identity
    row = snapshot_items[0]
    info = identity_map.get(row["title"]) or {}
    row.update({
        "media_source": info.get("media_source", ""),
        "media_id": info.get("media_id", ""),
        "title_cn": info.get("title_cn", ""),
    })
    assert row["title_cn"] == "蜘蛛侠：崭新一日"

    plugin.save_data("snapshot", {
        "updated_at": "2026-10-17 08:00:00", "total": 1,
        "boards": [{"board": "全球年度票房榜单", "type": "global",
                    "type_name": "全球年度票房", "platform": "", "platform_type": "",
                    "show_date": "2026-10-17", "url": "u", "count": 1,
                    "updated_at": "2026-10-17 08:00:00", "items": [row]}],
    })
    resp = plugin.rank_list(board_type="global")
    assert [media.title for media in resp.data] == ["蜘蛛侠：崭新一日"]


def test_fetch_total_board_parses_ssr_html(plugin, monkeypatch):
    """影史总票房榜解析 SSR 直出页面：片名、上映日期与票房量值，坏条目跳过。"""
    monkeypatch.setattr(maoyanpro, "RequestUtils", lambda *a, **k: SimpleNamespace(
        get_res=lambda *a, **k: SimpleNamespace(
            text=TOTAL_BOARD_HTML, raise_for_status=lambda: None)))

    board = plugin._MaoyanPro__fetch_total_board("2026-10-17")

    assert board["type"] == "total"
    assert board["type_name"] == "影史总票房"
    assert board["count"] == 2
    first = board["items"][0]
    assert (first["rank"], first["title"], first["releaseInfo"]) == (
        1, "哪吒之魔童闹海", "2025-01-29 上映")
    assert first["box_office"] == "154.46亿"
    assert first["total_box_office"] == "154.46亿"
    assert board["items"][1]["box_office"] == "57.75亿"
    assert board["items"][1]["heat"] == ""


def test_total_board_flows_to_recommend_source_and_rank_list(plugin, monkeypatch):
    """勾选影史总票房榜后：推荐页注册独立源，rank-list 可按 board_type=total 取数。"""
    monkeypatch.setattr(maoyanpro, "RequestUtils", lambda *a, **k: SimpleNamespace(
        get_res=lambda *a, **k: SimpleNamespace(
            text=TOTAL_BOARD_HTML, raise_for_status=lambda: None)))
    board = plugin._MaoyanPro__fetch_total_board("2026-10-17")
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-17 08:00:00", "total": 2, "boards": [board],
    })
    plugin._type = ["movie", "total"]

    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    total_source = next(
        s for s in event_data.extra_sources if s.name == "猫眼影史总票房")
    assert total_source.api_path == "plugin/MaoyanPro/rank-list?board_type=total"
    assert total_source.type == "电影"

    resp = plugin.rank_list(board_type="total")
    assert resp.success is True
    assert [media.title for media in resp.data] == ["哪吒之魔童闹海", "长津湖"]


@pytest.mark.parametrize("series_type,expected", [
    ("0", "web-all"), ("1", "web-all"), ("2", "zongyi"), ("9", "web-all"),
])
def test_board_type_resolved_from_series_type(plugin, series_type, expected):
    """剧集不再单独抓榜，除综艺（seriesType=2）外一律归为剧集综合榜。"""
    base = "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData"
    assert plugin._MaoyanPro__board_type_from_url(
        f"{base}?seriesType={series_type}") == expected
    assert plugin._MaoyanPro__board_type_from_url("") == "web-all"


def test_existing_subscription_behaviour_unchanged(plugin, monkeypatch):
    """既有订阅路径的抓取参数顺序与识别调用保持不变。"""
    captured = {}

    def fetch(movie_url, tv_urls, web_movie_url, num=10):
        """记录入参，确保刷新入口传给抓取的参数顺序未变。"""
        captured["args"] = (movie_url, tv_urls, web_movie_url, num)
        return [{"title": "电影"}], [{"title": "剧集"}], [{"title": "电影"}], []

    plugin._type = ["movie", "web-all"]
    plugin.get_data = lambda key: [] if key == "history" else None
    plugin.save_data = lambda key, value: None
    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)
    recognize = Mock(return_value=SimpleNamespace(
        title="电影", year="2026", type=MediaType.MOVIE,
        media_source=SimpleNamespace(value="themoviedb"), media_id="1",
        get_poster_image=lambda: "", overview=""))
    plugin.chain = SimpleNamespace(recognize_media=recognize)
    plugin.downloadchain = SimpleNamespace(
        get_no_exists_info=lambda meta, mediainfo: (False, {}))
    plugin.subscribechain = SimpleNamespace(
        exists=lambda mediainfo, meta: False, add=lambda **kwargs: None)

    plugin._MaoyanPro__refresh_maoyan()

    assert captured["args"][0].endswith("/dashboard-ajax/movie")
    assert captured["args"][2] == ""
    # 抓取条数取订阅条数与推荐条数的较大者（fixture：电影订阅 10 条、推荐 20 条）
    assert captured["args"][3] == 20
    assert recognize.called


def _form_fields(plugin):
    """把表单结构拍平成 {model: props}，返回字段表与默认值。"""
    form, defaults = plugin.get_form()
    fields = {}

    def walk(node):
        if isinstance(node, dict):
            props = node.get("props") or {}
            if "model" in props:
                fields[props["model"]] = props
            for value in node.get("content") or []:
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(form)
    return fields, defaults


def test_form_separates_movie_and_tv_subscription_types(plugin):
    """订阅类型把电影与剧集分开设置，综艺并入剧集下拉，条数字段 0 即代表关闭。"""
    fields, defaults = _form_fields(plugin)

    assert fields["type_movie"]["label"] == "电影订阅类型"
    assert [item["value"] for item in fields["type_movie"]["items"]] == [
        "movie", "web-movie", "total", "year", "global"]
    assert [item["title"] for item in fields["type_movie"]["items"]] == [
        "电影票房榜单", "网络电影榜单", "影史总票房榜", "国内年度票房榜", "全球年度票房榜"]
    assert fields["type_tv"]["label"] == "剧集订阅类型"
    assert [item["value"] for item in fields["type_tv"]["items"]] == ["web-all", "zongyi"]
    # 剧集不再拆电视剧/网剧单榜；综艺并入剧集下拉后不再单列
    assert "type_zongyi" not in fields
    assert [item["title"] for item in fields["type_tv"]["items"]] == [
        "剧集综合榜单（电视剧+网剧）", "综艺热度榜单",
    ]

    # 三组「订阅条数 + 推荐开关」：订阅条数可选 0（不订阅）、推荐为开关（v1.2.2 起）
    for model in ("movie_sub_num", "tv_sub_num", "zongyi_sub_num"):
        assert fields[model]["items"][0] == {"title": "0", "value": 0}
    # 平台榜保留订阅条数；推荐统一为开关，不再有数量下拉
    for model in ("tx_sub_num", "iqy_sub_num", "mg_sub_num", "yk_sub_num"):
        assert model in fields
        assert fields[model]["items"][0]["value"] == 0
    for model in ("movie_rec_enabled", "tv_rec_enabled", "zongyi_rec_enabled",
                  "tx_rec_enabled", "iqy_rec_enabled", "mg_rec_enabled",
                  "yk_rec_enabled"):
        assert model in fields
    assert not any(model.endswith("_rec_num") for model in fields)

    # 旧的合并 type 字段与五个平台/全网开关均已移除
    assert "type" not in fields
    assert not {"all_enabled", "tx_enabled", "iqy_enabled",
                "mg_enabled", "yk_enabled"} & set(fields)

    assert defaults["type_movie"] == ["movie", "web-movie"]
    assert defaults["type_tv"] == ["web-all", "zongyi"]
    assert "type_zongyi" not in defaults
    assert defaults["tx_sub_num"] == 0
    assert defaults["tx_rec_enabled"] is False
    assert defaults["movie_rec_enabled"] is True


def test_legacy_types_merge_into_combined_tv_board():
    """旧配置里独立的综艺、电视剧/网剧单榜会并入剧集综合榜，且不产生重复项。"""
    instance = object.__new__(MaoyanPro)
    instance._MaoyanPro__load_type_config({
        "type_movie": ["movie"],
        "type_tv": ["web-heat", "web-tv"],
        "type_zongyi": ["zongyi"],
    })

    assert instance._type_tv == ["web-all", "zongyi"]
    assert instance._type == ["movie", "web-all", "zongyi"]
    assert instance._type_zongyi == ["zongyi"]

    # 已含综合榜的旧配置不会被重复插入
    instance._MaoyanPro__load_type_config({
        "type_movie": ["movie"],
        "type_tv": ["web-heat", "web-all"],
        "type_zongyi": ["zongyi"],
    })

    assert instance._type_tv == ["web-all", "zongyi"]
    assert instance._type == ["movie", "web-all", "zongyi"]


def test_config_groups_types_and_numbers():
    """电影/剧集（含综艺）两组类型与条数各自独立生效。"""
    instance = object.__new__(MaoyanPro)
    instance._MaoyanPro__load_type_config({
        "type_movie": ["movie"],
        "type_tv": ["web-all"],
        "type_zongyi": [],
        "movie_sub_num": 5, "movie_rec_enabled": True,
        "tv_sub_num": 0, "tv_rec_enabled": True,
        "zongyi_sub_num": 0, "zongyi_rec_enabled": False,
        "tx_sub_num": 3, "iqy_sub_num": 0, "mg_sub_num": 7, "yk_sub_num": 0,
        "tx_rec_enabled": True, "iqy_rec_enabled": False,
        "mg_rec_enabled": True, "yk_rec_enabled": False,
    })

    assert instance._type == ["movie", "web-all"]
    assert (instance._movie_sub_num, instance._movie_rec_enabled) == (5, True)
    # 推荐开关打开按满榜 10 条派生，关闭为 0
    assert (instance._movie_rec_num, instance._tv_rec_num,
            instance._zongyi_rec_num) == (10, 10, 0)
    assert (instance._tx_sub_num, instance._mg_sub_num) == (3, 7)
    assert (instance._iqy_sub_num, instance._yk_sub_num) == (0, 0)
    assert (instance._tx_rec_num, instance._mg_rec_num) == (10, 10)
    assert (instance._iqy_rec_num, instance._yk_rec_num) == (0, 0)


def test_legacy_rec_nums_migrate_to_switches():
    """旧版数值推荐条数 > 0 迁移为推荐开关打开。"""
    instance = object.__new__(MaoyanPro)
    instance._MaoyanPro__load_type_config({
        "movie_rec_num": 30,
        "tv_rec_num": 0,
        "tx_rec_num": 5,
        "yk_rec_num": 0,
    })

    assert instance._movie_rec_enabled is True
    assert instance._tv_rec_enabled is False
    assert instance._tx_rec_enabled is True
    assert instance._yk_rec_enabled is False


def test_legacy_config_migrates_to_split_groups():
    """旧版合并 type 与平台开关的配置可平滑迁移，不必重配。"""
    instance = object.__new__(MaoyanPro)
    instance._MaoyanPro__load_type_config({
        "type": ["movie", "web-all", "zongyi"],
        "num": 3, "all_num": 5, "recommend_num": 30,
        "tx_enabled": True, "tx_num": 7,
        "iqy_enabled": False, "iqy_num": 9,
    })

    assert instance._type == ["movie", "web-all", "zongyi"]
    assert instance._movie_sub_num == 3
    assert instance._tv_sub_num == 5
    assert instance._zongyi_sub_num == 5
    # 旧 recommend_num=30 > 0 → 三组推荐开关都打开，按满榜 10 条派生
    assert instance._movie_rec_num == instance._tv_rec_num == instance._zongyi_rec_num == 10
    assert instance._movie_rec_enabled is True
    assert instance._tx_sub_num == 7
    # 旧开关关闭的平台视为不订阅
    assert instance._iqy_sub_num == 0


def test_refresh_uses_independent_numbers_and_combined_board(plugin, monkeypatch):
    """综合榜走不带 seriesType 的地址；订阅条数与推荐条数互不影响。"""
    captured = {}

    def fetch(movie_url, tv_urls, web_movie_url, num=10):
        captured["tv_urls"] = tv_urls
        captured["movie_url"] = movie_url
        return [], [], [], []

    plugin._type = ["movie", "web-all", "zongyi"]
    plugin._movie_sub_num, plugin._movie_rec_num = 5, 20
    plugin._tv_sub_num, plugin._tv_rec_num = 5, 20
    plugin._zongyi_sub_num, plugin._zongyi_rec_num = 0, 0
    plugin._tx_sub_num = plugin._iqy_sub_num = 0
    plugin._mg_sub_num = plugin._yk_sub_num = 0
    plugin.get_data = lambda key: [] if key == "history" else None
    plugin.save_data = lambda key, value: None
    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)

    plugin._MaoyanPro__refresh_maoyan()

    urls = {item[0]: (item[1], item[2], item[3]) for item in captured["tv_urls"]}
    base = "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData"
    # 剧集只有综合榜一条：不传 seriesType，抓取条数取 max(订阅, 推荐) = 20，订阅上限仍是 5
    combined = next(url for url in urls if "seriesType" not in url)
    assert combined.startswith(base)
    assert urls[combined] == (20, 5, True)
    # 综艺订阅/推荐条数都为 0 → 该榜完全不抓取
    assert not any("seriesType=2" in url for url in urls)


def test_refresh_keeps_platform_boards_out_of_snapshot(plugin, monkeypatch):
    """平台推荐条数为 0 时，平台细分榜只服务订阅，不写入榜单快照。"""
    captured = {}

    def fetch(movie_url, tv_urls, web_movie_url, num=10):
        captured["tv_urls"] = tv_urls
        return [], [], [], []

    plugin._type = ["web-all"]
    plugin._tv_sub_num, plugin._tv_rec_num = 5, 20
    plugin._tx_sub_num = 3
    plugin._iqy_sub_num = plugin._mg_sub_num = plugin._yk_sub_num = 0
    plugin.get_data = lambda key: [] if key == "history" else None
    plugin.save_data = lambda key, value: None
    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)

    plugin._MaoyanPro__refresh_maoyan()

    by_snapshot = {item[3] for item in captured["tv_urls"]}
    assert by_snapshot == {True, False}
    platform = [item for item in captured["tv_urls"] if not item[3]]
    assert len(platform) == 1 and "platformType=3" in platform[0][0]
    assert platform[0][1] == platform[0][2] == 3


def test_refresh_puts_platform_boards_into_snapshot(plugin, monkeypatch):
    """平台推荐条数 > 0 时平台榜写入快照，剧集与综艺两个系列都覆盖。"""
    captured = {}

    def fetch(movie_url, tv_urls, web_movie_url, num=10):
        captured["tv_urls"] = tv_urls
        return [], [], [], []

    plugin._type = ["web-all", "zongyi"]
    plugin._tv_sub_num, plugin._tv_rec_num = 5, 20
    plugin._zongyi_sub_num, plugin._zongyi_rec_num = 10, 0
    plugin._tx_sub_num, plugin._tx_rec_num = 3, 15
    plugin._iqy_rec_num = plugin._mg_rec_num = plugin._yk_rec_num = 0
    plugin.get_data = lambda key: [] if key == "history" else None
    plugin.save_data = lambda key, value: None
    monkeypatch.setattr(plugin, "_MaoyanPro__get_url_info", fetch)

    plugin._MaoyanPro__refresh_maoyan()

    urls = {item[0]: (item[1], item[2], item[3]) for item in captured["tv_urls"]}
    # 剧集综合榜与综艺榜各带一条腾讯平台榜：抓取条数 = max(订阅 3, 推荐 15) = 15，入快照
    tx = [url for url in urls if "platformType=3" in url]
    assert len(tx) == 2
    for url in tx:
        assert urls[url] == (15, 3, True)
    # 其它平台订阅/推荐条数都为 0，完全不抓取
    assert not any(f"platformType={t}" in url for url in urls for t in (2, 7, 1))


def test_board_type_from_url_detects_combined_board(plugin):
    """热度接口不传 seriesType 时为电视剧+网络剧综合榜。"""
    base = "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData"
    assert plugin._MaoyanPro__board_type_from_url(f"{base}?showDate=20261015") == "web-all"
    assert plugin._MaoyanPro__board_type_from_url(f"{base}?seriesType=1&showDate=20261015") == "web-all"


@pytest.mark.parametrize("release_info,year", [
    ("上映首日", 2026), ("今日上映", 2026), ("上映10天", 2025),
    ("2024-12-31上映", 2024), ("2024年12月31日", 2024),
    ("2024-02-30", None), ("敬请期待", None), ("还有10天上映", None),
])
def test_release_year_parsing_unchanged(release_info, year):
    """上映年份解析规则保持既有行为。"""
    assert MaoyanPro._MaoyanPro__release_year(
        release_info, datetime.datetime(2026, 1, 5)) == year


def test_recommend_event_enum_is_host_value(plugin):
    """推荐源注册使用宿主的 RecommendSource 事件。"""
    assert ChainEventType.RecommendSource.value == "recommend.source"