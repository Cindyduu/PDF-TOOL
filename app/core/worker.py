"""后台任务执行器：**一个**守护线程 + 一个 `queue.Queue`，严格串行。

为什么不用线程池：这类处理是 CPU 与磁盘密集的整文档操作，同时跑两个任务
只会互相抢内存，还会让「当前在处理哪个任务」变得含混。串行是特性，不是妥协。

线程只做一件事：从队列取 job_id，交给 `JobService` 跑完整流水线。
所有状态写入都经过 `state_machine.transition()`，这里不直接改 status。

`drain()` 是给测试和「进程即将退出」用的同步入口：在调用者线程里把队列跑空，
与后台线程走**同一段**执行逻辑，不存在两套代码路径。
"""

from __future__ import annotations

import queue
import threading
from typing import Any

from app.pdf.pipeline import PipelineError, run_pipeline

__all__ = ["JobWorker"]

_STOP = object()


class JobWorker:
    def __init__(self, ctx, *, name: str = "pdf-worker") -> None:
        self.ctx = ctx
        self.log = ctx.log
        self._name = name
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        # 记录最近一次执行结果，方便测试与日志判断"跑过没有"
        self.last_error: BaseException | None = None

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
            self._thread = thread
            thread.start()
        self.log.info("任务执行线程已启动")

    def stop(self, timeout: float = 10.0) -> None:
        with self._guard:
            thread = self._thread
            if thread is None:
                return
            self._stopping.set()
        self._queue.put(_STOP)
        thread.join(timeout=timeout)
        with self._guard:
            self._thread = None
        self.log.info("任务执行线程已停止")

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---- 入队 -----------------------------------------------------------
    def submit(self, job_id: str) -> None:
        """只入队，不启动线程。

        线程由 `AppContext.start()` 统一启动。测试里用 `start_services=False`
        建应用时不会有意外的后台线程，任务就停在 `queued`，状态完全可预测。
        """
        self._queue.put(job_id)

    def pending(self) -> int:
        return self._queue.qsize()

    # ---- 执行 -----------------------------------------------------------
    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self._execute(item)
            except BaseException as exc:  # noqa: BLE001 - 线程不能死
                # 线程绝不能因为单个任务抛错而退出：后面的任务还在队列里等着
                self.last_error = exc
                self.log.exception("任务执行线程捕获到未处理异常：%r", item)
            finally:
                self._queue.task_done()

    def drain(self, limit: int = 100) -> int:
        """在调用者线程里把队列跑空，返回处理条数。测试与退出前使用。"""
        processed = 0
        while processed < limit:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if item is _STOP:
                    continue
                self._execute(item)
                processed += 1
            finally:
                self._queue.task_done()
        return processed

    def _execute(self, job_id: str) -> None:
        """跑一个任务。任何异常都必须落到 job.json 里，不能留在内存里。"""
        jobs = self.ctx.jobs
        try:
            job = jobs.get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            self.last_error = exc
            self.log.warning("队列里的任务不存在，已跳过：%s", job_id)
            return

        if job.get("status") != "queued":
            # 重复入队、或者已经被 reconcile 标记中断：直接跳过，不重复处理
            self.log.info("任务 %s 状态为 %s，跳过执行", job_id, job.get("status"))
            return

        try:
            self._run(job_id)
        except BaseException as exc:  # noqa: BLE001
            self.last_error = exc
            self.log.exception("任务 %s 执行失败", job_id)
            try:
                jobs.finish_failed(job_id, exc)
            except Exception:  # noqa: BLE001
                self.log.exception("任务 %s 的失败状态写入失败", job_id)

    def _run(self, job_id: str) -> None:
        jobs = self.ctx.jobs
        job = jobs.mark_running(job_id)
        source, pending = jobs.work_paths(job_id)

        def on_stage(stage: str) -> None:
            jobs.record_stage(job_id, stage)

        try:
            result = run_pipeline(
                source,
                pending,
                exempt_pages=tuple(job["config"].get("exempt_pages") or ()),
                tag_choice=job["config"].get("tag_choice"),
                stage_callback=on_stage,
            )
        except PipelineError as exc:
            jobs.finish_failed(job_id, exc, stage=None)
            return
        jobs.finish_success(job_id, result, pending)


def worker_stage_sequence() -> tuple[str, ...]:
    """后台线程可能上报的阶段名（供测试与文档核对，避免手写字符串）。"""
    from app.schemas import STAGE_NAMES

    return tuple(STAGE_NAMES)
