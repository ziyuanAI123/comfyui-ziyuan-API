"""Process-local background jobs for connected submit/fetch nodes."""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass


@dataclass
class Job:
    kind: str
    future: object
    cancel: threading.Event


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ziyuan")
_jobs = {}
_lock = threading.Lock()


def submit(kind, operation):
    cancel = threading.Event()

    def check_cancel():
        if cancel.is_set():
            raise InterruptedError("本地异步任务已停止等待；服务器任务可能仍在运行。")

    with _lock:
        # Bound retained IMAGE tensors and queued requests; evict completed jobs only.
        if len(_jobs) >= 32:
            completed = next((key for key, job in _jobs.items() if job.future.done()), None)
            if completed is None:
                raise RuntimeError("已有 32 个异步任务，请先等待现有任务完成。")
            del _jobs[completed]
        task_id = uuid.uuid4().hex
        future = _executor.submit(operation, check_cancel)
        _jobs[task_id] = Job(kind, future, cancel)
    return task_id


def fetch(task_id, kind, check_cancel):
    with _lock:
        job = _jobs.get(task_id.strip())
    if job is None:
        raise ValueError("找不到本地异步任务：请连接同一次 ComfyUI 运行中的提交节点。重启后旧任务编号失效。")
    if job.kind != kind:
        raise ValueError("任务类型不匹配：图片任务连接图片获取节点，视频任务连接视频获取节点。")
    while True:
        try:
            check_cancel()
        except Exception:
            job.cancel.set()
            job.future.cancel()
            raise
        try:
            return job.future.result(timeout=0.2)
        except FutureTimeout:
            # A finished operation can itself raise TimeoutError: propagate it.
            if job.future.done():
                return job.future.result()
