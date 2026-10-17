"""NetflixPro 插件测试：TSV 解析、快照、识别、对外接口与页面。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import schemas
from app.plugins.netflixpro import (
    NetflixPro,
    NetflixRefreshResult,
    _fmt_hours,
    _fmt_runtime,
    _fmt_view_count,
)

# 全球榜 TSV 样本（结构与 Netflix 官方文件一致）
GLOBAL_TSV = (
    "week\tcategory\tweekly_rank\tshow_title\tseason_title\t"
    "weekly_hours_viewed\truntime\tweekly_views\tcumulative_weeks_in_top_10\n"
    "2026-10-04\tFilms (English)\t1\tUNABOMBER\tN/A\t41500000\t1.6667\t24900000\t2\n"
    "2026-10-04\tFilms (English)\t2\tThe Widower\tN/A\t17800000\t1.5333\t11600000\t1\n"
    "2026-10-04\tFilms (Non-English)\t1\tKPop Demon Hunters\tN/A\t31200000\t1.6\t19500000\t5\n"
    "2026-10-04\tTV (English)\t1\tWednesday\tWednesday: Season 2\t62100000\t\t48200000\t3\n"
    "2026-10-04\tTV (Non-English)\t1\tSquid Game\tSquid Game: Season 3\t88000000\t\t61100000\t1\n"
    "2026-09-27\tFilms (English)\t1\tOld Hit\tN/A\t5000000\t2.0\t3000000\t9\n"
)

# 国家/地区榜 TSV 样本
COUNTRIES_TSV = (
    "country_name\tcountry_iso2\tweek\tcategory\tweekly_rank\t"
    "show_title\tseason_title\tcumulative_weeks_in_top_10\n"
    "Hong Kong\tHK\t2026-10-04\tFilms\t1\tKPop Demon Hunters\tN/A\t4\n"
    "Hong Kong\tHK\t2026-10-04\tFilms\t2\tUNABOMBER\tN/A\t1\n"
    "Hong Kong\tHK\t2026-10-04\tTV\t1\tWednesday\tWednesday: Season 2\t2\n"
    "Taiwan\tTW\t2026-10-04\tFilms\t1\tDetective Chinatown 1900\tN/A\t6\n"
    "Taiwan\tTW\t2026-09-27\tFilms\t1\tOld Regional\tN/A\t3\n"
    "Japan\tJP\t2026-10-04\tFilms\t1\tAnime Film\tN/A\t2\n"
)


def _recognize_factory():
    """识别链打桩：按标题返回预置媒体信息。"""

    def recognize(meta, mtype, cache=True):
        mapping = {
            "UNABOMBER": SimpleNamespace(
                media_source=SimpleNamespace(value="themoviedb"),
                media_id="1586199", title="炸弹追缉令", year="2026",
                overview="改编自真实案件", tmdb_info={
                    "release_date": "2026-09-25"},
                get_poster_image=lambda: "https://image.tmdb.org/t/p/w500/u.jpg"),
            "Wednesday": SimpleNamespace(
                media_source=SimpleNamespace(value="themoviedb"),
                media_id="119460", title="星期三", year="2025",
                overview="亚当斯一家的女儿", tmdb_info={
                    "first_air_date": "2025-09-03"},
                get_poster_image=lambda: "https://image.tmdb.org/t/p/w500/w.jpg"),
            "KPop Demon Hunters": SimpleNamespace(
                media_source=SimpleNamespace(value="themoviedb"),
                media_id="1123030", title="K-POP 猎魔女团", year="2025",
                overview="", tmdb_info={},
                get_poster_image=lambda: ""),
        }
        return mapping.get(meta.name)

    return SimpleNamespace(recognize_media=recognize)


@pytest.fixture
def plugin(monkeypatch):
    """绕过宿主初始化，隔离网络与识别链。"""
    instance = object.__new__(NetflixPro)
    instance._data = {}
    instance._regions = ['global', 'hk']
    instance._enabled = True
    instance._recommend_enabled = True
    instance._api_enabled = True

    def fetch_factory(global_text=GLOBAL_TSV, countries_text=COUNTRIES_TSV):
        def fetch(url):
            response = SimpleNamespace(status_code=200,
                                       text=(global_text if "global" in url
                                             else countries_text))
            return SimpleNamespace(get_res=lambda *a, **k: response)
        return fetch

    monkeypatch.setattr(
        "app.plugins.netflixpro.RequestUtils",
        lambda *a, **k: SimpleNamespace(get_res=None))
    instance.chain = _recognize_factory()
    # __fetch_tsv 走实例上的 RequestUtils 引用
    monkeypatch.setattr(instance, "_NetflixPro__fetch_tsv",
                        lambda url: (GLOBAL_TSV if "global" in url
                                     else COUNTRIES_TSV))
    return instance


# ==================== 数值格式化 ====================

def test_format_helpers():
    assert _fmt_view_count(24900000) == "2490 万"
    assert _fmt_view_count(150000000) == "1.5 亿"
    assert _fmt_view_count(200000000) == "2 亿"
    assert _fmt_view_count(0) == ""
    assert _fmt_hours(41500000) == "4150 万小时"
    assert _fmt_hours(150000000) == "1.5 亿小时"
    assert _fmt_hours(0) == ""
    assert _fmt_runtime(1.6667) == "1 小时 40 分"
    assert _fmt_runtime(0.75) == "45 分钟"
    assert _fmt_runtime(2.0) == "2 小时"
    assert _fmt_runtime(0) == ""


# ==================== TSV 解析 ====================

def test_parse_tsv_handles_blank_rows():
    rows = NetflixPro._NetflixPro__parse_tsv("a\tb\n1\t2\n\n\n3\t4\n")
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_parse_global_board_filters_latest_week(plugin):
    """全球榜只保留最新一周，四类目按 kind 分榜。"""
    rows = NetflixPro._NetflixPro__parse_tsv(GLOBAL_TSV)
    week, boards = plugin._NetflixPro__parse_global_board(rows)

    assert week == "2026-10-04"
    assert {b["category"] for b in boards} == {
        "Films (English)", "Films (Non-English)",
        "TV (English)", "TV (Non-English)"}
    # 旧周数据不得混入
    assert all("Old Hit" not in json.dumps(b, ensure_ascii=False)
               for b in boards)

    film_en = next(b for b in boards if b["category"] == "Films (English)")
    assert film_en["region"] == "" and film_en["kind"] == "film"
    assert film_en["count"] == 2
    assert film_en["items"][0]["title"] == "UNABOMBER"
    assert film_en["items"][0]["hours_viewed"] == 41500000
    assert film_en["items"][0]["views"] == 24900000
    assert film_en["items"][0]["runtime"] == 1.6667
    assert film_en["items"][0]["weeks_in_top10"] == 2

    tv_en = next(b for b in boards if b["category"] == "TV (English)")
    assert tv_en["kind"] == "tv"
    assert tv_en["items"][0]["season_title"] == "Wednesday: Season 2"
    # TV 榜没有 runtime，空串转 0
    assert tv_en["items"][0]["runtime"] == 0.0


def test_parse_country_board_filters_region_and_week(plugin):
    """地区榜按 ISO2 过滤，且只取该地区最新一周。"""
    rows = NetflixPro._NetflixPro__parse_tsv(COUNTRIES_TSV)
    week, boards = plugin._NetflixPro__parse_country_board(rows, "hk")

    assert week == "2026-10-04"
    assert {b["kind"] for b in boards} == {"film", "tv"}
    hk_film = next(b for b in boards if b["kind"] == "film")
    assert hk_film["region"] == "hk"
    assert hk_film["region_name"] == "中国香港"
    assert hk_film["board"] == "Netflix 中国香港 · 电影"
    assert [i["title"] for i in hk_film["items"]] == [
        "KPop Demon Hunters", "UNABOMBER"]
    # 台湾条目不得混入香港榜
    assert all("Detective Chinatown" not in json.dumps(b, ensure_ascii=False)
               for b in boards)


def test_parse_country_board_unknown_region(plugin):
    """未配置/无数据的地区返回空榜单，不抛异常。"""
    rows = NetflixPro._NetflixPro__parse_tsv(COUNTRIES_TSV)
    week, boards = plugin._NetflixPro__parse_country_board(rows, "kr")
    assert week == ""
    assert boards == []


# ==================== 抓取与快照 ====================

def test_refresh_netflix_builds_snapshot_with_identity(plugin):
    """端到端：解析 → 识别 → 快照结构完整。"""
    stats = plugin._NetflixPro__refresh_netflix()

    assert stats["board_count"] == 6   # 全球 4 榜 + 香港 2 榜
    assert stats["item_count"] == 8
    assert stats["week"] == "2026-10-04"

    snapshot = plugin.get_data("snapshot")
    assert snapshot["total"] == 8
    assert snapshot["updated_at"]

    film_en = next(b for b in snapshot["boards"]
                   if b["category"] == "Films (English)")
    first = film_en["items"][0]
    # 识别结果合并进快照
    assert first["title_cn"] == "炸弹追缉令"
    assert first["media_id"] == "1586199"
    assert first["poster"] == "https://image.tmdb.org/t/p/w500/u.jpg"
    assert first["release_date"] == "2026-09-25"
    assert first["year"] == "2026"
    # 识别不到标题（识别链返回 None）保持空身份，不炸
    assert film_en["items"][1]["title_cn"] == ""

    # 识别缓存按「类型|标题」持久化
    cache = plugin.get_data("identity_cache")
    assert "film|UNABOMBER" in cache and "tv|Wednesday" in cache


def test_refresh_netflix_reuses_identity_cache(plugin):
    """二轮刷新复用识别缓存，不再请求识别链。"""
    plugin._NetflixPro__refresh_netflix()

    class BoomChain:
        def recognize_media(self, *a, **k):
            raise AssertionError("缓存未命中时不应再识别")

    plugin.chain = BoomChain()
    stats = plugin._NetflixPro__refresh_netflix()

    assert stats["item_count"] == 8
    snapshot = plugin.get_data("snapshot")
    film_en = next(b for b in snapshot["boards"]
                   if b["category"] == "Films (English)")
    # 缓存复用后中文名仍在
    assert film_en["items"][0]["title_cn"] == "炸弹追缉令"


def test_refresh_netflix_handles_download_failure(plugin, monkeypatch):
    """数据下载失败时返回失败统计，不抛异常、不写坏快照。"""

    def boom(url):
        raise RuntimeError("network down")

    monkeypatch.setattr(plugin, "_NetflixPro__fetch_tsv", boom)
    stats = plugin._NetflixPro__refresh_netflix()

    assert stats["board_count"] == 0
    assert "下载失败" in stats["message"]
    assert plugin.get_data("snapshot") is None


def test_refresh_netflix_handles_empty_tsv(plugin, monkeypatch):
    """TSV 为空（格式变更）时安全返回。"""
    monkeypatch.setattr(plugin, "_NetflixPro__fetch_tsv", lambda url: "")
    stats = plugin._NetflixPro__refresh_netflix()
    assert stats["board_count"] == 0
    assert "为空" in stats["message"]


# ==================== 对外接口 ====================

def test_get_api_declares_enveloped_typed_responses(plugin):
    """接口清单须声明响应模型、bear 认证与信封包裹（V3 合同）。"""
    apis = {api["path"]: api for api in plugin.get_api()}

    assert set(apis) == {"/rank", "/rank-list", "/refresh"}
    for path in apis:
        assert apis[path]["response_model"] is not None, f"{path} 未声明响应模型"
        assert apis[path]["auth"] == "bear", f"{path} 未声明 bear 认证"
        assert apis[path]["summary"]
        # 防漂移：必须用宿主信封，否则普通数据客户端判「无效响应」
        metadata = getattr(apis[path]["response_model"],
                           "__pydantic_generic_metadata__", None) or {}
        origin = metadata.get("origin") or getattr(
            apis[path]["response_model"], "__origin__", None)
        assert origin is schemas.Response, f"{path} 未用 schemas.Response 信封"
    assert apis["/refresh"]["methods"] == ["GET", "POST"]


def test_get_rank_snapshot_filters_by_region_and_kind(plugin):
    """/rank 按地区与内容类型过滤，返回信封。"""
    plugin._NetflixPro__refresh_netflix()

    result = plugin.get_rank_snapshot(board_type="global")
    assert result.success is True
    assert len(result.data.boards) == 4
    assert result.data.total == 5

    result = plugin.get_rank_snapshot(board_type="global", kind="film")
    assert len(result.data.boards) == 2

    result = plugin.get_rank_snapshot(board_type="hk", kind="tv")
    assert len(result.data.boards) == 1
    assert result.data.boards[0].region == "hk"

    # 未配置地区返回空榜单而非报错
    result = plugin.get_rank_snapshot(board_type="jp")
    assert result.success is True
    assert result.data.boards == []


def test_get_rank_snapshot_without_data(plugin):
    """无快照时返回空信封。"""
    result = plugin.get_rank_snapshot()
    assert result.success is True
    assert result.data.total == 0
    assert result.data.boards == []


def test_rank_list_prefers_chinese_title(plugin):
    """/rank-list 扁平返回媒体信息，中文片名优先。"""
    plugin._NetflixPro__refresh_netflix()

    result = plugin.rank_list(board_type="global")
    assert result.success is True
    titles = [m.title for m in result.data]
    # 识别到的用中文名，未识别的回落英文原名
    assert "炸弹追缉令" in titles
    assert "The Widower" in titles
    # 电影排在剧集前，各榜内按名次
    film_titles = [m.title for m in result.data if m.type == "电影"]
    assert film_titles[0] == "炸弹追缉令"
    assert film_titles[1] == "K-POP 猎魔女团"
    tv = [m for m in result.data if m.type == "电视剧"]
    assert tv[0].title == "星期三"
    assert tv[0].media_id == "119460"
    assert tv[0].title_year == "星期三 (2025)"

    # kind 缩窄
    result = plugin.rank_list(board_type="global", kind="tv")
    assert all(m.type == "电视剧" for m in result.data)
    assert len(result.data) == 2

    # limit 截断
    result = plugin.rank_list(board_type="global", limit=3)
    assert len(result.data) == 3

    # 未配置地区返回空列表
    result = plugin.rank_list(board_type="kr")
    assert result.data == []


def test_refresh_now_returns_envelope(plugin):
    """/refresh 返回宿主信封，成功与失败都带业务数据。"""
    result = plugin.refresh_now()
    assert result.success is True
    assert isinstance(result.data, NetflixRefreshResult)
    assert result.data.refreshed is True
    assert result.data.board_count == 6
    assert result.data.item_count == 8
    assert result.data.week == "2026-10-04"

    # 未配置地区时业务失败但不抛异常
    plugin._regions = []
    result = plugin.refresh_now()
    assert result.success is False
    assert result.data.refreshed is False
    assert "未配置" in result.data.message


# ==================== 推荐源 ====================

def test_recommend_source_registers_region_kind_sources(plugin):
    """按「地区 × 内容类型」注册推荐源，api_path 带过滤参数。"""
    event = SimpleNamespace(event_data=schemas.RecommendSourceEventData())
    plugin.recommend_source(event)

    sources = event.event_data.extra_sources
    # global + hk 两个地区，各电影/剧集
    assert len(sources) == 4
    by_name = {s.name: s for s in sources}
    assert "Netflix 全球电影" in by_name
    assert "Netflix 全球剧集" in by_name
    assert "Netflix 中国香港电影" in by_name
    assert by_name["Netflix 全球电影"].type == "电影"
    assert by_name["Netflix 全球剧集"].type == "电视剧"
    assert by_name["Netflix 全球电影"].api_path == (
        "plugin/NetflixPro/rank-list?board_type=global&kind=film")


def test_recommend_source_respects_switches(plugin):
    """推荐或接口开关关闭时不注册任何源。"""
    event = SimpleNamespace(event_data=schemas.RecommendSourceEventData())

    plugin._recommend_enabled = False
    plugin.recommend_source(event)
    assert not event.event_data.extra_sources

    plugin._recommend_enabled = True
    plugin._api_enabled = False
    plugin.recommend_source(event)
    assert not event.event_data.extra_sources


def test_recommend_source_appends_to_existing(plugin):
    """已有其它插件的源时叠加而非替换。"""
    event = SimpleNamespace(event_data=schemas.RecommendSourceEventData(
        extra_sources=[schemas.RecommendMediaSource(
            name="其它插件源", api_path="plugin/Other/rank-list", type="电影")]))
    plugin.recommend_source(event)

    names = [s.name for s in event.event_data.extra_sources]
    assert names[0] == "其它插件源"
    assert len(names) == 5


# ==================== 详情页与配置 ====================

def test_get_page_renders_board_sections(plugin):
    """详情页渲染刷新按钮与各分区，条目卡片带识别结果。"""
    plugin._NetflixPro__refresh_netflix()
    nodes = plugin.get_page()

    blob = json.dumps(nodes, ensure_ascii=False)
    assert "实时榜单" in blob
    assert "刷新榜单" in blob
    assert "plugin/NetflixPro/refresh" in blob
    assert "Netflix 全球 · 电影（英语）" in blob
    assert "Netflix 中国香港 · 电影" in blob
    # 榜单周期与快照时间
    assert "2026-10-04" in blob
    # 识别出的中文片名与观看量格式化
    assert "炸弹追缉令" in blob
    assert "4150 万小时" in blob
    assert "2490 万次观看" in blob
    assert "1 小时 40 分" in blob
    assert "在榜 2 周" in blob
    # 季标题展示
    assert "Wednesday: Season 2" in blob


def test_get_page_without_snapshot(plugin):
    """无快照时给出刷新提示，不炸。"""
    nodes = plugin.get_page()
    blob = json.dumps(nodes, ensure_ascii=False)
    assert "暂无榜单快照" in blob
    assert "刷新榜单" in blob


def test_get_form_defaults_and_controls(plugin):
    """设置页控件齐全，默认值符合预期。"""
    form_items, defaults = plugin.get_form()

    blob = json.dumps(form_items, ensure_ascii=False)
    assert "VForm" in blob
    assert "启用插件" in blob
    assert "立即运行一次" in blob
    assert "执行周期" in blob
    assert "抓取地区" in blob
    assert "开放榜单接口" in blob
    assert "推荐页数据源" in blob
    assert "清理插件数据" in blob
    # 地区选项覆盖全球与五个常用地区
    for name in ("全球", "中国香港", "中国台湾", "日本", "韩国", "新加坡"):
        assert name in blob

    assert defaults["enabled"] is True
    assert defaults["regions"] == ["global", "hk"]
    assert defaults["api_enabled"] is True
    assert defaults["recommend_enabled"] is True


def test_plugin_version_matches_package_metadata():
    """plugin_version 必须与 package.v3.json 登记版本一致。"""
    package = json.loads(
        Path("package.v3.json").read_text(encoding="utf-8"))
    meta = package.get("NetflixPro") or {}
    assert meta, "package.v3.json 未登记 NetflixPro"
    assert meta.get("version") == NetflixPro.plugin_version
