"""BackgroundScheduler 垫片：只记录任务，不真正起线程。"""


class BackgroundScheduler:
    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs = []
        self.running = False

    def add_job(self, func=None, trigger=None, name=None, run_date=None):
        self.jobs.append({"func": func, "trigger": trigger, "name": name})

    def get_jobs(self):
        return list(self.jobs)

    def print_jobs(self):
        pass

    def start(self):
        self.running = True

    def remove_all_jobs(self):
        self.jobs = []

    def shutdown(self, wait=True):
        self.running = False
