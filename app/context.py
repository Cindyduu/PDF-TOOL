"""应用运行时上下文：把配置、存储、锁和服务串在一起。

服务按需惰性构造，避免模块导入顺序耦合。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings, load_settings
from app.core.logging import get_logger
from app.core.locking import LockManager
from app.core.storage import Storage


class AppContext:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.storage = Storage(settings)
        self.storage.init()
        self.generation_id = self.storage.ensure_active()
        self.locks = LockManager(self.storage)
        self.log = get_logger("ctx")
        self._jobs = None
        self._history = None
        self._backups = None
        self._worker = None

    # ---- 服务（惰性） ---------------------------------------------------
    @property
    def jobs(self):
        if self._jobs is None:
            from app.services.jobs import JobService

            self._jobs = JobService(self)
        return self._jobs

    @property
    def history(self):
        if self._history is None:
            from app.services.history import HistoryService

            self._history = HistoryService(self)
        return self._history

    @property
    def backups(self):
        if self._backups is None:
            from app.services.backups import BackupService

            self._backups = BackupService(self)
        return self._backups

    @property
    def worker(self):
        if self._worker is None:
            from app.core.worker import JobWorker

            self._worker = JobWorker(self)
        return self._worker

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        # 先回收残留任务，再启动执行线程：反过来的话，刚被放上队列的旧任务
        # 会在 reconcile 眼里是"正在跑"，被误标成中断。
        self.jobs.reconcile_interrupted()
        self.worker.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()

    def var_dir(self) -> Path:
        return self.settings.var_dir


def build_context(var_dir: Path | str | None = None, settings: Settings | None = None) -> AppContext:
    return AppContext(settings or load_settings(var_dir))
