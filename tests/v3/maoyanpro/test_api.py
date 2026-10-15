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
    instance._type = ['movie', 'web-heat', 'web-tv', 'zongyi', 'web-movie']
    instance._enabled = True
    instance._recommend_enabled = True
    instance._api_enabled = True
    instance._num = 10
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
            "platform": "", "show_date": "2026-10-15",
            "url": "https://piaofang.maoyan.com/dashboard-ajax/movie",
            "count": 2, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "生化危机：爆发夜", "board_type": "movie",
                 "type_name": "电影票房", "platformDesc": "院线", "releaseInfo": "上映11天",
                 "board": "电影票房榜单", "show_date": "2026-10-15",
                 "box_office": "161.93万", "heat": "28.1%",
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
            "board": "剧集热度榜单（腾讯视频）", "type": "web-heat", "type_name": "剧集热度",
            "platform": "腾讯视频", "show_date": "2026-10-15",
            "url": ("https://piaofang.maoyan.com/i/api/encrypt/dashboard/"
                    "webHeatData?seriesType=0&platformType=3"),
            "count": 1, "updated_at": "2026-10-15 12:00:00",
            "items": [
                {"rank": 1, "title": "沉默的荣耀", "board_type": "web-heat",
                 "type_name": "剧集热度", "platformDesc": "腾讯视频",
                 "releaseInfo": "上线28天", "board": "剧集热度榜单（腾讯视频）",
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
        "电影票房榜单", "剧集热度榜单（腾讯视频）",
    ]
    first = result.data.boards[0].items[0]
    assert (first.rank, first.title, first.box_office, first.heat) == (
        1, "生化危机：爆发夜", "161.93万", "28.1%",
    )
    assert (first.media_source, first.media_id) == ("themoviedb", "1586199")
    # 上榜但未识别到媒体的条目仍要保留，只是身份为空
    assert result.data.boards[0].items[1].title == "神探之痕迹"
    assert result.data.boards[0].items[1].media_id == ""


def test_rank_endpoint_filters_by_type_board_and_limit(plugin):
    """`/rank` 支持按类型、榜单名筛选与条数限制。"""
    plugin.save_data("snapshot", {
        "updated_at": "2026-10-15 12:00:00", "total": 3, "boards": snapshot_boards(),
    })

    only_heat = plugin.get_rank_snapshot(board_type="web-heat")
    assert len(only_heat.data.boards) == 1
    assert only_heat.data.boards[0].type == "web-heat"

    limited = plugin.get_rank_snapshot(limit=1)
    assert limited.data.total == 2
    assert [len(board.items) for board in limited.data.boards] == [1, 1]

    by_board = plugin.get_rank_snapshot(board="腾讯")
    assert len(by_board.data.boards) == 1
    assert by_board.data.boards[0].platform == "腾讯视频"

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

    assert plugin.rank_list(board_type="web-heat", page=2).data == []
    assert len(plugin.rank_list(board_type="web-heat", page=1).data) == 1


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
    for expected in ["猫眼电影票房", "猫眼电视剧热度", "猫眼网络剧热度", "猫眼综艺热度"]:
        assert expected in names
    for source in event_data.extra_sources[1:]:
        assert source.api_path.startswith("plugin/MaoyanPro/rank-list?board_type=")
    movie_source = next(s for s in event_data.extra_sources if s.name == "猫眼电影票房")
    assert movie_source.type == "电影"


def test_recommend_source_skips_when_disabled(plugin):
    """未开启推荐源或插件停用时都不注册数据源。"""
    plugin._recommend_enabled = False
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources == []

    plugin._recommend_enabled = True
    plugin._enabled = False
    event_data = RecommendSourceEventData()
    plugin.recommend_source(SimpleNamespace(event_data=event_data))
    assert event_data.extra_sources == []


def test_recommend_source_only_includes_selected_boards(plugin):
    """只配置电影时不注册剧集类数据源。"""
    plugin._type = ["movie"]
    event_data = RecommendSourceEventData()

    plugin.recommend_source(SimpleNamespace(event_data=event_data))

    assert [source.name for source in event_data.extra_sources] == ["猫眼电影票房"]


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
    plugin._type = ['movie', 'web-heat']
    plugin._all_enabled = True
    plugin._tx_enabled = plugin._iqy_enabled = False
    plugin._mg_enabled = plugin._yk_enabled = False
    saved = {}
    monkeypatch.setattr(plugin, "get_data", lambda key: [] if key == "history" else None)
    monkeypatch.setattr(plugin, "save_data",
                        lambda key, value: saved.__setitem__(key, value))

    def fetch(movie_url, tv_urls, web_movie_url, num):
        """模拟一次抓取：设置榜单元数据并返回待订阅条目。"""
        plugin._last_boards = [{
            "board": "电影票房榜单", "type": "movie", "type_name": "电影票房",
            "platform": "", "show_date": "2026-10-15", "url": movie_url, "count": 1,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "items": [{"rank": 1, "title": "院线电影", "board_type": "movie",
                       "type_name": "", "platformDesc": "院线", "releaseInfo": "上映3天",
                       "board": "电影票房榜单", "show_date": "2026-10-15",
                       "box_office": "100万", "heat": "20%"}],
        }]
        return [{"title": "院线电影", "releaseInfo": "上映3天"}], []

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
                        lambda *args, **kwargs: ([], []))

    plugin._MaoyanPro__refresh_maoyan()

    assert "snapshot" not in saved
    assert plugin.get_data("snapshot") == previous


def test_rank_metrics_reads_current_maoyan_fields(plugin):
    """量值解析覆盖现网字段：票房用 boxSplitUnit，热度用 currHeatDesc。"""
    metrics = plugin._MaoyanPro__rank_metrics(
        {"boxSplitUnit": {"num": "161.93", "unit": "万"}, "boxRate": "28.1%",
         "showCount": 98919},
        {"releaseInfo": "上映11天", "platformDesc": "院线"},
    )
    assert metrics["box_office"] == "161.93万"
    assert metrics["heat"] == "28.1%"
    assert metrics["release_info"] == "上映11天"

    heat = plugin._MaoyanPro__rank_metrics(
        {"currHeatDesc": "9820", "barValue": "9820"}, {"platformDesc": "腾讯视频"})
    assert heat["heat"] == "9820"
    assert heat["platform"] == "腾讯视频"

    empty = plugin._MaoyanPro__rank_metrics({}, {})
    assert (empty["box_office"], empty["heat"]) == ("", "")


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


@pytest.mark.parametrize("series_type,expected", [
    ("0", "web-heat"), ("1", "web-tv"), ("2", "zongyi"),
])
def test_board_type_resolved_from_series_type(plugin, series_type, expected):
    """热度榜单按 seriesType 区分电视剧、网络剧与综艺。"""
    base = "https://piaofang.maoyan.com/i/api/encrypt/dashboard/webHeatData"
    assert plugin._MaoyanPro__board_type_from_url(
        f"{base}?seriesType={series_type}") == expected
    assert plugin._MaoyanPro__board_type_from_url("") == "web-heat"


def test_existing_subscription_behaviour_unchanged(plugin, monkeypatch):
    """既有订阅路径的抓取参数顺序与识别调用保持不变。"""
    captured = {}

    def fetch(movie_url, tv_urls, web_movie_url, num=10):
        """记录入参，确保刷新入口传给抓取的参数顺序未变。"""
        captured["args"] = (movie_url, tv_urls, web_movie_url, num)
        return [{"title": "电影"}], [{"title": "剧集"}]

    plugin._type = ["movie", "web-heat"]
    plugin._all_enabled = True
    plugin._tx_enabled = plugin._iqy_enabled = False
    plugin._mg_enabled = plugin._yk_enabled = False
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
    assert captured["args"][3] == 10
    assert recognize.called


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