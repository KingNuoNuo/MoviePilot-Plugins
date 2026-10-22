"""CronTrigger 垫片：只保留 from_crontab 入口。"""


class CronTrigger:
    def __init__(self, expr=""):
        self.expr = expr

    @classmethod
    def from_crontab(cls, expr):
        return cls(expr)
