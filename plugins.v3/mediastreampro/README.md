# 媒体流播放（MediaStreamPro）

把**已整理入库的视频**按原始文件流式转发给客户端，让 MoviePilotApp 的「下载 → 整理记录」能点开就播、能拖进度。

装好即用：**没有开关、没有配置项**。

## 它解决什么问题

MoviePilot 宿主自己没有「下载记录 → 视频直链」的接口：

- `GET /api/v1/mediaserver/play/{itemid}` 只返回**媒体服务器的网页地址**（Emby 拼 `web/index.html#!/item?id=…`），播放器（ExoPlayer / IJK 之类）吃不了；
- `POST /api/v1/storage/download` 是 POST、且走 `StorageHelper.download_file()`（网盘会先落一份临时文件），不适合边下边播。

所以由本插件补一条**带 HTTP Range 的 GET 文件流**。

## 插件页面

设置页（`get_form`）与详情页（`get_page`）顶部各有一张 **MoviePilotApp 推荐卡**：
左侧是 MoviePilot 官方 logo，右侧「下载 App」直达 Releases 最新版、旁边是 GitHub
仓库文字按钮。两个位置用同一个 `promo_card()` 渲染，与 MaoyanPro / NetflixPro /
GlobalRankPro 完全一致（实现来自各插件目录下的 `app_promo.py`，由
`tools/gen_app_promo.py` 生成，四份逐字节相同）。

配置页没有可配项，推荐卡下面只有一条「装好即用」的说明；详情页推荐卡下面是当前
放行的媒体库目录。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>` | 返回原始视频文件，支持 `Range`（客户端可拖动进度） |
| `HEAD /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>` | 只回头（`Content-Type`、`Content-Length`、`Accept-Ranges: bytes`），不打开文件读数据、不回文件体，供外部播放器/下载器探路 |
| `GET /api/v1/plugin/MediaStreamPro/ping` | 可用性探测，返回 `{success, data:{version, roots}}`，客户端用它决定是否显示播放入口 |
| `GET /api/v1/plugin/MediaStreamPro/season?path=<媒体文件绝对路径>` | 返回**同一季还有哪些集**（`{season, stem, episodes:[{path, episode, label, name, size}]}`），供客户端做选集 |

认证走宿主 `bear` 模式（JWT / API 令牌 / token），插件自己不做任何凭据解析。

**客户端请把凭据放在请求头**（`Authorization: Bearer <token>`），不要拼进 URL —— URL 会落进 nginx `access.log`、`Referer`、投屏外链与播放器错误日志。MoviePilotApp 用的是 `DefaultHttpDataSource` 的默认请求头，token 不进 URL。

## 放行哪些文件

**媒体库根目录不写死、也不存配置**：每次请求实时读宿主「设置 → 目录」里的 `library_path`
（即宿主的 `systemconfig["Directories"][*].library_path`）。所以在宿主里改了媒体库路径、
新增一个媒体库，插件立刻跟随，**不用重装、不用重启、不用再配一遍**。

`download_path`（下载目录）**刻意不放行** —— 播放只面向已整理入库的文件，权限面最小。

路径校验：

1. 必须是绝对路径，且不含 NUL；
2. `realpath` 归一（消掉 `..`、重复分隔符、符号链接）后，必须落在某个媒体库根目录内；
   比较时用「根目录 + 路径分隔符」，挡掉 `/DSM/media2` 被 `/DSM/media` 放行这类前缀串；
3. 扩展名必须在视频白名单内（`.mp4 .mkv .ts .m2ts .mov .m4v .avi .flv .wmv .webm .rmvb .mpg …`），
   同目录下的 `.nfo` / `.jpg` / `.srt` 一律读不出去。

宿主目录配置为空时，按 `/DSM/media`、`/media` 两个常见挂载点兜底，避免插件装了却完全不可用；
读到目录后会在插件日志里打印一行「放行目录」。

## 已知限制

- **直读原文件，不转码**：能不能播取决于客户端解码能力。手机端 H.264 / HEVC + AAC 没问题，
  **DTS / TrueHD / 部分 AC-3 会没有声音**，这种片子在手机上看就是静音画面。想要兼容性请走媒体服务器（Emby）转码。
  MoviePilotApp v0.5.1 起，内置播放器解不了时可以点「系统播放器打开」，把这条流交给手机里别的播放器（App 内起
  一次性本机中转转发，`Authorization` 头由中转补上、`Range` 原样转发，token 不进 URL）。
- 只做单文件转发与**同目录同季清单**，不做转码、不做跨目录合集：`/season` 只扫传入文件所在的那一个目录
  （不递归）。宿主默认整理规则是「剧名/Season 2/」，同一季必定同目录；如果你的整理规则把不同季甚至不同剧
  塞进同一个目录，靠文件名里的 `S02E05` / `第5集` 与剧名主干过滤，通常也能认对，但认不出集号的（电影、花絮）
  只会返回它自己。

## 选集是怎么认的（`/season`）

客户端只能从宿主整理记录里拼选集，而 `/history/transfer` 只保留最近几天、逐集下载时每集又是独立种子
（没有共同的 `download_hash`）—— 两头都会漏集。所以交给插件直接看文件系统：**文件在，集就在**。

1. 从传入文件名解析季/集：`S02E05` / `S02.E05` / `EP05` / `E05` / `第5集` / `第5话`；
2. 剧名主干 = 抹掉季集标记及其后的规格（`2160p`、`WEB-DL`、字幕组），只留字母数字与汉字、转小写。
   于是 `某剧.S02E05.2160p.WEB-DL.mkv` 与 `某剧 第05集.mkv` 都会落到 `某剧`；
3. 同目录里**主干相同且季号相同**的视频文件才算同一季；
4. 同一集有多个版本（4K + 1080p）按集号去重：正在播的那份优先，否则留体积最大的；
5. 认不出集号（电影、花絮）或主干为空时，只返回传入的那一个文件 —— 客户端会当单片播。
