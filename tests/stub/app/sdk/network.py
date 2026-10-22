"""宿主 ``app.sdk.network`` 垫片。

宿主的 ``app.sdk.network`` 只是转发，真正实现在
``app.adapters.network.http.RequestUtils``（MoviePilot v3）。这里照它的
**公开签名**做最小替身，插件里用到的每个方法都在这里有对应入口：

======================  ==================================================
宿主方法                 插件里的用法
======================  ==================================================
``request(method, url,  ``douban.py`` 走这条：要拿 ``Response`` 对象
raise_exception=True,    （``status_code`` / ``json()``），不能用返回文本的
**kwargs)``              ``get``；``raise_exception=True`` 让连接异常抛出
``get(url, params,      猫眼 / Netflix 抓取走这条（要响应文本）
**kwargs)``
``get_res(url, ...)``   猫眼榜 HTML 抓取（要 ``.text`` 与 ``raise_for_status``）
======================  ==================================================

⚠️ 超时语义照宿主：``RequestUtils(timeout=None)`` 时**默认 20 秒**，
不会因为没显式传 timeout 而无限等待。
"""


class RequestUtils:
    """HTTP 请求替身：测试中会被 monkeypatch 替换，这里只保证接口齐全。"""

    def __init__(self, headers=None, ua=None, cookies=None, proxies=None,
                 session=None, use_session=False, timeout=None, verify=False,
                 referer=None, content_type=None, accept_type=None):
        self.headers = headers or {}
        self.ua = ua
        self.cookies = cookies
        self.proxies = proxies
        self.session = session
        self.use_session = use_session
        # 与宿主一致：未显式指定时默认 20 秒
        self.timeout = timeout or 20
        self.verify = verify
        self.referer = referer

    def request(self, method, url, raise_exception=False, **kwargs):
        """发起请求。宿主实现在网络异常时按 ``raise_exception`` 决定抛出还是返回 None。"""
        raise RuntimeError("network disabled in stub environment")

    def get(self, url, params=None, **kwargs):
        """GET 并返回响应文本（宿主语义：异常时返回 None）。"""
        raise RuntimeError("network disabled in stub environment")

    def post(self, url, data=None, json=None, **kwargs):
        raise RuntimeError("network disabled in stub environment")

    def put(self, url, data=None, **kwargs):
        raise RuntimeError("network disabled in stub environment")

    def get_res(self, url, **kwargs):
        """GET 并返回 Response 对象（要 ``.text`` / ``.status_code``）。"""
        raise RuntimeError("network disabled in stub environment")

    def delete(self, url, **kwargs):
        raise RuntimeError("network disabled in stub environment")


__all__ = ["RequestUtils"]
