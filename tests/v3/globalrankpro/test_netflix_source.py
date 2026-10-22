"""NetflixSource 插件测试：TSV 解析、快照、识别、对外接口与页面。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import schemas
from app.plugins import _PluginBase
from app.plugins.globalrankpro.netflix import (
    NetflixSource,
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
    """识别链打桩：按标题返回预置媒体信息。

    宿主 ``MetaInfo`` 会对全大写英文标题做词形归一（``UNABOMBER`` →
    ``Unabomber``），所以这里按大小写不敏感查表，避免与宿主版本绑死。
    """
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
    lowered = {k.lower(): v for k, v in mapping.items()}

    def recognize(meta, mtype, cache=True):
        return lowered.get(str(getattr(meta, "name", "") or "").lower())

    return SimpleNamespace(recognize_media=recognize)


@pytest.fixture
def plugin(monkeypatch):
    """绕过宿主初始化，隔离网络与识别链。"""
    host = _PluginBase()
    instance = NetflixSource(host)
    instance.apply_config({'enabled': True, 'regions': ['global', 'hk']})

    def fetch_factory(global_text=GLOBAL_TSV, countries_text=COUNTRIES_TSV):
        def fetch(url):
            response = SimpleNamespace(status_code=200,
                                       text=(global_text if "global" in url
                                             else countries_text))
            return SimpleNamespace(get_res=lambda *a, **k: response)
        return fetch

    monkeypatch.setattr(
        "app.plugins.globalrankpro.netflix.RequestUtils",
        lambda *a, **k: SimpleNamespace(get_res=None))
    instance.chain = _recognize_factory()
    # __fetch_tsv 走实例上的 RequestUtils 引用
    monkeypatch.setattr(instance, "_NetflixSource__fetch_tsv",
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
    rows = NetflixSource._NetflixSource__parse_tsv("a\tb\n1\t2\n\n\n3\t4\n")
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_parse_global_board_filters_latest_week(plugin):
    """全球榜只保留最新一周，四类目按 kind 分榜。"""
    rows = NetflixSource._NetflixSource__parse_tsv(GLOBAL_TSV)
    week, boards = plugin._NetflixSource__parse_global_board(rows)

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
    rows = NetflixSource._NetflixSource__parse_tsv(COUNTRIES_TSV)
    week, boards = plugin._NetflixSource__parse_country_board(rows, "hk")

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
    rows = NetflixSource._NetflixSource__parse_tsv(COUNTRIES_TSV)
    week, boards = plugin._NetflixSource__parse_country_board(rows, "kr")
    assert week == ""
    assert boards == []


# ==================== 抓取与快照 ====================

def test_refresh_netflix_builds_snapshot_with_identity(plugin):
    """端到端：解析 → 识别 → 快照结构完整。"""
    stats = plugin._NetflixSource__refresh_netflix()

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
    plugin._NetflixSource__refresh_netflix()

    class BoomChain:
        def recognize_media(self, *a, **k):
            raise AssertionError("缓存未命中时不应再识别")

    plugin.chain = BoomChain()
    stats = plugin._NetflixSource__refresh_netflix()

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

    monkeypatch.setattr(plugin, "_NetflixSource__fetch_tsv", boom)
    stats = plugin._NetflixSource__refresh_netflix()

    assert stats["board_count"] == 0
    assert "下载失败" in stats["message"]
    assert plugin.get_data("snapshot") is None


def test_refresh_netflix_handles_empty_tsv(plugin, monkeypatch):
    """TSV 为空（格式变更）时安全返回。"""
    monkeypatch.setattr(plugin, "_NetflixSource__fetch_tsv", lambda url: "")
    stats = plugin._NetflixSource__refresh_netflix()
    assert stats["board_count"] == 0
    assert "为空" in stats["message"]


# ==================== 对外接口 ====================

def test_rank_boards_filters_by_region_and_kind(plugin):
    """统一榜单转换按地区与内容类型过滤（主类据此聚合 /rank）。"""
    plugin._NetflixSource__refresh_netflix()

    boards = plugin.rank_boards(board_type="global")
    assert len(boards) == 4
    assert sum(b.count for b in boards) == 5
    assert all(b.source == "netflix" for b in boards)
    # 快照里全球榜 region 是空串，对外必须还原成 global，调用方才能回传过滤
    assert all(b.board_type == "global" for b in boards)

    boards = plugin.rank_boards(board_type="global", kind="film")
    assert len(boards) == 2

    boards = plugin.rank_boards(board_type="hk", kind="tv")
    assert len(boards) == 1
    assert boards[0].region == "hk"

    # 未配置地区返回空榜单而非报错
    assert plugin.rank_boards(board_type="jp") == []


def test_rank_boards_without_data(plugin):
    """无快照时返回空列表。"""
    assert plugin.rank_boards() == []
    assert plugin.boards() == []
    assert plugin.snapshot_meta() == {}


def test_rank_list_prefers_chinese_title(plugin):
    """/rank-list 扁平返回媒体信息，中文片名优先。"""
    plugin._NetflixSource__refresh_netflix()

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



def test_fetch_returns_summary(plugin):
    """fetch() 返回抓取摘要（主类据此汇总 /refresh 结果）。"""
    result = plugin.fetch()
    assert result["board_count"] == 6
    assert result["item_count"] == 8
    assert result["week"] == "2026-10-04"


# ==================== 推荐源 ====================

def test_recommend_sources_register_region_kind(plugin):
    """按「地区 × 内容类型」注册推荐源，api_path 带统一接口的 source 参数。"""
    sources = plugin.recommend_sources()
    # global + hk 两个地区，各电影/剧集
    assert len(sources) == 4
    by_name = {s.name: s for s in sources}
    assert "Netflix 全球电影" in by_name
    assert "Netflix 全球剧集" in by_name
    assert "Netflix 中国香港电影" in by_name
    assert by_name["Netflix 全球电影"].type == "电影"
    assert by_name["Netflix 全球剧集"].type == "电视剧"
    assert by_name["Netflix 全球电影"].api_path == (
        "plugin/GlobalRankPro/rank-list?source=netflix&board_type=global&kind=film")

def test_fetch_tsv_decodes_utf8_not_sniffed(monkeypatch):
    """抓取必须按 UTF-8 解码，不能依赖 requests 的编码嗅探。

    线上实录：Tudum TSV 是 UTF-8 但响应不带 charset，``response.text`` 被嗅探成
    latin-1 系后，弯引号/破折号全变成 ``â\x80\x99``/``â\x80\x94`` 乱码，
    TMDB 识别随之失败。这里用带 ``content`` 字节与错误 ``text`` 的假响应复现。
    """
    raw = ("week\tcategory\tweekly_rank\tshow_title\n"
           "2026-10-04\tFilms (English)\t1\tSchumacher \u201994 \u2014 Legend\n"
           "2026-10-04\tTV (Non-English)\t1\tSTEEL BALL RUN JoJo\u2019s Adventure\n"
           "2026-10-04\tFilms (Non-English)\t1\t鬼灭之刃：无限城篇\n")
    content = raw.encode("utf-8")
    # 模拟 requests 嗅探失败后的 response.text：UTF-8 字节按 cp1252 解读
    sniffed = content.decode("cp1252", errors="replace")
    response = SimpleNamespace(status_code=200, text=sniffed, content=content)

    monkeypatch.setattr(
        "app.plugins.globalrankpro.netflix.RequestUtils",
        lambda *a, **k: SimpleNamespace(get_res=lambda *a, **k: response))

    instance = object.__new__(NetflixSource)
    text = instance._NetflixSource__fetch_tsv("https://example.test/global.tsv")

    assert "Schumacher \u201994 \u2014 Legend" in text
    assert "JoJo\u2019s" in text
    assert "鬼灭之刃：无限城篇" in text
    assert "\u00e2\u0080\u0099" not in text  # 不出现 cp1252 误读痕迹


def test_fetch_tsv_falls_back_to_text_without_content(monkeypatch):
    """响应没有 content 字节时退回 text，行为与旧版一致。"""
    response = SimpleNamespace(status_code=200, text="a\tb\n1\t2\n", content=None)
    monkeypatch.setattr(
        "app.plugins.globalrankpro.netflix.RequestUtils",
        lambda *a, **k: SimpleNamespace(get_res=lambda *a, **k: response))

    instance = object.__new__(NetflixSource)
    text = instance._NetflixSource__fetch_tsv("https://example.test/global.tsv")
    assert text == "a\tb\n1\t2\n"


def test_fetch_tsv_accepts_str_content(monkeypatch):
    """某些响应对象的 content 已是 str 时原样返回。"""
    response = SimpleNamespace(status_code=200, text="ignored", content="x\ty\n")
    monkeypatch.setattr(
        "app.plugins.globalrankpro.netflix.RequestUtils",
        lambda *a, **k: SimpleNamespace(get_res=lambda *a, **k: response))

    instance = object.__new__(NetflixSource)
    text = instance._NetflixSource__fetch_tsv("https://example.test/global.tsv")
    assert text == "x\ty\n"
