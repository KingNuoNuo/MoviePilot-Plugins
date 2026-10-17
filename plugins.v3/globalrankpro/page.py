"""插件设置表单与详情页的节点构造。

合并插件要在一个设置页里放下「猫眼」与「Netflix」两套配置、在一个详情页里
同时展示两套榜单，因此把节点构造从插件主类里拆出来：这里只做纯 UI 拼装，
抓取与存储一律通过传入的 ``host`` 与数据源对象完成。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.schemas import MediaSource, MediaType
from app.sdk.media import normalize_media_source

from .base import SOURCE_MAOYAN, SOURCE_NETFLIX
from .netflix import _fmt_hours, _fmt_runtime, _fmt_view_count, _safe_float, _safe_int

# 详情页底部/设置页顶部共用的 App 推荐卡内容
APP_PROMO = {
    'name': 'MoviePilotApp',
    'subtitle': 'Jetpack Compose · Material 3 安卓客户端',
    'desc': '首页榜单 · 媒体订阅 · 下载整理，随时随地管理媒体库；'
            '猫眼与 Netflix 榜单共用一套接口',
    'repo': 'https://github.com/KingNuoNuo/MoviePilotApp',
}

# 详情页单个分区最多展示的条目数，完整数据走推荐页与对外接口
PAGE_BOARD_LIMIT = 10


def form_col(component: str, props: dict, md: int = 6) -> dict:
    """构造宿主表单的一列（栅格：窄屏占满整行，宽屏按 md 分栏）。"""
    return {
        'component': 'VCol',
        'props': {'cols': 12, 'md': md},
        'content': [{'component': component, 'props': props}],
    }


def num_col(model: str, label: str, items: List[dict] = None) -> dict:
    """构造「条数」下拉列。"""
    return form_col('VSelect', {
        'model': model,
        'label': label,
        'items': items or [{'title': str(n), 'value': n}
                           for n in (0, 1, 2, 3, 5, 7, 10)],
    })


def type_col(model: str, label: str, items: List[dict]) -> dict:
    """构造「榜单类型」多选列。"""
    return form_col('VSelect', {
        'model': model,
        'label': label,
        'items': items,
        'chips': True,
        'multiple': True,
    })


def section_hint(title: str, text: str) -> dict:
    """构造分组标题行：宿主表单没有分组组件，用信息条分隔两个数据源。"""
    return {
        'component': 'VRow',
        'content': [{
            'component': 'VCol',
            'props': {'cols': 12},
            'content': [{
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'title': title,
                    'text': text,
                },
            }],
        }],
    }


def promo_card() -> dict:
    """构造 MoviePilotApp 推荐卡：数据页与设置页共用，均置于页面顶部。"""
    promo = APP_PROMO
    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'mt-2'},
        'content': [{
            'component': 'div',
            'props': {'class': 'd-flex align-center flex-wrap ga-4',
                      'style': {'padding': '16px'}},
            'content': [
                {'component': 'div',
                 'props': {'class': 'd-flex align-center '
                                   'justify-center flex-shrink-0',
                           'style': {'width': '48px', 'height': '48px',
                                     'border-radius': '12px',
                                     'background': '#8D51F9'}},
                 'content': [{'component': 'VIcon',
                              'props': {'icon': 'mdi-android',
                                        'size': 28, 'color': 'white'}}]},
                {'component': 'div',
                 'props': {'class': 'min-w-0 flex-grow-1'},
                 'content': [
                     {'component': 'div',
                      'props': {'class': 'text-subtitle-1 font-weight-bold'},
                      'text': promo['name']},
                     {'component': 'div',
                      'props': {'class': 'text-body-2 text-medium-emphasis'},
                      'text': promo['subtitle']},
                     {'component': 'div',
                      'props': {'class': 'text-caption text-medium-emphasis'},
                      'text': promo['desc']},
                     {'component': 'div',
                      'props': {'class': 'text-caption text-medium-emphasis'},
                      'text': promo['repo'].replace('https://', '')},
                 ]},
                {'component': 'VBtn',
                 'props': {'href': promo['repo'], 'target': '_blank',
                           'color': 'primary', 'variant': 'tonal',
                           'prepend-icon': 'mdi-github'},
                 'text': 'GitHub 仓库'},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# 设置表单
# ---------------------------------------------------------------------------

def build_form(host: Any) -> Tuple[List[dict], Dict[str, Any]]:
    """拼装合并插件的设置表单与默认配置。"""
    maoyan = host.sources.get(SOURCE_MAOYAN)
    netflix = host.sources.get(SOURCE_NETFLIX)
    regions = list(getattr(netflix, "_regions", ['global', 'hk', 'tw']))
    region_items = [{'title': name, 'value': code}
                    for code, name in (getattr(netflix, "_REGION_NAMES", {}) or {}).items()]

    rows: List[dict] = [
        # ---- 通用 ----
        {
            'component': 'VRow',
            'content': [
                form_col('VSwitch', {
                    'model': 'enabled', 'label': '启用插件',
                    'hint': '开启后按周期抓取已启用数据源的榜单',
                    'persistent-hint': True,
                }),
                form_col('VSwitch', {
                    'model': 'onlyonce', 'label': '立即运行一次',
                    'hint': '保存后立即抓取一次全部已启用数据源',
                    'persistent-hint': True,
                }),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                form_col('VCronField', {
                    'model': 'cron',
                    'label': '执行周期',
                    'placeholder': '0 9 * * *',
                    'hint': '猫眼每日更新、Netflix 每周二发榜，默认每天 09:00 抓取'
                            '（Netflix 无新数据时不会重复落库）',
                    'persistent-hint': True,
                }),
                form_col('VSwitch', {
                    'model': 'clear', 'label': '清理插件数据',
                    'hint': '清空两个数据源的榜单快照与识别缓存',
                    'persistent-hint': True,
                }),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                form_col('VSwitch', {
                    'model': 'api_enabled', 'label': '开放榜单接口',
                    'hint': '开启后 /rank、/rank-list、/refresh 可被 App 与脚本调用',
                    'persistent-hint': True,
                }),
                form_col('VSwitch', {
                    'model': 'recommend_enabled', 'label': '接入推荐页数据源',
                    'hint': '在宿主推荐页展示两个数据源的榜单（需同时开放接口）',
                    'persistent-hint': True,
                }),
            ],
        },
        # ---- 猫眼 ----
        section_hint(
            '猫眼榜单',
            '抓取猫眼电影票房、剧集与综艺热度榜，可自动订阅新上榜条目。'
            '订阅类型决定抓取哪些榜单；订阅条数为 0 表示不订阅（仍抓取供推荐页使用）；'
            '推荐开关关闭表示推荐页与榜单接口不展示该榜。推荐按满榜展示（单榜最多 10 条）。',
        ),
        {
            'component': 'VRow',
            'content': [
                form_col('VSwitch', {
                    'model': 'maoyan_enabled', 'label': '启用猫眼数据源',
                    'hint': '关闭后不抓取猫眼榜单，接口也不返回猫眼数据',
                    'persistent-hint': True,
                }),
                form_col('VSwitch', {
                    'model': 'maoyan_sub_enabled', 'label': '自动订阅新上榜',
                    'hint': '关闭则只更新榜单快照，不向下载/订阅链提交',
                    'persistent-hint': True,
                }),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                type_col('maoyan_type_movie', '电影榜单类型', [
                    {'title': '电影票房榜单', 'value': 'movie'},
                    {'title': '网络电影榜单', 'value': 'web-movie'},
                    {'title': '影史总票房榜', 'value': 'total'},
                    {'title': '国内年度票房榜', 'value': 'year'},
                    {'title': '全球年度票房榜', 'value': 'global'},
                ]),
                type_col('maoyan_type_tv', '剧集榜单类型', [
                    {'title': '剧集综合榜单（电视剧+网剧）', 'value': 'web-all'},
                    {'title': '综艺热度榜单', 'value': 'zongyi'},
                ]),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_movie_sub_num', '电影榜单订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_movie_rec_enabled',
                                     'label': '电影榜单推荐'}),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_tv_sub_num', '剧集榜单订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_tv_rec_enabled',
                                     'label': '剧集榜单推荐'}),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_zongyi_sub_num', '综艺榜单订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_zongyi_rec_enabled',
                                     'label': '综艺榜单推荐'}),
            ],
        },
        section_hint(
            '猫眼平台细分榜',
            '各视频平台榜单的订阅条数与推荐开关（左订阅、右推荐）：'
            '订阅条数只影响自动订阅，推荐开关决定推荐页与榜单接口是否展示该平台榜单。',
        ),
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_tx_sub_num', '腾讯视频订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_tx_rec_enabled',
                                     'label': '腾讯视频推荐'}),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_iqy_sub_num', '爱奇艺订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_iqy_rec_enabled',
                                     'label': '爱奇艺推荐'}),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_mg_sub_num', '芒果TV订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_mg_rec_enabled',
                                     'label': '芒果TV推荐'}),
            ],
        },
        {
            'component': 'VRow',
            'content': [
                num_col('maoyan_yk_sub_num', '优酷订阅条数'),
                form_col('VSwitch', {'model': 'maoyan_yk_rec_enabled',
                                     'label': '优酷推荐'}),
            ],
        },
        # ---- Netflix ----
        section_hint(
            'Netflix 榜单',
            '数据来自 Netflix 官方 Tudum Top 10 榜单（每周二更新）。'
            '全球榜附周观看量与在榜周数，地区榜仅名次；Netflix 未进入中国大陆，'
            '地区榜提供中国香港、中国台湾等选项。',
        ),
        {
            'component': 'VRow',
            'content': [
                form_col('VSwitch', {
                    'model': 'netflix_enabled', 'label': '启用 Netflix 数据源',
                    'hint': '关闭后不抓取 Netflix 榜单，接口也不返回相关数据',
                    'persistent-hint': True,
                }),
                form_col('VSelect', {
                    'model': 'netflix_regions',
                    'label': '抓取地区',
                    'items': region_items,
                    'chips': True,
                    'multiple': True,
                    'hint': '全球榜附观看量与在榜周数；地区榜仅名次',
                    'persistent-hint': True,
                }),
            ],
        },
    ]

    defaults = {
        'enabled': False,
        'onlyonce': False,
        'cron': '',
        'clear': False,
        'api_enabled': True,
        'recommend_enabled': False,
        # 猫眼
        'maoyan_enabled': True,
        'maoyan_sub_enabled': True,
        'maoyan_type_movie': ['movie', 'web-movie'],
        'maoyan_type_tv': ['web-all', 'zongyi'],
        'maoyan_movie_sub_num': 10,
        'maoyan_movie_rec_enabled': True,
        'maoyan_tv_sub_num': 10,
        'maoyan_tv_rec_enabled': True,
        'maoyan_zongyi_sub_num': 10,
        'maoyan_zongyi_rec_enabled': True,
        'maoyan_tx_sub_num': 0,
        'maoyan_iqy_sub_num': 0,
        'maoyan_mg_sub_num': 0,
        'maoyan_yk_sub_num': 0,
        'maoyan_tx_rec_enabled': False,
        'maoyan_iqy_rec_enabled': False,
        'maoyan_mg_rec_enabled': False,
        'maoyan_yk_rec_enabled': False,
        # Netflix
        'netflix_enabled': True,
        'netflix_regions': regions,
    }
    # 用当前运行态覆盖默认值，让设置页回显已保存的配置
    defaults.update(host.serialize_config())
    return [promo_card(), {'component': 'VForm', 'content': rows}], defaults


# ---------------------------------------------------------------------------
# 详情页
# ---------------------------------------------------------------------------

def _safe_text(value, default: str = "") -> str:
    """把详情页里可能为 None 的字段收敛成展示文本。"""
    if value is None:
        return default
    text = str(value).strip()
    if not text or text.lower() == 'none':
        return default
    return text


def _safe_day(value, default: str = "") -> str:
    """日期展示：猫眼年度榜的 show_date 是 20261017 这种紧凑格式，补上分隔符。"""
    text = _safe_text(value, default)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _tmdb_href(media_type, tmdb_id) -> str:
    """根据媒体类型生成 TMDB 链接，缺少 ID 时返回空串。"""
    clean_id = _safe_text(tmdb_id)
    if not clean_id:
        return ""
    if str(media_type) == MediaType.TV.value:
        return f"https://www.themoviedb.org/tv/{clean_id}"
    return f"https://www.themoviedb.org/movie/{clean_id}"


def _poster_box(poster: str, rank: int) -> dict:
    """海报 + 名次角标（前三名用主色）。"""
    rank_top = 0 < rank <= 3
    badge = {
        'component': 'span',
        'props': {
            'style': {
                'position': 'absolute', 'top': '6px', 'left': '6px',
                'min-width': '20px', 'height': '20px',
                'border-radius': '6px', 'display': 'flex',
                'align-items': 'center', 'justify-content': 'center',
                'font-size': '11.5px', 'font-weight': '500',
                'background': '#8D51F9' if rank_top else 'rgba(11,19,34,0.78)',
                'color': '#FFFFFF' if rank_top else 'rgba(242,245,250,0.85)',
                'border': 'none' if rank_top else '0.5px solid rgba(242,245,250,0.25)',
            },
        },
        # text 必须挂在节点上：放进 props 宿主会当成未知属性，角标就只剩一个空方块
        'text': str(rank),
    }
    image = {
        'component': 'VImg',
        'props': {'src': poster, 'height': 132, 'width': 88,
                  'aspect-ratio': '2/3', 'cover': True},
    } if poster else {
        'component': 'div',
        'props': {'class': 'd-flex align-center justify-center '
                           'bg-grey-lighten-3 text-caption '
                           'text-medium-emphasis',
                  'style': {'width': '88px', 'height': '132px'}},
        'text': '无海报',
    }
    return {
        'component': 'div',
        'props': {'style': {'position': 'relative', 'width': '88px',
                            'height': '132px', 'flex-shrink': '0',
                            'border-radius': '4px', 'overflow': 'hidden'}},
        'content': [image, badge],
    }


def _value_line(label: str, value: str) -> dict:
    """量值行：左标签右数值。"""
    return {
        'component': 'div',
        'props': {'class': 'd-flex justify-space-between ga-2 mb-1'},
        'content': [
            {'component': 'span',
             'props': {'class': 'text-caption text-medium-emphasis'},
             'text': label},
            {'component': 'span',
             'props': {'class': 'text-body-2 font-weight-medium'},
             'text': value},
        ],
    }


def _maoyan_rank_card(row: dict, board_type: str) -> dict:
    """猫眼榜单条目卡片。"""
    rank = int(row.get("rank") or 0)
    title = _safe_text(row.get("title"), "未命名")
    title_cn = _safe_text(row.get("title_cn"))
    main_title = title_cn or title
    sub_title = title if (title_cn and title_cn != title) else ""
    media_type = {
        'movie': MediaType.MOVIE, 'total': MediaType.MOVIE,
        'year': MediaType.MOVIE, 'global': MediaType.MOVIE,
        'web-movie': MediaType.MOVIE,
    }.get(board_type, MediaType.TV)
    href = ""
    if normalize_media_source(row.get("media_source")) == MediaSource.TMDB:
        href = _tmdb_href(media_type.value, row.get("media_id"))
    title_node = {
        'component': 'a' if href else 'span',
        'props': {'href': href, 'target': '_blank',
                  'class': 'text-decoration-none'} if href else {},
        'text': main_title,
    }

    info: List[dict] = [{
        'component': 'div',
        'props': {'class': 'text-subtitle-2 font-weight-bold mb-1'},
        'content': [title_node],
    }]
    if sub_title:
        info.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis '
                               'text-truncate mb-1'},
            'text': sub_title,
        })

    box_office = _safe_text(row.get("box_office"))
    total_box = _safe_text(row.get("total_box_office"))
    release = _safe_text(row.get("release_info"))
    if board_type == "movie":
        for label, value in (("当日票房", box_office), ("累计票房", total_box)):
            if value:
                info.append(_value_line(label, value))
    elif board_type == "total":
        if box_office:
            info.append(_value_line("影史票房", box_office))
    elif board_type in ("year", "global"):
        if box_office:
            info.append(_value_line("年度票房", box_office))
    else:
        # 剧集/综艺的热度是猫眼 PUA 加密字体，抓出来是乱码，不上页面
        if board_type == "web-movie":
            platform = _safe_text(row.get("platform"))
            if platform:
                info.append({
                    'component': 'VChip',
                    'props': {'size': 'x-small', 'variant': 'tonal',
                              'color': 'info', 'class': 'mb-1'},
                    'text': platform,
                })
        if release:
            info.append({'component': 'div',
                         'props': {'class': 'text-body-2'},
                         'text': release})

    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'h-100'},
        'content': [{
            'component': 'div',
            'props': {'class': 'd-flex flex-nowrap ga-3 pa-3'},
            'content': [
                _poster_box(_safe_text(row.get("poster")), rank),
                {'component': 'div',
                 'props': {'class': 'min-w-0 flex-grow-1'},
                 'content': info},
            ],
        }],
    }


def _netflix_rank_card(row: dict) -> dict:
    """Netflix 榜单条目卡片。"""
    rank = _safe_int(row.get("rank"))
    title = _safe_text(row.get("title_cn")) or _safe_text(row.get("title"))
    en_title = _safe_text(row.get("title"))

    info: List[dict] = [{
        'component': 'div',
        'props': {'class': 'text-subtitle-2 font-weight-bold mb-1'},
        'text': title,
    }]
    if en_title and en_title != title:
        info.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis '
                               'text-truncate mb-1'},
            'text': en_title,
        })

    meta: List[str] = []
    if row.get("year"):
        meta.append(_safe_text(row.get("year")))
    season = _safe_text(row.get("season_title"))
    if season and season != 'N/A':
        meta.append(season)
    if meta:
        info.append({'component': 'div',
                     'props': {'class': 'text-caption text-medium-emphasis'},
                     'text': ' · '.join(meta)})

    values: List[str] = []
    hours = _fmt_hours(_safe_int(row.get("hours_viewed")))
    views = _fmt_view_count(_safe_int(row.get("views")))
    if hours and views:
        values.append(f"{hours} · {views}次观看")
    elif hours:
        values.append(hours)
    elif views:
        values.append(f"{views}次观看")
    runtime = _fmt_runtime(_safe_float(row.get("runtime")))
    if runtime:
        values.append(runtime)
    weeks = _safe_int(row.get("weeks_in_top10"))
    if weeks > 1:
        values.append(f"在榜 {weeks} 周")
    if values:
        info.append({'component': 'div',
                     'props': {'class': 'text-caption text-medium-emphasis'},
                     'text': ' · '.join(values)})

    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'h-100'},
        'content': [{
            'component': 'div',
            'props': {'class': 'd-flex flex-nowrap ga-3 pa-3'},
            'content': [
                _poster_box(_safe_text(row.get("poster")), rank),
                {'component': 'div',
                 'props': {'class': 'min-w-0 flex-grow-1'},
                 'content': info},
            ],
        }],
    }


def _board_section(source_id: str, board: dict, title: str,
                   subtitle: str = "") -> dict:
    """单个榜单分区：标题 + 条数 + 可选副标题 + 条目网格。"""
    board_type = str(board.get("type") or "")
    items = [row for row in (board.get("items") or []) if isinstance(row, dict)]
    shown = items[:PAGE_BOARD_LIMIT]

    header: List[dict] = [
        {'component': 'span',
         'props': {'class': 'text-subtitle-2 font-weight-bold'},
         'text': title},
        {'component': 'VChip',
         'props': {'size': 'x-small', 'variant': 'tonal', 'color': 'primary'},
         'text': f"{len(items)} 条"},
    ]
    if subtitle:
        header.append({'component': 'span',
                       'props': {'class': 'text-caption text-medium-emphasis'},
                       'text': subtitle})

    build = ((lambda row: _maoyan_rank_card(row, board_type))
             if source_id == SOURCE_MAOYAN else _netflix_rank_card)
    children = [
        {'component': 'div',
         'props': {'class': 'd-flex align-center flex-wrap ga-2 mb-3'},
         'content': header},
        {'component': 'div',
         'props': {'class': 'grid gap-3 grid-info-card'},
         'content': [build(row) for row in shown]},
    ]
    if len(items) > PAGE_BOARD_LIMIT:
        children.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis mt-2'},
            'text': f"共 {len(items)} 条，展示前 {PAGE_BOARD_LIMIT} 条，完整数据见推荐页",
        })
    return {'component': 'div', 'props': {'class': 'mb-6'}, 'content': children}


def _refresh_button(source_id: str) -> dict:
    """数据源刷新按钮：只刷新本数据源，避免一次点击串行抓两边。"""
    return {
        'component': 'VBtn',
        'props': {'color': 'primary', 'variant': 'tonal', 'size': 'small',
                  'prepend-icon': 'mdi-refresh'},
        'text': '刷新',
        'events': {'click': {'api': 'plugin/GlobalRankPro/refresh',
                             'method': 'GET',
                             'params': {'source': source_id}}},
    }


def _source_section(host: Any, source: Any, title: str) -> List[dict]:
    """构造一个数据源的榜单区块（标题栏 + 各榜单分区）。"""
    boards = source.boards()
    updated_at = str(source.snapshot_meta().get("updated_at") or "")

    header = {
        'component': 'div',
        'props': {'class': 'd-flex justify-space-between align-center '
                           'flex-wrap ga-2 mb-4'},
        'content': [
            {'component': 'div', 'content': [
                {'component': 'div',
                 'props': {'class': 'text-h6'}, 'text': title},
                {'component': 'div',
                 'props': {'class': 'text-caption text-medium-emphasis'},
                 'text': (f"快照更新于 {_safe_text(updated_at, '未知时间')}"
                          if updated_at else "暂无榜单快照")},
            ]},
            _refresh_button(source.source_id),
        ],
    }

    if not boards:
        return [header, {
            'component': 'VAlert',
            'props': {'type': 'info', 'variant': 'tonal',
                      'text': '暂无榜单快照，点击「刷新」或等待定时任务运行'},
        }]

    nodes: List[dict] = [header]
    if source.source_id == SOURCE_MAOYAN:
        # 猫眼：同一类型可能有多份平台细分榜，分区只展示主榜（全网榜）
        grouped: Dict[str, List[dict]] = {}
        for board in boards:
            board_type = str(board.get("type") or "")
            if board_type:
                grouped.setdefault(board_type, []).append(board)

        def primary(board_list: List[dict]) -> dict:
            for board in board_list:
                name = str(board.get("board") or "")
                platform = str(board.get("platform") or "")
                if not name.endswith("）") and platform in ("", "全网"):
                    return board
            return board_list[0]

        order = getattr(source, "_PAGE_BOARD_ORDER", tuple(grouped))
        for board_type in order:
            if board_type not in grouped:
                continue
            board = primary(grouped[board_type])
            label = (getattr(source, "_TYPE_NAMES", {}) or {}).get(
                board_type, str(board.get("type_name") or board_type))
            nodes.append(_board_section(
                source.source_id, board, label,
                _safe_day(board.get("show_date"))))
    else:
        # Netflix：按地区 × 内容类型逐榜展示
        region_names = getattr(source, "_REGION_NAMES", {}) or {}
        kind_names = getattr(source, "_KIND_NAMES", {}) or {}
        order_regions = list(getattr(source, "_regions", []))
        order = {code: index for index, code in enumerate(order_regions)}
        boards = sorted(
            boards,
            key=lambda b: (order.get(str(b.get("region") or ""), 99),
                           0 if str(b.get("kind") or "") == "film" else 1))
        for board in boards:
            region = str(board.get("region") or "")
            kind = str(board.get("kind") or "")
            # 榜单名自带「地区 · 内容类型（语种）」，比按地区+类型拼更准：
            # 全球榜会同时有「电影（英语）」「电影（非英语）」两份，拼出来会重名
            label = _safe_text(board.get("board"))
            if label.startswith("Netflix "):
                label = label[len("Netflix "):]
            if not label:
                label = (f"{region_names.get(region, '全球' if not region else region)}"
                         f"{kind_names.get(kind, kind)}")
            nodes.append(_board_section(
                source.source_id, board, label,
                _safe_text(board.get("week"))))
    return nodes


def _history_section(source: Any) -> List[dict]:
    """猫眼订阅历史（Netflix 无订阅语义，不展示）。"""
    historys = source.get_data('history') or []
    if not isinstance(historys, list):
        historys = [historys]
    historys = sorted(
        [h for h in historys if isinstance(h, dict)],
        key=lambda x: x.get('time') or "", reverse=True)
    if not historys:
        return [{'component': 'div',
                 'props': {'class': 'text-center pa-6 text-medium-emphasis'},
                 'text': '暂无订阅历史'}]

    rows = []
    for index, history in enumerate(historys, start=1):
        media_type = _safe_text(history.get("type"), "未知")
        rows.append({
            "index": index,
            "title": _safe_text(history.get("title"), "未命名"),
            "release_info": _safe_text(history.get("releaseInfo"), "暂无上线信息"),
            "platform": _safe_text(history.get("platformDesc"), "未知"),
            "type": media_type,
            "time": _safe_text(history.get("time"), "未知时间"),
            "poster": _safe_text(history.get("poster")),
            "tmdb_href": _tmdb_href(
                media_type,
                history.get("media_id")
                if normalize_media_source(history.get("media_source")) == MediaSource.TMDB
                else None),
        })

    return [{
        'component': 'VDataTable',
        'props': {
            'headers': [
                {'title': '#', 'key': 'index', 'sortable': False},
                {'title': '标题', 'key': 'title'},
                {'title': '类型', 'key': 'type'},
                {'title': '平台', 'key': 'platform'},
                {'title': '上线信息', 'key': 'release_info'},
                {'title': '时间', 'key': 'time'},
            ],
            'items': rows,
            'items-per-page': 10,
            'density': 'compact',
        },
    }]


def build_page(host: Any) -> List[dict]:
    """拼装插件详情页：App 推荐 + 各数据源榜单 + 猫眼订阅历史。"""
    nodes: List[dict] = [promo_card()]

    for source_id, title, label in (
            (SOURCE_MAOYAN, '猫眼榜单', '猫眼'),
            (SOURCE_NETFLIX, 'Netflix 榜单', 'Netflix')):
        source = host.sources.get(source_id)
        if source is None:
            continue
        if not getattr(source, "enabled", False):
            # 提示文案里的配置项名与设置页保持一致（「启用猫眼数据源」等）
            nodes.append({
                'component': 'VAlert',
                'props': {'type': 'warning', 'variant': 'tonal',
                          'text': f"{title}数据源未启用：在插件设置里打开"
                                  f"「启用{label}数据源」"},
            })
            continue
        nodes.extend(_source_section(host, source, title))

    maoyan = host.sources.get(SOURCE_MAOYAN)
    if maoyan is not None and getattr(maoyan, "enabled", False):
        nodes.append({'component': 'div',
                      'props': {'class': 'text-h6 mb-3'},
                      'text': '订阅历史'})
        nodes.extend(_history_section(maoyan))
    return nodes
