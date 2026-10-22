"""宿主 app.sdk.browser 垫片。"""


def launch_browser_context(headless=True):
    """真实浏览器上下文由测试注入的假页替换，这里只保证导入成功。"""
    raise RuntimeError("browser unavailable in stub environment")
