"""MoviePilotApp 推荐卡（本文件由 ``tools/gen_app_promo.py`` 生成，请勿手改）。

每个插件各存一份，内容逐字节相同 —— 宿主按插件目录隔离安装，插件之间不能互相
import，共享代码只能各带一份。改文案请改 ``tools/gen_app_promo.py`` 里的
``TEMPLATE`` 再重跑生成器。

logo 源文件 sha256：a4d264cb3290a5a5165f0185b8cd7931f662662ab2d9b23bbb93602c0d4db2b0
"""
from __future__ import annotations

APP_NAME = 'MoviePilotApp'
APP_TAGLINE = '安卓客户端 · 榜单 / 订阅 / 下载整理 / 在线播放'
APP_REPO = 'https://github.com/KingNuoNuo/MoviePilotApp'
APP_RELEASES = 'https://github.com/KingNuoNuo/MoviePilotApp/releases/latest'

# MoviePilot 官方 logo（宿主根路径的 logo.svg，10.4 KB，无外链、无脚本）。
#
# 内嵌成 data URI 而不是写 ``/logo.svg``：
#   * 宿主反代到子路径（``/moviepilot/``）时绝对路径 404；
#   * 相对路径 ``logo.svg`` 会被当前路由深度带偏（``/plugin/detail`` 下解析成
#     ``/plugin/logo.svg``），同样 404。
# 一旦取不到，卡片左侧就是个空白洞。内嵌跟着插件走，任何部署、任何断网状态都
# 显示得出来，也不受宿主未来换 logo 影响（图形冻结在这一版）。
_LOGO_B64 = (
    'PHN2ZyB3aWR0aD0iM2VtIiBoZWlnaHQ9IjNlbSIgdmlld0JveD0iMCAwIDE5MiAxOTIiIHZlcnNpb249IjEuMSIgeG1sbnM9'
    'Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIiB4bWxuczp4bGluaz0iaHR0cDovL3d3dy53My5vcmcvMTk5OS94bGluayIg'
    'eG1sOnNwYWNlPSJwcmVzZXJ2ZSIgeG1sbnM6c2VyaWY9Imh0dHA6Ly93d3cuc2VyaWYuY29tLyIgc3R5bGU9ImZpbGwtcnVs'
    'ZTpldmVub2RkO2NsaXAtcnVsZTpldmVub2RkO3N0cm9rZS1saW5lY2FwOnJvdW5kO3N0cm9rZS1saW5lam9pbjpyb3VuZDtz'
    'dHJva2UtbWl0ZXJsaW1pdDoyO3NoYXBlLXJlbmRlcmluZzpnZW9tZXRyaWNQcmVjaXNpb247Ij4KICAgIDxnIHRyYW5zZm9y'
    'bT0ibWF0cml4KDEsMCwwLDEsLTI2MDYsLTIzNikiPgogICAgICAgIDxnIGlkPSJhMi1jIiB0cmFuc2Zvcm09Im1hdHJpeCgx'
    'LDAsMCwxLDI2MDYsMjM2KSI+CiAgICAgICAgICAgIDxyZWN0IHg9IjAiIHk9IjAiIHdpZHRoPSIxOTIiIGhlaWdodD0iMTky'
    'IiBzdHlsZT0iZmlsbDpub25lOyIvPgogICAgICAgICAgICA8ZyB0cmFuc2Zvcm09Im1hdHJpeCgtMC44MDA3OTgsMC40NjIz'
    'NDEsLTAuNzY5OTcyLC0xLjMzMzYzLDE4NjkuMTEsLTg5Ni43MTgpIj4KICAgICAgICAgICAgICAgIDxwYXRoIGQ9Ik0yMjQx'
    'LjI3LC0yOC4xNzVDMjIzOC44NiwtMjguOTMxIDIyMzYuNjQsLTI5LjE4MSAyMjM0LjQ4LC0yOS4yNTRMMjE1OS43OCwtMjku'
    'Mjg2TDIxNjUuMDEsLTExLjIwN0MyMTY3LjE2LC0xMy4xMjEgMjE2OS42NCwtMTMuNzIyIDIxNzIuMjYsLTEzLjgwOEwyMjIy'
    'LjEyLC0xMy44MjJDMjIyMy41MiwtMTMuODI0IDIyMjUsLTEzLjcwMSAyMjI2Ljc4LC0xMy4xMDhMMjI0MS4yNywtMjguMTc1'
    'WiIgc3R5bGU9ImZpbGw6dXJsKCNfTGluZWFyMSk7Ii8+CiAgICAgICAgICAgIDwvZz4KICAgICAgICAgICAgPGcgdHJhbnNm'
    'b3JtPSJtYXRyaXgoMSwwLDAsMSwtMjE1Ny42NywtMjA4LjQyMykiPgogICAgICAgICAgICAgICAgPHBhdGggZD0iTTIyMDUu'
    'NjcsMzMxLjQyOEwyMjA1LjY3LDMzMi4yNUwyMjA1LjY3LDM1Mi44MzVDMjIwNS42NywzNTQuMjYzIDIyMDQuOTEsMzU1LjU4'
    'MyAyMjAzLjY3LDM1Ni4yOThDMjIwMi40MywzNTcuMDEyIDIyMDAuOTEsMzU3LjAxMyAyMTk5LjY3LDM1Ni4zTDIxOTAuNzgs'
    'MzUxLjE3NEMyMTg5LjczLDM1MC41OTUgMjE4OC44MywzNTAuMDgzIDIxODguMDMsMzQ5LjU5TDIxODcuNDUsMzQ5LjI1N0My'
    'MTg2LjY2LDM0OC43MjUgMjE4NS45MSwzNDguMTQyIDIxODUuMjEsMzQ3LjQ2MUMyMTg1LjA4LDM0Ny4zMzEgMjE4NC45NSwz'
    'NDcuMTk4IDIxODQuODIsMzQ3LjA2MUMyMTg0LjI2LDM0Ni40NTcgMjE4My43NSwzNDUuNzc4IDIxODMuMywzNDQuOTk1QzIx'
    'ODIuMTYsMzQzLjA1IDIxODEuNjksMzQxLjAyNCAyMTgxLjY4LDMzOC45NDhMMjE4MS42NywyNjguOTIzTDIyMDkuNzcsMjc0'
    'LjQyNUMyMjA3LjUsMjc1LjYzOSAyMjA1LjY4LDI3OC4zIDIyMDUuNjcsMjgxLjQyOUwyMjA1LjY3LDMzMS40MjhaIiBzdHls'
    'ZT0iZmlsbDp1cmwoI19MaW5lYXIyKTsiLz4KICAgICAgICAgICAgPC9nPgogICAgICAgICAgICA8ZyB0cmFuc2Zvcm09Im1h'
    'dHJpeCgxLDAsMCwxLC0yMTU3LjY3LC0yMDguNDIzKSI+CiAgICAgICAgICAgICAgICA8cGF0aCBkPSJNMjI5NS45MywzNjMu'
    'MDY0QzIyOTkuNDgsMzYwLjg4MiAyMzAxLjQ2LDM1Ny41NSAyMzAxLjY3LDM1Mi45MjZMMjMwMS42NywyNzcuODc5TDIzMDEu'
    'NjcsMjc2Ljc3NUwyMzAxLjY3LDI1Mi41MTVDMjMwMS42NywyNTEuODAxIDIzMDIuMDUsMjUxLjE0IDIzMDIuNjcsMjUwLjc4'
    'M0MyMzAzLjI5LDI1MC40MjYgMjMwNC4wNSwyNTAuNDI2IDIzMDQuNjcsMjUwLjc4NEwyMzE5LjgxLDI1OS41NEMyMzIxLjU5'
    'LDI2MC42MTcgMjMyMi45NSwyNjIuMTE1IDIzMjQuMDQsMjYzLjg3NUMyMzI1LjAzLDI2NS41NTEgMjMyNS41NiwyNjcuMzcg'
    'MjMyNS42NywyNjkuODM1TDIzMjUuNjcsMzM5LjkxQzIzMjUuMTgsMzQzLjY0NSAyMzIzLjUxLDM0Ni43MDUgMjMyMC4zLDM0'
    'OC44ODdMMjI5NS45MywzNjMuMDY0Wk0yMjk1LjkzLDM2My4wNjRDMjI5NS43MywzNjMuMTg0IDIyOTUuNTMsMzYzLjMwMSAy'
    'Mjk1LjMyLDM2My40MTRMMjI5NS45MywzNjMuMDY0WiIgc3R5bGU9ImZpbGw6cmdiKDE0MSw4MSwyNDkpOyIvPgogICAgICAg'
    'ICAgICA8L2c+CiAgICAgICAgICAgIDxnIHRyYW5zZm9ybT0ibWF0cml4KDEsMCwwLDEsLTIxNTcuNjcsLTIwOC40MjMpIj4K'
    'ICAgICAgICAgICAgICAgIDxwYXRoIGQ9Ik0yMjk1LjkzLDM2My4wNjRDMjI5OS40OCwzNjAuODgyIDIzMDEuNDYsMzU3LjU1'
    'IDIzMDEuNjcsMzUyLjkyNkwyMzAxLjY3LDI3Ny44NzlMMjMwMS42NywyNzYuNzc1TDIzMDEuNjcsMjUyLjUxNUMyMzAxLjY3'
    'LDI1MS44MDEgMjMwMi4wNSwyNTEuMTQgMjMwMi42NywyNTAuNzgzQzIzMDMuMjksMjUwLjQyNiAyMzA0LjA1LDI1MC40MjYg'
    'MjMwNC42NywyNTAuNzg0TDIzMTkuODEsMjU5LjU0QzIzMjEuNTksMjYwLjYxNyAyMzIyLjk1LDI2Mi4xMTUgMjMyNC4wNCwy'
    'NjMuODc1QzIzMjUuMDMsMjY1LjU1MSAyMzI1LjU2LDI2Ny4zNyAyMzI1LjY3LDI2OS44MzVMMjMyNS42NywzMzkuOTFDMjMy'
    'NS4xOCwzNDMuNjQ1IDIzMjMuNTEsMzQ2LjcwNSAyMzIwLjMsMzQ4Ljg4N0wyMjk1LjkzLDM2My4wNjRaTTIyOTkuNzksMzYw'
    'LjIzOEMyMjk5Ljc5LDM2MC4yMzggMjMyMC4wMywzNDguNDY0IDIzMjAuMDQsMzQ4LjQ2MUMyMzIzLjEsMzQ2LjM3MiAyMzI0'
    'LjY5LDM0My40NDQgMjMyNS4xNywzMzkuODc3QzIzMjUuMTcsMzM5Ljg3NyAyMzI1LjE3LDI2OS44NDYgMjMyNS4xNywyNjku'
    'ODM5QzIzMjUuMDYsMjY3LjQ4MiAyMzI0LjU2LDI2NS43MzkgMjMyMy42MSwyNjQuMTMzQzIzMjIuNTYsMjYyLjQ0NSAyMzIx'
    'LjI2LDI2MS4wMDUgMjMxOS41NSwyNTkuOTdMMjMwNC40MiwyNTEuMjE3QzIzMDMuOTYsMjUwLjk0OSAyMzAzLjM5LDI1MC45'
    'NDggMjMwMi45MiwyNTEuMjE2QzIzMDIuNDYsMjUxLjQ4NCAyMzAyLjE3LDI1MS45NzkgMjMwMi4xNywyNTIuNTE1TDIzMDIu'
    'MTcsMjc2Ljc3NUwyMzAyLjE3LDI3Ny44NzlMMjMwMi4xNywzNTIuOTI2QzIzMDIuMTcsMzUyLjkzMyAyMzAyLjE3LDM1Mi45'
    'NDEgMjMwMi4xNywzNTIuOTQ4QzIzMDIuMDQsMzU1Ljg2MSAyMzAxLjIzLDM1OC4yNzkgMjI5OS43OSwzNjAuMjM4WiIgc3R5'
    'bGU9ImZpbGw6dXJsKCNfTGluZWFyMyk7Ii8+CiAgICAgICAgICAgIDwvZz4KICAgICAgICAgICAgPGcgdHJhbnNmb3JtPSJt'
    'YXRyaXgoMSwwLDAsMSwtMjE1Ny42NywtMjA4LjQyMykiPgogICAgICAgICAgICAgICAgPHBhdGggZD0iTTIyNTMuNjcsMjIz'
    'LjI1NkMyMjU1LjI2LDIyMy4yNDUgMjI1Ny4wMiwyMjMuNTYgMjI1OS4xMSwyMjQuNTU3TDIyNzUuNjcsMjM0LjEwMkMyMjc2'
    'LjkxLDIzNC44MTYgMjI3Ny42NywyMzYuMTM4IDIyNzcuNjcsMjM3LjU2OEwyMjc3LjY3LDI1OS41MDhDMjI3Ny42NywyNjAu'
    'MjIyIDIyNzcuMjksMjYwLjg4MiAyMjc2LjY3LDI2MS4yMzlDMjI3Ni4wNSwyNjEuNTk3IDIyNzUuMjksMjYxLjU5NyAyMjc0'
    'LjY3LDI2MS4yNEwyMjU3LjUyLDI1MS4zNTNDMjI1Ni4zOCwyNTAuNzMxIDIyNTUuMTIsMjUwLjM0MSAyMjUzLjY3LDI1MC4z'
    'NDdDMjI1Mi4yNiwyNTAuMzM5IDIyNTAuOTksMjUwLjcyMSAyMjQ5LjgyLDI1MS4zNTNMMjE4Ny44NywyODcuMDRDMjE4NC4y'
    'MywyODkuMTQ3IDIxODEuOTYsMjkyLjQ3OCAyMTgxLjY3LDI5Ny41N0wyMTgxLjY4LDI2OS44NjVDMjE4MS44NSwyNjUuMTY3'
    'IDIxODMuOTMsMjYxLjY1MyAyMTg3LjkyLDI1OS4zMjJMMjI0OC4yMywyMjQuNTU3QzIyNDkuNjksMjIzLjc5NiAyMjUxLjUs'
    'MjIzLjI5IDIyNTMuNjcsMjIzLjI1NloiIHN0eWxlPSJmaWxsOnJnYigxNjUsMTE4LDI1NSk7Ii8+CiAgICAgICAgICAgIDwv'
    'Zz4KICAgICAgICAgICAgPGcgdHJhbnNmb3JtPSJtYXRyaXgoMSwwLDAsMSwtMjE1Ny42NywtMjA4LjQyMykiPgogICAgICAg'
    'ICAgICAgICAgPHBhdGggZD0iTTIyNTMuNjcsMjIzLjI1NkMyMjU1LjI2LDIyMy4yNDUgMjI1Ny4wMiwyMjMuNTYgMjI1OS4x'
    'MSwyMjQuNTU3TDIyNzUuNjcsMjM0LjEwMkMyMjc2LjkxLDIzNC44MTYgMjI3Ny42NywyMzYuMTM4IDIyNzcuNjcsMjM3LjU2'
    'OEwyMjc3LjY3LDI1OS41MDhDMjI3Ny42NywyNjAuMjIyIDIyNzcuMjksMjYwLjg4MiAyMjc2LjY3LDI2MS4yMzlDMjI3Ni4w'
    'NSwyNjEuNTk3IDIyNzUuMjksMjYxLjU5NyAyMjc0LjY3LDI2MS4yNEwyMjU3LjUyLDI1MS4zNTNDMjI1Ni4zOCwyNTAuNzMx'
    'IDIyNTUuMTIsMjUwLjM0MSAyMjUzLjY3LDI1MC4zNDdDMjI1Mi4yNiwyNTAuMzM5IDIyNTAuOTksMjUwLjcyMSAyMjQ5Ljgy'
    'LDI1MS4zNTNMMjE4Ny44NywyODcuMDRDMjE4NC4yMywyODkuMTQ3IDIxODEuOTYsMjkyLjQ3OCAyMTgxLjY3LDI5Ny41N0wy'
    'MTgxLjY4LDI2OS44NjVDMjE4MS44NSwyNjUuMTY3IDIxODMuOTMsMjYxLjY1MyAyMTg3LjkyLDI1OS4zMjJMMjI0OC4yMywy'
    'MjQuNTU3QzIyNDkuNjksMjIzLjc5NiAyMjUxLjUsMjIzLjI5IDIyNTMuNjcsMjIzLjI1NlpNMjI1My42OCwyMjMuNzU2QzIy'
    'NTEuNiwyMjMuNzg5IDIyNDkuODcsMjI0LjI2OSAyMjQ4LjQ3LDIyNC45OTZMMjE4OC4xNywyNTkuNzU0QzIxODQuMzUsMjYx'
    'Ljk5MiAyMTgyLjM1LDI2NS4zNjcgMjE4Mi4xOCwyNjkuODc0QzIxODIuMTgsMjY5Ljg3NCAyMTgyLjE3LDI5Mi43NTkgMjE4'
    'Mi4xNywyOTIuNzU3QzIxODMuMjUsMjkwLjA0NyAyMTg1LjEzLDI4OC4wNTEgMjE4Ny42MiwyODYuNjA3TDIyNDkuNTcsMjUw'
    'LjkxOUMyMjQ5LjU4LDI1MC45MTcgMjI0OS41OCwyNTAuOTE1IDIyNDkuNTksMjUwLjkxM0MyMjUwLjgzLDI1MC4yNDMgMjI1'
    'Mi4xNywyNDkuODM5IDIyNTMuNjcsMjQ5Ljg0N0MyMjU1LjIxLDI0OS44NDEgMjI1Ni41NCwyNTAuMjUzIDIyNTcuNzYsMjUw'
    'LjkxNEMyMjU3Ljc2LDI1MC45MTYgMjI1Ny43NiwyNTAuOTE3IDIyNTcuNzYsMjUwLjkxOUwyMjc0LjkyLDI2MC44MDdDMjI3'
    'NS4zOCwyNjEuMDc1IDIyNzUuOTUsMjYxLjA3NCAyMjc2LjQyLDI2MC44MDZDMjI3Ni44OCwyNjAuNTM4IDIyNzcuMTcsMjYw'
    'LjA0MyAyMjc3LjE3LDI1OS41MDhMMjI3Ny4xNywyMzcuNTY4QzIyNzcuMTcsMjM2LjMxNyAyMjc2LjUsMjM1LjE2IDIyNzUu'
    'NDIsMjM0LjUzNUMyMjc1LjQyLDIzNC41MzUgMjI1OC44OCwyMjUgMjI1OC44NywyMjQuOTk2QzIyNTYuODcsMjI0LjA0OSAy'
    'MjU1LjIsMjIzLjc0NiAyMjUzLjY4LDIyMy43NTZaIiBzdHlsZT0iZmlsbDp1cmwoI19MaW5lYXI0KTsiLz4KICAgICAgICAg'
    'ICAgPC9nPgogICAgICAgICAgICA8ZyB0cmFuc2Zvcm09Im1hdHJpeCgwLjgwMDc5OCwwLjQ2MjM0MSwwLjc2OTk3MiwtMS4z'
    'MzM2MywtMTY3Ny4yMiwtODk2Ljg1OCkiPgogICAgICAgICAgICAgICAgPHBhdGggZD0iTTIyNDEuNTUsLTI4LjE4NEMyMjM5'
    'LjEsLTI4Ljk4OSAyMjM2LjgzLC0yOS4yMDQgMjIzNC42OCwtMjkuMjk1QzIyMzQuNjgsLTI5LjI5NSAyMjIwLjgyLC0yOS4z'
    'IDIyMTUuMDMsLTI5LjMwM0MyMjEzLjQ4LC0yOS4zMDMgMjIxMi4wNSwtMjguODA4IDIyMTEuMjgsLTI4LjAwNEMyMjA4LjY1'
    'LC0yNS4yNzUgMjIwMi41NiwtMTguOTM2IDIxOTkuNDUsLTE1LjcwOUMyMTk5LjA3LC0xNS4zMDYgMjE5OS4wNywtMTQuODA5'
    'IDIxOTkuNDYsLTE0LjQwNkMyMTk5Ljg1LC0xNC4wMDQgMjIwMC41NywtMTMuNzU4IDIyMDEuMzQsLTEzLjc2MUMyMjA4LjM2'
    'LC0xMy43ODggMjIyMi43MiwtMTMuODQ1IDIyMjIuNzIsLTEzLjg0NUMyMjIzLjk4LC0xMy44NTEgMjIyNS40NCwtMTMuNjU3'
    'IDIyMjcuMDYsLTEzLjExN0wyMjQxLjU1LC0yOC4xODRaIiBzdHlsZT0iZmlsbDpyZ2IoMTQxLDgxLDI0OSk7Ii8+CiAgICAg'
    'ICAgICAgIDwvZz4KICAgICAgICAgICAgPGcgdHJhbnNmb3JtPSJtYXRyaXgoLTQuMzIzMDksMCwwLDEyLjQ0NTQsOTYxMC4z'
    'NSwtMTQ1MC4zNSkiPgogICAgICAgICAgICAgICAgPHBhdGggZD0iTTIyMDUuMzEsMTIxLjk2NkMyMjA1LjMxLDEyMS44OCAy'
    'MjA1LjE4LDEyMS44IDIyMDQuOTYsMTIxLjc1N0MyMjA0Ljc0LDEyMS43MTQgMjIwNC40OCwxMjEuNzE0IDIyMDQuMjcsMTIx'
    'Ljc1N0MyMjAxLjc1LDEyMi4yNjMgMjE5NS4zNiwxMjMuNTQ3IDIxOTIuODUsMTI0LjA1MkMyMTkyLjYzLDEyNC4wOTUgMjE5'
    'Mi41LDEyNC4xNzQgMjE5Mi41LDEyNC4yNjFDMjE5Mi41LDEyNC4zNDcgMjE5Mi42MywxMjQuNDI2IDIxOTIuODUsMTI0LjQ2'
    'OUMyMTk1LjM2LDEyNC45NzQgMjIwMS43NSwxMjYuMjU1IDIyMDQuMjcsMTI2Ljc1OUMyMjA0LjQ4LDEyNi44MDIgMjIwNC43'
    'NCwxMjYuODAyIDIyMDQuOTYsMTI2Ljc1OUMyMjA1LjE4LDEyNi43MTYgMjIwNS4zMSwxMjYuNjM2IDIyMDUuMzEsMTI2LjU1'
    'QzIyMDUuMzEsMTI1LjU0MSAyMjA1LjMxLDEyMi45NzYgMjIwNS4zMSwxMjEuOTY2WiIgc3R5bGU9ImZpbGw6cmdiKDEwNCww'
    'LDE5Nyk7Ii8+CiAgICAgICAgICAgICAgICA8Y2xpcFBhdGggaWQ9Il9jbGlwNSI+CiAgICAgICAgICAgICAgICAgICAgPHBh'
    'dGggZD0iTTIyMDUuMzEsMTIxLjk2NkMyMjA1LjMxLDEyMS44OCAyMjA1LjE4LDEyMS44IDIyMDQuOTYsMTIxLjc1N0MyMjA0'
    'Ljc0LDEyMS43MTQgMjIwNC40OCwxMjEuNzE0IDIyMDQuMjcsMTIxLjc1N0MyMjAxLjc1LDEyMi4yNjMgMjE5NS4zNiwxMjMu'
    'NTQ3IDIxOTIuODUsMTI0LjA1MkMyMTkyLjYzLDEyNC4wOTUgMjE5Mi41LDEyNC4xNzQgMjE5Mi41LDEyNC4yNjFDMjE5Mi41'
    'LDEyNC4zNDcgMjE5Mi42MywxMjQuNDI2IDIxOTIuODUsMTI0LjQ2OUMyMTk1LjM2LDEyNC45NzQgMjIwMS43NSwxMjYuMjU1'
    'IDIyMDQuMjcsMTI2Ljc1OUMyMjA0LjQ4LDEyNi44MDIgMjIwNC43NCwxMjYuODAyIDIyMDQuOTYsMTI2Ljc1OUMyMjA1LjE4'
    'LDEyNi43MTYgMjIwNS4zMSwxMjYuNjM2IDIyMDUuMzEsMTI2LjU1QzIyMDUuMzEsMTI1LjU0MSAyMjA1LjMxLDEyMi45NzYg'
    'MjIwNS4zMSwxMjEuOTY2WiIvPgogICAgICAgICAgICAgICAgPC9jbGlwUGF0aD4KICAgICAgICAgICAgICAgIDxnIGNsaXAt'
    'cGF0aD0idXJsKCNfY2xpcDUpIj4KICAgICAgICAgICAgICAgICAgICA8ZyB0cmFuc2Zvcm09Im1hdHJpeCgwLjEyNDUwMiww'
    'LjA3NDkwNywwLjIwNjYyMywtMC4wNDE0Mzg0LDE5OTcuNjIsLTcuNDAyMzUpIj4KICAgICAgICAgICAgICAgICAgICAgICAg'
    'PHBhdGggZD0iTTE3MjYuMTcsLTY0LjI0OUwxNzA4LjE2LC03Mi4zMDNMMTcwOC4wNSwtMjMuNTE0TDE3MjEuODgsLTMyLjM4'
    'NkMxNzIyLjk2LC0zMy4yNDEgMTcyMy4wOSwtMzMuOTQ0IDE3MjMuMTUsLTM0LjYzNkwxNzIzLjE1LC01NC4zNzNDMTcyMy4x'
    'OSwtNTYuMjM4IDE3MjQuOTYsLTU3LjU5NCAxNzI2Ljg3LC01Ni42ODZMMTcyNi4xNywtNjQuMjQ5WiIgc3R5bGU9ImZpbGw6'
    'dXJsKCNfTGluZWFyNik7Ii8+CiAgICAgICAgICAgICAgICAgICAgPC9nPgogICAgICAgICAgICAgICAgICAgIDxnIHRyYW5z'
    'Zm9ybT0ibWF0cml4KC0wLjEyNjAzNiwwLjA3NjczNzcsMC41Njk4NTksMC4xMTI5MzMsMjQzNS4wMSwtMy4wOTIyNSkiPgog'
    'ICAgICAgICAgICAgICAgICAgICAgICA8cGF0aCBkPSJNMTcyNi4xNywtNDUuNjYxTDE3MDQuNDcsLTQwLjI1NEMxNzA2LjI4'
    'LC00MC41MjcgMTcwOC4xNCwtNDAuMjEyIDE3MDguMTYsLTM5LjQxNkwxNzA4LjE2LC0xOC45NzZMMTcyNi4xNywtMTguOTc2'
    'TDE3MjYuMTcsLTQ1LjY2MVoiIHN0eWxlPSJmaWxsOnJnYigxNDEsODEsMjQ5KTsiLz4KICAgICAgICAgICAgICAgICAgICA8'
    'L2c+CiAgICAgICAgICAgICAgICAgICAgPGcgdHJhbnNmb3JtPSJtYXRyaXgoLTAuMTI2MDM2LDAuMDc2NzM3NywwLjU2OTg1'
    'OSwwLjExMjkzMywyNDM1LjAxLC0zLjA5MjI1KSI+CiAgICAgICAgICAgICAgICAgICAgICAgIDxwYXRoIGQ9Ik0xNzI2LjE3'
    'LC00NS42NjFMMTcyNi4xNywtMTguOTc2TDE3MDguMTYsLTE4Ljk3NkwxNzA4LjE2LC0zOS40MTZDMTcwNy43OSwtNDAuNzMy'
    'IDE3MDQuNSwtNDAuMjk4IDE3MDIuNjgsLTQwLjAyNUwxNzI2LjE3LC00NS42NjFaTTE3MDUuNDksLTQwLjQ5MUMxNzA2LjIs'
    'LTQwLjUwNyAxNzA2Ljg3LC00MC40NjQgMTcwNy40LC00MC4zMjdDMTcwOC4wMSwtNDAuMTczIDE3MDguNDgsLTM5Ljg5OSAx'
    'NzA4LjYyLC0zOS40MzZDMTcwOC42MiwtMzkuNDI5IDE3MDguNjIsLTM5LjQyMyAxNzA4LjYyLC0zOS40MTZMMTcwOC42Miwt'
    'MTkuMTUyQzE3MDguNjIsLTE5LjE1MiAxNzI1LjcyLC0xOS4xNTIgMTcyNS43MiwtMTkuMTUyTDE3MjUuNzIsLTQ1LjM0NUwx'
    'NzA1LjQ5LC00MC40OTFaIiBzdHlsZT0iZmlsbDp1cmwoI19SYWRpYWw3KTsiLz4KICAgICAgICAgICAgICAgICAgICA8L2c+'
    'CiAgICAgICAgICAgICAgICA8L2c+CiAgICAgICAgICAgIDwvZz4KICAgICAgICA8L2c+CiAgICA8L2c+CiAgICA8ZGVmcz4K'
    'ICAgICAgICA8bGluZWFyR3JhZGllbnQgaWQ9Il9MaW5lYXIxIiB4MT0iMCIgeTE9IjAiIHgyPSIxIiB5Mj0iMCIgZ3JhZGll'
    'bnRVbml0cz0idXNlclNwYWNlT25Vc2UiIGdyYWRpZW50VHJhbnNmb3JtPSJtYXRyaXgoLTcwLjA3MTEsLTAuOTI3NjExLDEu'
    'NTQ0ODIsLTQyLjA3NTIsMjIzMy41OSwtMjAuMTg5MSkiPjxzdG9wIG9mZnNldD0iMCIgc3R5bGU9InN0b3AtY29sb3I6cmdi'
    'KDE0MSw4MSwyNDkpO3N0b3Atb3BhY2l0eToxIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdHlsZT0ic3RvcC1jb2xvcjpyZ2IoMTE2'
    'LDUwLDIyMyk7c3RvcC1vcGFjaXR5OjEiLz48L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBpZD0i'
    'X0xpbmVhcjIiIHgxPSIwIiB5MT0iMCIgeDI9IjEiIHkyPSIwIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgZ3Jh'
    'ZGllbnRUcmFuc2Zvcm09Im1hdHJpeCg0Ljc4MTkzZS0xNSwtNzguMDk0OSw3OC4wOTQ5LDQuNzgxOTNlLTE1LDIxOTUuNzIs'
    'MzU0LjAyMSkiPjxzdG9wIG9mZnNldD0iMCIgc3R5bGU9InN0b3AtY29sb3I6cmdiKDE0MSw4MSwyNDkpO3N0b3Atb3BhY2l0'
    'eToxIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdHlsZT0ic3RvcC1jb2xvcjpyZ2IoMTE2LDUwLDIyMyk7c3RvcC1vcGFjaXR5OjEi'
    'Lz48L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBpZD0iX0xpbmVhcjMiIHgxPSIwIiB5MT0iMCIg'
    'eDI9IjEiIHkyPSIwIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgZ3JhZGllbnRUcmFuc2Zvcm09Im1hdHJpeCg0'
    'MS42MDg5LDQxLjU4NjYsLTQxLjU4NjYsNDEuNjA4OSwyMjgyLjMxLDI2Mi44MzcpIj48c3RvcCBvZmZzZXQ9IjAiIHN0eWxl'
    'PSJzdG9wLWNvbG9yOnJnYigyMTEsMTg3LDI1NSk7c3RvcC1vcGFjaXR5OjEiLz48c3RvcCBvZmZzZXQ9IjEiIHN0eWxlPSJz'
    'dG9wLWNvbG9yOnJnYigyMTEsMTg3LDI1NSk7c3RvcC1vcGFjaXR5OjAiLz48L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxs'
    'aW5lYXJHcmFkaWVudCBpZD0iX0xpbmVhcjQiIHgxPSIwIiB5MT0iMCIgeDI9IjEiIHkyPSIwIiBncmFkaWVudFVuaXRzPSJ1'
    'c2VyU3BhY2VPblVzZSIgZ3JhZGllbnRUcmFuc2Zvcm09Im1hdHJpeCg5LjI1NjE2LDE2LjcwMDUsLTE2LjcwMDUsOS4yNTYx'
    'NiwyMjE1LDI0My43MTIpIj48c3RvcCBvZmZzZXQ9IjAiIHN0eWxlPSJzdG9wLWNvbG9yOnJnYigyMTEsMTg3LDI1NSk7c3Rv'
    'cC1vcGFjaXR5OjEiLz48c3RvcCBvZmZzZXQ9IjEiIHN0eWxlPSJzdG9wLWNvbG9yOnJnYigyMTEsMTg3LDI1NSk7c3RvcC1v'
    'cGFjaXR5OjAiLz48L2xpbmVhckdyYWRpZW50PgogICAgICAgIDxsaW5lYXJHcmFkaWVudCBpZD0iX0xpbmVhcjYiIHgxPSIw'
    'IiB5MT0iMCIgeDI9IjEiIHkyPSIwIiBncmFkaWVudFVuaXRzPSJ1c2VyU3BhY2VPblVzZSIgZ3JhZGllbnRUcmFuc2Zvcm09'
    'Im1hdHJpeCgtMC4xMzAxNjQsLTYxLjk5MzcsNTkuNDAwMywtMC4xMzU4NDcsMTcxMS42MywtMjUuNzk1NykiPjxzdG9wIG9m'
    'ZnNldD0iMCIgc3R5bGU9InN0b3AtY29sb3I6cmdiKDExNiw1MCwyMjMpO3N0b3Atb3BhY2l0eToxIi8+PHN0b3Agb2Zmc2V0'
    'PSIwLjUxIiBzdHlsZT0ic3RvcC1jb2xvcjpyZ2IoMTEwLDM4LDIxNyk7c3RvcC1vcGFjaXR5OjEiLz48c3RvcCBvZmZzZXQ9'
    'IjEiIHN0eWxlPSJzdG9wLWNvbG9yOnJnYig5MSwwLDE5Nyk7c3RvcC1vcGFjaXR5OjEiLz48L2xpbmVhckdyYWRpZW50Pgog'
    'ICAgICAgIDxyYWRpYWxHcmFkaWVudCBpZD0iX1JhZGlhbDciIGN4PSIwIiBjeT0iMCIgcj0iMSIgZ3JhZGllbnRVbml0cz0i'
    'dXNlclNwYWNlT25Vc2UiIGdyYWRpZW50VHJhbnNmb3JtPSJtYXRyaXgoMTMuODY1OSw0LjcxNDM2LC0xMi4xNjA5LDUuMzc1'
    'MzQsMTcwOC4xNiwtMzIuMjg3KSI+PHN0b3Agb2Zmc2V0PSIwIiBzdHlsZT0ic3RvcC1jb2xvcjpyZ2IoMjExLDE4NywyNTUp'
    'O3N0b3Atb3BhY2l0eToxIi8+PHN0b3Agb2Zmc2V0PSIxIiBzdHlsZT0ic3RvcC1jb2xvcjpyZ2IoMjExLDE4NywyNTUpO3N0'
    'b3Atb3BhY2l0eTowIi8+PC9yYWRpYWxHcmFkaWVudD4KICAgIDwvZGVmcz4KPC9zdmc+Cg=='
)
LOGO_URI = 'data:image/svg+xml;base64,' + _LOGO_B64


def promo_card() -> dict:
    """构造 App 推荐卡：设置页与详情页共用，永远放在第一位。

    版式（Vuetify 3，与宿主其他卡片同一套密度）：

        [官方 logo 40] MoviePilotApp                        [GitHub] [下载 App]
                       安卓客户端 · 榜单 / 订阅 / 下载整理 / 在线播放

    几个刻意的取舍：

    * **不垫紫色底块**。官方 logo 本身就是紫色渐变，再垫一层紫底会把六边形的
      边缘吃掉，深色主题下两层紫叠在一起更糊。
    * **「下载 App」是主按钮**（实底主色、``mdi-download``），GitHub 降为文字
      按钮。推荐卡的目的就是让人把 App 装上，落到仓库首页还得自己找 Release。
    * **窄屏靠 ``flex-wrap`` 折行**，文字块给 ``min-width: 150px`` 兜住 ——
      只靠 ``min-w-0`` 的话标题会被压成竖排，按钮永远不换行。
    """
    return {
        'component': 'VCard',
        'props': {'variant': 'outlined', 'class': 'mb-3'},
        'content': [
            {
                'component': 'div',
                'props': {
                    'class': 'd-flex align-center flex-wrap ga-3',
                    'style': {'padding': '12px 16px'},
                },
                'content': [
                    {
                        # 原生 ``img`` 而不是 ``VImg``：VImg 默认走 IntersectionObserver
                        # 懒加载 + 透明度淡入，在宿主弹窗里首帧是一片空白（截图时肉眼可见
                        # 只有半个六边形轮廓），而这里只有一张 14 KB 的 data URI，没有
                        # 懒加载的必要。
                        'component': 'img',
                        'props': {
                            'src': LOGO_URI,
                            'alt': APP_NAME,
                            'width': 40,
                            'height': 40,
                            'class': 'flex-shrink-0',
                            'style': {
                                'width': '40px',
                                'height': '40px',
                                'object-fit': 'contain',
                                'display': 'block',
                            },
                        },
                    },
                    {
                        'component': 'div',
                        'props': {
                            'class': 'min-w-0 flex-grow-1',
                            'style': {'min-width': '150px'},
                        },
                        'content': [
                            {
                                'component': 'div',
                                'props': {'class': 'text-body-1 font-weight-bold'},
                                'text': APP_NAME,
                            },
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'text-caption text-medium-emphasis',
                                    'style': {'line-height': '1.5'},
                                },
                                'text': APP_TAGLINE,
                            },
                        ],
                    },
                    {
                        'component': 'div',
                        'props': {
                            'class': 'd-flex align-center ga-2 flex-shrink-0',
                        },
                        'content': [
                            {
                                'component': 'VBtn',
                                'props': {
                                    'href': APP_REPO,
                                    'target': '_blank',
                                    'variant': 'text',
                                    'size': 'small',
                                    'class': 'text-medium-emphasis',
                                    # 宿主的 Vuetify 默认 text-transform: uppercase，
                                    # 会把「GitHub」写成「GITHUB」、「App」写成「APP」
                                    'style': {'text-transform': 'none'},
                                },
                                'text': 'GitHub',
                            },
                            {
                                'component': 'VBtn',
                                'props': {
                                    'href': APP_RELEASES,
                                    'target': '_blank',
                                    'color': 'primary',
                                    'variant': 'flat',
                                    'size': 'small',
                                    'prepend-icon': 'mdi-download',
                                    'style': {'text-transform': 'none'},
                                },
                                'text': '下载 App',
                            },
                        ],
                    },
                ],
            }
        ],
    }
