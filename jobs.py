"""Process-local background jobs for submit/fetch nodes."""

import hashlib
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass


@dataclass
class Job:
    kind: str
    future: object
    cancel: threading.Event
    api_key_digest: bytes
    retrieved: bool = False


_executors = {kind: ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"ziyuan-{kind}")
              for kind in ("image", "video")}
_jobs = {}
_fetch_batches = {}
_fetching = set()
_lock = threading.Lock()


class BatchResults(list):
    def __init__(self, values, report=""):
        super().__init__(values)
        self.report = report


def resolve_api_key(api_key):
    key = (str(api_key or "").strip() or os.environ.get("ZIYUAN_API_KEY", "").strip())
    if not key:
        raise ValueError("请填写 API Key，或设置 ZIYUAN_API_KEY 环境变量。")
    return key


def _api_key_digest(api_key):
    return hashlib.sha256(resolve_api_key(api_key).encode("utf-8")).digest()


def submit(kind, operation, api_key):
    cancel = threading.Event()
    key_digest = _api_key_digest(api_key)

    def check_cancel():
        if cancel.is_set():
            raise InterruptedError("本地异步任务已停止等待；服务器任务可能仍在运行。")

    with _lock:
        # Like KR's queue, unread tasks are retained until fetched.
        protected = {task_id for key, batch in _fetch_batches.items() if key in _fetching for task_id in batch}
        for completed in [key for key, job in _jobs.items()
                          if job.retrieved and job.future.done() and key not in protected]:
            del _jobs[completed]
            for batch_key, batch in list(_fetch_batches.items()):
                if completed in batch:
                    del _fetch_batches[batch_key]
        task_id = uuid.uuid4().hex
        future = _executors[kind].submit(operation, check_cancel)
        _jobs[task_id] = Job(kind, future, cancel, key_digest)
    return task_id


def fetch_batch(kind, check_cancel, api_key, max_wait=300):
    key_digest = _api_key_digest(api_key)
    batch_key = (kind, key_digest)
    with _lock:
        if batch_key in _fetching:
            raise RuntimeError("同一密钥、同一类型的结果正在获取，请等待本次获取结束。")
        batch = tuple(task_id for task_id, job in _jobs.items()
                      if not job.retrieved and job.kind == kind
                      and secrets.compare_digest(job.api_key_digest, key_digest))
        batch = batch or _fetch_batches.get(batch_key, ())
        selected = tuple(_jobs[task_id] for task_id in batch)
        if selected:
            _fetching.add(batch_key)
    if not selected:
        label = "图片" if kind == "image" else "视频"
        raise ValueError(f"找不到该 API密钥的本地{label}任务，请先执行对应的异步提交节点；重启后本地记录失效。")
    try:
        deadline = time.monotonic() + max_wait
        while True:
            check_cancel()
            if all(job.future.done() for job in selected) or time.monotonic() >= deadline:
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        results, finished, successful, failures = [], [], [], []
        for index, (task_id, job) in enumerate(zip(batch, selected), 1):
            if not job.future.done():
                continue
            finished.append(task_id)
            try:
                results.append(job.future.result())
                successful.append(task_id)
            except Exception as exc:
                detail = str(exc).replace(resolve_api_key(api_key), "[REDACTED]")
                failures.append(f"第{index}项失败：{detail}")
        pending_count = len(selected) - len(finished)
        report = (f"本批{len(selected)}项：成功{len(results)}，失败{len(failures)}，等待中{pending_count}。"
                  "未完成任务保留到下次获取；获取不会重新提交生成。")
        if failures:
            report += "\n" + "\n".join(failures)
        with _lock:
            for task_id in finished:
                _jobs[task_id].retrieved = True
            if successful:
                _fetch_batches[batch_key] = tuple(successful)
            else:
                _fetch_batches.pop(batch_key, None)
        if not results:
            if pending_count:
                raise TimeoutError(report)
            raise RuntimeError(report)
        return BatchResults(results, report if failures or pending_count else "")
    finally:
        with _lock:
            _fetching.discard(batch_key)


def fetch(task_id, kind, check_cancel, api_key):
    with _lock:
        job = _jobs.get(task_id.strip())
    if job is None:
        raise ValueError("找不到本地异步任务：请先执行同一次 ComfyUI 运行中的提交节点。重启后本地记录失效。")
    if job.kind != kind:
        raise ValueError("任务类型不匹配：图片任务使用图片获取节点，视频任务使用视频获取节点。")
    if not secrets.compare_digest(job.api_key_digest, _api_key_digest(api_key)):
        raise ValueError("API密钥与提交该任务时使用的密钥不一致。")
    return _wait(job, check_cancel)


def _wait(job, check_cancel):
    while True:
        # Stopping a fetch must not destroy a server task or its polling worker.
        check_cancel()
        try:
            return job.future.result(timeout=0.2)
        except FutureTimeout:
            # A finished operation can itself raise TimeoutError: propagate it.
            if job.future.done():
                return job.future.result()
