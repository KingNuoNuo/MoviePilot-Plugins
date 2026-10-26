<div align="center">

# MoviePilot 插件仓库

**自建 MoviePilot v3 插件市场源** —— MoviePilotApp必备插件源。

[![Stars](https://img.shields.io/github/stars/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins/stargazers)
[![Forks](https://img.shields.io/github/forks/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins/network/members)
[![Watchers](https://img.shields.io/github/watchers/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins/watchers)
[![Issues](https://img.shields.io/github/issues/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins/issues)
[![License](https://img.shields.io/github/license/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins/commits/main)
[![Top Language](https://img.shields.io/github/languages/top/KingNuoNuo/MoviePilot-Plugins?style=flat&color=4F46E5)](https://github.com/KingNuoNuo/MoviePilot-Plugins)

[📦 插件市场安装](#install-market) · [🧩 插件一览](#插件一览) · [🐛 反馈问题](https://github.com/KingNuoNuo/MoviePilot-Plugins/issues) · [💡 功能建议](https://github.com/KingNuoNuo/MoviePilot-Plugins/issues)

`MoviePilot` `MoviePilot Plugin` `TgtoDrive` `STRM` `Media Server` `Emby` `Jellyfin` `Plex` `Netdisk` `NAS` `Self-hosted` `Maoyan` `Ranking` `Python`

</div>

---

## 插件一览

本仓库同时提供 v2/v3 两代插件清单，添加为自定义市场源即可同时看到全部插件。
**点插件名进入该插件的独立说明页**（安装、配置、接口与已知边界都在里面）。

| 插件 | 版本 | 简介 | 说明 |
| --- | --- | --- | --- |
| **[TgtoDrive STRM 同步](plugins/tgtodrivesync/README.md)**<br><sub>`TgtoDriveSync` · `plugins/tgtodrivesync`</sub> | v1.8.1 | 整理入库后自动调用 TgtoDrive OpenAPI 生成网盘 STRM，智能等待生成后刷新全部已配置媒体服务器并发送通知。 | [→ 插件说明](plugins/tgtodrivesync/README.md) |
| **[全球影视音乐榜](plugins.v3/globalrankpro/README.md)**<br><sub>`GlobalRankPro` · `plugins.v3/globalrankpro`</sub> | v1.5.3 | 把猫眼榜单、Netflix 榜单与红果短剧榜单合并为一个插件，对外只暴露一套统一接口：`/rank`（快照，`source=maoyan/netflix/hongguo/all`，三源字段取并集、`board_type` 按源语义过滤）、`/rank-list`（统一媒体信息列表）、`/refresh`（按 `source` 抓取并汇总）；三源在插件内各自独立（数据键带 `maoyan_`/`netflix_`/`hongguo_` 前缀），可分别开关与抓取；红果抓官网 SSR 榜单页（四个官方热播榜，无需签名），短剧不在 TMDB/豆瓣里故不做识别与订阅；设置页同页配置三源，详情页只报三源抓取状态，推荐页数据源合并注册。 | [→ 插件说明](plugins.v3/globalrankpro/README.md) |
| **[媒体流播放](plugins.v3/mediastreampro/README.md)**<br><sub>`MediaStreamPro` · `plugins.v3/mediastreampro`</sub> | v1.5.8 | 把已整理入库与下载目录里的视频按原始文件流式转发给客户端，让 MoviePilotApp 的「下载 → 整理记录」点开就播、能拖进度、能选集：`GET /stream?path=…` 返回带 HTTP Range 的文件流，`HEAD /stream` 只回头供外部播放器探路，`GET /season?path=…` 扫同目录返回**同一季的集清单**（按「剧名主干 + 季号」认亲，跟下载记录和种子 hash 都无关），`GET /ping` 供客户端探测。v1.4.0 起 **有 STRM 就优先走 STRM**：`.strm` 文件（或 STRM 目录里能对上同名 `.strm` 的影片，本地文件存在与否）读出网盘直链后 **302 跳转**，流量不过 NAS；STRM 目录可自己填，也可开「跟随 TgtoDrive 插件」直接用 TgtoDrive STRM 同步里配置的目录。v1.5.0 起**默认替客户端解析直链**：STRM 直链里的容器名 `TgtoDrive` 手机与容器都解析不了，插件先走完 302 跳转链、把最终公网 CDN 地址给手机，内外网都能播。v1.5.1 起新增 `POST /strm-check` 批量判断媒体路径是否对上同名 `.strm`，供 App 给 STRM 影片打标识。放行的媒体库与下载根目录每次请求实时读宿主「设置 → 目录」（宿主改目录即刻生效），生效的 STRM 目录一并放行；路径经 `realpath` 归一后按「根目录 + 分隔符」比较挡住前缀串与 `..` 穿越，扩展名限定视频容器或 `.strm`；认证沿用宿主 bear 模式，不支持把 token 拼进 URL。 | [→ 插件说明](plugins.v3/mediastreampro/README.md) |

> **来源说明**：`全球影视榜单` 里的猫眼榜部分 **Fork 自 [jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins)**
> 的「猫眼榜单订阅」插件（原作者 [逗猫 @baozaodetudou](https://github.com/baozaodetudou)），基于上游 v4.1.4 改造后
> 并入本插件（作者署名与版本号均已独立），详见
> [插件说明](plugins.v3/globalrankpro/README.md#来源)。

**安装**：MoviePilot →「设定 → 插件市场 → 添加市场」，填入仓库地址 `https://github.com/KingNuoNuo/MoviePilot-Plugins`，保存后在市场内搜索插件名安装。

**其它链接**：[打包产物](#packages)（直接下载压缩包） · [反馈问题 / 功能建议](https://github.com/KingNuoNuo/MoviePilot-Plugins/issues)

---

## 安装

<a id="install-market"></a>

1. MoviePilot →「设定 → 插件市场 → 添加市场」，填入本仓库地址：

   ```
   https://github.com/KingNuoNuo/MoviePilot-Plugins
   ```

2. 保存后，在市场里搜索插件名（如「**TgtoDrive STRM 同步**」「**全球影视榜单**」「**媒体流播放**」）并安装、启用。

本地部署也可把插件目录整个拷贝到 MoviePilot 的插件目录（容器内通常为 `/app/app/plugins/`，
以你的实际部署为准），重启 MoviePilot 容器即可在插件列表看到：

- TgtoDrive → `plugins/tgtodrivesync`
- 全球影视榜单 → `plugins.v3/globalrankpro`
- 媒体流播放 → `plugins.v3/mediastreampro`

## 仓库结构

```
MoviePilot-Plugins/
├── package.json                 # V1/V2 插件清单（市场识别）
├── package.v3.json              # V3 插件清单（市场识别）
├── plugins/                     # v2 代插件目录
│   └── tgtodrivesync/
│       ├── __init__.py          # 插件主体（类名 TgtoDriveSync）
│       ├── app_promo.py         # MoviePilotApp 推荐卡（tools/gen_app_promo.py 生成）
│       └── README.md            # 插件说明
├── plugins.v3/                  # v3 代插件目录
│   ├── globalrankpro/           # 全球影视榜单（猫眼 / Netflix / 红果 / 音乐）
│   │   ├── __init__.py          # 插件主体（类名 GlobalRankPro）
│   │   ├── base.py              # 数据源基类与快照结构
│   │   ├── maoyan.py            # 猫眼榜抓取解析
│   │   ├── netflix.py           # Netflix 榜抓取解析
│   │   ├── hongguo.py           # 红果榜抓取解析
│   │   ├── music.py             # 音乐榜（三平台）抓取解析
│   │   ├── douban.py            # 豆瓣评分查询与限流熔断
│   │   ├── page.py              # 插件详情页
│   │   └── README.md            # 插件说明
│   └── mediastreampro/          # 媒体流播放
│       ├── __init__.py          # 插件主体（类名 MediaStreamPro）
│       └── README.md            # 插件说明
├── tests/                       # 插件测试（按 tests/v3/<插件id>/ 组织）
└── dist/                        # 打包产物
```

> MoviePilot v3 会同时扫描 `package.json` 与 `package.v3.json`（`plugins/` 与 `plugins.v3/`），
> 同名插件按版本号取高者，因此两代插件可以共存在同一个仓库里。
> **插件目录名必须是小写的插件类名**（如 `GlobalRankPro` → `plugins.v3/globalrankpro/`）。

## 打包产物

<a id="packages"></a>

`dist/` 目录下提供可直接使用的压缩包：

| 文件 | 说明 |
| --- | --- |
| `MoviePilot-Plugins.zip` | 完整插件仓库结构（`package.json` + `package.v3.json` + `plugins/` + `plugins.v3/`），解压即可推 Git 或当市场源 |
| `tgtodrivesync.zip` | 仅 TgtoDrive 插件目录，解压后把 `tgtodrivesync/` 放进 MoviePilot 插件目录 |
| `globalrankpro.zip` | 仅全球影视榜单插件目录，解压后把 `globalrankpro/` 放进 MoviePilot 插件目录 |
| `mediastreampro.zip` | 仅媒体流播放插件目录，解压后把 `mediastreampro/` 放进 MoviePilot 插件目录 |

> 打包产物由 `python scripts/build_dist.py` 重建。

## 许可

本仓库以 [GNU General Public License v3.0](LICENSE)（GPL-3.0）发布。

`全球影视榜单` 的猫眼榜部分派生自 [jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins)（GPL-3.0），因此本仓库包含 GPL-3.0 代码，遵循同一许可。这些插件又在 MoviePilot 宿主进程内运行、直接调用宿主模块（宿主同为 GPL-3.0），从许可角度同样要求以 GPL 兼容的方式分发。

---

# English

A self-hosted [MoviePilot](https://github.com/jxxghp/MoviePilot) plugin market source with two plugins.
Add `https://github.com/KingNuoNuo/MoviePilot-Plugins` as a custom market source, then install any plugin by name.
**Click a plugin name below for its own documentation page.**

| Plugin | Version | What it does | Docs |
| --- | --- | --- | --- |
| **TgtoDrive STRM Sync** (`TgtoDriveSync`) | v1.8.1 | Calls the official TgtoDrive OpenAPI when MoviePilot finishes transferring files (`TransferComplete`) to generate cloud-drive STRM links, optionally polls a mounted STRM directory until the new files appear, then refreshes every configured media server and sends a notification. | [→ Docs](plugins/tgtodrivesync/README.md) |
| **GlobalRankPro** (`GlobalRankPro`) | v1.5.3 | Unified ranking boards plugin: Maoyan box-office/popularity boards, Netflix Tudum Top 10 per region, Hongguo short-drama boards and music boards (NetEase / QQ Music / Apple Music) behind one set of endpoints — `/rank` (snapshot), `/rank-list` (unified media list) and `/refresh`. Each source can be enabled and refreshed independently. | [→ Docs](plugins.v3/globalrankpro/README.md) |

> **Origin**: the Maoyan board part of `GlobalRankPro` is derived from
> [jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins)
> (original author [@baozaodetudou](https://github.com/baozaodetudou)), re-released as part of this
> plugin with its own authorship and versioning.

> Licensed under the [GNU General Public License v3.0](LICENSE).

<!-- traffic:start -->

## 📊 访问统计

![近 14 天访问趋势](docs/traffic.svg)

| 指标 | 近 14 天 |
| --- | ---: |
| 页面浏览量 | 138 |
| 独立访客 | 43 |
| 克隆次数 | 1076 |
| 独立克隆者 | 363 |

<sub>数据窗口 2026-10-12 ~ 2026-10-25 · 由 GitHub Actions 每日更新于 2026-10-26 · 来源：GitHub Traffic API（仅统计最近 14 天）</sub>

<!-- traffic:end -->
