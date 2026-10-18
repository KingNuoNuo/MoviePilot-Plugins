"""生成四个插件里的 ``app_promo.py``（App 推荐卡）。

为什么是一份代码复制四份，而不是抽成公共包：

    宿主的插件按目录隔离安装（``/app/app/plugins/<插件名>/``），插件之间的
    import 不保证可用，跨插件的相对导入在打包成 zip 后直接失效。共享代码只能
    各带一份。

代价是四份副本可能漂移，所以这里用生成器兜底：**唯一来源是本脚本的 ``TEMPLATE``
与 ``assets/moviepilot-logo.svg``**，四份文件逐字节相同。

用法：

    python tools/gen_app_promo.py           # 写入四个插件目录
    python tools/gen_app_promo.py --check   # 只校验，四份不一致/过期时退出码 1

改推荐卡文案 → 改下面的 ``TEMPLATE`` → 重跑本脚本。不要手改插件目录里的副本。
"""
from __future__ import annotations

import base64
import hashlib
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / 'assets' / 'moviepilot-logo.svg'

# 需要带这张卡片的插件目录（相对 plugins.v3/）
PLUGINS = (
    'globalrankpro',
    'maoyanpro',
    'netflixpro',
    'mediastreampro',
)

# 横幅里写进源文件指纹，方便一眼看出某份副本是不是过期了
BANNER = '''"""MoviePilotApp 推荐卡（本文件由 ``tools/gen_app_promo.py`` 生成，请勿手改）。

四个插件各存一份，内容逐字节相同 —— 宿主按插件目录隔离安装，插件之间不能互相
import，共享代码只能各带一份。改文案请改 ``tools/gen_app_promo.py`` 里的
``TEMPLATE`` 再重跑生成器。

logo 源文件 sha256：{logo_sha}
"""
'''

TEMPLATE = '''
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
{logo_literal}
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
    return {{
        'component': 'VCard',
        'props': {{'variant': 'outlined', 'class': 'mb-3'}},
        'content': [
            {{
                'component': 'div',
                'props': {{
                    'class': 'd-flex align-center flex-wrap ga-3',
                    'style': {{'padding': '12px 16px'}},
                }},
                'content': [
                    {{
                        # 原生 ``img`` 而不是 ``VImg``：VImg 默认走 IntersectionObserver
                        # 懒加载 + 透明度淡入，在宿主弹窗里首帧是一片空白（截图时肉眼可见
                        # 只有半个六边形轮廓），而这里只有一张 14 KB 的 data URI，没有
                        # 懒加载的必要。
                        'component': 'img',
                        'props': {{
                            'src': LOGO_URI,
                            'alt': APP_NAME,
                            'width': 40,
                            'height': 40,
                            'class': 'flex-shrink-0',
                            'style': {{
                                'width': '40px',
                                'height': '40px',
                                'object-fit': 'contain',
                                'display': 'block',
                            }},
                        }},
                    }},
                    {{
                        'component': 'div',
                        'props': {{
                            'class': 'min-w-0 flex-grow-1',
                            'style': {{'min-width': '150px'}},
                        }},
                        'content': [
                            {{
                                'component': 'div',
                                'props': {{'class': 'text-body-1 font-weight-bold'}},
                                'text': APP_NAME,
                            }},
                            {{
                                'component': 'div',
                                'props': {{
                                    'class': 'text-caption text-medium-emphasis',
                                    'style': {{'line-height': '1.5'}},
                                }},
                                'text': APP_TAGLINE,
                            }},
                        ],
                    }},
                    {{
                        'component': 'div',
                        'props': {{
                            'class': 'd-flex align-center ga-2 flex-shrink-0',
                        }},
                        'content': [
                            {{
                                'component': 'VBtn',
                                'props': {{
                                    'href': APP_REPO,
                                    'target': '_blank',
                                    'variant': 'text',
                                    'size': 'small',
                                    'class': 'text-medium-emphasis',
                                    # 宿主的 Vuetify 默认 text-transform: uppercase，
                                    # 会把「GitHub」写成「GITHUB」、「App」写成「APP」
                                    'style': {{'text-transform': 'none'}},
                                }},
                                'text': 'GitHub',
                            }},
                            {{
                                'component': 'VBtn',
                                'props': {{
                                    'href': APP_RELEASES,
                                    'target': '_blank',
                                    'color': 'primary',
                                    'variant': 'flat',
                                    'size': 'small',
                                    'prepend-icon': 'mdi-download',
                                    'style': {{'text-transform': 'none'}},
                                }},
                                'text': '下载 App',
                            }},
                        ],
                    }},
                ],
            }}
        ],
    }}
'''


def logo_literal() -> str:
    """把 base64 拆成每行 96 字符的字符串字面量，避免一行 14 KB 的源码。"""
    encoded = base64.b64encode(LOGO.read_bytes()).decode('ascii')
    lines = textwrap.wrap(encoded, 96)
    return '\n'.join("    '{0}'".format(line) for line in lines)


def build() -> str:
    digest = hashlib.sha256(LOGO.read_bytes()).hexdigest()
    banner = BANNER.format(logo_sha=digest)
    return banner + TEMPLATE.format(logo_literal=logo_literal()).lstrip('\n')


def main() -> int:
    check_only = '--check' in sys.argv[1:]
    content = build()
    stale = []
    for name in PLUGINS:
        target = ROOT / 'plugins.v3' / name / 'app_promo.py'
        current = target.read_text(encoding='utf-8') if target.exists() else None
        if current == content:
            print('ok      {0}'.format(target.relative_to(ROOT)))
            continue
        stale.append(target)
        if not check_only:
            target.write_text(content, encoding='utf-8', newline='\n')
            print('wrote   {0}'.format(target.relative_to(ROOT)))
        else:
            print('STALE   {0}'.format(target.relative_to(ROOT)))
    if check_only and stale:
        print('\n{0} 份副本与生成源不一致，跑一次 python tools/gen_app_promo.py'.format(
            len(stale)))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
