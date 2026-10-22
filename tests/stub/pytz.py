"""pytz 垫片：提供被测代码用到的时区（固定 +08:00，等价 Asia/Shanghai）。"""

import datetime
from zoneinfo import ZoneInfo


class _FixedShanghai(datetime.tzinfo):
    """固定 +08:00 时区，等价 Asia/Shanghai（无夏令时）。"""

    _OFFSET = datetime.timedelta(hours=8)

    def utcoffset(self, dt):
        return self._OFFSET

    def tzname(self, dt):
        return "Asia/Shanghai"

    def dst(self, dt):
        return datetime.timedelta(0)


def timezone(name):
    """按名称返回时区；本环境只用到亚洲/上海。"""
    if name in {"Asia/Shanghai", "Asia/Chongqing", "Asia/Harbin"}:
        return _FixedShanghai()
    try:
        return ZoneInfo(name)
    except Exception:
        return _FixedShanghai()


utc = datetime.timezone.utc
