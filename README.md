<div align="center">

# MoviePilot 插件仓库

**自建 MoviePilot v3 插件市场源** —— 整理入库自动生成网盘 STRM 并刷新媒体库；猫眼榜单订阅与榜单对外接口。

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
| **[TgtoDrive STRM 同步](plugins/tgtodrivesync/README.md)**<br><sub>`TgtoDriveSync` · `plugins/tgtodrivesync`</sub> | v1.7.1 | 整理入库后自动调用 TgtoDrive OpenAPI 生成网盘 STRM，完成后刷新全部已配置媒体服务器并发送通知。 | [→ 插件说明](plugins/tgtodrivesync/README.md) |
| **[猫眼榜单订阅](plugins.v3/maoyanpro/README.md)**<br><sub>`MaoyanPro` · `plugins.v3/maoyanpro`</sub> | v1.1.1 | 抓取猫眼电影票房、剧集综合榜（电视剧+网剧）与综艺热度榜单并自动订阅（电影/剧集分开配置，订阅与推荐页数据源相互独立）；提供 `/rank`、`/rank-list`、`/refresh` 对外接口，可作为宿主推荐页数据源。 | [→ 插件说明](plugins.v3/maoyanpro/README.md) |

> **来源说明**：`猫眼榜单订阅` **Fork 自 [jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins)**
> 的同名插件（原作者 [逗猫 @baozaodetudou](https://github.com/baozaodetudou)），基于上游 v4.1.4 改造为
> **独立发行版**（插件 ID `MaoyanPro`），作者署名与版本号（自 **v1.0.0** 重新计数）均已独立，详见
> [插件说明](plugins.v3/maoyanpro/README.md#来源)。

**安装**：MoviePilot →「设定 → 插件市场 → 添加市场」，填入仓库地址 `https://github.com/KingNuoNuo/MoviePilot-Plugins`，保存后在市场内搜索插件名安装。

**其它链接**：[打包产物](#packages)（直接下载压缩包） · [反馈问题 / 功能建议](https://github.com/KingNuoNuo/MoviePilot-Plugins/issues)

---

## 安装

<a id="install-market"></a>

1. MoviePilot →「设定 → 插件市场 → 添加市场」，填入本仓库地址：

   ```
   https://github.com/KingNuoNuo/MoviePilot-Plugins
   ```

2. 保存后，在市场里搜索插件名（如「**TgtoDrive STRM 同步**」「**猫眼榜单订阅**」）并安装、启用。

本地部署也可把插件目录整个拷贝到 MoviePilot 的插件目录（容器内通常为 `/app/app/plugins/`，
以你的实际部署为准），重启 MoviePilot 容器即可在插件列表看到：

- TgtoDrive → `plugins/tgtodrivesync`
- 猫眼榜单 → `plugins.v3/maoyanpro`

## 仓库结构

```
MoviePilot-Plugins/
├── package.json                 # V1/V2 插件清单（市场识别）
├── package.v3.json              # V3 插件清单（市场识别）
├── plugins/                     # v2 代插件目录
│   └── tgtodrivesync/
│       ├── __init__.py          # 插件主体（类名 TgtoDriveSync）
│       └── README.md            # 插件说明
├── plugins.v3/                  # v3 代插件目录
│   └── maoyanpro/
│       ├── __init__.py          # 插件主体（类名 MaoyanPro）
│       └── README.md            # 插件说明
├── tests/                       # 插件测试（按 tests/v3/<插件id>/ 组织）
└── dist/                        # 打包产物
```

> MoviePilot v3 会同时扫描 `package.json` 与 `package.v3.json`（`plugins/` 与 `plugins.v3/`），
> 同名插件按版本号取高者，因此两代插件可以共存在同一个仓库里。
> **插件目录名必须是小写的插件类名**（如 `MaoyanPro` → `plugins.v3/maoyanpro/`）。

## 打包产物

<a id="packages"></a>

`dist/` 目录下提供可直接使用的压缩包：

| 文件 | 说明 |
| --- | --- |
| `MoviePilot-Plugins.zip` | 完整插件仓库结构（`package.json` + `package.v3.json` + `plugins/` + `plugins.v3/`），解压即可推 Git 或当市场源 |
| `tgtodrivesync.zip` | 仅 TgtoDrive 插件目录，解压后把 `tgtodrivesync/` 放进 MoviePilot 插件目录 |
| `maoyanpro.zip` | 仅猫眼榜单插件目录，解压后把 `maoyanpro/` 放进 MoviePilot 插件目录 |

## 自动同步上游（MaoyanRank → MaoyanPro）

`猫眼榜单订阅` 是独立发行的 Fork。工作流 `.github/workflows/sync-maoyanrank.yml` 每天检查上游
[jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins) 的 `plugins.v3/maoyanrank/`，
有改动时用 **三方合并**把上游改动套进本仓库的 `plugins.v3/maoyanpro/`，递增 patch 版本号并重建打包产物，然后**开 PR** 等人工审核。

| 情况 | 工作流行为 |
| --- | --- |
| 上游无改动 | 直接结束，不产生任何提交 |
| 合并无冲突 | 递增版本（如 `1.0.0 → 1.0.1`）、更新索引与两处 README 版本号、重建 `dist/`，然后开 PR |
| 合并有冲突 | 分支里保留冲突标记，**不动版本号、不重建打包产物**，并开 Issue；解决并合并后，下一次运行会自动收尾 |

- 三方合并的基线放在 `.upstream/maoyanpro/`，每次成功同步后随 PR 一起推进
- 手动触发：Actions → Sync MaoyanRank from upstream → Run workflow（可勾选 `force` 强制执行一次）

---

# English

A self-hosted [MoviePilot](https://github.com/jxxghp/MoviePilot) plugin market source with two plugins.
Add `https://github.com/KingNuoNuo/MoviePilot-Plugins` as a custom market source, then install any plugin by name.
**Click a plugin name below for its own documentation page.**

| Plugin | Version | What it does | Docs |
| --- | --- | --- | --- |
| **TgtoDrive STRM Sync** (`TgtoDriveSync`) | v1.7.1 | Calls the official TgtoDrive OpenAPI when MoviePilot finishes transferring files (`TransferComplete`) to generate cloud-drive STRM links, waits for the sync, then refreshes every configured media server and sends a notification. | [→ Docs](plugins/tgtodrivesync/README.md) |
| **MaoyanPro** (`MaoyanPro`) | v1.1.1 | Scrapes Maoyan box-office, combined TV+web-drama and variety popularity boards and auto-subscribes; exposes `/rank`, `/rank-list` and `/refresh` endpoints and can register itself as a host recommendation source. | [→ Docs](plugins.v3/maoyanpro/README.md) |

> **Origin**: `MaoyanPro` is forked from [jxxghp/moviepilot-plugins](https://github.com/jxxghp/moviepilot-plugins)
> (original author [@baozaodetudou](https://github.com/baozaodetudou)), re-released as an
> **independent fork** with its own authorship and versioning starting at v1.0.0.

<!-- traffic:start -->

## 📊 访问统计

![近 14 天访问趋势](docs/traffic.svg)

| 指标 | 近 14 天 |
| --- | ---: |
| 页面浏览量 | 1 |
| 独立访客 | 1 |
| 克隆次数 | 85 |
| 独立克隆者 | 40 |

<sub>数据窗口 2026-09-30 ~ 2026-10-13 · 由 GitHub Actions 每日更新于 2026-10-15 · 来源：GitHub Traffic API（仅统计最近 14 天）</sub>

<!-- traffic:end -->