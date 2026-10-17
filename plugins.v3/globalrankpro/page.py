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


# 宿主的 @core 样式把 ``.v-switch.v-input`` 的 --v-input-control-height 改成了
# auto，开关高度塌缩成文字行高，视觉高度溢出到下一行，与下方控件叠在一起
# （截图里「电影榜」压着开关、「自动订阅条数」压着下拉框）。插件改不了宿主 CSS，
# 用内联样式把固定高度还原——内联优先级高于那条类选择器。
SWITCH_FIX = {'--v-input-control-height': '40px'}

# 自动订阅条数可选档位
SUB_NUM_CHOICES = (0, 1, 2, 3, 5, 7, 10)


def form_col(component: str, props: dict, md: int = 6) -> dict:
    """构造宿主表单的一列（栅格：窄屏占满整行，宽屏按 md 分栏）。"""
    return form_col_nodes([{'component': component, 'props': props}], md)


def form_col_nodes(nodes: List[dict], md: int = 6) -> dict:
    """一列里放多个节点（例如开关 + 文字）。"""
    return {'component': 'VCol', 'props': {'cols': 12, 'md': md}, 'content': nodes}


def form_row(cols: List[dict], cls: str = 'align-center') -> dict:
    """一行：栅格间距用 compact，避免默认 12px gutter 把行拉高。"""
    return {
        'component': 'VRow',
        'props': {'class': cls, 'density': 'compact'},
        'content': cols,
    }


def switch_col(model: str, label: str, md: int = 6) -> dict:
    """开关列。

    开关自带的 label 在 compact 密度下会比轨道高几个像素，但比「开关 + 外部
    文字」稳：后者在宿主的 flex 布局里 .v-input 宽度会塌成 0，文字直接压到
    轨道上。这里保留 label，只把宿主压塌的高度用内联变量还原。
    """
    return form_col('VSwitch', {
        'model': model,
        'label': label,
        'color': 'primary',
        'density': 'compact',
        'hide-details': True,
        'style': dict(SWITCH_FIX),
    }, md=md)


def num_select(model: str, width: int = 84) -> dict:
    """自动订阅条数：限宽的小下拉，避免一个数字撑出一个大方框。"""
    return {
        'component': 'VSelect',
        'props': {
            'model': model,
            'items': [{'title': str(n), 'value': n} for n in SUB_NUM_CHOICES],
            'density': 'compact',
            'variant': 'outlined',
            'hide-details': True,
            'style': {'width': '{0}px'.format(width)},
        },
    }


def num_group(title: str, items: List[Tuple[str, str]]) -> dict:
    """一行「组名 + 榜名 + 条数框」，窄屏时用 flex-wrap 自动折行。"""
    content: List[dict] = [{
        'component': 'span',
        'props': {'class': 'text-body-2', 'style': {'min-width': '96px'}},
        'text': title,
    }]
    for label, model in items:
        content.append({
            'component': 'div',
            'props': {'class': 'd-flex align-center ga-2',
                      'style': {'margin-right': '24px'}},
            'content': [
                {'component': 'span',
                 'props': {'class': 'text-body-2'},
                 'text': label},
                num_select(model),
            ],
        })
    return {'component': 'div',
            'props': {'class': 'd-flex flex-wrap align-center py-1'},
            'content': content}


def type_select(model: str, label: str, items: List[dict], md: int = 6) -> dict:
    """榜单类型多选（chip 可点掉，比再点一次列表更直观）。"""
    return form_col('VSelect', {
        'model': model,
        'label': label,
        'items': items,
        'chips': True,
        'multiple': True,
        'closable-chips': False,
        'chip-props': {'color': 'primary', 'variant': 'tonal', 'size': 'small'},
        'density': 'compact',
        'variant': 'outlined',
        'hide-details': True,
    }, md=md)


def setting_card(title: str, desc: str, rows: List[dict]) -> dict:
    """一张设置卡：卡片标题 + 灰色说明 + 若干行。

    宿主表单没有分组组件，用 outlined 卡片替代原来刺眼的蓝色 VAlert 信息条，
    视觉上把「运行 / 猫眼 / Netflix」切成三块。
    """
    head: List[dict] = [{
        'component': 'div',
        'props': {'class': 'text-subtitle-2 font-weight-bold'},
        'text': title,
    }]
    if desc:
        head.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis mt-1',
                      'style': {'line-height': '1.55'}},
            'text': desc,
        })
    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'mb-3'},
        'content': [{
            'component': 'VCardText',
            'props': {'class': 'pa-4 pt-3'},
            'content': head + [{'component': 'div',
                                'props': {'class': 'mb-2'}}] + rows,
        }],
    }


def promo_card() -> dict:
    """构造 MoviePilotApp 推荐卡：数据页与设置页共用，均置于页面顶部。

    只保留两行文字（名称 + 一句话说明），避免在设置页顶部吃掉一百多像素。
    """
    promo = APP_PROMO
    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'mb-3'},
        'content': [{
            'component': 'div',
            'props': {'class': 'd-flex align-center flex-wrap ga-3',
                      'style': {'padding': '12px 16px'}},
            'content': [
                {'component': 'div',
                 'props': {'class': 'd-flex align-center '
                                   'justify-center flex-shrink-0',
                           'style': {'width': '40px', 'height': '40px',
                                     'border-radius': '10px',
                                     'background': '#8D51F9'}},
                 'content': [{'component': 'VIcon',
                              'props': {'icon': 'mdi-android',
                                        'size': 24, 'color': 'white'}}]},
                {'component': 'div',
                 'props': {'class': 'min-w-0 flex-grow-1'},
                 'content': [
                     {'component': 'div',
                      'props': {'class': 'text-body-1 font-weight-bold'},
                      'text': promo['name']},
                     {'component': 'div',
                      'props': {'class': 'text-caption text-medium-emphasis'},
                      'text': promo['subtitle'] + ' · 榜单接口与宿主插件共用'},
                 ]},
                {'component': 'VBtn',
                 'props': {'href': promo['repo'], 'target': '_blank',
                           'color': 'primary', 'variant': 'tonal',
                           'size': 'small', 'prepend-icon': 'mdi-github'},
                 'text': 'GitHub'},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# 设置表单
# ---------------------------------------------------------------------------

def build_form(host: Any) -> Tuple[List[dict], Dict[str, Any]]:
    """拼装合并插件的设置表单与默认配置。

    排版按「三张卡 + 一行多项」组织：运行 / 猫眼榜单 / Netflix 榜单各一张卡，
    卡内每行放 1~3 个同类字段，说明统一收进卡片描述，字段只留名字——原来每个
    字段都拖一行 hint，行高参差且页面被拉得很长。

    每个榜单的「抓取开关」与「订阅条数」不再上下堆叠：宿主的 switch 高度会塌缩，
    堆叠时必然压住下面的下拉框；订阅条数改成一行并排的小号下拉。
    """
    netflix = host.sources.get(SOURCE_NETFLIX)
    regions = list(getattr(netflix, "_regions", ['global', 'hk', 'tw']))
    region_names = getattr(netflix, "_REGION_NAMES", {}) or {}
    region_items = [{'title': name, 'value': code}
                    for code, name in region_names.items()]

    rows: List[dict] = [
        setting_card(
            '运行',
            '按执行周期抓取已启用数据源。「立即运行一次」保存后跑一次并自动取消；'
            '「清理插件数据」清空快照、识别缓存与订阅历史，不可恢复；'
            '「推荐页取数授权」关闭后宿主推荐页取不到榜单。',
            [
                form_row([
                    switch_col('enabled', '启用插件', 4),
                    switch_col('onlyonce', '立即运行一次', 4),
                    switch_col('clear', '清理插件数据', 4),
                ]),
                form_row([
                    form_col('VCronField', {
                        'model': 'cron',
                        'label': '执行周期',
                        'placeholder': '0 9 * * *',
                    }, 5),
                    switch_col('api_enabled', '推荐页取数授权', 4),
                    switch_col('recommend_enabled', '接入推荐页', 3),
                ]),
            ],
        ),
        setting_card(
            '猫眼榜单',
            '勾选的榜单类型决定抓取哪些榜；「自动订阅条数」取该榜前 N 条提交订阅，'
            '选 0 表示只抓榜、不订阅。平台专项榜只服务自动订阅，不写入首页快照。',
            [
                form_row([switch_col('maoyan_enabled', '启用猫眼数据源', 6)]),
                form_row([
                    type_select('maoyan_type_movie', '电影榜单', [
                        {'title': '电影票房', 'value': 'movie'},
                        {'title': '网络电影', 'value': 'web-movie'},
                        {'title': '影史总票房', 'value': 'total'},
                        {'title': '国内年度', 'value': 'year'},
                        {'title': '全球年度', 'value': 'global'},
                    ], 7),
                    type_select('maoyan_type_tv', '剧集/综艺榜单', [
                        {'title': '剧集综合（电视剧+网剧）', 'value': 'web-all'},
                        {'title': '综艺热度', 'value': 'zongyi'},
                    ], 5),
                ], cls='align-start'),
                num_group('自动订阅', [
                    ('猫眼全网电影榜', 'maoyan_movie_sub_num'),
                    ('猫眼全网剧集榜', 'maoyan_tv_sub_num'),
                    ('猫眼全网综艺榜', 'maoyan_zongyi_sub_num'),
                ]),
                num_group('平台专项榜', [
                    ('腾讯视频', 'maoyan_tx_sub_num'),
                    ('爱奇艺', 'maoyan_iqy_sub_num'),
                    ('芒果TV', 'maoyan_mg_sub_num'),
                    ('优酷', 'maoyan_yk_sub_num'),
                ]),
            ],
        ),
        setting_card(
            'Netflix 榜单',
            'Netflix 官方 Tudum Top 10，每周二更新。全球榜带周观看量与在榜周数，'
            '地区榜只有名次；Netflix 未进入中国大陆，地区榜可选中国香港、中国台湾等。',
            [
                form_row([switch_col('netflix_enabled', '启用 Netflix 数据源', 6)]),
                form_row([
                    type_select('netflix_regions', '榜单地区',
                                region_items, 12),
                ]),
            ],
        ),
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
        'maoyan_type_movie': ['movie', 'web-movie'],
        'maoyan_type_tv': ['web-all', 'zongyi'],
        'maoyan_movie_sub_num': 0,
        'maoyan_tv_sub_num': 0,
        'maoyan_zongyi_sub_num': 0,
        'maoyan_tx_sub_num': 0,
        'maoyan_iqy_sub_num': 0,
        'maoyan_mg_sub_num': 0,
        'maoyan_yk_sub_num': 0,
        # Netflix
        'netflix_enabled': True,
        'netflix_regions': regions,
    }
    # 用当前运行态覆盖默认值，让设置页回显已保存的配置；
    # 未渲染的死开关（*_rec_enabled 等）也一并带回，保存时不会丢字段。
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
        # 折叠面板是展开时才创建节点，加 eager 让图片立刻开始加载，
        # 否则要等一次重排触发 IntersectionObserver，展开瞬间会先看到空框
        'props': {'src': poster, 'height': 132, 'width': 88,
                  'aspect-ratio': '2/3', 'cover': True, 'eager': True},
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


def _board_panel(source_id: str, board: dict, title: str, value: str,
                 subtitle: str = "") -> dict:
    """单个榜单 = 一个可折叠面板。

    宿主的 JSON 页面渲染器只是 ``h()`` 静态展开，没有组件状态（拿不到
    ``v-model``），横排 ``VTabs`` 切不动内容；折叠面板自带展开状态，外层用
    accordion 保证一次只展开一个，交互上等价于「切换榜单」而非「往下翻页」。
    """
    board_type = str(board.get("type") or "")
    items = [row for row in (board.get("items") or []) if isinstance(row, dict)]
    shown = items[:PAGE_BOARD_LIMIT]

    # 标题行：榜单名（过长省略）+ 条数 + 日期；右侧留给宿主的展开箭头
    head: List[dict] = [
        {'component': 'span',
         'props': {'class': 'text-body-1 font-weight-medium '
                            'text-truncate min-w-0'},
         'text': title},
        {'component': 'VChip',
         'props': {'size': 'x-small', 'variant': 'tonal', 'color': 'primary',
                   'class': 'flex-shrink-0'},
         'text': f"{len(items)} 条"},
    ]
    if subtitle:
        # 日期推到箭头左侧：没有这个占位时它会紧贴条数徽标，标题行中间留一大片空
        head.append({'component': 'div', 'props': {'class': 'flex-grow-1'}})
        head.append({'component': 'span',
                     'props': {'class': 'text-caption text-medium-emphasis '
                                        'flex-shrink-0'},
                     'text': subtitle})

    build = ((lambda row: _maoyan_rank_card(row, board_type))
             if source_id == SOURCE_MAOYAN else _netflix_rank_card)
    body: List[dict] = [{
        'component': 'div',
        'props': {'class': 'grid gap-3 grid-info-card'},
        'content': [build(row) for row in shown],
    }]
    if len(items) > PAGE_BOARD_LIMIT:
        body.append({
            'component': 'div',
            'props': {'class': 'text-caption text-medium-emphasis mt-3'},
            'text': f"共 {len(items)} 条，展示前 {PAGE_BOARD_LIMIT} 条，"
                    f"完整数据见推荐页",
        })

    return {
        'component': 'VExpansionPanel',
        'props': {'value': value},
        'content': [
            {'component': 'VExpansionPanelTitle',
             'content': [{
                 'component': 'div',
                 'props': {'class': 'd-flex align-center ga-3 '
                                    'flex-grow-1 min-w-0'},
                 'content': head,
             }]},
            {'component': 'VExpansionPanelText',
             'props': {'class': 'pt-0'},
             'content': body},
        ],
    }


def _refresh_button(source_id: str, variant: str = "tonal") -> dict:
    """数据源刷新按钮：只刷新本数据源，避免一次点击串行抓两边。"""
    return {
        'component': 'VBtn',
        'props': {'color': 'primary', 'variant': variant, 'size': 'small',
                  'prepend-icon': 'mdi-refresh', 'class': 'flex-shrink-0'},
        'text': '刷新',
        'events': {'click': {'api': 'plugin/GlobalRankPro/refresh',
                             'method': 'GET',
                             'params': {'source': source_id}}},
    }


def _overview_card(host: Any) -> dict:
    """概览卡：两个数据源的榜数、条数、更新时间与独立刷新。"""
    rows: List[dict] = []
    for source_id, label, icon in (
            (SOURCE_MAOYAN, '猫眼榜单', 'mdi-movie-open-outline'),
            (SOURCE_NETFLIX, 'Netflix 榜单', 'mdi-television-classic')):
        source = host.sources.get(source_id)
        if source is None:
            continue
        enabled = bool(getattr(source, "enabled", False))
        boards = source.boards() if enabled else []
        total = sum(len(b.get("items") or []) for b in boards)
        updated = _safe_text(source.snapshot_meta().get("updated_at")) if enabled else ""

        if not enabled:
            detail = '未启用'
        elif not boards:
            detail = '暂无快照'
        else:
            detail = f"{len(boards)} 个榜 · {total} 条"
            if updated:
                detail = f"{detail} · 更新于 {updated}"

        right: List[dict] = [
            {'component': 'div', 'props': {'class': 'flex-grow-1'}},
            ({'component': 'span',
              'props': {'class': 'text-caption text-medium-emphasis'},
              'text': '在设置里启用'} if not enabled
             else _refresh_button(source_id)),
        ]
        rows.append({
            'component': 'div',
            'props': {'class': 'd-flex align-center flex-wrap ga-3 py-2'},
            'content': [
                {'component': 'VIcon',
                 'props': {'icon': icon, 'size': 20,
                           'color': 'primary' if enabled else 'disabled'}},
                {'component': 'span',
                 'props': {'class': 'text-body-1 font-weight-medium '
                                    'flex-shrink-0'},
                 'text': label},
                {'component': 'span',
                 'props': {'class': 'text-caption text-medium-emphasis'},
                 'text': detail},
            ] + right,
        })

    content: List[dict] = []
    for index, row in enumerate(rows):
        if index:
            content.append({'component': 'VDivider'})
        content.append(row)
    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'mb-6'},
        'content': [{
            'component': 'div',
            'props': {'style': {'padding': '6px 16px'}},
            'content': content,
        }],
    }


def _source_panels(source: Any) -> List[dict]:
    """把一个数据源的各榜单装进 accordion，一次只展开一个。"""
    boards = source.boards()
    panels: List[dict] = []

    if source.source_id == SOURCE_MAOYAN:
        # 猫眼：同一类型可能有多份平台细分榜，只展示主榜（全网榜）
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
            panels.append(_board_panel(
                source.source_id, board, label, board_type,
                _safe_day(board.get("show_date"))))
    else:
        # Netflix：按地区 × 内容类型逐榜展示，顺序跟随设置里勾选的地区
        region_names = getattr(source, "_REGION_NAMES", {}) or {}
        kind_names = getattr(source, "_KIND_NAMES", {}) or {}
        order_regions = list(getattr(source, "_regions", []))
        order = {code: index for index, code in enumerate(order_regions)}
        # 全球榜在快照里的 region 是空串（沿用原 NetflixPro 的历史约定），
        # 直接取出来会落到默认序 99，把全球榜排到所有地区榜后面
        boards = sorted(
            boards,
            key=lambda b: (order.get(str(b.get("region") or "") or "global", 99),
                           0 if str(b.get("kind") or "") == "film" else 1))
        for index, board in enumerate(boards):
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
            panels.append(_board_panel(
                source.source_id, board, label, f"netflix:{index}",
                _safe_text(board.get("week"))))

    if not panels:
        return []
    return [{
        'component': 'VExpansionPanels',
        'props': {'variant': 'accordion', 'multiple': False,
                  'flat': True, 'class': 'mb-6'},
        'content': panels,
    }]


def _history_rows(source: Any) -> List[dict]:
    """猫眼订阅历史转表格行。"""
    historys = source.get_data('history') or []
    if not isinstance(historys, list):
        historys = [historys]
    historys = sorted(
        [h for h in historys if isinstance(h, dict)],
        key=lambda x: x.get('time') or "", reverse=True)
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
    return rows


def _history_panel(source: Any) -> dict:
    """订阅历史包成一个折叠面板，默认收起，不占首屏。"""
    rows = _history_rows(source)
    if rows:
        body: List[dict] = [{
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
    else:
        body = [{'component': 'div',
                 'props': {'class': 'text-center pa-4 text-medium-emphasis'},
                 'text': '暂无订阅历史'}]

    return {
        'component': 'VExpansionPanels',
        'props': {'variant': 'accordion', 'multiple': False, 'flat': True},
        'content': [{
            'component': 'VExpansionPanel',
            'props': {'value': 'history'},
            'content': [
                {'component': 'VExpansionPanelTitle',
                 'content': [{
                     'component': 'div',
                     'props': {'class': 'd-flex align-center ga-3 flex-grow-1'},
                     'content': [
                         {'component': 'span',
                          'props': {'class': 'text-body-1 font-weight-medium'},
                          'text': '自动订阅记录'},
                         {'component': 'VChip',
                          'props': {'size': 'x-small', 'variant': 'tonal',
                                    'color': 'primary'},
                          'text': f"{len(rows)} 条"},
                     ],
                 }]},
                {'component': 'VExpansionPanelText',
                 'props': {'class': 'pt-0'},
                 'content': body},
            ],
        }],
    }


def _section_title(title: str, hint: str = "") -> dict:
    """数据源分区标题行。"""
    content: List[dict] = [{'component': 'div',
                            'props': {'class': 'text-h6'},
                            'text': title}]
    if hint:
        content.append({'component': 'span',
                        'props': {'class': 'text-caption text-medium-emphasis'},
                        'text': hint})
    return {'component': 'div',
            'props': {'class': 'd-flex align-baseline flex-wrap ga-3 mb-2'},
            'content': content}


def build_page(host: Any) -> List[dict]:
    """拼装插件详情页：概览 → 两个数据源的可折叠榜单 → 订阅历史 → App 推荐。"""
    nodes: List[dict] = [_overview_card(host)]

    for source_id, title, label in (
            (SOURCE_MAOYAN, '猫眼榜单', '猫眼'),
            (SOURCE_NETFLIX, 'Netflix 榜单', 'Netflix')):
        source = host.sources.get(source_id)
        if source is None:
            continue
        nodes.append(_section_title(
            title, '点击任意榜单展开，展开另一个时自动收起'))
        if not getattr(source, "enabled", False):
            # 提示文案里的配置项名与设置页保持一致（「启用猫眼数据源」等）
            nodes.append({
                'component': 'VAlert',
                'props': {'type': 'warning', 'variant': 'tonal',
                          'class': 'mb-6',
                          'text': f"{title}数据源未启用：在插件设置里打开"
                                  f"「启用{label}数据源」"},
            })
            continue
        panels = _source_panels(source)
        if not panels:
            nodes.append({
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal',
                          'class': 'mb-6',
                          'text': '暂无榜单快照，点击上方「刷新」或等待定时任务运行'},
            })
            continue
        nodes.extend(panels)

    maoyan = host.sources.get(SOURCE_MAOYAN)
    if maoyan is not None and getattr(maoyan, "enabled", False):
        nodes.append(_section_title('订阅历史'))
        nodes.append(_history_panel(maoyan))

    nodes.append(promo_card())
    return nodes
