import os
import sys
import unittest
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.environ.get("COMFYUI_PATH"):
    sys.path.insert(0, os.environ["COMFYUI_PATH"])

import media_loader as media

from media_loader import (  # noqa: E402
    ZiyuanMediaBundle,
    ZiyuanMediaItem,
    EMPTY_BLOCK,
    EMPTY_NONE,
    MAX_AUDIOS,
    MAX_IMAGES,
    MAX_VIDEOS,
    media_state_json,
    normalize_filename,
    normalize_media_state,
    resolve_empty_mode,
    split_bundle,
    ZiyuanMediaLoader,
    ZiyuanMediaSplitter,
    ZiyuanVideoToReferenceFrames,
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
)


class TestNormalizeFilename(unittest.TestCase):
    def test_posix(self):
        self.assertEqual(normalize_filename("a/b.png"), "a/b.png")

    def test_backslash(self):
        self.assertEqual(normalize_filename("a\\b.png"), "a/b.png")

    def test_reject_abs(self):
        self.assertEqual(normalize_filename("C:/secret.png"), "")
        # POSIX 绝对路径会先剥掉前导 /，变成 input 相对路径，和原插件一致
        self.assertEqual(normalize_filename("/etc/passwd"), "etc/passwd")

    def test_reject_dotdot(self):
        self.assertEqual(normalize_filename("../x.png"), "")
        self.assertEqual(normalize_filename("a/../b.png"), "")

    def test_reject_empty(self):
        self.assertEqual(normalize_filename(""), "")
        self.assertEqual(normalize_filename("   "), "")


class TestNormalizeState(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(
            normalize_media_state(""),
            {"image": [], "audio": [], "video": []},
        )

    def test_json_and_cap(self):
        payload = {
            "images": [{"filename": f"{i}.png"} for i in range(12)],
            "audios": [{"filename": "a.wav"}, {"filename": "b.mp3"}, {"filename": "c.flac"}, {"filename": "d.ogg"}],
            "videos": ["v1.mp4", {"filename": "v2.mov"}, "v3.webm", "v4.mkv"],
        }
        state = normalize_media_state(payload)
        self.assertEqual(len(state["image"]), MAX_IMAGES)
        self.assertEqual(len(state["audio"]), MAX_AUDIOS)
        self.assertEqual(len(state["video"]), MAX_VIDEOS)
        self.assertEqual(state["image"][0], {"filename": "0.png"})
        self.assertEqual(state["video"][-1], {"filename": "v3.webm"})

    def test_dedupe_and_skip_bad(self):
        payload = {
            "images": [
                {"filename": "a.png"},
                "a.png",
                {"filename": "../hack.png"},
                {"filename": "b.png"},
            ]
        }
        state = normalize_media_state(payload)
        self.assertEqual([item["filename"] for item in state["image"]], ["a.png", "b.png"])

    def test_json_roundtrip(self):
        raw = '{"images":[{"filename":"x.png"}],"audios":[],"videos":[{"filename":"a.mp4"}]}'
        encoded = media_state_json(raw)
        self.assertEqual(
            encoded,
            '{"images":[{"filename":"x.png"}],"audios":[],"videos":[{"filename":"a.mp4"}]}',
        )
        again = normalize_media_state(encoded)
        self.assertEqual(again["image"], [{"filename": "x.png"}])
        self.assertEqual(again["video"], [{"filename": "a.mp4"}])


class TestEmptyMode(unittest.TestCase):
    def test_block(self):
        self.assertEqual(resolve_empty_mode("阻断缺失"), EMPTY_BLOCK)
        self.assertEqual(resolve_empty_mode("block_missing"), EMPTY_BLOCK)

    def test_none(self):
        self.assertEqual(resolve_empty_mode("允许空值"), EMPTY_NONE)
        self.assertEqual(resolve_empty_mode("allow_none"), EMPTY_NONE)

    def test_bad(self):
        with self.assertRaises(ValueError):
            resolve_empty_mode("whatever")


class TestSplitBundle(unittest.TestCase):
    def test_automatic_counts_ignore_stale_manual_values(self):
        frames = torch.zeros((2, 8, 8, 3))
        video = SimpleNamespace(save_to=lambda *args: None,
                                get_components=lambda: SimpleNamespace(images=frames))
        audio = {"waveform": torch.zeros((1, 1, 32)), "sample_rate": 16000}
        bundle = ZiyuanMediaBundle((
            ZiyuanMediaItem(1, "image", "first"),
            ZiyuanMediaItem(2, "image", "second"),
            ZiyuanMediaItem(3, "image", "third"),
            ZiyuanMediaItem(4, "video", video),
            ZiyuanMediaItem(5, "audio", audio),
        ))
        for counts in ((1, 0, 0), (0, 0, 0), (9, 3, 3)):
            with self.subTest(legacy_counts=counts):
                outputs = split_bundle(bundle, *counts, empty_output_mode=EMPTY_NONE)
                self.assertEqual(outputs[:3], ("first", "second", "third"))
                self.assertIs(outputs[9], video)
                self.assertIs(outputs[12]["waveform"], audio["waveform"])
                self.assertIsNone(outputs[3])
                self.assertIsNone(outputs[10])
                self.assertIsNone(outputs[13])
        # Removing every asset must reset the outputs, not retain cached data.
        outputs = split_bundle(ZiyuanMediaBundle(()), 9, 3, 3, EMPTY_NONE)
        self.assertTrue(all(value is None for value in outputs))

    def test_output_length(self):
        with self.assertRaises(ValueError):
            split_bundle("not-a-bundle", 1, 0, 0)

    def test_counts_and_block(self):
        frames = torch.zeros((2, 8, 8, 3))
        video = SimpleNamespace(save_to=lambda *args: None, get_components=lambda: SimpleNamespace(images=frames))
        bundle = ZiyuanMediaBundle(
            (
                ZiyuanMediaItem(1, "image", "img-a"),
                ZiyuanMediaItem(2, "video", video),
            )
        )
        outputs = split_bundle(bundle, image_count=2, video_count=1, audio_count=0, empty_output_mode=EMPTY_BLOCK)
        self.assertEqual(len(outputs), MAX_IMAGES + MAX_VIDEOS + MAX_AUDIOS)
        self.assertEqual(outputs[0], "img-a")
        self.assertIsNotNone(outputs[1])
        self.assertNotEqual(outputs[1], "img-a")
        self.assertIsInstance(outputs[1], media.ExecutionBlocker)
        self.assertIs(outputs[MAX_IMAGES], video)

    def test_allow_none(self):
        bundle = ZiyuanMediaBundle((ZiyuanMediaItem(1, "image", "img-a"),))
        outputs = split_bundle(bundle, image_count=2, video_count=0, audio_count=0, empty_output_mode=EMPTY_NONE)
        self.assertEqual(outputs[0], "img-a")
        self.assertIsNone(outputs[1])


    def test_video_tensor_is_not_native_video(self):
        bundle = ZiyuanMediaBundle((ZiyuanMediaItem(1, "video", torch.zeros((2, 8, 8, 3))),))
        with self.assertRaises(ValueError):
            split_bundle(bundle, 0, 1, 0)

    def test_audio_and_image_order(self):
        audio = {"waveform": torch.zeros((1, 2, 32)), "sample_rate": 16000}
        bundle = ZiyuanMediaBundle((
            ZiyuanMediaItem(1, "image", "first"),
            ZiyuanMediaItem(2, "audio", audio),
            ZiyuanMediaItem(3, "image", "second"),
        ))
        result = split_bundle(bundle, 2, 0, 1, EMPTY_NONE)
        self.assertEqual(result[:2], ("first", "second"))
        self.assertIs(result[12]["waveform"], audio["waveform"])
        self.assertEqual(result[12]["sample_rate"], 16000)

    def test_output_contract(self):
        self.assertEqual(ZiyuanMediaSplitter.RETURN_TYPES,
                         ("IMAGE",) * 9 + ("VIDEO",) * 3 + ("AUDIO",) * 3)
        self.assertEqual(ZiyuanMediaLoader.RETURN_TYPES,
                         ZiyuanMediaSplitter.INPUT_TYPES()["required"]["media_bundle"])
        self.assertEqual(NODE_DISPLAY_NAME_MAPPINGS, {
            "ZiyuanMediaLoader": "ziyuanAI统一素材加载",
            "ZiyuanMediaSplitter": "ziyuanAI素材拆分",
            "ZiyuanVideoToReferenceFrames": "ziyuanAI视频转参考帧",
        })
        self.assertEqual(set(NODE_CLASS_MAPPINGS), set(NODE_DISPLAY_NAME_MAPPINGS))
        for node in NODE_CLASS_MAPPINGS.values():
            self.assertEqual(node.CATEGORY, "ziyuanAI")


class TestVideoToReferenceFrames(unittest.TestCase):
    def test_frames_are_exposed_for_reference_nodes(self):
        frames = [torch.zeros((index + 2, 8, 12, 3)) for index in range(3)]
        audios = [{"waveform": torch.full((1, 1, 32), float(index)), "sample_rate": 16000}
                  for index in range(3)]
        videos = [
            SimpleNamespace(get_components=lambda index=index: SimpleNamespace(
                images=frames[index], audio=audios[index],
            ))
            for index in range(3)
        ]
        result = ZiyuanVideoToReferenceFrames().convert(*videos)
        self.assertEqual(len(result), 6)
        for index in range(3):
            self.assertIs(result[index], frames[index])
            self.assertIs(result[index + 3], audios[index])

    def test_unconnected_optional_videos_return_empty_outputs(self):
        frames = torch.zeros((5, 8, 12, 3))
        video = SimpleNamespace(get_components=lambda: SimpleNamespace(
            images=frames, audio=None, frame_rate=24,
        ))
        result = ZiyuanVideoToReferenceFrames().convert(video)
        self.assertIs(result[0], frames)
        self.assertEqual(result[1:], (None,) * 5)

    def test_silent_middle_video_preserves_audio_numbering(self):
        frames = torch.zeros((2, 8, 12, 3))
        audios = [{"waveform": torch.zeros((1, 1, 32)), "sample_rate": 16000}, None,
                  {"waveform": torch.ones((1, 2, 64)), "sample_rate": 24000}]
        videos = [SimpleNamespace(get_components=lambda audio=audio: SimpleNamespace(
            images=frames, audio=audio,
        )) for audio in audios]
        result = ZiyuanVideoToReferenceFrames().convert(*videos)
        for index, audio in enumerate(audios):
            self.assertIs(result[index + 3], audio)

    def test_non_video_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "原生 VIDEO"):
            ZiyuanVideoToReferenceFrames().convert(torch.zeros((1, 8, 8, 3)))

    def test_schema_has_three_video_slots(self):
        inputs = ZiyuanVideoToReferenceFrames.INPUT_TYPES()
        self.assertEqual(set(inputs["required"]), {"video"})
        self.assertEqual(set(inputs["optional"]), {"video_2", "video_3"})
        self.assertEqual(ZiyuanVideoToReferenceFrames.RETURN_TYPES, ("IMAGE",) * 3 + ("AUDIO",) * 3)
        self.assertEqual(
            ZiyuanVideoToReferenceFrames.RETURN_NAMES,
            ("video_frames_1", "video_frames_2", "video_frames_3",
             "video_audio_1", "video_audio_2", "video_audio_3"),
        )


class TestNativeFiles(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("COMFYUI_PATH"), "Set COMFYUI_PATH for native media loading")
    def test_load_split_and_existing_video_input(self):
        import av
        import importlib
        from PIL import Image
        from comfy_api.latest import Input

        # Import the standalone generation module; no network or generation call.
        nodes = importlib.import_module("test_plugin").nodes
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (64, 48), "red").save(root / "image.png")
            with wave.open(str(root / "audio.wav"), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 16000)
            with av.open(str(root / "video.mp4"), "w", format="mp4") as container:
                stream = container.add_stream("libx264", rate=24)
                stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
                for _ in range(3):
                    frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), "blue"))
                    for packet in stream.encode(frame):
                        container.mux(packet)
                for packet in stream.encode():
                    container.mux(packet)
            state = media_state_json({"images": ["image.png"], "videos": ["video.mp4"],
                                      "audios": ["audio.wav"]})
            with patch.object(media.folder_paths, "get_input_directory", return_value=directory):
                self.assertIs(ZiyuanMediaLoader.VALIDATE_INPUTS(state), True)
                signature = ZiyuanMediaLoader.IS_CHANGED(state)
                bundle, = ZiyuanMediaLoader().load(state)
                outputs = ZiyuanMediaSplitter().split(bundle, 1, 1, 1)
                self.assertEqual(tuple(outputs[0].shape), (1, 48, 64, 3))
                self.assertIsInstance(outputs[9], Input.Video)
                self.assertEqual(outputs[9].get_dimensions(), (64, 48))
                self.assertEqual(outputs[12]["sample_rate"], 8000)
                self.assertEqual(tuple(outputs[12]["waveform"].shape), (1, 1, 16000))
                converted = ZiyuanVideoToReferenceFrames().convert(outputs[9])
                self.assertEqual(tuple(converted[0].shape), (3, 48, 64, 3))
                self.assertEqual(converted[1:], (None,) * 5)
                self.assertTrue(nodes.collect_videos({"参考视频1": outputs[9]})[0])
                self.assertTrue(nodes.collect_audio({"参考音频1": outputs[12]}, 3, 15)[0])
                self.assertEqual(ZiyuanMediaLoader.IS_CHANGED(state), signature)
                missing = media_state_json({"images": ["missing.png"]})
                self.assertIsInstance(ZiyuanMediaLoader.VALIDATE_INPUTS(missing), str)


if __name__ == "__main__":
    unittest.main()
