# 全球影视榜单

把 **猫眼榜单**（原 [MaoyanPro](../maoyanpro/README.md)）与 **Netflix 榜单**
（原 [NetflixPro](../netflixpro/README.md)）合并成一个插件（MoviePilot v3），
对外只暴露一套统一接口。

## 为什么要合并

猫眼与 Netflix 的榜单数据源、抓取方式和条目字段都不同，但「榜单」这件事的消费方
（App 首页、宿主推荐页、脚本）只关心「有哪些榜、每个榜有哪些条目、条目对应哪部媒体」。
合并后：

- **一套接口**：`/rank`、`/rank-list`、`/refresh` 用 `source` 参数区分数据源，
  返回结构统一（两源字段取并集），调用方不必为每个数据源写一套解析；
- **一次安装**：不用同时装两个插件、配两套执行周期；
- **各自独立**：两个数据源在插件内部仍是独立对象（可分别开关、分别抓取），
  数据键带 `maoyan_` / `netflix_` 前缀，快照与识别缓存互不覆盖。

> 原 `MaoyanPro` / `NetflixPro` 插件本仓库仍保留并继续可用，接口与配置不变。
> 新部署建议直接用本插件。

## 数据源

| 数据源 | `source` | 内容 |
|---|---|---|
| 猫眼 | `maoyan` | 电影票房、网络电影、影史总票房、国内年度票房、全球年度票房（Box Office Mojo）、剧集综合榜（电视剧+网剧）、综艺热度；爱优腾芒平台细分榜 |
| Netflix | `netflix` | 官方 Tudum Top 10 静态 TSV：全球榜（四类目，含周观看小时数/观看次数/片长/在榜周数）+ 约 90 个国家/地区榜（仅名次），每周二更新 |

⚠️ Netflix 未进入中国大陆，地区榜提供中国香港、中国台湾、美国、日本、韩国、新加坡选项
（官方数据文件实际覆盖 93 个国家/地区，白名单外的地区暂未开放）。

## 配置

设置页分三张卡：「运行」「猫眼榜单」「Netflix 榜单」，卡内一行放 1~3 个同类项，
说明写在卡片副标题里，字段本身只留名字。

### 运行

| 配置项 | 说明 |
|---|---|
| 启用插件 | 开启后按周期抓取已启用的数据源 |
| 立即运行一次 | 保存后立即抓取一次，跑完自动取消勾选 |
| 执行周期 | Cron 表达式，默认 `0 9 * * *`（猫眼每日更新，Netflix 每周二发榜） |
| 推荐页取数授权 | 允许宿主推荐页通过本插件接口取榜单；关闭后推荐页取不到数据（**不影响** `/rank` 等接口与 App） |
| 接入推荐页 | 在宿主推荐页注册两源榜单，需与「推荐页取数授权」同时打开 |
| 清理插件数据 | 清空两源的快照、识别缓存与订阅历史，**不可恢复** |

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

## 对外接口

统一使用宿主的 `bear` 认证（接受 App 登录态 JWT、`X-API-KEY` 头与 `apikey` 查询参数）
与 `schemas.Response` 信封（`success/message/data` 三键结构）。

### GET `/rank`

返回统一榜单快照。

| 参数 | 说明 |
|---|---|
| `source` | `maoyan` / `netflix` / `all`（默认 `all`；非法值按 `all` 处理） |
| `board_type` | 逗号分隔多值。**按源语义解释**：猫眼取榜单类型（`movie`/`web-movie`/`web-all`/`zongyi`/`total`/`year`/`global`），Netflix 取地区代码（`global`/`hk`/`tw`/`us`/`jp`/`kr`/`sg`）。`source=all` 时两边各自匹配 |
| `kind` | `film` / `tv`（仅 Netflix 生效） |
| `limit` | 每个榜单最多返回的条目数 |
| `board` | 榜单名包含该关键字（仅猫眼生效，如「腾讯」） |
| `platform` | 猫眼 `platformType`（`3`=腾讯视频 `2`=爱奇艺 `7`=芒果TV `1`=优酷） |

```bash
# 两个数据源全部榜单
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>"

# 只看猫眼电影票房榜
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>&source=maoyan&board_type=movie"

# 只看 Netflix 中国香港电影榜
curl "http://<host>:3000/api/v1/plugin/GlobalRankPro/rank?apikey=<token>&source=netflix&board_type=hk&kind=film"
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

- `board_type` 是统一层的「榜单种类」：猫眼取 `type`，Netflix 取地区代码（全球榜为 `global`）；
- 条目字段是两源并集，与源无关的字段保持空值/0，调用方按 `source` 取自己关心的字段即可；
- `sources` 只包含本次真正返回了榜单的数据源；未启用的数据源会在 `message` 里说明。

### GET `/rank-list`

供宿主推荐页与浏览页消费，返回统一的 `schemas.MediaInfo` 列表。

| 参数 | 说明 |
|---|---|
| `source` | `maoyan`（默认）/ `netflix` |
| `board_type` | 猫眼默认 `web-all`，Netflix 默认 `global` |
| `kind` | `film` / `tv`（仅 Netflix） |
| `page` | 页码（猫眼平台细分榜按页编组时有效） |
| `limit` | 返回条目上限 |
| `platform` | 猫眼 `platformType` |

### GET|POST `/refresh`

立即抓取，`source` 取 `maoyan` / `netflix` / `all`（默认 `all`），逐个抓取并汇总：

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

单个数据源失败不会影响另一个：失败信息落在对应 `sources[].message`，
只要有一个源成功，外层 `success` 就是 `true`。

## 推荐页数据源

开启「推荐页数据源」+「开放榜单接口」后，按两个数据源的实际配置合并注册：

- 猫眼：`猫眼电影票房`、`猫眼影史总票房`、`国内年度票房`、`全球年度票房`、
  `猫眼剧集综合热度`、`猫眼综艺热度`，以及「平台专项榜」勾选了的平台各一个
  `猫眼<平台>剧集榜`（如 `猫眼腾讯视频剧集榜`、`猫眼爱奇艺剧集榜`、`猫眼芒果TV剧集榜`、
  `猫眼优酷剧集榜`，主榜为剧集综合榜）；
- Netflix：按「地区 × 电影/剧集」逐个注册，如 `Netflix 全球电影`。

关掉某个数据源后，它对应的推荐源不再注册。

## 插件详情页

页面只保留三张卡，自上而下：

1. **MoviePilotApp 推荐卡**：App 名称、简介与 GitHub 入口；
2. **概览卡**：两个数据源的榜数、条数、更新时间与独立「刷新」按钮；
   抓取结果不在页面平铺——榜单数据经 `/rank`、`/rank-list` 供给宿主推荐页与 App；
3. **自动订阅记录**：猫眼订阅流水表格（时间倒序，含标题、类型、平台、
   上线信息、时间，TMDB 条目标题可点击跳转）。订阅记录由
   「自动全网订阅 / 自动平台榜订阅」产生，未开启时显示引导文案。

Netflix 无订阅语义，订阅记录卡只展示猫眼来源。

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
- 两源都走宿主识别链补全中文片名与海报，识别结果按源分别持久化，跨周复用。
