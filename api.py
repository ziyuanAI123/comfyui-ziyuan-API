from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests


IMAGE_MODELS = ("gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
VIDEO_MODEL = "omni-flash-components"
SEEDANCE_MINI_MODEL = "doubao-seedance-2.0-mini"
QIAOMO_MODELS = ("doubao-seedance-2.0", SEEDANCE_MINI_MODEL, "doubao-seedance-2.5")
DONE = {"succeeded", "success", "completed", "finished", "done"}
FAILED = {"failed", "failure", "error", "cancelled", "canceled", "expired"}


def task_field(body, *names):
    if not isinstance(body, dict):
        return None
    for name in names:
        if body.get(name) is not None:
            return body[name]
    for name in ("data", "task", "result"):
        value = task_field(body.get(name), *names)
        if value is not None:
            return value
    return None


def media_sources(body):
    """Read output fields only; never mistake echoed reference images for output."""
    def visit(value):
        if isinstance(value, list):
            # Separate list entries are separate requested results, even if identical.
            return [source for item in value for source in visit(item)]
        elif isinstance(value, dict):
            # Gateways can repeat the same results under several compatibility aliases.
            for name in ("data", "result", "output", "images", "videos"):
                sources = visit(value.get(name))
                if sources:
                    return sources
            for name in ("url", "image_url", "video_url", "b64_json"):
                item = value.get(name)
                if isinstance(item, str) and item:
                    return [item]
                elif isinstance(item, dict):
                    sources = visit(item)
                    if sources:
                        return sources
        elif isinstance(value, str) and value.startswith(("https://", "http://", "data:", "/")):
            return [value]
        return []

    return visit(body)


def validate_size(value):
    parts = value.strip().lower().replace("×", "x").split("x")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        raise ValueError("尺寸请填写宽x高，例如 1024x1024。")
    width, height = map(int, parts)
    if min(width, height) <= 0:
        raise ValueError("图片或视频宽高必须大于 0。")
    return f"{width}x{height}"


class ZiyuanClient:
    def __init__(self, base_url, api_key, timeout, check_cancel=lambda: None, bypass_proxy=True, poll_interval=3):
        self.base_url = base_url.strip().rstrip("/")
        if self.base_url.endswith("/v1"):
            self.base_url = self.base_url[:-3]
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("API 地址必须是完整的 HTTP(S) 地址。")
        self.api_key = (api_key.strip() or os.environ.get("ZIYUAN_API_KEY", "")).strip()
        if not self.api_key:
            raise ValueError("请填写 API Key，或设置 ZIYUAN_API_KEY 环境变量。")
        self.deadline = time.monotonic() + timeout
        self.check_cancel = check_cancel
        self.poll_interval = poll_interval
        self.session = requests.Session()
        self.session.trust_env = not bypass_proxy

    def close(self):
        self.session.close()

    def _remaining(self):
        self.check_cancel()
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("等待接口超时；已提交的任务可能仍在服务器运行。")
        return seconds

    def _headers(self, url):
        target, origin = urlsplit(url), urlsplit(self.base_url)
        if (target.scheme, target.netloc) == (origin.scheme, origin.netloc):
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _check_response(self, response):
        if not response.ok:
            detail = response.text[:800].replace(self.api_key, "[REDACTED]")
            raise RuntimeError(f"HTTP {response.status_code}: {detail}")

    def _json(self, method, endpoint, payload=None, files=None):
        url = self.base_url + endpoint
        request_body = {"data": payload, "files": files} if files else {"json": payload}
        try:
            with self.session.request(
                method, url, headers={**self._headers(url), "Accept": "application/json"},
                **request_body, timeout=(min(15, self._remaining()), self._remaining()),
                allow_redirects=False,
            ) as response:
                self._check_response(response)
                if 300 <= response.status_code < 400:
                    raise RuntimeError("API 请求发生重定向，请检查 API 地址。")
                body = response.json()
        except requests.Timeout as exc:
            raise TimeoutError("接口响应超时；请求没有自动重发，请先查看网站任务记录。") from exc
        error = task_field(body, "error")
        if error or (isinstance(body, dict) and body.get("success") is False):
            detail = str(error or body.get("message") or "接口返回失败")
            raise RuntimeError(detail[:800].replace(self.api_key, "[REDACTED]"))
        return body

    def generate(self, kind, payload):
        endpoint = "/v1/videos" if kind == "video" else (
            "/v1/images/edits" if payload.get("image") else "/v1/images/generations"
        )
        files = None
        if kind == "video" and payload.get("model") in QIAOMO_MODELS:
            files = []
            for index, source in enumerate(payload.get("images", []), 1):
                # ComfyUI reference tensors are encoded as PNG data URIs.
                if not source.startswith("data:image/png;base64,"):
                    raise ValueError("Seedance 参考图必须由 IMAGE 输入提供。")
                raw = base64.b64decode(source.split(",", 1)[1], validate=True)
                if not raw or len(raw) > 10 * 1024 * 1024:
                    raise ValueError("Seedance 每张参考图须大于 0 且不超过 10 MiB。")
                field = ("first_frame", "last_frame")[index - 1] if payload.get("_frame_mode") else "input_reference"
                files.append((field, (f"reference-{index}.png", raw, "image/png")))
            for index, raw in enumerate(payload.get("_video_files", []), 1):
                if not raw or len(raw) > 50 * 1024 * 1024:
                    raise ValueError("每段参考视频须大于0且不超过50 MiB。")
                files.append(("input_video", (f"video-{index}.mp4", raw, "video/mp4")))
            for index, raw in enumerate(payload.get("_audio_files", []), 1):
                if not raw or len(raw) > 15 * 1024 * 1024:
                    raise ValueError("每段参考音频须大于0且不超过15 MiB。")
                files.append(("input_audio", (f"audio-{index}.wav", raw, "audio/wav")))
            payload = {key: value for key, value in payload.items() if key not in ("images", "_frame_mode", "_video_files", "_audio_files")}
            if files:
                payload["metadata"] = json.dumps(payload["metadata"], ensure_ascii=False)
        body = self._json("POST", endpoint, payload, files=files)
        task_id = task_field(body, "task_id", "taskId", "id")
        try:
            while True:
                self._remaining()
                status = str(task_field(body, "status", "state", "task_status", "taskStatus") or "").lower()
                if status in FAILED:
                    message = task_field(body, "message", "fail_reason", "error") or status
                    raise RuntimeError(f"生成失败：{str(message)[:800].replace(self.api_key, '[REDACTED]')}")
                sources = media_sources(body)
                if sources and (not status or status in DONE):
                    return sources, str(task_id or "")
                if status in DONE:
                    if kind == "video" and task_id:
                        return [self.base_url + f"/v1/videos/{quote(str(task_id), safe='')}/content"], str(task_id)
                    raise RuntimeError("任务已完成，但接口没有返回图片或视频。")
                if not task_id:
                    raise RuntimeError("接口未返回可识别的媒体结果或任务 ID。")
                # Short waits allow ComfyUI's Cancel button to interrupt polling.
                for _ in range(max(1, int(self.poll_interval * 2))):
                    time.sleep(min(0.5, self._remaining()))
                path = "videos" if kind == "video" else "images"
                body = self._json("GET", f"/v1/{path}/{quote(str(task_id), safe='')}")
        except (RuntimeError, TimeoutError) as exc:
            raise type(exc)(f"{exc}\n任务 ID：{task_id or '未返回'}") from exc

    def _download(self, source):
        url = urljoin(self.base_url + "/", source)
        for _ in range(6):
            if urlsplit(url).scheme not in ("http", "https"):
                raise ValueError("下载地址必须使用 HTTP(S)。")
            response = self.session.get(
                url, headers=self._headers(url), stream=True, allow_redirects=False,
                timeout=(min(15, self._remaining()), min(60, self._remaining())),
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("下载重定向缺少地址。")
                url = urljoin(url, location)
                continue
            try:
                self._check_response(response)
            except Exception:
                response.close()
                raise
            return response
        raise RuntimeError("下载重定向次数过多。")

    def image_bytes(self, source):
        if source.startswith("data:"):
            return base64.b64decode(source.split(",", 1)[1], validate=True)
        if not source.startswith(("https://", "http://", "/")):
            return base64.b64decode(source, validate=True)
        with self._download(source) as response:
            result = bytearray()
            for chunk in response.iter_content(1024 * 1024):
                self._remaining()
                result.extend(chunk)
            return bytes(result)

    def save_video(self, source, destination):
        destination = Path(destination)
        partial = destination.with_suffix(".part")
        try:
            with self._download(source) as response:
                # Some gateways return a URL object from the content endpoint.
                if "json" in response.headers.get("Content-Type", "").lower():
                    sources = media_sources(response.json())
                    if not sources or sources[0] == source:
                        raise RuntimeError("视频内容接口没有返回可下载的视频。")
                    with self._download(sources[0]) as media:
                        self._write_stream(media, partial)
                else:
                    self._write_stream(response, partial)
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)

    def _write_stream(self, response, path):
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/" in content_type or "json" in content_type:
            raise RuntimeError("下载接口返回了文本，未返回视频文件。")
        with path.open("wb") as target:
            for chunk in response.iter_content(1024 * 1024):
                self._remaining()
                target.write(chunk)
        expected = response.headers.get("Content-Length")
        actual = path.stat().st_size
        if not actual or (expected and not response.headers.get("Content-Encoding") and actual != int(expected)):
            raise RuntimeError("视频下载为空或不完整。")
