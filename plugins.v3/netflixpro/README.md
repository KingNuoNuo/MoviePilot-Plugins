# Netflix榜单

Netflix 官方 Tudum Top 10 榜单抓取与展示插件（MoviePilot v3）。

## 数据源

数据源为 Netflix 官方发布的静态 TSV 文件（免费、无鉴权，每周二更新一次全量历史）：

| 文件 | 地址 | 内容 |
|---|---|---|
| 全球榜 | `https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv` | 275 周全量历史；四类目（Films/TV × English/Non-English）各 10 条；含周观看小时数、观看次数、片长、在榜周数 |
| 国家/地区榜 | `https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv` | 约 90 个国家/地区，每区 Films/TV 各 10 条；仅名次与在榜周数，无观看量 |

⚠️ Netflix 未进入中国大陆，地区榜提供中国香港、中国台湾、日本、韩国、新加坡选项。

## 配置

| 配置项 | 说明 |
|---|---|
| 启用插件 | 开启后按周期自动抓取榜单 |
| 立即运行一次 | 保存后立即抓取一次 |
| 执行周期 | Cron 表达式，默认 `0 9 * * 3`（每周三 09:00，Netflix 每周二发榜） |
| 抓取地区 | 多选，默认「全球 + 中国香港 + 中国台湾」 |
| 开放榜单接口 | 开启后 `/rank`、`/rank-list` 可被 App 与脚本调用 |
| 推荐页数据源 | 在宿主推荐页展示 Netflix 榜单（需同时开放接口） |
| 清理插件数据 | 清空榜单快照与识别缓存 |

## 媒体识别

榜单条目只有英文片名，抓取后统一走宿主识别链补全中文片名、海报、年份与发行日期。
识别结果按「内容类型|标题」持久化到 `identity_cache`，跨周复用；Netflix 单周全量仅
百余条，顺序识别即可。识别不到的条目保留英文原名，不影响其余数据。

## 对外接口

统一使用宿主的 `bear` 认证（接受 App 登录态 JWT、`X-API-KEY` 头与 `apikey` 查询参数）
与 `schemas.Response` 信封（`success/message/data` 三键结构）。

### GET `/rank`

返回最近一次抓取的榜单快照。

- `board_type`：地区代码，`global`（默认）为全球榜，`hk`/`tw`/`jp`/`kr`/`sg` 为地区榜
- `kind`：`film` 仅电影、`tv` 仅剧集，不传返回该地区全部类目

```bash
curl "http://<host>:3000/api/v1/plugin/NetflixPro/rank?apikey=<token>&board_type=global&kind=film"
```

### GET `/rank-list`

供宿主推荐页与浏览页消费的媒体信息列表（扁平，电影在前、剧集在后，各榜按名次）。

- `board_type`：地区代码，默认 `global`
- `kind`：`film`/`tv`，不传返回全部
- `page`/`limit`：宿主列表视图翻页与条数控制

### GET|POST `/refresh`

立即触发一次抓取与媒体识别，返回抓取统计。

## 推荐页数据源

推荐开关开启时，按「地区 × 内容类型」注册数据源（如「Netflix 全球电影」
「Netflix 中国香港剧集」），与其它插件的数据源叠加而非替换。

## 插件详情页

宿主插件管理页点开插件即可看到详情页（`get_page`）：最顶部是 MoviePilotApp 推荐卡
（官方 logo + 「下载 App」直达 Releases + GitHub 文字按钮，与设置页 `get_form`
用同一个 `promo_card()` 渲染），其下是实时榜单区，直接读快照，按「地区 × 类目」
分区展示（名次角标前三主色、中文片名优先、季标题、观看量格式化），右上角
「刷新榜单」按钮触发一次立即抓取。
