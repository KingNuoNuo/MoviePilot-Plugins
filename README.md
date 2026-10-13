# TgtoDrive STRM 同步插件（MoviePilot）

MoviePilot 整理入库（`TransferComplete` 事件）后，自动调用 **TgtoDrive 官方 OpenAPI**
触发生成网盘 STRM，等待生成完成后刷新 **Emby** / **飞牛影视** 媒体库，并发送通知。

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
刷新媒体库
  ├─ Emby（复用 MoviePilot 已配置的 Emby，或插件内单独配置的 Emby）
  └─ 飞牛影视（启用时，全库扫描或指定媒体库扫描）
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

## 前置（可选）：获取飞牛影视的 secret 与 api_key

飞牛影视**没有官方开放 API**，插件走的是它的网页接口，每个请求都带 `authx` 签名，
签名依赖 `secret` 与 `api_key`，所以要先把这两个值抓出来：

1. 浏览器登录飞牛影视后台（如 `http://<NAS_IP>:5666`）。
2. 右键「查看网页源代码」（或 `Ctrl+U`），找到其中的 `.js` 文件并打开。
3. `Ctrl+F` 搜索 `,s,l,c,o,e].join("_")`，左边引号内那一串就是 **secret**。
4. `Ctrl+F` 搜索 `${Ld}/sys/progressThumb`，右边那一串就是 **api_key**。

> 这两个值可能随飞牛版本更新或重启而变化，失效时重新抓一次即可。
> 若账号开了两步验证，用「用户名+密码」登录会失败，请在飞牛里手动登录后把 token 填到插件的「飞牛 token」项。

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
| Emby 刷新方式 | 刷新全部媒体库 / 按入库条目刷新 | 全部媒体库 |
| 触发后等待秒数 | 等待 STRM 生成的时间 | 60 |
| 防抖秒数 | 合并短时间内的多次入库 | 30 |
| Emby 地址 / API Key | 可选。留空则复用 MoviePilot 已配置的媒体服务器 | 空 |
| 启用飞牛影视刷新 | 是否在 STRM 生成后刷新飞牛影视 | 关 |
| 飞牛扫描范围 | 扫描全部媒体库 / 仅扫描指定媒体库 | 全部媒体库 |
| 飞牛影视地址 | 例如 `http://10.0.0.4:5666` | 空 |
| 指定媒体库名称 | 扫描范围选「仅指定」时填写，如 `电影库` | 空 |
| 飞牛用户名 / 密码 | 用于自动登录换取 token | 空 |
| 飞牛 secret / api_key | authx 签名必需 | 空 |
| 飞牛 token | 可选，填了免登录；开了两步验证时用这个 | 空 |

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
- 「按入库条目刷新」依赖 MoviePilot 提供的 `RefreshMediaItem`；若匹配不到媒体库会退化为无操作，
  建议优先使用「刷新全部媒体库」。
- **飞牛影视刷新不稳定属预期**：它没有官方 API，插件用的是抓包得到的网页接口 + `authx` 签名，
  飞牛升级后接口或签名算法若变动，需要重新适配。
