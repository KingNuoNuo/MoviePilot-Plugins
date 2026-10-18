"""插件设置表单与详情页的节点构造。

合并插件要在一个设置页里放下「猫眼」「Netflix」「红果短剧」三套配置、在一个详情页里
同时展示三套数据的状态，因此把节点构造从插件主类里拆出来：这里只做纯 UI 拼装，
抓取与存储一律通过传入的 ``host`` 与数据源对象完成。

详情页只保留「订阅记录」这一类数据：三个数据源的榜单条目本身都通过 /rank、/rank-list
提供给推荐页与 App 消费，不在页面上重复平铺；页面只回答「抓到了多少、什么时候抓的、
能不能订阅」——各源的状态与刷新入口收在概览卡里。红果短剧不在 TMDB / 豆瓣里、没有
订阅入口，它抓到的内容同样只走接口，页面不另开一块铺榜。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from app.schemas import MediaSource, MediaType
from app.sdk.media import normalize_media_source

from .base import SOURCE_HONGGUO, SOURCE_MAOYAN, SOURCE_NETFLIX

# 详情页底部/设置页顶部共用的 App 推荐卡内容
APP_PROMO = {
    'name': 'MoviePilotApp',
    'subtitle': 'Jetpack Compose · Material 3 安卓客户端',
    'desc': '首页榜单 · 媒体订阅 · 下载整理，随时随地管理媒体库；'
            '猫眼、Netflix 与红果短剧榜单共用一套接口',
    'repo': 'https://github.com/KingNuoNuo/MoviePilotApp',
}

# 红果每榜抓取条数档位（官网每页 20 条）
HONGGUO_LIMIT_CHOICES = (20, 40, 60, 100)

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
            # d-flex 里 .v-input 默认 flex:1 1 auto，会把限宽撑回整列
            'style': {'width': '{0}px'.format(width), 'flex': '0 0 auto'},
        },
    }


def limit_select(model: str, choices=(), width: int = 100) -> dict:
    """通用小号数值下拉（红果每榜抓取条数这类自由档位）。"""
    values = list(choices) or list(HONGGUO_LIMIT_CHOICES)
    return {
        'component': 'VSelect',
        'props': {
            'model': model,
            'items': [{'title': '{0} 条'.format(n), 'value': n} for n in values],
            'density': 'compact',
            'variant': 'outlined',
            'hide-details': True,
            # d-flex 里 .v-input 默认 flex:1 1 auto，会把限宽撑回整列
            'style': {'width': '{0}px'.format(width), 'flex': '0 0 auto'},
        },
    }


def num_group(title: str, items: List[Tuple[str, str]]) -> dict:
    """一行「组名 + 榜名 + 条数框」，窄屏时用 flex-wrap 自动折行。"""
    content: List[dict] = [{
        'component': 'span',
        'props': {'class': 'text-body-2', 'style': {'min-width': '112px'}},
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

    排版按「四张卡 + 一行多项」组织：运行 / 猫眼榜单 / Netflix 榜单 / 红果短剧榜单
    各一张卡，卡内每行放 1~3 个同类字段，说明统一收进卡片描述，字段只留名字——
    原来每个字段都拖一行 hint，行高参差且页面被拉得很长。

    每个榜单的「抓取开关」与「订阅条数」不再上下堆叠：宿主的 switch 高度会塌缩，
    堆叠时必然压住下面的下拉框；订阅条数改成一行并排的小号下拉。
    """
    netflix = host.sources.get(SOURCE_NETFLIX)
    regions = list(getattr(netflix, "_regions", ['global', 'hk', 'tw']))
    region_names = getattr(netflix, "_REGION_NAMES", {}) or {}
    region_items = [{'title': name, 'value': code}
                    for code, name in region_names.items()]
    maoyan = host.sources.get(SOURCE_MAOYAN)
    platform_items = [
        {'title': label, 'value': code}
        for code, label in getattr(maoyan, "_PLATFORM_LABELS",
                                   {'tx': '腾讯视频', 'iqy': '爱奇艺',
                                    'mg': '芒果TV', 'yk': '优酷'}).items()
    ]
    hongguo = host.sources.get(SOURCE_HONGGUO)
    hongguo_items = [
        {'title': name, 'value': code}
        for code, name in getattr(hongguo, "_BOARD_NAMES", {}).items()
    ]

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
            '「电影/剧集综艺榜单」与「平台专项榜」多选决定抓哪些榜并展示在详情页与接口里；'
            '「自动全网订阅」「自动平台榜订阅」各自取榜上前 N 条提交订阅，'
            '0 表示只抓榜、不订阅。',
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
                num_group('自动全网订阅', [
                    ('猫眼全网电影榜', 'maoyan_movie_sub_num'),
                    ('猫眼全网剧集榜', 'maoyan_tv_sub_num'),
                    ('猫眼全网综艺榜', 'maoyan_zongyi_sub_num'),
                ]),
                form_row([type_select('maoyan_platforms', '平台专项榜',
                                      platform_items, 12)]),
                num_group('自动平台榜订阅', [
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
        setting_card(
            '红果短剧榜单',
            '红果短剧官网热播榜（SSR 直取，无需签名）。短剧不在 TMDB / 豆瓣里，'
            '本源不做媒体识别也不提供订阅，只用于榜单展示与推荐页选片；'
            '「每榜抓取条数」按 20 一页向上翻页。'
            '「海报经宿主转发」把海报地址换成插件自己的 /poster 接口：'
            '手机端不必直连字节 CDN，关掉则回到官网原图地址。',
            [
                form_row([
                    switch_col('hongguo_enabled', '启用红果数据源', 4),
                    switch_col('hongguo_poster_proxy', '海报经宿主转发', 8),
                ]),
                form_row([
                    type_select('hongguo_boards', '抓取榜单', hongguo_items, 8),
                    form_col_nodes([{
                        'component': 'div',
                        'props': {'class': 'd-flex align-center ga-2 pt-1'},
                        'content': [
                            {'component': 'span',
                             'props': {'class': 'text-body-2'},
                             'text': '每榜条数'},
                            limit_select('hongguo_limit'),
                        ],
                    }], 4),
                ], cls='align-start'),
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
        'maoyan_platforms': [],
        # Netflix
        'netflix_enabled': True,
        'netflix_regions': regions,
        # 红果短剧
        'hongguo_enabled': True,
        'hongguo_boards': list(getattr(hongguo, "_boards", ['hot'])),
        'hongguo_limit': int(getattr(hongguo, "_limit", 20) or 20),
        'hongguo_poster_proxy': True,
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


def _tmdb_href(media_type, tmdb_id) -> str:
    """根据媒体类型生成 TMDB 链接，缺少 ID 时返回空串。"""
    clean_id = _safe_text(tmdb_id)
    if not clean_id:
        return ""
    if str(media_type) == MediaType.TV.value:
        return f"https://www.themoviedb.org/tv/{clean_id}"
    return f"https://www.themoviedb.org/movie/{clean_id}"


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
    """概览卡：三个数据源的榜数、条数、更新时间与独立刷新。"""
    rows: List[dict] = []
    for source_id, label, icon in (
            (SOURCE_MAOYAN, '猫眼榜单', 'mdi-movie-open-outline'),
            (SOURCE_NETFLIX, 'Netflix 榜单', 'mdi-television-classic'),
            (SOURCE_HONGGUO, '红果短剧榜单', 'mdi-filmstrip')):
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
        'props': {'variant': 'outlined', 'class': 'mb-3'},
        'content': [{
            'component': 'div',
            'props': {'style': {'padding': '6px 16px'}},
            'content': content,
        }],
    }


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


def _history_card(source: Any) -> dict:
    """订阅记录卡：表格直接铺开，页面只剩这一块数据时不再折叠。"""
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
                 'props': {'class': 'text-center pa-4 text-medium-emphasis',
                           'style': {'padding': '12px 0 4px'}},
                 'text': '暂无订阅记录——打开「自动全网订阅」或「自动平台榜订阅」'
                         '的条数后，新上榜的条目会记在这里'}]

    return {
        'component': 'VCard',
        'props': {'variant': 'outlined'},
        'content': [
            {
                'component': 'div',
                'props': {'class': 'd-flex align-center ga-3',
                          'style': {'padding': '12px 16px 0'}},
                'content': [
                    {'component': 'span',
                     'props': {'class': 'text-subtitle-1 font-weight-medium'},
                     'text': '自动订阅记录'},
                    {'component': 'VChip',
                     'props': {'size': 'x-small', 'variant': 'tonal',
                               'color': 'primary'},
                     'text': "{0} 条".format(len(rows))},
                ],
            },
            {
                'component': 'div',
                'props': {'style': {'padding': '2px 8px 8px'}},
                'content': body,
            },
        ],
    }


def build_page(host: Any) -> List[dict]:
    """拼装插件详情页：App 推荐 → 概览 → 订阅记录。

    三个数据源的榜单条目都不在页面上平铺——抓取结果经 /rank、/rank-list 供给
    推荐页与 App，页面只回答「抓到了多少、什么时候抓的」（概览卡）与「订阅了什么」
    （猫眼订阅记录），刷新入口也在概览卡里。红果短剧不在 TMDB / 豆瓣里、没有订阅
    入口，但它同样只走接口，页面不为此单开一块。
    """
    maoyan = host.sources.get(SOURCE_MAOYAN)
    nodes: List[dict] = [
        promo_card(),
        _overview_card(host),
    ]
    if maoyan is not None:
        nodes.append(_history_card(maoyan))
    return nodes
