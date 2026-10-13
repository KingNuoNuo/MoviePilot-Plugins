# TgtoDrive STRM 同步插件（MoviePilot）

MoviePilot 整理入库（`TransferComplete` 事件）后，自动调用 **TgtoDrive 官方 OpenAPI**
触发生成网盘 STRM，等待生成完成后**刷新全部已配置的媒体服务器**，并发送通知。
媒体库刷新是**通用适配**的，自动覆盖 Emby / Jellyfin / Plex / 飞牛影视 / 绿联影视 /
Navidrome / Vyo / 极影视以及自定义等所有类型，宿主后续新增类型也无需改插件。

## 工作流程

```
MoviePilot 整理入库
      │  (EventType.TransferComplete)
      ▼
[防抖合并 N 秒]
      ▼
POST {TgtoDrive}/api/strm-sync/run   { "source": "123云盘" }
      │  (Bearer OpenAPI Token)
      ▼
等待 X 秒（TgtoDrive 的 STRM 同步为后台异步任务）
      ▼
刷新媒体库（遍历 MoviePilot 中已配置并启用的全部媒体服务器）
  ├─ Emby / Jellyfin / Plex / 飞牛影视 / 绿联影视 / Navidrome / Vyo / 极影视 / 自定义 …
  ├─ 整库刷新：实例.refresh_root_library()
  └─ 按条目刷新：实例.refresh_library_by_items()（不支持者自动回退整库）
      ▼
发送通知 + 记录执行历史
```

> ⚠️ TgtoDrive 的 `/api/strm-sync/run` 只返回「任务已开始」，官方 OpenAPI 中**没有提供
> STRM 同步的完成回调 / 进度查询接口**。因此本插件用「触发后等待 N 秒」来估算生成时间，
> 请根据网盘规模把「触发后等待秒数」调到合适值（默认 60 秒）。

## 前置：在 TgtoDrive 里创建 OpenAPI Token

1. 打开 TgtoDrive 管理台 `http://<NAS_IP>:12366` 并登录。
2. 进入「**实用工具 → OpenAPI**」。
3. 点击创建访问令牌，**完整令牌只在创建时显示一次**，复制保存（服务端只存 SHA-256 摘要）。
4. 令牌具有后台管理员权限，请只用于可信脚本；最多同时保存 20 个，撤销后立即失效。

## 前置（可选）：在 MoviePilot 中配置媒体服务器

插件通过 MoviePilot 的 `MediaServerHelper` 取出「**设定 → 媒体服务器**」中已配置并启用的
全部服务器实例，逐个调用其刷新能力。因此**只需在 MoviePilot 里配好媒体服务器**，插件会自动适配，
无需在插件内重复填写地址或密钥。

> ⚠️ 飞牛影视需使用**管理员账号**：飞牛只有管理员才能触发媒体库刷新，非管理员会直接失败。
> Jellyfin、Navidrome 的模块只提供整库刷新，「按入库条目刷新」模式下会自动回退整库。

## 安装

### 方式一：作为自定义插件市场（推荐）

1. MoviePilot →「设定 → 插件市场 → 添加市场」，填入本仓库地址：

   ```
   https://github.com/KingNuoNuo/MoviePilot-Plugins
   ```

2. 保存后，在市场里搜索「**TgtoDrive STRM 同步**」并安装、启用。

仓库结构：

```
MoviePilot-Plugins/
├── package.json                 # 插件清单（市场识别）
└── plugins/
    └── tgtodrivesync/
        └── __init__.py          # 插件主体（类名 TgtoDriveSync）
```

### 方式二：本地直接放置

把 `plugins/tgtodrivesync` 整个目录拷贝到 MoviePilot 的插件目录（容器内通常为
`/app/app/plugins/`，以你的实际部署为准），重启 MoviePilot 容器即可在插件列表看到。

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| 启用插件 | 总开关 | 关 |
| 发送通知 | 流程结果是否发通知 | 开 |
| TgtoDrive 地址 | 例如 `http://10.0.0.4:12366` | 空 |
| TgtoDrive OpenAPI Token | 上一步创建的令牌 | 空 |
| STRM 同步来源 | `123云盘` / `115网盘` / `光鸭云盘` 及各自音乐/分享 | `123云盘` |
| 媒体库刷新方式 | 刷新全部媒体库 / 按入库条目刷新 | 全部媒体库 |
| 触发后等待秒数 | 等待 STRM 生成的时间 | 60 |
| 防抖秒数 | 合并短时间内的多次入库 | 30 |
| 只刷新指定媒体服务器 | 可选。填 MoviePilot 里的服务器名称，多个用英文逗号分隔，留空=全部 | 空 |

## 手动触发

- **插件 API**（需带 MoviePilot 的 `API_TOKEN`）：
  - `GET /api/v1/plugin/TgtoDriveSync/trigger?apikey=xxx` — 立即触发同步并刷新
  - `GET /api/v1/plugin/TgtoDriveSync/test?apikey=xxx` — 测试 TgtoDrive 连接
- **远程命令**：`/tgtodrive_sync`（在支持命令的渠道中发送）

## 直接验证 TgtoDrive 接口

```bash
# 触发 123 云盘 STRM 同步
curl -X POST "http://10.0.0.4:12366/api/strm-sync/run" \
  -H "Authorization: Bearer <OPENAPI_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"source":"123云盘"}'

# 期望响应： {"success": true, "message": "..."}
```

## 打包产物

`dist/` 目录下提供可直接使用的压缩包：

| 文件 | 说明 |
| --- | --- |
| `MoviePilot-Plugins.zip` | 完整插件仓库结构（`package.json` + `plugins/tgtodrivesync/`），解压即可推 Git 或当市场源 |
| `tgtodrivesync.zip` | 仅插件目录，解压后把 `tgtodrivesync/` 放进 MoviePilot 插件目录 |

## 已知边界

- TgtoDrive 无 STRM 完成回调，刷新的时机靠「等待秒数」估算；若同步较慢可调大该值。
- `source` 取值取自 TgtoDrive 前端枚举，若其版本升级后新增来源，请在
  `__init__.py` 顶部的 `STRM_SOURCES` 中同步补充。
- **刷新哪个库，由 MoviePilot 决定**：插件只刷新「设定 → 媒体服务器」里已配置并启用的服务器；
  未配置的不会出现在结果里。要跳过某些服务器，用「只刷新指定媒体服务器」按名称过滤。
- **Jellyfin / Navidrome 只支持整库刷新**：它们的模块未提供按条目刷新接口，
  「按入库条目刷新」模式下这两个会自动回退为整库刷新，其它服务器仍按条目刷新。
- **取实例的路径分代**：`MediaServerHelper` 在 v2 位于 `app.helper.mediaserver`、
  v3 位于 `app.application.mediaserver`，插件已做双路径兼容；若宿主未提供该类，
  会返回明确的错误提示，不影响 TgtoDrive 触发这一步。
- 刷新调用的是各服务器模块的 `refresh_root_library()` / `refresh_library_by_items()`，
  返回 `None` 表示未认证或服务不可用，会记为失败并写入原因。
