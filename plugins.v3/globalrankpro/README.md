# 全球影视榜单

把 **猫眼榜单**（原 [MaoyanPro](../maoyanpro/README.md)）、**Netflix 榜单**
（原 [NetflixPro](../netflixpro/README.md)）与 **红果短剧榜单** 合并成一个插件
（MoviePilot v3），对外只暴露一套统一接口。

## 为什么要合并

猫眼、Netflix 与红果的榜单数据源、抓取方式和条目字段都不同，但「榜单」这件事的消费方
（App 首页、宿主推荐页、脚本）只关心「有哪些榜、每个榜有哪些条目、条目对应哪部媒体」。
合并后：

- **一套接口**：`/rank`、`/rank-list`、`/refresh` 用 `source` 参数区分数据源，
  `/rating` 按标题批量查豆瓣评分（服务搜索结果页），`/poster` 转发红果海报
  （手机端不必直连字节 CDN），
  返回结构统一（三源字段取并集），调用方不必为每个数据源写一套解析；
- **一次安装**：不用同时装三个插件、配三套执行周期；
- **各自独立**：三个数据源在插件内部仍是独立对象（可分别开关、分别抓取），
  数据键带 `maoyan_` / `netflix_` / `hongguo_` 前缀，快照与识别缓存互不覆盖。

> 原 `MaoyanPro` / `NetflixPro` 插件本仓库仍保留并继续可用，接口与配置不变。
> 新部署建议直接用本插件。

## 数据源

| 数据源 | `source` | 内容 |
|---|---|---|
| 猫眼 | `maoyan` | 电影票房、网络电影、影史总票房、国内年度票房、全球年度票房（Box Office Mojo）、剧集综合榜（电视剧+网剧）、综艺热度；爱优腾芒平台细分榜 |
| Netflix | `netflix` | 官方 Tudum Top 10 静态 TSV：全球榜（四类目，含周观看小时数/观看次数/片长/在榜周数）+ 约 90 个国家/地区榜（仅名次），每周二更新 |
| 红果短剧 | `hongguo` | 红果短剧官网（hongguoduanju.com）SSR 榜单页：红果热播榜、真人剧热播榜、漫剧热播榜、AI 剧热播榜，每榜按 20 条一页翻页，含热度/评分/收藏/点赞、题材与状态标签、简介与前 3 集试看直链 |

⚠️ Netflix 未进入中国大陆，地区榜提供中国香港、中国台湾、美国、日本、韩国、新加坡选项
（官方数据文件实际覆盖 93 个国家/地区，白名单外的地区暂未开放）。

⚠️ 红果短剧**不做媒体识别也不提供订阅**：短剧基本不在 TMDB / 豆瓣里，强行走识别只会
得到错误匹配，条目直接用官网自带的中文标题与竖版海报；因此 `media_source` /
`media_id` 为空，仅用于榜单展示与推荐页选片。

## 插件页面

设置页（`get_form`）与详情页（`get_page`）顶部各有一张 **MoviePilotApp 推荐卡**：
左侧是 MoviePilot 官方 logo，右侧「下载 App」直达 Releases 最新版、旁边是 GitHub
仓库文字按钮。两个位置用同一个 `promo_card()` 渲染，与 MaoyanPro / NetflixPro /
MediaStreamPro 完全一致（实现在 `app_promo.py`，由 `tools/gen_app_promo.py` 生成，
四份逐字节相同）。详情页那张卡下面是三源概览卡，再往下才是猫眼订阅记录。

## 配置

设置页分四张卡：「运行」「猫眼榜单」「Netflix 榜单」「红果短剧榜单」，卡内一行放
1~3 个同类项，说明写在卡片副标题里，字段本身只留名字。

### 运行

| 配置项 | 说明 |
|---|---|
| 启用插件 | 开启后按周期抓取已启用的数据源 |
| 立即运行一次 | 保存后立即抓取一次，跑完自动取消勾选 |
| 执行周期 | Cron 表达式，默认 `0 9 * * *`（猫眼每日更新，Netflix 每周二发榜） |
| 推荐页取数授权 | 允许宿主推荐页通过本插件接口取榜单；关闭后推荐页取不到数据（**不影响** `/rank` 等接口与 App） |
| 接入推荐页 | 在宿主推荐页注册三源榜单，需与「推荐页取数授权」同时打开 |
| 清理插件数据 | 清空三源的快照、识别缓存与订阅历史，**不可恢复** |

### 猫眼

| 配置项 | 说明 |
|---|---|
| 启用猫眼数据源 | 关闭后不抓取猫眼榜单，接口也不返回猫眼数据 |
| 电影榜单 | 多选：电影票房、网络电影、影史总票房、国内年度、全球年度——勾选哪些就抓哪些 |
| 剧集/综艺榜单 | 多选：剧集综合（电视剧+网剧）、综艺热度 |
| 自动全网订阅 | 猫眼全网电影榜 / 猫眼全网剧集榜 / 猫眼全网综艺榜，各自「前 N 条」 |
| 平台专项榜 | 多选：腾讯视频 / 爱奇艺 / 芒果TV / 优酷——勾选哪些平台，哪些平台榜就抓取并展示 |
| 自动平台榜订阅 | 四个平台各自的「前 N 条」，默认全 0 |

三组多选（电影榜单、剧集/综艺榜单、平台专项榜）决定抓哪些榜并展示在详情页与接口里；
两组订阅条数各自取榜上前 N 条提交订阅，`0` = 只抓榜、不订阅。

> v1.0.3 曾把平台榜的 4 个 `maoyan_*_rec_enabled` 误判为无消费方从设置页移除，
> v1.0.4 起以 `maoyan_platforms` 多选回归；旧配置里的开关键仍能平滑迁移。
> 另有「各榜推荐开关」（`maoyan_*_rec_enabled` 主榜部分）与「自动订阅新上榜」
> （`maoyan_sub_enabled`）确实没有消费方，保持移除；这些键仍保留在配置里，
> 不影响旧配置读取。

### Netflix

| 配置项 | 说明 |
|---|---|
| 启用 Netflix 数据源 | 关闭后不抓取 Netflix 榜单，接口也不返回相关数据 |
| 榜单地区 | 多选，默认七地：全球、美国、日本、韩国、中国香港、中国台湾、新加坡；全球榜含观看量与在榜周数，地区榜只有名次 |

### 红果短剧

| 配置项 | 说明 |
|---|---|
| 启用红果数据源 | 关闭后不抓取红果榜单，接口也不返回相关数据 |
| 抓取榜单 | 多选：红果热播榜 / 真人剧热播榜 / 漫剧热播榜 / AI 剧热播榜——勾选哪些就抓哪些 |
| 每榜条数 | 20 / 40 / 60 / 100，按 20 条一页向上翻页；默认 20 |

海报固定经插件 `/poster` 接口转发，不提供开关：手机端只需能访问 MoviePilot，
不必直连字节 CDN。

## 对外接口

统一使用宿主的 `bear` 认证（接受 App 登录态 JWT、`X-API-KEY` 头与 `apikey` 查询参数）
与 `schemas.Response` 信封（`success/message/data` 三键结构）。唯一的例外是
`/poster`：图片标签带不了请求头，它按匿名接口注册。

### GET `/rank`

返回统一榜单快照。

| 参数 | 说明 |
|---|---|
| `source` | `maoyan` / `netflix` / `hongguo` / `all`（默认 `all`；非法值按 `all` 处理） |
| `board_type` | 逗号分隔多值。**按源语义解释**：猫眼取榜单类型（`movie`/`web-movie`/`web-all`/`zongyi`/`total`/`year`/`global`），Netflix 取地区代码（`global`/`hk`/`tw`/`us`/`jp`/`kr`/`sg`），红果取榜单代码（`hot`/`real`/`comic`/`ai`）。`source=all` 时各源各自匹配 |
| `kind` | `film` / `tv`（仅 Netflix 生效） |
| `limit` | 每个榜单最多返回的条目数 |
| `board` | 榜单名包含该关键字（仅猫眼生效，如「腾讯」） |
| `platform` | 猫眼 `platformType`（`3`=腾讯视频 `2`=爱奇艺 `7`=芒果TV `1`=优酷） |

```bash
# 三个数据源全部榜单
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>"

# 只看猫眼电影票房榜
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>&source=maoyan&board_type=movie"

# 只看 Netflix 中国香港电影榜
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>&source=netflix&board_type=hk&kind=film"

# 只看红果热播榜与漫剧热播榜
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>&source=hongguo&board_type=hot,comic"
```

响应 `data` 结构：

```json
{
  "updated_at": "2026-10-17 08:00:00",
  "week": "2026-10-04",
  "total": 4,
  "sources": ["maoyan", "netflix"],
  "boards": [
    {
      "source": "hongguo",
      "board": "红果热播榜",
      "board_type": "hot",
      "type": "hot", "type_name": "热播榜",
      "platform": "", "platform_type": "", "show_date": "",
      "region": "", "region_name": "", "kind": "",
      "category": "hot", "category_name": "热播榜", "week": "",
      "url": "https://hongguoduanju.com/rank/hot-drama", "count": 20,
      "updated_at": "2026-10-18 09:00:00",
      "items": [
        {
          "rank": 1, "title": "开局签到十万亿功德", "title_cn": "开局签到十万亿功德",
          "media_source": "", "media_id": "",
          "poster": "https://p3-novel.byteimg.com/novel-pic/xxx~tplv-shrink:1080:0.image",
          "overview": "一朝穿越", "year": "",
          "type": "", "type_name": "", "platform": "",
          "release_info": "", "show_date": "",
          "box_office": "", "total_box_office": "", "heat": "热度 2145万",
          "season_title": "", "hours_viewed": 0, "views": 0,
          "runtime": 0.0, "weeks_in_top10": 0, "release_date": "",
          "series_id": "7501234567890123456", "episode_cnt": 5,
          "score": "评分9.0", "favorite": "55.6万收藏", "likes": "137.2万点赞",
          "tags": "玄幻,逆袭,修真", "status_tags": "新剧,爆剧",
          "preview_url": "https://hongguoduanju.com/video/public/<vid>/360p.mp4,..."
        }
      ]
    },
    {
      "source": "maoyan",
      "board": "电影票房榜单",
      "board_type": "movie",
      "type": "movie",
      "type_name": "电影票房",
      "platform": "", "platform_type": "", "show_date": "2026-10-17",
      "region": "", "region_name": "", "kind": "", "category": "",
      "category_name": "", "week": "", "url": "...", "count": 10,
      "updated_at": "2026-10-17 08:00:00",
      "items": [
        {
          "rank": 1, "title": "生化危机：爆发夜", "title_cn": "",
          "media_source": "themoviedb", "media_id": "1586199",
          "poster": "...", "overview": "...", "year": "2026",
          "vote_average": 6.8,
          "type": "movie", "type_name": "电影票房", "platform": "院线",
          "release_info": "上映 11 天", "show_date": "2026-10-17",
          "box_office": "161.93万", "total_box_office": "1.87亿", "heat": "28.1%",
          "season_title": "", "hours_viewed": 0, "views": 0,
          "runtime": 0.0, "weeks_in_top10": 0, "release_date": ""
        }
      ]
    }
  ]
}
```

- `board_type` 是统一层的「榜单种类」：猫眼取 `type`，Netflix 取地区代码（全球榜为 `global`），红果取榜单代码；
- 条目字段是三源并集，与源无关的字段保持空值/0，调用方按 `source` 取自己关心的字段即可；
- `vote_average` 是**豆瓣评分**（0 表示暂无）：猫眼与 Netflix 的原始数据都不带评分，由插件抓取时
  用豆瓣检索补好（`j/subject_suggest` 拿词条 + `j/subject_abstract` 取 `rate`），结果按条目缓存
  30 天、单轮限量分摊；红果用自己的官方评分（`score` 字段），这里保持 0；
- `sources` 只包含本次真正返回了榜单的数据源；未启用的数据源会在 `message` 里说明。

### GET `/rank-list`

供宿主推荐页与浏览页消费，返回统一的 `schemas.MediaInfo` 列表。

| 参数 | 说明 |
|---|---|
| `source` | `maoyan`（默认）/ `netflix` / `hongguo` |
| `board_type` | 猫眼默认 `web-all`，Netflix 默认 `global`，红果不传则返回全部已抓取榜单 |
| `kind` | `film` / `tv`（仅 Netflix） |
| `page` | 页码（猫眼平台细分榜按页编组时有效） |
| `limit` | 返回条目上限 |
| `platform` | 猫眼 `platformType` |

### GET|POST `/refresh`

立即抓取，`source` 取 `maoyan` / `netflix` / `hongguo` / `all`（默认 `all`），
逐个抓取并汇总：

```json
{
  "refreshed": true,
  "board_count": 8,
  "item_count": 12,
  "subscribed": 0,
  "message": "maoyan：已抓取 2 个榜单 4 条；netflix：已抓取 6 个榜单 8 条",
  "sources": [
    {"source": "maoyan", "refreshed": true, "board_count": 2, "item_count": 4,
     "subscribed": 0, "week": "", "message": "..."},
    {"source": "netflix", "refreshed": true, "board_count": 6, "item_count": 8,
     "subscribed": 0, "week": "2026-10-04", "message": "..."}
  ]
}
```

单个数据源失败不会影响其它源：失败信息落在对应 `sources[].message`，
只要有一个源成功，外层 `success` 就是 `true`。

### GET `/rating`

按标题批量查**豆瓣评分**（App 搜索结果页用）。榜单快照里的 `vote_average` 只覆盖
上榜的几百条，搜索结果里绝大多数片子不在榜上，所以单独开了这个入口。

| 参数 | 说明 |
|---|---|
| `q` | JSON 数组，每项 `{"t": 标题, "y": 年份, "k": 类型}`（后两项可省）；`urlencode` 后传入 |
| `budget` | 本次最多请求豆瓣多少条（默认 10，上限 20）。豆瓣检索是**一条 2 个 HTTP**、全局 0.35s 节流，超出的条目留在下次再查 |

命中插件持久缓存（`douban_rating`，有分 30 天 / 未开分 3 天）立刻返回，不发外部请求；
未命中的才按 `budget` 去豆瓣检索。返回：

```json
{
  "success": true,
  "message": "得分 2 条（缓存 1，请求 1）",
  "data": {
    "rates": { "流人": "8.9", "海贼王": "9.3" },
    "cached": 1,
    "queried": 1,
    "blocked": false
  }
}
```

`rates` 只含查到的标题；`blocked=true` 表示豆瓣正在限流冷却，剩下的条目下次再试。
缓存随插件配置里的「清理数据」一起清。

```bash
curl -G "http://<host>:3000/api/v1/plugin/GlobalRankPro/rating?apikey=<token>" \
  --data-urlencode 'q=[{"t":"流人","y":"2026","k":"电视剧"}]'
```

### GET `/poster`

红果海报转发。**匿名可访问**（`<img>` 标签带不了请求头），供客户端直接引用。

| 参数 | 说明 |
|---|---|
| `u` | 红果官网的海报原图地址（`urlencode` 后） |

只放行字节图床（`*.byteimg.com/novel-pic/`、`*.ecombdimg.com/obj/`）的图片，
其余返回 `403`；取图失败返回
`502`。响应带 `Cache-Control: public, max-age=604800`，进程内另有 64 张的
LRU 缓存。

`/rank-list` 返回的 `poster_path` 就是本接口地址
（形如 `https://你的域名/api/v1/plugin/GlobalRankPro/poster?u=...`），
客户端只需要能访问 MoviePilot 本身。宿主既没配 `APP_DOMAIN`、又拿不到请求
地址时才会退回官网原图。

## 推荐页数据源

开启「推荐页数据源」+「开放榜单接口」后，按三个数据源的实际配置合并注册：

- 猫眼：`猫眼电影票房`、`猫眼影史总票房`、`国内年度票房`、`全球年度票房`、
  `猫眼剧集综合热度`、`猫眼综艺热度`，以及「平台专项榜」勾选了的平台各一个
  `猫眼<平台>剧集榜`（如 `猫眼腾讯视频剧集榜`、`猫眼爱奇艺剧集榜`、`猫眼芒果TV剧集榜`、
  `猫眼优酷剧集榜`，主榜为剧集综合榜）；
- Netflix：按「地区 × 电影/剧集」逐个注册，如 `Netflix 全球电影`；
- 红果短剧：按勾选的榜单逐个注册 `红果热播榜`、`红果真人剧热播榜`、
  `红果漫剧热播榜`、`红果AI 剧热播榜`，分类一律「电视剧」。

关掉某个数据源后，它对应的推荐源不再注册。

## 插件详情页

页面共三张卡，自上而下：

1. **MoviePilotApp 推荐卡**：App 名称、简介与 GitHub 入口；
2. **概览卡**：三个数据源各自的榜数、条数、更新时间与独立「刷新」按钮；
   未启用的源标「未启用」，已启用但还没抓过的标「暂无快照」；
3. **自动订阅记录**：猫眼订阅流水表格（时间倒序，含标题、类型、平台、
   上线信息、时间，TMDB 条目标题可点击跳转）。订阅记录由
   「自动全网订阅 / 自动平台榜订阅」产生，未开启时显示引导文案。

三个数据源的榜单条目**都不在页面平铺**——抓取结果经 `/rank`、`/rank-list` 供给
宿主推荐页与 App，页面只回答「抓到了多少、什么时候抓的、能不能订阅」。红果短剧
不在 TMDB / 豆瓣里、没有订阅入口，但它同样只走接口，页面不为此单开一块。

## 与旧插件的关系

| 旧插件 | 本插件对应 |
|---|---|
| `MaoyanPro` `/rank?board_type=movie` | `/rank?source=maoyan&board_type=movie` |
| `NetflixPro` `/rank?board_type=global&kind=film` | `/rank?source=netflix&board_type=global&kind=film` |
| `MaoyanPro` `/rank-list?board_type=web-all` | `/rank-list?source=maoyan&board_type=web-all` |
| `NetflixPro` `/rank-list?board_type=hk&kind=tv` | `/rank-list?source=netflix&board_type=hk&kind=tv` |
| `MaoyanPro` `/refresh` | `/refresh?source=maoyan` |
| `NetflixPro` `/refresh` | `/refresh?source=netflix` |

响应字段沿用旧插件的命名（`rank`/`title`/`title_cn`/`poster`/`release_info`/`box_office`
等），既有调用方换 URL 即可，解析逻辑不用改。

## 已知边界

- 猫眼的热度值是 PUA 字体加密，抓出来是乱码，剧集/综艺榜不展示热度值，改展示上线信息；
- 猫眼榜单接口有反爬（美团 H5guard），抓取依赖无头浏览器拿 Cookie，失败时保留上一次快照；
- Netflix 地区榜只有名次与在榜周数，没有观看量；全球榜才有观看小时数/次数/片长；
- 猫眼与 Netflix 走宿主识别链补全中文片名与海报，识别结果按源分别持久化，跨周复用；
- 红果官网只公开**前 3 集**的 **360p** 直链（`preview_url`），第 4 集起与更高清晰度
  均返回 404；海报源图上限 896×1280（缩略后缀已提到 1080，再大不会放大）；
- 红果海报是字节图床外链，桌面直连没问题，手机端（移动网络 / App 内 WebView）
  经常拿不到；海报一律经插件 `/poster` 代取，客户端不再依赖第三方 CDN；
- 红果官网没有公开榜单 API，抓的是 SSR 页面；官网同一个 URL 会随机返回两种渲染输出
  （数据分别挂在 `data-fn-name="r"` 与 `mergeLoaderData` 上），插件两种都认，
  页面结构真变了则保留上一次快照并在刷新结果里报错。
