from __future__ import annotations

import base64
import io
import math
import uuid
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
import torch
from PIL import Image, ImageOps

from .api import IMAGE_MODELS, VIDEO_MODEL, SEEDANCE_MINI_MODEL, QIAOMO_MODELS, ZiyuanClient, validate_size
from . import jobs


RATIOS = ["自动", "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"]


def common_inputs(timeout):
    return {
        "种子": ("INT", {"default": 0, "min": 0, "max": 2147483647, "control_after_generate": True,
                         "tooltip": "用于控制重复执行，不发送到服务器，不保证复现。"}),
        "最大等待秒数": ("INT", {"default": timeout, "min": 30, "max": 7200}),
        "API密钥": ("STRING", {"default": "", "tooltip": "可留空使用 ZIYUAN_API_KEY 环境变量；填入的密钥会随工作流保存。"}),
        "API地址": ("STRING", {"default": "https://ziyuanai.cn"}),
        "绕过代理": ("BOOLEAN", {"default": True}),
    }


def reference_inputs(limit=14):
    return {f"参考图{i}": ("IMAGE",) for i in range(1, limit + 1)}


def selected_ratio(label, inputs, fallback):
    if label not in RATIOS:
        raise ValueError("请选择有效的比例。")
    if label == "自动":
        label = fallback
        for i in range(1, 15):
            image = inputs.get(f"参考图{i}", inputs.get(f"图片{i}"))
            if image is not None:
                if image.ndim != 4 or min(image.shape[1:3]) <= 0:
                    raise ValueError("参考图形状无效。")
                ratio = image.shape[2] / image.shape[1]
                return ratio
    width, height = map(int, label.split(":"))
    return width / height


def image_size(ratio, resolution):
    pixels = {"1K": 1024 ** 2, "2K": 2048 ** 2, "4K": 3840 * 2160}[resolution]
    width, height = math.sqrt(pixels * ratio), math.sqrt(pixels / ratio)
    scale = min(1, 3840 / max(width, height))
    # Round down to preserve the pixel and long-edge ceilings.
    return f"{max(16, int(width * scale / 16) * 16)}x{max(16, int(height * scale / 16) * 16)}"


def video_size(ratio, resolution):
    short_edge = {"720p": 720, "1080p": 1080}[resolution]
    width, height = (short_edge * ratio, short_edge) if ratio >= 1 else (short_edge, short_edge / ratio)
    return f"{max(2, round(width / 2) * 2)}x{max(2, round(height / 2) * 2)}"


def collect_references(inputs, limit=14):
    references = []
    for i in range(1, limit + 1):
        image = inputs.get(f"参考图{i}", inputs.get(f"图片{i}"))
        if image is None:
            continue
        if image.ndim != 4 or image.shape[-1] not in (1, 3, 4):
            raise ValueError(f"参考图{i} 不是有效的 ComfyUI IMAGE。")
        for frame in image.detach().cpu().clamp(0, 1):
            if len(references) >= limit:
                raise ValueError(f"参考图总数超过 {limit} 张（包括每个 IMAGE 输入中的全部批次）。")
            array = (frame.numpy() * 255).round().astype(np.uint8)
            if array.shape[-1] == 1:
                array = array[:, :, 0]
            buffer = io.BytesIO()
            Image.fromarray(array).convert("RGB").save(buffer, format="PNG")
            references.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
    return references


def check_cancel():
    import comfy.model_management
    comfy.model_management.throw_exception_if_processing_interrupted()


class ZiyuanImageNode:
    CATEGORY = "ziyuanAI"
    FUNCTION = "run"
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "提示词": ("STRING", {"multiline": True, "default": ""}),
                "模型": (list(IMAGE_MODELS),),
                "比例": (RATIOS, {"default": "自动", "tooltip": "自动沿用第一张参考图比例；无参考图时为 1:1。"}),
                "分辨率": (["1K", "2K", "4K"], {"default": "2K"}),
                "质量": (["自动", "高", "中等", "低"], {"default": "自动"}),
                "输出格式": (["自动", "png", "jpeg", "webp"], {"default": "自动"}),
                "生成数量": ("INT", {"default": 1, "min": 1, "max": 10}),
                **common_inputs(1200),
            },
            "optional": reference_inputs(),
        }

    def run(self, 提示词, 模型, 比例, 分辨率, 质量, 输出格式, 生成数量, 种子, 最大等待秒数, API密钥, API地址, 绕过代理, **kwargs):
        size = image_size(selected_ratio(比例, kwargs, "1:1"), 分辨率)
        quality = {"自动": None, "高": "high", "中等": "medium", "低": "low"}[质量]
        return self.generate(提示词, API密钥, API地址, 最大等待秒数, 种子, 模型, size, 生成数量,
                             _quality=quality, _output_format=None if 输出格式 == "自动" else 输出格式,
                             _bypass_proxy=绕过代理, **kwargs)

    def generate(self, prompt, api_key, api_base_url, 超时秒数, 重新生成编号, model, size, n, **kwargs):
        if model not in IMAGE_MODELS:
            raise ValueError("请选择支持的图片模型。")
        if type(n) is not int or not 1 <= n <= 10:
            raise ValueError("图片生成数量须为 1–10 的整数。")
        refs = collect_references(kwargs)
        if not prompt.strip() and not refs:
            raise ValueError("请输入提示词或连接参考图。")
        payload = {"model": model, "prompt": prompt, "size": validate_size(size), "n": n, "response_format": "url"}
        if kwargs.get("_quality"):
            payload["quality"] = kwargs["_quality"]
        if kwargs.get("_output_format"):
            payload["output_format"] = kwargs["_output_format"]
        if refs:
            payload["image"] = refs[0] if len(refs) == 1 else refs
        client = ZiyuanClient(api_base_url, api_key, 超时秒数, kwargs.get("_cancel_check", check_cancel), kwargs.get("_bypass_proxy", True))
        try:
            sources, task_id = client.generate("image", payload)
            tensors = []
            for source in sources[:n]:
                with Image.open(io.BytesIO(client.image_bytes(source))) as image:
                    array = np.array(ImageOps.exif_transpose(image).convert("RGB"), dtype=np.float32) / 255.0
                tensors.append(torch.from_numpy(array).unsqueeze(0))
            if len({tuple(t.shape[1:]) for t in tensors}) != 1:
                raise RuntimeError("返回图片尺寸不一致，不能组成 IMAGE 批次；请将生成数量设为 1。")
            note = f"；接口返回 {len(sources)} 条图片结果，按请求数量输出前 {n} 张" if len(sources) > n else ""
            return torch.cat(tensors), f"生成完成；模型：{model}；图片：{len(tensors)}；任务 ID：{task_id or '同步返回'}{note}"
        finally:
            client.close()


class ZiyuanVideoNode:
    MODEL = VIDEO_MODEL
    REFERENCE_LIMIT = 14
    AUDIO_LIMIT = 0
    VIDEO_LIMIT = 0
    CATEGORY = "ziyuanAI"
    FUNCTION = "run"
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("视频", "文件路径", "状态")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "提示词": ("STRING", {"multiline": True, "default": ""}),
                "模型": ([VIDEO_MODEL],),
                "比例": (RATIOS, {"default": "16:9"}),
                "分辨率": (["720p", "1080p"], {"default": "720p"}),
                "时长秒数": ("INT", {"default": 5, "min": 1, "max": 60, "tooltip": "实际支持的时长以模型接口为准。"}),
                "轮询间隔秒": ("INT", {"default": 3, "min": 1, "max": 60}),
                **common_inputs(1800),
            },
            "optional": reference_inputs(),
        }

    def run(self, 提示词, 模型, 比例, 分辨率, 时长秒数, 轮询间隔秒, 种子, 最大等待秒数, API密钥, API地址, 绕过代理, **kwargs):
        size = video_size(selected_ratio(比例, kwargs, "16:9"), 分辨率)
        return self.generate(提示词, API密钥, API地址, 最大等待秒数, 种子, 模型, size, 时长秒数,
                             _bypass_proxy=绕过代理, _poll_interval=轮询间隔秒, **kwargs)

    def generate(self, prompt, api_key, api_base_url, 超时秒数, 重新生成编号, model, size, seconds, **kwargs):
        # Check VIDEO support before submitting a billable request.
        from comfy_api.input_impl import VideoFromFile
        import folder_paths

        if model != self.MODEL:
            raise ValueError(f"请选择 {self.MODEL}。")
        refs = collect_references(kwargs, self.REFERENCE_LIMIT)
        if not prompt.strip():
            raise ValueError("请输入视频提示词。")
        payload = self.build_payload(prompt, model, size, seconds, refs, **kwargs)
        output_dir = Path(folder_paths.get_output_directory()) / "ziyuan"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"ziyuan_{uuid.uuid4().hex}.mp4"
        client = ZiyuanClient(api_base_url, api_key, 超时秒数, kwargs.get("_cancel_check", check_cancel),
                              kwargs.get("_bypass_proxy", True), kwargs.get("_poll_interval", 3))
        try:
            sources, task_id = client.generate("video", payload)
            client.save_video(sources[0], output_path)
            video = VideoFromFile(str(output_path))
            try:
                video.get_dimensions()
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                raise RuntimeError(f"任务 {task_id or '同步返回'} 下载结果无法解码为视频。") from exc
            return video, str(output_path), f"生成完成；模型：{model}；任务 ID：{task_id or '同步返回'}"
        finally:
            client.close()

    def build_payload(self, prompt, model, size, seconds, refs, **kwargs):
        payload = {"model": model, "prompt": prompt, "size": validate_size(size), "seconds": seconds}
        if refs:
            payload["images"] = refs
        return payload


def collect_audio(inputs, limit, max_duration):
    files = []
    for i in range(1, limit + 1):
        audio = inputs.get(f"参考音频{i}")
        if audio is None:
            continue
        if not isinstance(audio, dict):
            raise ValueError(f"参考音频{i} 须为 ComfyUI AUDIO。")
        waveform, rate = audio.get("waveform"), audio.get("sample_rate")
        if (not isinstance(waveform, torch.Tensor) or waveform.ndim != 3 or min(waveform.shape) <= 0
                or waveform.shape[1] not in (1, 2) or type(rate) is not int or rate <= 0):
            raise ValueError(f"参考音频{i} 须为有效的单声道或双声道 ComfyUI AUDIO。")
        if len(files) + waveform.shape[0] > limit:
            raise ValueError(f"参考音频最多{limit}段（包括 AUDIO 批次）。")
        if not 2 <= waveform.shape[2] / rate <= max_duration:
            raise ValueError(f"参考音频{i} 每段时长须为2–{max_duration}秒，不会自动裁剪。")
        if waveform.shape[1] * waveform.shape[2] * 2 + 44 > 15 * 1024 * 1024:
            raise ValueError(f"参考音频{i} 转换后的 WAV 不得超过15 MiB。")
        if not torch.isfinite(waveform).all():
            raise ValueError(f"参考音频{i} 包含无效采样值。")
        for frame in waveform.detach().cpu():
            pcm = (frame.clamp(-1, 1).transpose(0, 1).numpy() * 32767).round().astype("<i2")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(frame.shape[0])
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(pcm.tobytes())
            files.append(buffer.getvalue())
    return files


def collect_videos(inputs, limit=3):
    from comfy_api.latest import Input

    files = []
    for i in range(1, limit + 1):
        video = inputs.get(f"参考视频{i}")
        if video is None:
            continue
        if not isinstance(video, Input.Video):
            raise ValueError(f"参考视频{i} 须为 ComfyUI VIDEO，请连接原生“加载视频”的 VIDEO 输出。")
        duration = video.get_duration()
        if not math.isfinite(duration) or not 0 < duration <= 15:
            raise ValueError(f"参考视频{i} 时长须大于0且不超过15秒。")
        buffer = io.BytesIO()
        video.save_to(buffer, format="mp4", codec="h264")
        raw = buffer.getvalue()
        if not raw or len(raw) > 50 * 1024 * 1024:
            raise ValueError(f"参考视频{i} 转换后的 MP4 须大于 0 且不超过 50 MiB。")
        files.append(raw)
    return files


class ZiyuanQiaomoNode(ZiyuanVideoNode):
    MODEL = "doubao-seedance-2.0"
    REFERENCE_LIMIT = 9
    VIDEO_LIMIT = 3
    AUDIO_LIMIT = 3
    AUDIO_MAX_DURATION = 15
    RESOLUTIONS = ["720p", "480p", "1080p", "4k"]

    @classmethod
    def INPUT_TYPES(cls):
        inputs = super().INPUT_TYPES()
        inputs["required"]["模型"] = ([cls.MODEL],)
        inputs["required"]["比例"] = (["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"] +
                                     (["自动"] if cls.MODEL == SEEDANCE_MINI_MODEL else []), {"default": "16:9"})
        inputs["required"]["分辨率"] = (cls.RESOLUTIONS, {"default": "720p"})
        inputs["required"]["时长秒数"] = ("INT", {"default": 4, "min": 4, "max": 15})
        inputs["required"]["生成声音"] = ("BOOLEAN", {"default": True})
        # Keep widget names/order so saved Mini workflows retain their values.
        inputs["optional"] = {
            **reference_inputs(cls.REFERENCE_LIMIT),
            **{f"参考音频{i}": ("AUDIO", {"tooltip": f"每段2–{cls.AUDIO_MAX_DURATION}秒，转为 WAV 后最多15 MiB。"})
               for i in range(1, cls.AUDIO_LIMIT + 1)},
            **{f"参考视频{i}": ("VIDEO", {"tooltip": "MP4 最大 50 MiB，视频不超过 15 秒；由中转站上传。"})
               for i in range(1, cls.VIDEO_LIMIT + 1)},
            "Mini素材模式": (["组合参考", "首尾帧"], {"default": "组合参考",
                              "tooltip": "首尾帧模式：参考图1为首帧，参考图2为可选尾帧，不混用音视频。"}),
            **{name: ("STRING", {"default": "", "multiline": True,
                                "tooltip": "兼容旧工作流链接；2.0/2.5 必须使用巧模签名素材链接。新工作流直接连接参考素材。"})
               for name in ("Mini图片链接", "Mini视频链接")},
            "Mini音频链接": ("STRING", {"default": "", "multiline": True,
                             "tooltip": "兼容旧工作流音频链接；2.0/2.5须使用签名链接。新工作流直接连接 AUDIO。"}),
        }
        return inputs

    def run(self, 提示词, 模型, 比例, 分辨率, 时长秒数, 轮询间隔秒, 种子, 最大等待秒数, API密钥, API地址, 绕过代理, 生成声音=True, **kwargs):
        if not isinstance(生成声音, bool):
            raise ValueError("生成声音须为开关值。")
        return self.generate(提示词, API密钥, API地址, 最大等待秒数, 种子, 模型, "", 时长秒数,
                             _aspect_ratio="adaptive" if 比例 == "自动" else 比例, _resolution=分辨率, _enable_sound=生成声音,
                             _bypass_proxy=绕过代理, _poll_interval=轮询间隔秒, **kwargs)

    def build_payload(self, prompt, model, size, seconds, refs, **kwargs):
        urls = {}
        for name in ("Mini图片链接", "Mini视频链接", "Mini音频链接"):
            text = kwargs.get(name, "")
            if not isinstance(text, str):
                raise ValueError(f"{name} 须为每行一个素材地址的文本。")
            urls[name] = [line.strip() for line in text.splitlines() if line.strip()]
            for url in urls[name]:
                parsed = urlsplit(url)
                mini_asset = model == SEEDANCE_MINI_MODEL and parsed.scheme == "asset" and parsed.netloc
                if (not mini_asset and (parsed.scheme != "https" or not parsed.hostname or not parsed.path)
                        or parsed.username or parsed.fragment or any(c.isspace() for c in url) or "\\" in url):
                    raise ValueError(f"{name} 须使用有效 HTTPS 素材链接；Mini 也接受 asset://。")
                if model != SEEDANCE_MINI_MODEL:
                    query = parse_qs(parsed.query)
                    if not parsed.path.startswith("/api/reference/") or not query.get("expires") or not query.get("signature"):
                        raise ValueError("2.0/2.5 需要巧模签名素材链接；请直接连接本地图片/视频，由中转站上传。")
        images, videos, audio = (urls[name] for name in ("Mini图片链接", "Mini视频链接", "Mini音频链接"))
        local_video_count = sum(kwargs.get(f"参考视频{i}") is not None for i in range(1, self.VIDEO_LIMIT + 1))
        audio_files = collect_audio(kwargs, self.AUDIO_LIMIT, self.AUDIO_MAX_DURATION)
        audio_count = len(audio_files) + len(audio)
        if refs and images or local_video_count and videos or audio_files and audio:
            raise ValueError("同类素材不能同时使用本地连线和旧链接，请清空旧链接或断开对应连线。")
        image_count, video_count = len(refs) + len(images), local_video_count + len(videos)
        if image_count > self.REFERENCE_LIMIT or video_count > self.VIDEO_LIMIT or audio_count > self.AUDIO_LIMIT:
            raise ValueError(f"{model} 最多{self.REFERENCE_LIMIT}张参考图、{self.VIDEO_LIMIT}段参考视频、{self.AUDIO_LIMIT}段参考音频。")
        if image_count + video_count + audio_count > 12:
            raise ValueError("图片、视频、音频合计最多12个参考素材（包括 IMAGE/AUDIO 批次）。")
        mode = kwargs.get("Mini素材模式", "组合参考")
        if mode not in ("组合参考", "首尾帧"):
            raise ValueError("请选择有效的素材模式。")
        if mode == "首尾帧" and (not 1 <= image_count <= 2 or audio_count or video_count):
            raise ValueError("首尾帧模式须提供1–2张图片（先首帧、后尾帧），不能混用参考音频或视频。")
        if audio_count and not image_count and not video_count and model != "doubao-seedance-2.5":
            raise ValueError(f"{model} 的参考音频须搭配参考图片或参考视频。")
        content = []
        for kind, items in (("image", images), ("video", videos), ("audio", audio)):
            for index, url in enumerate(items):
                role = ("first_frame", "last_frame")[index] if mode == "首尾帧" else f"reference_{kind}"
                content.append({"type": f"{kind}_url", f"{kind}_url": {"url": url}, "role": role})
        metadata = {"resolution": kwargs["_resolution"], "ratio": kwargs["_aspect_ratio"],
                    "generate_audio": kwargs.get("_enable_sound", True)}
        if content:
            metadata["content"] = content
        payload = {"model": model, "prompt": prompt, "seconds": str(seconds), "metadata": metadata}
        if refs:
            payload["images"] = refs
            payload["_frame_mode"] = mode == "首尾帧"
        if local_video_count:
            payload["_video_files"] = collect_videos(kwargs, self.VIDEO_LIMIT)
        if audio_files:
            payload["_audio_files"] = audio_files
        return payload


class ZiyuanSeedanceMiniNode(ZiyuanQiaomoNode):
    MODEL = SEEDANCE_MINI_MODEL
    RESOLUTIONS = ["720p"]
    AUDIO_MAX_DURATION = 30


class ZiyuanQiaomo25Node(ZiyuanQiaomoNode):
    MODEL = "doubao-seedance-2.5"
    RESOLUTIONS = ["720p", "480p", "1080p"]
    REFERENCE_LIMIT = 12
    VIDEO_LIMIT = 12
    AUDIO_LIMIT = 10
    AUDIO_MAX_DURATION = 30


VIDEO_NODES = {VIDEO_MODEL: ZiyuanVideoNode, "doubao-seedance-2.0": ZiyuanQiaomoNode,
               SEEDANCE_MINI_MODEL: ZiyuanSeedanceMiniNode, "doubao-seedance-2.5": ZiyuanQiaomo25Node}


class ZiyuanUnifiedVideoNode(ZiyuanVideoNode):
    CATEGORY = "ziyuanAI"

    @classmethod
    def INPUT_TYPES(cls):
        inputs = ZiyuanVideoNode.INPUT_TYPES()
        profiles = {}
        for model, node in VIDEO_NODES.items():
            required = node.INPUT_TYPES()["required"]
            duration = required["时长秒数"]
            profiles[model] = {"ratios": required["比例"][0], "resolutions": required["分辨率"][0],
                               "seconds": duration[0] if isinstance(duration[0], list) else None,
                               "duration": duration[1], "images": node.REFERENCE_LIMIT,
                               "audio": node.AUDIO_LIMIT > 0, "audio_limit": node.AUDIO_LIMIT,
                               "videos": node.VIDEO_LIMIT, "sound": "生成声音" in required,
                               "qiaomo": model in QIAOMO_MODELS}
        inputs["required"]["模型"] = (list(VIDEO_NODES), {"ziyuan_profiles": profiles})
        inputs["required"]["分辨率"] = (["720p", "1080p", "480p", "4k"], {"default": "720p"})
        inputs["required"]["时长秒数"] = (list(range(1, 61)), {"default": 5})
        inputs["required"]["生成声音"] = ("BOOLEAN", {"default": True})
        # Retain legacy sockets in the schema so old incompatible links fail explicitly.
        # The frontend only exposes each current model's supported sockets.
        inputs["optional"] = {**ZiyuanQiaomo25Node.INPUT_TYPES()["optional"], **reference_inputs(30),
                              **{f"参考音频{i}": ("AUDIO",) for i in range(1, 11)}}
        return inputs

    def run(self, 提示词, 模型, 比例, 分辨率, 时长秒数, 轮询间隔秒, 种子, 最大等待秒数, API密钥, API地址, 绕过代理, 生成声音=True, **kwargs):
        node_type = VIDEO_NODES.get(模型)
        if node_type is None:
            raise ValueError("请选择支持的视频模型。")
        required = node_type.INPUT_TYPES()["required"]
        if 比例 not in required["比例"][0] or 分辨率 not in required["分辨率"][0]:
            raise ValueError(f"{模型} 不支持所选比例或分辨率，请重新选择。")
        duration = required["时长秒数"]
        if type(时长秒数) is not int:
            raise ValueError("视频时长须为整数秒。")
        valid_duration = (时长秒数 in duration[0] if isinstance(duration[0], list)
                          else duration[1]["min"] <= 时长秒数 <= duration[1]["max"])
        if not valid_duration:
            raise ValueError(f"{模型} 不支持所选时长，请重新选择。")
        for i in range(node_type.REFERENCE_LIMIT + 1, 31):
            if kwargs.get(f"参考图{i}") is not None:
                raise ValueError(f"{模型} 最多支持 {node_type.REFERENCE_LIMIT} 张参考图，请断开参考图{i}。")
        for prefix, limit in (("参考音频", node_type.AUDIO_LIMIT), ("参考视频", node_type.VIDEO_LIMIT)):
            for i in range(limit + 1, 13):
                if kwargs.get(f"{prefix}{i}") is not None:
                    raise ValueError(f"{模型} 最多支持 {limit} 段{prefix}，请断开{prefix}{i}。")
        if 模型 in QIAOMO_MODELS:
            kwargs["生成声音"] = 生成声音
        return node_type().run(提示词, 模型, 比例, 分辨率, 时长秒数, 轮询间隔秒,
                               种子, 最大等待秒数, API密钥, API地址, 绕过代理, **kwargs)


def submit_job(kind, node, inputs):
    # Freeze reference inputs while the workflow continues on other branches.
    inputs = {key: value.detach().cpu().clone() if isinstance(value, torch.Tensor) else value
              for key, value in inputs.items()}
    for key, value in inputs.items():
        if isinstance(value, dict) and isinstance(value.get("waveform"), torch.Tensor):
            inputs[key] = {**value, "waveform": value["waveform"].detach().cpu().clone()}
    return (jobs.submit(kind, lambda cancel: node.run(**inputs, _cancel_check=cancel)),)


class ZiyuanImageSubmitNode(ZiyuanImageNode):
    FUNCTION = "submit"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("任务编号",)

    def submit(self, **kwargs):
        return submit_job("image", ZiyuanImageNode(), kwargs)


class ZiyuanUnifiedVideoSubmitNode(ZiyuanUnifiedVideoNode):
    FUNCTION = "submit"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("任务编号",)

    def submit(self, **kwargs):
        return submit_job("video", ZiyuanUnifiedVideoNode(), kwargs)


class ZiyuanImageFetchNode:
    CATEGORY = "ziyuanAI"
    FUNCTION = "fetch"
    RETURN_TYPES = ZiyuanImageNode.RETURN_TYPES
    RETURN_NAMES = ZiyuanImageNode.RETURN_NAMES
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"任务编号": ("STRING", {"forceInput": True})}}

    def fetch(self, 任务编号):
        return jobs.fetch(任务编号, "image", check_cancel)


class ZiyuanUnifiedVideoFetchNode(ZiyuanImageFetchNode):
    RETURN_TYPES = ZiyuanVideoNode.RETURN_TYPES
    RETURN_NAMES = ZiyuanVideoNode.RETURN_NAMES

    def fetch(self, 任务编号):
        return jobs.fetch(任务编号, "video", check_cancel)


NODE_CLASS_MAPPINGS = {
    "ZiyuanUnifiedVideoNode": ZiyuanUnifiedVideoNode,
    "ZiyuanUnifiedVideoSubmitNode": ZiyuanUnifiedVideoSubmitNode,
    "ZiyuanUnifiedVideoFetchNode": ZiyuanUnifiedVideoFetchNode,
    "ZiyuanImageNode": ZiyuanImageNode,
    "ZiyuanImageSubmitNode": ZiyuanImageSubmitNode,
    "ZiyuanImageFetchNode": ZiyuanImageFetchNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ZiyuanUnifiedVideoNode": "ziyuanAI. 视频生成",
    "ZiyuanUnifiedVideoSubmitNode": "ziyuanAI. 视频异步提交",
    "ZiyuanUnifiedVideoFetchNode": "ziyuanAI. 视频异步获取",
    "ZiyuanImageNode": "ziyuanAI. 图片生成",
    "ZiyuanImageSubmitNode": "ziyuanAI. 图片异步提交",
    "ZiyuanImageFetchNode": "ziyuanAI. 图片异步获取",
}
