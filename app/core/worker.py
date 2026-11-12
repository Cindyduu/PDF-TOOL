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
from pathlib import Path
from typing import Any

from app.core import atomic
from app.core.storage import utcnow_iso
from app.pdf.pipeline import PipelineError, run_pipeline
from app.report import render_process_log
from app.schemas import VERIFY_KEYS, expand_process_groups
from app.services.jobs import PROCESS_LOG_FILE_NAME
from app.services.verify import (
    PHASE_POST,
    PHASE_PRECHECK,
    build_failed_verify_result,
    build_verify_result,
)

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

        status = job.get("status")
        # 队列里只有两个合法入口状态：校验队列与处理队列。其余状态一律跳过
        # （重复入队、或已被 reconcile 标记中断），避免同一个任务被跑两遍。
        if status == "verify_queued":
            runner = self._run_verify
        elif status == "process_queued":
            runner = self._run
        else:
            self.log.info("任务 %s 状态为 %s，跳过执行", job_id, status)
            return

        try:
            runner(job_id)
        except BaseException as exc:  # noqa: BLE001
            self.last_error = exc
            self.log.exception("任务 %s 执行失败", job_id)
            try:
                jobs.finish_failed(job_id, exc)
            except Exception:  # noqa: BLE001
                self.log.exception("任务 %s 的失败状态写入失败", job_id)

    def _run_verify(self, job_id: str) -> None:
        """前置校验（R13）：只读打开源文件，跑十二项检查，落结果，不改文件。

        与 `_run` 的分工：这里全程不写 PDF，唯一的写操作是 `finish_verified`
        落 `job.json`。校验器自身也不允许改文件——`build_verify_result` 结束前
        会用上传时的 sha256 复核，不一致直接判失败。
        """
        jobs = self.ctx.jobs
        job = jobs.mark_verifying(job_id)
        source = jobs.source_path(job_id)
        # 报告里写的是**用户看到的名字**，不是磁盘上的存储名。`original.pdf`
        # 是内部约定，出现在报告标题上会让人以为自己的文件被改了名。
        name = str(
            job["source"].get("original_name")
            or job.get("original_name")
            or source.name
        )

        def on_item(key: str, result: Any, index: int, total: int) -> None:
            jobs.record_verify_progress(
                job_id,
                key=key,
                status=str(getattr(result, "status", "")),
                index=index,
                total=total,
            )

        result = build_verify_result(
            job,
            path=source,
            filename=name,
            phase=PHASE_PRECHECK,
            on_item=on_item,
        )
        jobs.finish_verified(job_id, result)

    def _run(self, job_id: str) -> None:
        jobs = self.ctx.jobs
        job = jobs.mark_running(job_id)
        source, pending = jobs.work_paths(job_id)

        def on_stage(stage: str) -> None:
            jobs.record_stage(job_id, stage)

        config = job["config"]
        try:
            result = run_pipeline(
                source,
                pending,
                exempt_pages=tuple(config.get("exempt_pages") or ()),
                operations=expand_process_groups(config.get("process_items") or ()),
                stage_callback=on_stage,
            )
        except PipelineError as exc:
            # AC-R27-N2：没有产物就没有后置校验，任务在这里就到终点了。
            jobs.finish_failed(job_id, exc, stage=None)
            return

        # 处理完成不等于可以交付：先进入后置校验（R27），
        # 校验通过才允许发布输出文件。
        jobs.begin_post_verify(job_id)
        post, forced_status = self._run_post_verify(job_id, pending)
        jobs.finish_post_verify(job_id, post)
        jobs.finish_success(job_id, result, pending, status=forced_status)

        # 日志放在发布之后：它描述的是「这个文件最后怎么了」，
        # 在输出名还没定下来的时刻渲染，日志里就会出现一个并不存在的名字。
        self._write_process_log(job_id)

    def _run_post_verify(
        self, job_id: str, pending: Path
    ) -> tuple[dict[str, Any] | None, str | None]:
        """对**产物**再跑一遍同样的十二项（R27）。

        返回 `(结果, 需要覆盖的终态)`。两种情况：

        - 正常：返回完整结果，终态交给处理项的结论决定（`None`）；
        - 读不出产物（损坏、被占用、磁盘错）：按 AC-R27-E1 整块记 `fail`，
          并把终态压到 `completed_with_warnings` —— 产物仍然发布（处理本身
          是成功的，用户需要一个能下载的文件），但绝不能顶着 `completed` 出去。

        后置校验的异常**不许**冒泡到 `_execute`：那里会把整个任务判成 `failed`，
        连已经处理好的产物一起丢掉，这比"校验没做成"严重得多。
        """
        jobs = self.ctx.jobs
        job = jobs.get_job(job_id)
        name = str(job.get("original_name") or pending.name)

        # AC-R27-N1 要求「完整 9 行」：后置校验是强制的固定动作（R27），
        # 不跟着用户在准备页勾了几项走。用户在准备页少勾两项，是"这次不想看"，
        # 不是"这份产物只查这七项"——产物该过哪些关，不由那次勾选决定。
        post_job = dict(job)
        post_job["config"] = {**job["config"], "verify_items": list(VERIFY_KEYS)}

        def on_item(key: str, result: Any, index: int, total: int) -> None:
            jobs.record_verify_progress(
                job_id,
                key=key,
                status=str(getattr(result, "status", "")),
                index=index,
                total=total,
                phase=PHASE_POST,
            )

        try:
            result = build_verify_result(
                post_job,
                path=pending,
                filename=name,
                phase=PHASE_POST,
                on_item=on_item,
            )
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            self.log.warning("任务 %s 的后置校验未能完成：%r", job_id, exc)
            first_line = str(exc).strip().splitlines()
            reason = f"后置校验未能执行：{first_line[0] if first_line else type(exc).__name__}"
            failed = build_failed_verify_result(
                post_job, filename=name, reason=reason, path=pending
            )
            return failed, "completed_with_warnings"
        return result, None

    def _write_process_log(self, job_id: str) -> None:
        """勾了「生成处理日志」就把整批的现状渲染成一份 HTML 落盘（R28）。

        AC-R28-E1：日志生成失败**不影响**处理结果与下载，只在结果页提示
        「日志生成失败」。所以这里全程吞异常，失败时把 `process_log_path`
        显式写成 `None` —— 页面据此把「导出日志」按钮置灰并给出提示，
        而不是让用户点一个必定 404 的按钮。
        """
        jobs = self.ctx.jobs
        try:
            job = jobs.get_job(job_id)
            if not job["config"].get("write_process_log"):
                return
            batch_id = job["batch_id"]
            reports = jobs.batch_results(batch_id)["files"]
            document = render_process_log(
                batch_id=batch_id, reports=reports, generated_at=utcnow_iso()
            )
            directory = jobs.work_paths(job_id)[0].parent
            atomic.atomic_write_bytes(
                directory / PROCESS_LOG_FILE_NAME, document.encode("utf-8")
            )
            jobs.record_process_log(job_id, PROCESS_LOG_FILE_NAME)
            # report.json 是机器数据源，也要跟着更新：否则页面 4 从 job.json
            # 读到"有日志"、从 report.json 读到"没有"，两个消费者各说一套。
            jobs.write_report(jobs.get_job(job_id))
        except Exception:  # noqa: BLE001 - AC-R28-E1
            self.log.exception("任务 %s 的处理日志生成失败（不影响处理结果）", job_id)
            try:
                jobs.record_process_log(job_id, None)
            except Exception:  # noqa: BLE001
                self.log.exception("任务 %s 的日志状态写入失败", job_id)


def worker_stage_sequence() -> tuple[str, ...]:
    """后台线程可能上报的阶段名（供测试与文档核对，避免手写字符串）。"""
    from app.schemas import STAGE_NAMES

    return tuple(STAGE_NAMES)
