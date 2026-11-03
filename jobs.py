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


_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ziyuan")
_jobs = {}
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
        # Bound retained IMAGE tensors and queued requests; evict completed jobs only.
        if len(_jobs) >= 32:
            completed = next((key for key, job in _jobs.items() if job.future.done()), None)
            if completed is None:
                raise RuntimeError("已有 32 个异步任务，请先等待现有任务完成。")
            del _jobs[completed]
        task_id = uuid.uuid4().hex
        future = _executor.submit(operation, check_cancel)
        _jobs[task_id] = Job(kind, future, cancel, key_digest)
    return task_id


def fetch_latest(kind, check_cancel, api_key):
    key_digest = _api_key_digest(api_key)
    with _lock:
        task_id = next((task_id for task_id, job in reversed(_jobs.items())
                        if job.kind == kind and secrets.compare_digest(job.api_key_digest, key_digest)), None)
    if task_id is None:
        label = "图片" if kind == "image" else "视频"
        raise ValueError(f"找不到该 API密钥的本地{label}任务，请先执行对应的异步提交节点；重启后本地记录失效。")
    return fetch(task_id, kind, check_cancel, api_key)


def fetch(task_id, kind, check_cancel, api_key):
    with _lock:
        job = _jobs.get(task_id.strip())
    if job is None:
        raise ValueError("找不到本地异步任务：请先执行同一次 ComfyUI 运行中的提交节点。重启后本地记录失效。")
    if job.kind != kind:
        raise ValueError("任务类型不匹配：图片任务使用图片获取节点，视频任务使用视频获取节点。")
    if not secrets.compare_digest(job.api_key_digest, _api_key_digest(api_key)):
        raise ValueError("API密钥与提交该任务时使用的密钥不一致。")
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
