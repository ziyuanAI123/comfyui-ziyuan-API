import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
from PIL import Image, ImageOps


CATEGORY = "ziyuanAI"
MEDIA_BUNDLE_TYPE = "ZIYUAN_MEDIA_BUNDLE"
MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
EMPTY_BLOCK = "阻断缺失"
EMPTY_NONE = "允许空值"
EMPTY_MODES = (EMPTY_BLOCK, EMPTY_NONE)

# (内部类型, JSON 键, 上限)
MEDIA_GROUPS = (
    ("image", "images", MAX_IMAGES),
    ("audio", "audios", MAX_AUDIOS),
    ("video", "videos", MAX_VIDEOS),
)


try:
    import folder_paths
except ImportError:
    folder_paths = None

try:
    from comfy_extras import nodes_audio
except ImportError:
    nodes_audio = None

try:
    from comfy_api.latest import InputImpl
except ImportError:
    InputImpl = None

try:
    from fractions import Fraction
except ImportError:
    Fraction = None

try:
    from comfy_execution.graph_utils import ExecutionBlocker
except ImportError:
    class ExecutionBlocker:
        def __init__(self, value=None):
            self.value = value


@dataclass(frozen=True)
class ZiyuanMediaItem:
    input_index: int
    media_type: str
    value: Any


@dataclass(frozen=True)
class ZiyuanMediaBundle:
    items: tuple


def normalize_filename(raw):
    filename = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if not filename or "\x00" in filename or os.path.isabs(filename):
        return ""
    parts = [part for part in filename.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def normalize_media_state(value):
    result = {kind: [] for kind, _key, _maximum in MEDIA_GROUPS}
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except (TypeError, ValueError):
            value = {}
    if not isinstance(value, Mapping):
        return result

    for kind, key, maximum in MEDIA_GROUPS:
        entries = value.get(key)
        if not isinstance(entries, list):
            continue
        seen = set()
        for raw in entries:
            filename = normalize_filename(
                raw.get("filename") if isinstance(raw, Mapping) else raw
            )
            if not filename or filename in seen:
                continue
            seen.add(filename)
            result[kind].append({"filename": filename})
            if len(result[kind]) >= maximum:
                break
    return result


def media_state_json(value):
    state = normalize_media_state(value)
    payload = {key: state[kind] for kind, key, _maximum in MEDIA_GROUPS}
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def media_input_path(filename):
    filename = normalize_filename(filename)
    if not filename:
        raise ValueError("素材文件名为空或非法。")
    if folder_paths is None:
        raise ValueError("找不到 ComfyUI folder_paths，无法解析 input 目录。")
    path = folder_paths.get_annotated_filepath(filename, folder_paths.get_input_directory())
    if not os.path.isfile(path):
        raise ValueError(f"素材文件不存在: {filename}")
    return path


def load_image_file(filename):
    path = media_input_path(filename)
    image = Image.open(path)
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    array = np.array(image).astype(np.float32) / 255.0
    return torch.from_numpy(array)[None,]


def load_audio_file(path):
    if nodes_audio is None:
        raise ValueError("当前环境没有 comfy_extras.nodes_audio，无法加载音频。")
    try:
        waveform, sample_rate = nodes_audio.load(path)
    except Exception as exc:
        raise ValueError(f"无法读取音频 {os.path.basename(path)!r}: {exc}") from exc
    if not isinstance(waveform, torch.Tensor) or waveform.ndim != 2 or waveform.shape[-1] < 1:
        raise ValueError(f"音频没有可用采样: {os.path.basename(path)!r}")
    return {
        "waveform": waveform.unsqueeze(0).to(device="cpu", dtype=torch.float32).contiguous(),
        "sample_rate": int(sample_rate),
    }


def audio_sample_rate(audio):
    if isinstance(audio, Mapping):
        try:
            return int(audio.get("sample_rate") or 44100)
        except (TypeError, ValueError):
            return 44100
    return 44100


def load_video_file(path):
    if InputImpl is None:
        raise ValueError("当前环境没有 comfy_api.latest.InputImpl，无法加载视频。")
    try:
        video = InputImpl.VideoFromFile(path)
        components = video.get_components()
    except Exception as exc:
        raise ValueError(f"无法读取视频 {os.path.basename(path)!r}: {exc}") from exc

    frames = components.images
    if not isinstance(frames, torch.Tensor) or frames.ndim != 4 or frames.shape[0] < 1:
        raise ValueError(f"视频没有可用帧: {os.path.basename(path)!r}")
    return video


def empty_output(mode):
    if mode == EMPTY_NONE:
        return None
    return ExecutionBlocker(None)


def resolve_empty_mode(value):
    raw = str(value or EMPTY_BLOCK).strip()
    compact = raw.replace(" ", "").casefold()
    if raw in (EMPTY_NONE, "allow_none") or compact in {
        "allow_none",
        "none",
        "允许空值",
        "允许空值(none)",
    }:
        return EMPTY_NONE
    if raw in (EMPTY_BLOCK, "block_missing") or compact in {
        "block_missing",
        "阻断缺失",
        "阻断缺失分支",
        "original",
    }:
        return EMPTY_BLOCK
    raise ValueError("empty_output_mode 只能是「阻断缺失」或「允许空值」。")


def standard_video(value):
    """保留原生 VIDEO 对象，直接连接 ziyuanAI 的参考视频输入。"""
    if not callable(getattr(value, "save_to", None)):
        raise ValueError("素材包视频不是原生 VIDEO 对象。")
    return value


def standard_audio(value):
    if not isinstance(value, Mapping):
        raise ValueError("素材包里的音频格式不支持。")
    waveform = value.get("waveform")
    if not isinstance(waveform, torch.Tensor) or waveform.ndim != 3:
        raise ValueError("音频必须是 [B, C, T] waveform。")
    return {
        "waveform": waveform,
        "sample_rate": audio_sample_rate(value),
    }


def split_bundle(bundle, image_count, video_count, audio_count, empty_output_mode=EMPTY_BLOCK):
    if not isinstance(bundle, ZiyuanMediaBundle):
        raise ValueError("请连接 ziyuanAI统一素材加载 的 media_bundle。")
    empty_mode = resolve_empty_mode(empty_output_mode)
    # Legacy count inputs remain for saved workflows; actual assets decide outputs.
    by_type = {kind: [] for kind, _key, _maximum in MEDIA_GROUPS}
    for item in bundle.items:
        if item.media_type in by_type:
            by_type[item.media_type].append(item.value)

    outputs = []
    for kind, maximum, _key in (
        ("image", MAX_IMAGES, "images"),
        ("video", MAX_VIDEOS, "videos"),
        ("audio", MAX_AUDIOS, "audios"),
    ):
        values = by_type[kind]
        for index in range(maximum):
            if index >= len(values):
                outputs.append(empty_output(empty_mode))
                continue
            value = values[index]
            if kind == "video":
                value = standard_video(value)
            elif kind == "audio":
                value = standard_audio(value)
            outputs.append(value)
    return tuple(outputs)


class ZiyuanMediaLoader:
    CATEGORY = CATEGORY
    FUNCTION = "load"
    RETURN_TYPES = (MEDIA_BUNDLE_TYPE,)
    RETURN_NAMES = ("media_bundle",)
    DESCRIPTION = "统一加载图片、音频、视频参考素材，输出给 ziyuanAI素材拆分。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "media_state": ("STRING", {"default": "", "multiline": False}),
            },
        }

    @classmethod
    def IS_CHANGED(cls, media_state="", **_kwargs):
        state = normalize_media_state(media_state)
        signature = []
        for kind, _key, _maximum in MEDIA_GROUPS:
            for entry in state[kind]:
                filename = entry["filename"]
                try:
                    stat = os.stat(media_input_path(filename))
                    signature.append((kind, filename, stat.st_mtime_ns, stat.st_size))
                except (OSError, ValueError):
                    signature.append((kind, filename, None, None))
        return repr(signature)

    @classmethod
    def VALIDATE_INPUTS(cls, media_state="", **_kwargs):
        state = normalize_media_state(media_state)
        for kind, _key, _maximum in MEDIA_GROUPS:
            for entry in state[kind]:
                try:
                    media_input_path(entry["filename"])
                except ValueError as exc:
                    return str(exc)
        return True

    def load(self, media_state=""):
        state = normalize_media_state(media_state)
        items = []
        input_index = 0
        for media_type, _key, _maximum in MEDIA_GROUPS:
            for entry in state[media_type]:
                filename = entry["filename"]
                path = media_input_path(filename)
                if media_type == "image":
                    value = load_image_file(filename)
                elif media_type == "audio":
                    value = load_audio_file(path)
                else:
                    value = load_video_file(path)
                input_index += 1
                items.append(ZiyuanMediaItem(input_index, media_type, value))
        return (ZiyuanMediaBundle(tuple(items)),)


class ZiyuanMediaSplitter:
    CATEGORY = CATEGORY
    FUNCTION = "split"
    RETURN_TYPES = (
        *(("IMAGE",) * MAX_IMAGES),
        *(("VIDEO",) * MAX_VIDEOS),
        *(("AUDIO",) * MAX_AUDIOS),
    )
    RETURN_NAMES = (
        *(f"image_{index}" for index in range(1, MAX_IMAGES + 1)),
        *(f"video_{index}" for index in range(1, MAX_VIDEOS + 1)),
        *(f"audio_{index}" for index in range(1, MAX_AUDIOS + 1)),
    )
    DESCRIPTION = "把统一素材包拆成 IMAGE、VIDEO 和 AUDIO，可直接接 ziyuanAI 生成节点。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "media_bundle": (MEDIA_BUNDLE_TYPE,),
                "image_count": ("INT", {"default": 1, "min": 0, "max": MAX_IMAGES, "step": 1}),
                "video_count": ("INT", {"default": 0, "min": 0, "max": MAX_VIDEOS, "step": 1}),
                "audio_count": ("INT", {"default": 0, "min": 0, "max": MAX_AUDIOS, "step": 1}),
                "empty_output_mode": (list(EMPTY_MODES), {"default": EMPTY_BLOCK}),
            }
        }

    @classmethod
    def VALIDATE_INPUTS(cls, empty_output_mode=EMPTY_BLOCK, **_kwargs):
        try:
            resolve_empty_mode(empty_output_mode)
        except ValueError as exc:
            return str(exc)
        return True

    def split(
        self,
        media_bundle,
        image_count=1,
        video_count=0,
        audio_count=0,
        empty_output_mode=EMPTY_BLOCK,
    ):
        return split_bundle(
            media_bundle,
            image_count,
            video_count,
            audio_count,
            empty_output_mode,
        )


class ZiyuanVideoToReferenceFrames:
    CATEGORY = CATEGORY
    FUNCTION = "convert"
    RETURN_TYPES = ("IMAGE",) * MAX_VIDEOS + ("AUDIO",) * MAX_VIDEOS
    RETURN_NAMES = tuple(f"{prefix}_{index}" for prefix in ("video_frames", "video_audio")
                         for index in range(1, MAX_VIDEOS + 1))
    DESCRIPTION = "最多提取 3 个原生 VIDEO 的画面和原声，分别连接 H3 同编号的视频参考和视频音轨输入；无原声时音轨为空。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"video": ("VIDEO",)},
            "optional": {f"video_{index}": ("VIDEO",) for index in range(2, MAX_VIDEOS + 1)},
        }

    @staticmethod
    def _frames_and_audio(video):
        get_components = getattr(video, "get_components", None)
        if not callable(get_components):
            raise ValueError("输入不是原生 VIDEO 对象。")
        try:
            components = get_components()
        except Exception as exc:
            raise ValueError(f"无法读取视频帧: {exc}") from exc
        frames = components.images
        if not isinstance(frames, torch.Tensor) or frames.ndim != 4 or frames.shape[0] < 1:
            raise ValueError("视频没有可用帧。")
        return frames, components.audio

    def convert(self, video, video_2=None, video_3=None):
        components = [self._frames_and_audio(value) if value is not None else (None, None)
                      for value in (video, video_2, video_3)]
        return tuple(frames for frames, audio in components) + tuple(audio for frames, audio in components)


NODE_CLASS_MAPPINGS = {
    "ZiyuanMediaLoader": ZiyuanMediaLoader,
    "ZiyuanMediaSplitter": ZiyuanMediaSplitter,
    "ZiyuanVideoToReferenceFrames": ZiyuanVideoToReferenceFrames,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "ZiyuanMediaLoader": "ziyuanAI统一素材加载",
    "ZiyuanMediaSplitter": "ziyuanAI素材拆分",
    "ZiyuanVideoToReferenceFrames": "ziyuanAI视频转参考帧",
}
