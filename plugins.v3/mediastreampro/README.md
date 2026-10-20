# 媒体流播放（MediaStreamPro）

把**已整理入库的视频**按原始文件流式转发给客户端，让 MoviePilotApp 的「下载 → 整理记录」能点开就播、能拖进度。
v1.3.0 起支持 **STRM 网盘直链播放**：v1.4.0 起**有 STRM 就优先走 STRM**（读出网盘直链后 302 跳转，流量不过 NAS），没有才直读本地。
v1.5.0 起**默认替客户端解析直链**：STRM 直链里写的是容器名（`TgtoDrive`），手机和 MoviePilot 容器都解析不了，现在由插件先走完 302 跳转链、把最终公网 CDN 地址给手机，内外网都能播。

本地文件播放**装好即用**；STRM 是可选配置（自己填一个目录，或直接「跟随 TgtoDrive 插件」）。

## 它解决什么问题

MoviePilot 宿主自己没有「下载记录 → 视频直链」的接口：

- `GET /api/v1/mediaserver/play/{itemid}` 只返回**媒体服务器的网页地址**（Emby 拼 `web/index.html#!/item?id=…`），播放器（ExoPlayer / IJK 之类）吃不了；
- `POST /api/v1/storage/download` 是 POST、且走 `StorageHelper.download_file()`（网盘会先落一份临时文件），不适合边下边播。

所以由本插件补一条**带 HTTP Range 的 GET 文件流**；STRM 模式下再补一步「读直链 → 302 跳转」。

## 插件页面

设置页（`get_form`）与详情页（`get_page`）顶部各有一张 **MoviePilotApp 推荐卡**：
左侧是 MoviePilot 官方 logo，右侧「下载 App」直达 Releases 最新版、旁边是 GitHub
仓库文字按钮。两个位置用同一个 `promo_card()` 渲染，与 MaoyanPro / NetflixPro /
GlobalRankPro 完全一致（实现来自各插件目录下的 `app_promo.py`，由
`tools/gen_app_promo.py` 生成，四份逐字节相同）。

配置页有 STRM 相关配置项（见下节）；详情页显示当前放行的媒体库目录与生效的 STRM 目录。

## STRM 网盘直链播放（v1.3.0）

STRM 模式（如 TgtoDrive 生成网盘 STRM 给媒体服务器扫库）下，整理记录里的影片本地没有实体文件，
之前点 ▶ 会 404。现在：

| 请求情况 | 行为 |
|---|---|
| `path` 就是 `.strm` 文件 | 读出文件里的网盘直链（第一条 `http(s)://` 行，`#` 注释跳过），**302 跳转**（v1.5.0 起默认先解析成最终 CDN 地址再跳） |
| 配置了 STRM 目录，且能对上同名 `<文件名>.strm`（先试同位置，再全树找一遍） | **302 跳转** —— v1.4.0 起**有 STRM 就优先走 STRM**，哪怕本地（网盘挂载）文件读得到也不再直读 |
| 对不上 `.strm`，或未配置 STRM 目录 | 本地文件存在就直接文件流（行为与 1.3.0 前一致）；不存在则 404 |

为什么是 302 跳转而不是代理：网盘直链的流量直接走 **播放器 ↔ 网盘 CDN**，不占 NAS 带宽，
Range / 拖动进度由播放器对直链续传。TgtoDrive 生成的直链本来就是给 Emby / Jellyfin 直接 GET 的。

### 直链解析（v1.5.0，默认开）

TgtoDrive 生成的 STRM 直链 host 是容器名 `TgtoDrive`（如 `http://TgtoDrive:12366/play/...`），
**手机和 MoviePilot 容器都解析不了这个名字** —— 直接 302 给手机会 DNS 失败、播不了。
Emby / Jellyfin 能播，是因为它们的**服务端在 NAS 上**（能解析 `TgtoDrive`）先请求直链、
跟随 302 拿到最终公网 CDN 地址，再把 CDN 地址给播放器。

v1.5.0 起本插件默认做同样的事：请求直链、逐跳跟随（最多 4 跳），把最终公网 CDN 直链
（如 `https://223-xxx.pd1.cjjd19.com/...`）302 给手机。手机最终连的是公网 CDN，
内外网都能播、流量不过 NAS。

两个相关配置：

| 配置 | 说明 |
|---|---|
| 替客户端解析网盘直链 | 默认开。关掉则退回 v1.4.0 行为（直接 302 原始直链，仅 NAS 本机/内网可用） |
| 直链主机名替换 | STRM 直链里的 host 在容器里解析不了时先替换成这个地址。TgtoDrive 是 host 网络模式，填它所在主机内网 IP（如 `10.0.0.4`）即可；插件请求直链时会把 `TgtoDrive` 换成它再跟随跳转 |

### STRM 目录配置

| 配置 | 说明 |
|---|---|
| 跟随 TgtoDrive 插件 | 打开后自动使用「**TgtoDrive STRM 同步**」插件里配置的 STRM 目录 —— 经宿主本机 API 读取（`GET /api/v1/plugin/TgtoDriveSync`），结果缓存 60 秒；读不到（插件未装/未配置）时退回下面自己填的值 |
| STRM 目录（容器内路径） | 跟随开关关闭（或跟随读不到）时生效。填 STRM 生成目录挂载进 MoviePilot 容器后的路径，如 `-v /vol1/1000/media-strm:/media-strm` 后填 `/media-strm` |

生效的 STRM 目录会**加入放行白名单**（`.strm` 文件可以直接请求），`/ping` 返回里也会带上 `strm_dir`。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>` | 返回原始视频文件，支持 `Range`（客户端可拖动进度）；STRM → 302 网盘直链 |
| `HEAD /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>` | 只回头（`Content-Type`、`Content-Length`、`Accept-Ranges: bytes`），不打开文件读数据、不回文件体，供外部播放器/下载器探路；STRM 同样 302 |
| `GET /api/v1/plugin/MediaStreamPro/ping` | 可用性探测，返回 `{success, data:{version, roots, strm_dir}}`，客户端用它决定是否显示播放入口 |
| `GET /api/v1/plugin/MediaStreamPro/season?path=<媒体文件绝对路径>` | 返回**同一季还有哪些集**（`{season, stem, episodes:[{path, episode, label, name, size}]}`），供客户端做选集；STRM 影片不做同季扫描（网盘直链没有目录结构可扫），只回它自己 |
| `POST /api/v1/plugin/MediaStreamPro/strm-check` | v1.5.1 起。body `{paths:[...]}`，批量判断一批媒体库绝对路径能否在 STRM 目录对上同名 `.strm`（路径本身是 `.strm` 也算有），返回 `{path: bool}` 映射，供客户端给可播的 STRM 影片打标识。未配置 STRM 目录时返回空映射 |

认证走宿主 `bear` 模式（JWT / API 令牌 / token），插件自己不做任何凭据解析。

**客户端请把凭据放在请求头**（`Authorization: Bearer <token>`），不要拼进 URL —— URL 会落进 nginx `access.log`、`Referer`、投屏外链与播放器错误日志。MoviePilotApp 用的是 `DefaultHttpDataSource` 的默认请求头，token 不进 URL。

## 放行哪些文件

**媒体库根目录不写死、也不存配置**：每次请求实时读宿主「设置 → 目录」里的 `library_path`
（即宿主的 `systemconfig["Directories"][*].library_path`）。所以在宿主里改了媒体库路径、
新增一个媒体库，插件立刻跟随，**不用重装、不用重启、不用再配一遍**。

v1.2.0 起 `download_path`（下载目录）**一并放行** —— 客户端要在「下载完成」页签直接播
还没整理（甚至整理失败）的文件。v1.3.0 起**生效的 STRM 目录**也在放行范围内。

路径校验：

1. 必须是绝对路径，且不含 NUL；
2. `realpath` 归一（消掉 `..`、重复分隔符、符号链接）后，必须落在某个放行根目录
   （媒体库 / 下载目录 / STRM 目录）内；比较时用「根目录 + 路径分隔符」，
   挡掉 `/DSM/media2` 被 `/DSM/media` 放行这类前缀串；
3. 扩展名必须是视频白名单（`.mp4 .mkv .ts .m2ts .mov .m4v .avi .flv .wmv .webm .rmvb .mpg …`）
   或 `.strm`；同目录下的 `.nfo` / `.jpg` / `.srt` 一律读不出去。

宿主媒体库目录配置为空时，按 `/DSM/media`、`/media` 两个常见挂载点兜底，避免插件装了却完全不可用；
读到目录后会在插件日志里打印一行「放行目录」。

## 已知限制

- **直读原文件，不转码**：能不能播取决于客户端解码能力。手机端 H.264 / HEVC + AAC 没问题，
  **DTS / TrueHD / 部分 AC-3 会没有声音**，这种片子在手机上看就是静音画面。想要兼容性请走媒体服务器（Emby）转码。
  MoviePilotApp v0.5.1 起，内置播放器解不了时可以点「系统播放器打开」，把这条流交给手机里别的播放器（App 内起
  一次性本机中转转发，`Authorization` 头由中转补上、`Range` 原样转发，token 不进 URL）；STRM 跳转后的网盘直链同样适用于「系统播放器打开」。
- 只做单文件转发与**同目录同季清单**，不做转码、不做跨目录合集：`/season` 只扫传入文件所在的那一个目录
  （不递归）。宿主默认整理规则是「剧名/Season 2/」，同一季必定同目录；如果你的整理规则把不同季甚至不同剧
  塞进同一个目录，靠文件名里的 `S02E05` / `第5集` 与剧名主干过滤，通常也能认对，但认不出集号的（电影、花絮）
  只会返回它自己。
- **STRM 播放不做同季扫描**：网盘直链没有「同季同目录」的文件系统可扫，`/season` 对 STRM 影片只返回它自己；
- **STRM 直链有时效**：网盘签名直链过期后播放器会拿到 403，重新点 ▶ 即可（插件每次都读 `.strm` 现跳，不缓存直链）；
- **直链解析依赖「直链主机名替换」配置**：`TgtoDrive` 是容器名，容器里解析不了；不填替换地址时插件请求直链会失败、退回原始直链（仅 NAS 本机能播）。填运行 TgtoDrive 的主机内网 IP 即可；
- STRM 目录树找不到同名 `.strm` 时维持 404（文件名不一致的 STRM 生成方式暂不支持）。

## 选集是怎么认的（`/season`）

客户端只能从宿主整理记录里拼选集，而 `/history/transfer` 只保留最近几天、逐集下载时每集又是独立种子
（没有共同的 `download_hash`）—— 两头都会漏集。所以交给插件直接看文件系统：**文件在，集就在**。

1. 从传入文件名解析季/集：`S02E05` / `S02.E05` / `EP05` / `E05` / `第5集` / `第5话`；
2. 剧名主干 = 抹掉季集标记及其后的规格（`2160p`、`WEB-DL`、字幕组），只留字母数字与汉字、转小写。
   于是 `某剧.S02E05.2160p.WEB-DL.mkv` 与 `某剧 第05集.mkv` 都会落到 `某剧`；
3. 同目录里**主干相同且季号相同**的视频文件才算同一季；
4. 同一集有多个版本（4K + 1080p）按集号去重：正在播的那份优先，否则留体积最大的；
5. 认不出集号（电影、花絮）或主干为空时，只返回传入的那一个文件 —— 客户端会当单片播。
