"""Process-local background jobs for submit/fetch nodes."""

import hashlib
import os
import secrets
import threading
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


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ziyuan")
_jobs = {}
_fetch_batches = {}
_lock = threading.Lock()


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
        # Never evict an unread result, even if generation has already finished.
        if len(_jobs) >= 32:
            completed = next((key for key, job in _jobs.items() if job.retrieved and job.future.done()), None)
            if completed is None:
                raise RuntimeError("已有 32 个未获取的异步任务，请先获取现有结果，再继续提交。")
            del _jobs[completed]
            for batch_key, batch in list(_fetch_batches.items()):
                if completed in batch:
                    del _fetch_batches[batch_key]
        task_id = uuid.uuid4().hex
        future = _executor.submit(operation, check_cancel)
        _jobs[task_id] = Job(kind, future, cancel, key_digest)
    return task_id


def fetch_batch(kind, check_cancel, api_key):
    key_digest = _api_key_digest(api_key)
    batch_key = (kind, key_digest)
    with _lock:
        batch = _fetch_batches.get(batch_key, ())
        # Freeze an unfinished fetch so later submissions cannot enter its retry.
        if not batch or all(_jobs[task_id].retrieved for task_id in batch):
            pending = tuple(task_id for task_id, job in _jobs.items()
                            if not job.retrieved and job.kind == kind
                            and secrets.compare_digest(job.api_key_digest, key_digest))
            batch = pending or batch
            if batch:
                _fetch_batches[batch_key] = batch
        selected = tuple(_jobs[task_id] for task_id in batch)
    if not selected:
        label = "图片" if kind == "image" else "视频"
        raise ValueError(f"找不到该 API密钥的本地{label}任务，请先执行对应的异步提交节点；重启后本地记录失效。")
    results = []
    for index, job in enumerate(selected, 1):
        try:
            results.append(_wait(job, check_cancel))
        except InterruptedError:
            raise
        except Exception as exc:
            raise RuntimeError(f"本批共 {len(selected)} 个任务，第 {index} 个任务获取失败：{exc}；"
                               "本批未标记为已获取，不会重新提交生成。") from exc
    with _lock:
        for job in selected:
            job.retrieved = True
    return results


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
