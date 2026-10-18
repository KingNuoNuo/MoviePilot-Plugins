# 媒体流播放（MediaStreamPro）

把**已整理入库的视频**按原始文件流式转发给客户端，让 MoviePilotApp 的「下载 → 下载记录」能点开就播、能拖进度。

装好即用：**没有开关、没有配置项**。

## 它解决什么问题

MoviePilot 宿主自己没有「下载记录 → 视频直链」的接口：

- `GET /api/v1/mediaserver/play/{itemid}` 只返回**媒体服务器的网页地址**（Emby 拼 `web/index.html#!/item?id=…`），播放器（ExoPlayer / IJK 之类）吃不了；
- `POST /api/v1/storage/download` 是 POST、且走 `StorageHelper.download_file()`（网盘会先落一份临时文件），不适合边下边播。

所以由本插件补一条**带 HTTP Range 的 GET 文件流**。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/v1/plugin/MediaStreamPro/stream?path=<媒体文件绝对路径>` | 返回原始视频文件，支持 `Range`（客户端可拖动进度） |
| `GET /api/v1/plugin/MediaStreamPro/ping` | 可用性探测，返回 `{success, data:{version, roots}}`，客户端用它决定是否显示播放入口 |

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
- 只做单文件转发，不做合集/选集编排 —— 选集是客户端（MoviePilotApp）按整理记录自己聚合的。
