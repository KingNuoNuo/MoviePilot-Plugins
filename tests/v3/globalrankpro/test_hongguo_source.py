"""HongguoSource 插件测试：SSR 内联数据解析、翻页、快照、对外接口与页面。

红果官网没有公开 API，数据来自榜单页 SSR 内联的 JSON（挂在
``<script data-fn-name="r" data-fn-args='...'>`` 上）。测试重点是「页面结构
变了不能炸」：解析失败要返回空而不是抛异常，翻页要有上限，海报后缀要能被
正确升级。
"""

import html as html_lib
import json
from types import SimpleNamespace

import pytest

from app.plugins import _PluginBase
from app.plugins.globalrankpro.hongguo import (
    HongguoSource,
    _extract_payload,
    _join_tags,
    _upgrade_cover,
)
from app.plugins.globalrankpro.page import build_page

VID_TEMPLATE = "v0200fg{}0000abcdefg"

# 单页 20 条；官网每页固定 20 条，这里用 3 条样本代替，够验证解析与翻页
PAGE_ROWS = [
    {
        "rank": 1,
        "seriesId": "7501234567890123456",
        "id": "7501234567890123456",
        "title": "开局签到十万亿功德",
        "cover": "https://p9-aio.ecombdimg.com/obj/tos-cn-i-xxxx/abc"
                 "~tplv-shrink:640:0.image",
        "href": "/detail?series_id=7501234567890123456",
        "playHref": "/player/7501234567890123456",
        "heatText": "热度 2145万",
        "scoreText": "评分9.0",
        "favoriteText": "55.6万收藏",
        "likeText": "137.2万点赞",
        "statusTags": ["新剧", "爆剧"],
        "tags": ["玄幻", "逆袭", "修真"],
        "description": "一朝穿越，他开局签到十万亿功德，从此横推诸天万界。",
        "episodeVids": [VID_TEMPLATE.format(i) for i in range(1, 6)],
    },
    {
        "rank": 2,
        "seriesId": "7501234567890123457",
        "id": "7501234567890123457",
        "title": "闪婚老公是首富",
        "cover": "https://p9-aio.ecombdimg.com/obj/tos-cn-i-yyyy/def"
                 "~tplv-shrink:640:0.image",
        "href": "/detail?series_id=7501234567890123457",
        "playHref": "/player/7501234567890123457",
        "heatText": "热度 1876万",
        "scoreText": "评分8.7",
        "favoriteText": "31.2万收藏",
        "likeText": "90.4万点赞",
        "statusTags": [],
        "tags": ["都市", "甜宠"],
        "description": "被逼相亲，她随手拉了个路人领证。",
        # 只有 1 集 → 试看直链只有 1 条
        "episodeVids": [VID_TEMPLATE.format(11)],
    },
    {
        "rank": 3,
        "seriesId": "7501234567890123458",
        "id": "7501234567890123458",
        "title": "我在天庭收废品",
        # 无 tplv 后缀的裸链接：不能改也不能炸
        "cover": "https://p9-aio.ecombdimg.com/obj/tos-cn-i-zzzz/ghi",
        "href": "/detail?series_id=7501234567890123458",
        "playHref": "/player/7501234567890123458",
        "heatText": "热度 1200万",
        "scoreText": "",
        "favoriteText": "",
        "likeText": "",
        "statusTags": ["新剧"],
        "tags": [],
        "description": "",
        "episodeVids": [],
    },
]


def make_merge_page_html(rows, has_next=False):
    """拼另一种 SSR 输出的页面：榜单被嵌进 mergeLoaderData 的 routerDataFnArgs。

    ⚠️ 官网同一个 URL 会随机返回这种结构（另一种见 ``make_page_html``），
    而且是「JSON 字符串里再嵌一层 JSON」——只按固定下标取会时灵时不灵。
    """
    inner = json.dumps({
        "isSuccess": True,
        "rankList": rows,
        "pagination": {"pageNum": 1, "totalPages": 5,
                       "nextUrl": "/rank/hot-drama?page=2" if has_next else ""},
    }, ensure_ascii=False)
    args = ["rank_hot-drama/page", [
        {"key": "content", "routerDataFnName": "p",
         "routerDataFnArgs": [inner]},
    ]]
    raw = html_lib.escape(json.dumps(args, ensure_ascii=False), quote=True)
    return ('<html><body><div id="root"></div>'
            '<script async="" data-script-src="modern-run-window-fn" '
            'data-fn-name="mergeLoaderData" data-fn-args="' + raw + '"></script>'
            '</body></html>')


def make_page_html(rows, has_next=False, success=True):
    """拼一份带内联数据的榜单页 HTML（与官网 SSR 输出结构一致）。"""
    args = [
        {"url": "/rank/hot-drama"},
        {"ssr": True},
        {
            "isSuccess": success,
            "rankList": rows,
            "pagination": {
                "currentPage": 1,
                "totalPages": 5,
                # 官网用 nextUrl 是否存在表示还有没有下一页
                "nextUrl": "/rank/hot-drama?page=2" if has_next else "",
            },
        },
    ]
    # 属性值是 HTML 转义过的 JSON 数组
    raw = html_lib.escape(json.dumps(args, ensure_ascii=False), quote=True)
    return (
        "<!DOCTYPE html><html><head><title>红果热播榜</title></head><body>"
        "<div id='root'></div>"
        "<script data-fn-name=\"r\" data-fn-args='" + raw + "'></script>"
        "<script>console.log('noise')</script>"
        "</body></html>"
    )


@pytest.fixture
def plugin(monkeypatch):
    """绕过宿主初始化，隔离网络。"""
    host = _PluginBase()
    instance = HongguoSource(host)
    instance.apply_config({
        'enabled': True,
        'boards': ['hot'],
        'limit': 20,
    })
    # 翻页间隔在测试里不需要，置零避免拖慢用例
    monkeypatch.setattr(
        "app.plugins.globalrankpro.hongguo._PAGE_INTERVAL", 0)
    return instance


def install_pages(monkeypatch, instance, pages):
    """按 URL 顺序喂页面：pages 是 {关键字: HTML} 的有序序列。"""
    calls = []

    def fetch_html(url):
        calls.append(url)
        for keyword, payload in pages:
            if keyword in url:
                return payload
        raise AssertionError("未预期的请求：{0}".format(url))

    monkeypatch.setattr(instance, "_HongguoSource__fetch_html", fetch_html)
    return calls


# ==================== 纯函数 ====================

def test_upgrade_cover_raises_width():
    """640 缩略图升级到 1080（源图上限 896×1280，再大不会放大）。"""
    assert _upgrade_cover("https://x/y~tplv-shrink:640:0.image") == \
        "https://x/y~tplv-shrink:1080:0.image"
    assert _upgrade_cover("https://x/y~tplv-shrink:640:0.webp") == \
        "https://x/y~tplv-shrink:1080:0.webp"
    # 已经是原图（noop 即 896×1280 上限）就不用再改
    assert _upgrade_cover("https://x/y~tplv-noop.image") == \
        "https://x/y~tplv-noop.image"
    # 无后缀 / 空值一律原样返回，不引入非法 URL
    assert _upgrade_cover("https://x/y") == "https://x/y"
    assert _upgrade_cover("") == ""
    assert _upgrade_cover(None) == ""


def test_join_tags_flattens_lists():
    assert _join_tags(["玄幻", "逆袭"]) == "玄幻,逆袭"
    assert _join_tags([]) == ""
    assert _join_tags(None) == ""
    assert _join_tags("都市") == "都市"


def test_extract_payload_picks_third_argument():
    """内联参数是 JSON 数组，第 3 项才是榜单本体。"""
    payload = _extract_payload(make_page_html(PAGE_ROWS))
    assert payload is not None
    assert payload["isSuccess"] is True
    assert len(payload["rankList"]) == 3


def test_extract_payload_handles_double_quoted_attribute():
    """属性值也可能是双引号包裹（内部再用 &quot; 转义）。"""
    args = [{}, {}, {"isSuccess": True, "rankList": []}]
    raw = html_lib.escape(json.dumps(args), quote=True)
    html_text = '<script data-fn-name="r" data-fn-args="' + raw + '"></script>'
    assert _extract_payload(html_text)["isSuccess"] is True


def test_extract_payload_handles_merge_loader_data_variant():
    """另一种 SSR 输出：榜单嵌在 mergeLoaderData 的 routerDataFnArgs 字符串里。

    官网同一个 URL 随机返回两种结构，只认一种会表现为「部分榜单抓不到」。
    """
    payload = _extract_payload(make_merge_page_html(PAGE_ROWS))
    assert payload is not None
    assert payload["isSuccess"] is True
    assert len(payload["rankList"]) == 3
    # 两种变体的字段名一致，下游无需分支
    assert payload["pagination"]["nextUrl"] == ""


def test_extract_payload_picks_whichever_variant_is_present():
    """两种变体混在一个页面里时，取到先出现的那份榜单数据。"""
    both = make_merge_page_html(PAGE_ROWS) + make_page_html(PAGE_ROWS[:1])
    assert len(_extract_payload(both)["rankList"]) == 3
    swapped = make_page_html(PAGE_ROWS[:1]) + make_merge_page_html(PAGE_ROWS)
    assert len(_extract_payload(swapped)["rankList"]) == 1


def test_extract_payload_returns_none_on_garbage():
    """结构变了（无标签 / 非 JSON / 参数不足 3 项）返回 None，不抛异常。"""
    assert _extract_payload("") is None
    assert _extract_payload("<html><body>没有榜单数据</body></html>") is None
    assert _extract_payload(
        '<script data-fn-name="r" data-fn-args=\'not json\'></script>') is None
    assert _extract_payload(
        '<script data-fn-name="r" data-fn-args=\'[]\'></script>') is None


# ==================== 条目映射 ====================

def test_to_item_maps_fields_and_preview(plugin):
    item = plugin._HongguoSource__to_item(PAGE_ROWS[0])

    assert item["rank"] == 1
    assert item["title"] == "开局签到十万亿功德"
    # 标题本身是中文，title_cn 同源，保证跨源展示逻辑一致
    assert item["title_cn"] == item["title"]
    assert item["series_id"] == "7501234567890123456"
    assert item["episode_cnt"] == 5
    assert item["heat"] == "热度 2145万"
    assert item["score"] == "评分9.0"
    assert item["favorite"] == "55.6万收藏"
    assert item["likes"] == "137.2万点赞"
    assert item["tags"] == "玄幻,逆袭,修真"
    assert item["status_tags"] == "新剧,爆剧"
    assert item["poster"].endswith("~tplv-shrink:1080:0.image")
    assert item["overview"].startswith("一朝穿越")
    # 短剧不在 TMDB / 豆瓣里，不做识别也不给身份
    assert item["media_source"] == "" and item["media_id"] == ""


def test_preview_url_covers_only_first_three_episodes(plugin):
    """官网只公开前 3 集的 360p 直链，第 4 集起 404，不能多拼。"""
    first = plugin._HongguoSource__to_item(PAGE_ROWS[0])
    urls = first["preview_url"].split(",")
    assert len(urls) == 3
    assert urls[0] == ("https://hongguoduanju.com/video/public/"
                       + VID_TEMPLATE.format(1) + "/360p.mp4")

    # 只有 1 集的条目 → 只拼 1 条
    second = plugin._HongguoSource__to_item(PAGE_ROWS[1])
    assert second["preview_url"].count("http") == 1

    # 没有 vids 的条目 → 空串，不拼出残缺 URL
    third = plugin._HongguoSource__to_item(PAGE_ROWS[2])
    assert third["preview_url"] == ""
    assert third["episode_cnt"] == 0


# ==================== 抓取与快照 ====================

def test_refresh_builds_snapshot(plugin, monkeypatch):
    """端到端：解析 → 落快照，条数与榜单元数据完整。"""
    install_pages(monkeypatch, plugin, [("hot-drama",
                                        make_page_html(PAGE_ROWS))])

    stats = plugin._HongguoSource__refresh_hongguo()

    assert stats["board_count"] == 1
    assert stats["item_count"] == 3

    snapshot = plugin.get_data("snapshot")
    assert snapshot["total"] == 3
    assert snapshot["updated_at"]
    board = snapshot["boards"][0]
    assert board["board"] == "红果热播榜"
    assert board["type"] == "hot"
    assert board["type_name"] == "热播榜"
    assert board["url"] == "https://hongguoduanju.com/rank/hot-drama"
    assert board["updated_at"] == snapshot["updated_at"]
    assert [i["title"] for i in board["items"]] == [
        "开局签到十万亿功德", "闪婚老公是首富", "我在天庭收废品"]


def test_refresh_paginates_until_limit(plugin, monkeypatch):
    """limit 大于单页条数时继续翻页，够了就停。"""
    plugin.apply_config({'enabled': True, 'boards': ['hot'], 'limit': 40})
    calls = install_pages(monkeypatch, plugin, [
        ("page=2", make_page_html(PAGE_ROWS[:2])),
        ("hot-drama", make_page_html(PAGE_ROWS, has_next=True)),
    ])

    stats = plugin._HongguoSource__refresh_hongguo()

    assert stats["item_count"] == 5          # 3 + 2
    assert len(calls) == 2                   # 翻到第 2 页后达到 40 上限就停


def test_refresh_stops_when_no_next_url(plugin, monkeypatch):
    """没有 nextUrl 时立即停止，不会按 totalPages 空转到上限页。"""
    calls = install_pages(monkeypatch, plugin, [
        ("hot-drama", make_page_html(PAGE_ROWS, has_next=False)),
    ])
    plugin.apply_config({'enabled': True, 'boards': ['hot'], 'limit': 100})

    plugin._HongguoSource__refresh_hongguo()
    assert len(calls) == 1


def test_refresh_reports_failure_without_raising(plugin, monkeypatch):
    """页面结构变化 / 网络异常 → 摘要带 message，不抛异常到调度线程。"""
    monkeypatch.setattr(plugin, "_HongguoSource__fetch_html",
                        lambda url: (_ for _ in ()).throw(
                            RuntimeError("连接超时")))

    stats = plugin._HongguoSource__refresh_hongguo()
    assert stats["board_count"] == 0
    assert "抓取失败" in stats["message"]
    # 失败不覆盖已有快照
    assert plugin.get_data("snapshot") is None


def test_refresh_works_with_merge_loader_data_variant(plugin, monkeypatch):
    """抓到的是 mergeLoaderData 变体时同样能落快照（不能只有一种结构能抓）。"""
    install_pages(monkeypatch, plugin, [
        ("hot-drama", make_merge_page_html(PAGE_ROWS)),
    ])

    stats = plugin._HongguoSource__refresh_hongguo()
    assert stats["board_count"] == 1
    assert stats["item_count"] == 3


def test_refresh_skips_unparsed_board(plugin, monkeypatch):
    """某一页 isSuccess=false → 该榜跳过，不影响其它榜。"""
    plugin.apply_config({'enabled': True, 'boards': ['hot', 'ai'],
                         'limit': 20})
    install_pages(monkeypatch, plugin, [
        ("hot-drama", make_page_html(PAGE_ROWS)),
        ("hot-ai-drama", make_page_html([], success=False)),
    ])

    stats = plugin._HongguoSource__refresh_hongguo()
    assert stats["board_count"] == 1
    assert [b["type"] for b in plugin.get_data("snapshot")["boards"]] == ["hot"]


# ==================== 配置 ====================

def test_apply_config_filters_unknown_boards(plugin):
    """设置页传来的非法榜单代码被丢弃，只保留认识的那几个。"""
    plugin.apply_config({'boards': ['hot', 'not-a-board'], 'limit': 30})
    assert plugin._boards == ['hot']
    assert plugin._limit == 30

    # 全非法 → 保留上一次的有效配置，不会被清空成抓不到东西
    plugin.apply_config({'boards': ['nope']})
    assert plugin._boards == ['hot']

    # limit 非法回退默认 20
    plugin.apply_config({'limit': 'abc'})
    assert plugin._limit == 20


def test_default_boards_cover_all_four_routes():
    """新实例默认抓全部四个官方热播榜。"""
    instance = HongguoSource(_PluginBase())
    assert instance._boards == ['hot', 'real', 'comic', 'ai']
    assert set(instance._BOARD_ROUTES.values()) == {
        'hot-drama', 'hot-real-drama', 'hot-comic-drama', 'hot-ai-drama'}
    assert instance._limit == 20


# ==================== 统一模型与接口 ====================

def test_rank_boards_filters_by_code_and_limit(plugin, monkeypatch):
    install_pages(monkeypatch, plugin, [("hot-drama",
                                        make_page_html(PAGE_ROWS))])
    plugin.fetch()

    boards = plugin.rank_boards()
    assert len(boards) == 1
    assert boards[0].source == "hongguo"
    assert boards[0].board_type == "hot"
    assert boards[0].count == 3
    assert boards[0].items[0].episode_cnt == 5
    assert boards[0].items[0].score == "评分9.0"

    # 按榜单代码过滤：不存在的代码返回空
    assert plugin.rank_boards(board_type="hot") != []
    assert plugin.rank_boards(board_type="nope") == []

    # limit 截断后 count 跟着变（存储里的 count 是抓取时原始条数）
    limited = plugin.rank_boards(limit=2)
    assert limited[0].count == 2
    assert len(limited[0].items) == 2


def test_rank_boards_tolerates_legacy_snapshot(plugin):
    """旧快照缺字段时补默认值，不能让 /rank 整体 500。"""
    plugin.save_data("snapshot", {"boards": [
        {"type": "hot", "items": [{"title": "老数据"}]},
        "非法条目",
    ]})
    boards = plugin.rank_boards()
    assert len(boards) == 1
    item = boards[0].items[0]
    assert item.title == "老数据"
    assert item.episode_cnt == 0 and item.preview_url == ""


def test_rank_list_returns_flat_media_info(plugin, monkeypatch):
    install_pages(monkeypatch, plugin, [("hot-drama",
                                        make_page_html(PAGE_ROWS))])
    plugin.fetch()
    # 这里验的是统一模型与排序，海报转发另有专门用例；先让宿主地址不可得
    monkeypatch.setattr("app.plugins.globalrankpro.hongguo.settings",
                        _proxy_settings(domain=""))

    result = plugin.rank_list()
    assert result.success is True
    assert len(result.data) == 3
    first = result.data[0]
    assert first.title == "开局签到十万亿功德"
    assert first.type == "电视剧"
    assert first.poster_path.endswith("~tplv-shrink:1080:0.image")
    assert first.overview.startswith("一朝穿越")
    # 没有媒体身份 → 宿主不会据此订阅，但列表可正常展示
    assert first.media_source is None and first.media_id is None

    # 按榜单代码过滤
    assert len(plugin.rank_list(board_type="hot").data) == 3
    assert plugin.rank_list(board_type="nope").data == []
    assert len(plugin.rank_list(limit=1).data) == 1


def test_recommend_sources_registers_each_board(plugin):
    plugin.apply_config({'boards': ['hot', 'comic']})
    sources = plugin.recommend_sources()

    assert [s.name for s in sources] == ["红果热播榜", "红果漫剧热播榜"]
    assert sources[0].type == "电视剧"
    assert "source=hongguo&board_type=hot" in sources[0].api_path
    assert "source=hongguo&board_type=comic" in sources[1].api_path


# ==================== 详情页 ====================

def test_detail_page_shows_status_only_not_board_items(plugin, monkeypatch):
    """详情页只报状态（榜数/条数/更新时间 + 刷新），条目一律走接口不铺开。

    红果在 TMDB / 豆瓣里没有身份、也没有订阅入口，但它和猫眼、Netflix 一样：
    抓到的条目由 /rank、/rank-list 供给推荐页与 App，页面不为它单开铺榜区域。
    """
    install_pages(monkeypatch, plugin, [("hot-drama",
                                        make_page_html(PAGE_ROWS))])
    plugin.fetch()

    host = SimpleNamespace(sources={"hongguo": plugin},
                           serialize_config=lambda: {})
    dumped = json.dumps(build_page(host), ensure_ascii=False)

    # 概览里仍有红果这一行：告诉用户抓到了多少
    assert "红果短剧榜单" in dumped
    assert "1 个榜 · 3 条" in dumped
    # 条目本身不进页面：标题、集数、试看直链都不出现
    assert "开局签到十万亿功德" not in dumped
    assert "/video/public/" not in dumped
    assert "VExpansionPanels" not in dumped


def test_detail_page_marks_disabled_source(plugin):
    """红果未启用时概览行标「未启用」，页面不因此缺块。"""
    plugin.apply_config({'enabled': False})
    host = SimpleNamespace(sources={"hongguo": plugin},
                           serialize_config=lambda: {})
    dumped = json.dumps(build_page(host), ensure_ascii=False)

    assert "红果短剧榜单" in dumped
    assert "未启用" in dumped


# ==================== 海报转发 ====================

POSTER_CDN = ("https://p6-novel.byteimg.com/novel-pic/"
              "abc~tplv-shrink:1080:0.image")


def _proxy_settings(domain="https://mp.example.com"):
    return SimpleNamespace(APP_DOMAIN=domain, API_V1_STR="/api/v1",
                           PROXY=None, TZ="Asia/Shanghai")


def test_poster_url_uses_host_proxy(plugin, monkeypatch):
    """开启转发后海报地址指向插件 /poster，原图整体 urlencode 进 u 参数。"""
    monkeypatch.setattr("app.plugins.globalrankpro.hongguo.settings",
                        _proxy_settings())

    url = plugin.poster_url_for(POSTER_CDN)

    assert url.startswith("https://mp.example.com/api/v1/plugin/")
    assert "/poster?u=" in url
    assert "~tplv-shrink%3A1080%3A0.image" in url
    assert "https://p6-novel" not in url



def test_poster_url_without_any_host_falls_back_to_cdn(plugin, monkeypatch):
    """APP_DOMAIN 没配、也没有请求地址时退回原图，不能拼出半截地址。"""
    monkeypatch.setattr("app.plugins.globalrankpro.hongguo.settings",
                        _proxy_settings(domain=""))

    assert plugin.poster_url_for(POSTER_CDN) == POSTER_CDN


def test_poster_response_rejects_untrusted_urls(plugin):
    """匿名接口只放行字节图床，其余一律 403（避免被当成开放代理）。"""
    from fastapi import HTTPException

    for bad in (
        "https://evil.example.com/a.jpg",
        "https://p6-novel.byteimg.com/other/x.jpg",
        "https://p9-aio.ecombdimg.com/other/x.jpg",
        "file:///etc/passwd",
        "https://byteimg.com.evil.example.com/novel-pic/x.jpg",
    ):
        with pytest.raises(HTTPException) as err:
            plugin.poster_response(bad)
        assert err.value.status_code == 403


def test_poster_response_streams_image(plugin, monkeypatch):
    """正常地址代取后原样回传字节，并带长期缓存头。"""
    fake = SimpleNamespace(
        status_code=200,
        content=b"\xff\xd8fake-jpeg-bytes",
        headers={"Content-Type": "image/jpeg; charset=utf-8"})

    class _FakeRequestUtils:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def get_res(self, url):
            assert url == POSTER_CDN
            return fake

    monkeypatch.setattr("app.plugins.globalrankpro.hongguo.RequestUtils",
                        _FakeRequestUtils)

    response = plugin.poster_response(POSTER_CDN)

    assert response.body == fake.content
    assert response.media_type == "image/jpeg"
    assert "max-age" in response.headers["Cache-Control"]
