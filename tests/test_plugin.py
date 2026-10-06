import base64
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from unittest.mock import patch

import torch
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("COMFYUI_PATH"):
    sys.path.insert(0, os.environ["COMFYUI_PATH"])
spec = importlib.util.spec_from_file_location("ziyuan_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
api = sys.modules["ziyuan_plugin.api"]
nodes = sys.modules["ziyuan_plugin.nodes"]


class FakeGateway(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        content_type = self.headers["Content-Type"]
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw)
            body = {"files": []}
            for part in message.iter_parts():
                name = part.get_param("name", header="Content-Disposition")
                if part.get_filename():
                    body["files"].append((name, part.get_filename(), part.get_content_type(), part.get_payload(decode=True)))
                else:
                    body[name] = part.get_payload(decode=True).decode()
        else:
            body = json.loads(raw)
        self.server.calls.append(("POST", self.path, body, self.headers.get("Authorization")))
        self.reply()

    def do_GET(self):
        self.server.calls.append(("GET", self.path, None, self.headers.get("Authorization")))
        self.reply()

    def reply(self):
        status, headers, body = self.server.responses.pop(0)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", **headers}
        self.send_response(status)
        for name, value in {"Content-Length": str(len(body)), **headers}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), "red").save(buffer, "PNG")
        cls.png = buffer.getvalue()
        cls.b64 = base64.b64encode(cls.png).decode()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        self.server.responses = []
        self.server.calls = []
        self.client = api.ZiyuanClient(self.base + "/v1", "test-key", 30)
        self.addCleanup(self.client.close)

    def respond(self, *bodies):
        self.server.responses.extend((200, {}, body) for body in bodies)

    def test_each_image_model_parses_real_png(self):
        for model in api.IMAGE_MODELS:
            with self.subTest(model=model):
                self.respond({"data": [{"b64_json": self.b64}]})
                with patch.object(nodes, "check_cancel"):
                    tensor, status = nodes.ZiyuanImageNode().generate("test", "test-key", self.base, 30, 0, model, "8x6", 1)
                self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
                self.assertAlmostEqual(tensor[0, 0, 0, 0].item(), 1)
                self.assertIn(model, status)
                self.assertEqual(self.server.calls[-1][1], "/v1/images/generations")
                self.assertEqual(self.server.calls[-1][2]["model"], model)

    def test_super_image_normal_async_and_reference_requests(self):
        model = "gpt-image-2-super"
        for cls in (nodes.ZiyuanImageNode, nodes.ZiyuanImageSubmitNode):
            self.assertIn(model, cls.INPUT_TYPES()["required"]["模型"][0])
        for asynchronous in (False, True):
            for reference in (False, True):
                with self.subTest(asynchronous=asynchronous, reference=reference):
                    self.respond({"data": [{"b64_json": self.b64}]})
                    options = self.image_options() | {"模型": model}
                    if reference:
                        options["参考图1"] = torch.zeros((1, 6, 8, 3))
                    with patch.object(nodes, "check_cancel"):
                        if asynchronous:
                            task, = nodes.ZiyuanImageSubmitNode().submit(**options)
                            tensor, status = nodes.ZiyuanImageFetchNode().fetch(task)
                        else:
                            tensor, status = nodes.ZiyuanImageNode().run(**options)
                    _, path, body, _ = self.server.calls[-1]
                    self.assertEqual(path, "/v1/images/edits" if reference else "/v1/images/generations")
                    self.assertEqual(body["model"], model)
                    self.assertEqual(body["n"], 1)
                    self.assertEqual("image" in body, reference)
                    self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
                    self.assertIn(model, status)
        self.assertEqual(len(self.server.calls), 4)

    def test_duplicate_image_results_preserve_batch_count(self):
        self.respond({"data": [{"b64_json": self.b64}, {"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"):
            tensor, _ = nodes.ZiyuanImageNode().generate("test", "test-key", self.base, 30, 0, api.IMAGE_MODELS[0], "8x6", 2)
        self.assertEqual(tuple(tensor.shape), (2, 6, 8, 3))

    def test_image_compatibility_aliases_are_not_extra_results(self):
        item = {"url": "https://example.test/a.png", "image_url": "https://example.test/a.png", "b64_json": self.b64}
        self.assertEqual(api.media_sources({"data": [item], "result": {"images": [item]},
                                           "output": [item], "images": [item]}), [item["url"]])
        self.assertEqual(api.media_sources({"data": [], "result": {"images": [item]}}), [item["url"]])
        result = {"b64_json": self.b64}
        for asynchronous in (False, True):
            self.respond({"data": [result], "result": [result], "output": [result], "images": [result]})
            with patch.object(nodes, "check_cancel"):
                if asynchronous:
                    task, = nodes.ZiyuanImageSubmitNode().submit(**self.image_options())
                    tensor, _ = nodes.ZiyuanImageFetchNode().fetch(task)
                else:
                    tensor, _ = nodes.ZiyuanImageNode().run(**self.image_options())
            self.assertEqual(tensor.shape[0], 1)
            self.assertEqual(self.server.calls[-1][2]["n"], 1)
        self.assertEqual(len(self.server.calls), 2)

    def test_image_output_count_is_bounded_even_if_gateway_returns_extra_images(self):
        with patch.object(nodes, "check_cancel"):
            for count in (1, 2):
                self.respond({"data": [{"url": self.base + f"/image-{i}"} for i in range(4)]})
                self.server.responses.extend((200, {"Content-Type": "image/png"}, self.png) for _ in range(count))
                tensor, status = nodes.ZiyuanImageNode().run(**(self.image_options() | {"生成数量": count}))
                self.assertEqual(tensor.shape[0], count)
                self.assertIn(f"接口返回 4 条图片结果，按请求数量输出前 {count} 张", status)
        self.assertEqual([x[0] for x in self.server.calls], ["POST", "GET", "POST", "GET", "GET"])

    def test_image_count_invalid_values_do_not_submit(self):
        for count in (0, -1, 11, True, 1.5, "1"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                nodes.ZiyuanImageNode().run(**(self.image_options() | {"生成数量": count}))
        self.assertEqual(self.server.calls, [])

    def test_all_reference_frames_reach_edit_request(self):
        self.respond({"data": [{"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"):
            nodes.ZiyuanImageNode().generate("edit", "test-key", self.base, 30, 0, api.IMAGE_MODELS[0], "8x6", 1,
                                            图片1=torch.zeros((2, 6, 8, 3)), 图片3=torch.ones((1, 6, 8, 3)))
        _, path, body, authorization = self.server.calls[0]
        self.assertEqual(path, "/v1/images/edits")
        self.assertEqual(len(body["image"]), 3)
        self.assertEqual(authorization, "Bearer test-key")
        self.assertNotIn("seed", body)
        for image in body["image"]:
            Image.open(io.BytesIO(base64.b64decode(image.split(",", 1)[1]))).verify()

    def test_async_image_waits_past_preview(self):
        self.respond({"task_id": "image/1", "status": "queued"},
                     {"status": "processing", "data": [{"url": "https://example.test/preview.png"}]},
                     {"status": "SUCCESS", "data": [{"b64_json": self.b64}]})
        with patch.object(api.time, "sleep"):
            sources, task = self.client.generate("image", {"prompt": "test"})
        self.assertEqual(task, "image/1")
        self.assertEqual(sources, [self.b64])
        self.assertEqual(self.server.calls[-1][1], "/v1/images/image%2F1")

    def test_completed_video_downloads_content(self):
        self.respond({"id": "video-1", "status": "queued"}, {"status": "completed"})
        self.server.responses.append((200, {"Content-Type": "video/mp4"}, b"test-video-bytes"))
        with patch.object(api.time, "sleep"):
            sources, task = self.client.generate("video", {"model": api.VIDEO_MODEL, "seconds": 5})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            self.client.save_video(sources[0], path)
            self.assertEqual(path.read_bytes(), b"test-video-bytes")
            self.assertFalse(path.with_suffix(".part").exists())
        self.assertEqual(task, "video-1")
        self.assertEqual(self.server.calls[-1][1], "/v1/videos/video-1/content")

    def test_failed_task_does_not_accept_output_url(self):
        self.respond({"id": "bad", "status": "failed", "message": "rejected", "url": "https://example.test/invalid.mp4"})
        with self.assertRaisesRegex(RuntimeError, "rejected.*\n任务 ID：bad"):
            self.client.generate("video", {})
        self.assertEqual(len(self.server.calls), 1)

    def test_submit_failure_is_not_retried_and_redacts_key(self):
        self.server.responses.append((504, {}, b"upstream timeout test-key"))
        with self.assertRaises(RuntimeError) as result:
            self.client.generate("image", {})
        self.assertEqual(len(self.server.calls), 1)
        self.assertNotIn("test-key", str(result.exception))

    def test_cancellation_stops_polling_without_resubmission(self):
        self.respond({"id": "pending", "status": "queued"})
        with patch.object(api.time, "sleep", side_effect=InterruptedError("cancelled")):
            with self.assertRaises(InterruptedError):
                self.client.generate("video", {})
        self.assertEqual(len(self.server.calls), 1)

    def test_timeout_retains_task_id(self):
        self.respond({"id": "pending", "status": "queued"})
        with patch.object(api.time, "sleep", side_effect=lambda _: setattr(self.client, "deadline", 0)):
            with self.assertRaisesRegex(TimeoutError, "任务 ID：pending"):
                self.client.generate("video", {})

    def test_redirect_does_not_forward_key_to_other_origin(self):
        other_origin = self.base.replace("127.0.0.1", "localhost")
        self.server.responses.extend([(302, {"Location": other_origin + "/file"}, b""),
                                      (200, {"Content-Type": "image/png"}, self.png)])
        self.assertEqual(self.client.image_bytes(self.base + "/redirect"), self.png)
        self.assertEqual(self.server.calls[0][3], "Bearer test-key")
        self.assertIsNone(self.server.calls[1][3])

    def test_video_json_link_and_bad_download_cleanup(self):
        self.respond({"url": self.base + "/video"})
        self.server.responses.append((200, {"Content-Type": "text/html"}, b"error page"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            with self.assertRaisesRegex(RuntimeError, "文本"):
                self.client.save_video(self.base + "/content", path)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_validation_prevents_network_requests(self):
        with self.assertRaises(ValueError):
            api.validate_size("0x1024")
        with self.assertRaises(ValueError):
            nodes.collect_references({"图片1": torch.zeros((15, 2, 2, 3))})
        with self.assertRaises(ValueError):
            nodes.ZiyuanImageNode().generate("", "test-key", self.base, 30, 0, api.IMAGE_MODELS[0], "1024x1024", 1)
        self.assertEqual(self.server.calls, [])

    def test_echoed_inputs_are_not_results(self):
        self.assertEqual(api.media_sources({"request": {"images": ["https://example.test/input.png"]}, "status": "queued"}), [])


    def image_options(self):
        return dict(提示词="test", 模型=api.IMAGE_MODELS[0], 比例="16:9", 分辨率="2K", 质量="自动", 输出格式="自动",
                    生成数量=1, 种子=0, 最大等待秒数=30, API密钥="test-key", API地址=self.base, 绕过代理=True)


    def test_only_unified_video_and_image_nodes_are_registered(self):
        expected = {"ZiyuanImageNode", "ZiyuanImageSubmitNode", "ZiyuanImageFetchNode",
                    "ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode", "ZiyuanUnifiedVideoFetchNode"}
        self.assertEqual(set(nodes.NODE_CLASS_MAPPINGS), expected)
        self.assertEqual(set(nodes.NODE_DISPLAY_NAME_MAPPINGS), expected)
        for node in nodes.NODE_CLASS_MAPPINGS.values():
            self.assertEqual(node.CATEGORY, "ziyuanAI")
        for path in (ROOT / "examples").glob("*.json"):
            with self.subTest(workflow=path.name):
                workflow = json.loads(path.read_text(encoding="utf-8"))
                for node in workflow["nodes"]:
                    self.assertIn(node["type"], expected)

    def test_chinese_image_controls_build_payload(self):
        self.respond({"data": [{"b64_json": self.b64}]})
        options = self.image_options()
        options.update(质量="高", 输出格式="webp")
        with patch.object(nodes, "check_cancel"):
            nodes.ZiyuanImageNode().run(**options)
        body = self.server.calls[0][2]
        width, height = map(int, body["size"].split("x"))
        self.assertAlmostEqual(width / height, 16 / 9, delta=0.02)
        self.assertEqual(body["quality"], "high")
        self.assertEqual(body["output_format"], "webp")
        self.assertNotIn("seed", body)


    def test_auto_ratio_follows_first_reference_and_resolution_limits(self):
        ratio = nodes.selected_ratio("自动", {"参考图2": torch.zeros((1, 16, 9, 3))}, "1:1")
        self.assertEqual(ratio, 9 / 16)
        for resolution in ("1K", "2K", "4K"):
            for label in nodes.RATIOS[1:]:
                ratio = nodes.selected_ratio(label, {}, "1:1")
                width, height = map(int, nodes.image_size(ratio, resolution).split("x"))
                self.assertEqual(width % 16, 0)
                self.assertEqual(height % 16, 0)
                self.assertLessEqual(max(width, height), 3840)
                self.assertLessEqual(width * height, 8294400)
        self.assertEqual(nodes.video_size(16 / 9, "720p"), "1280x720")
        self.assertEqual(nodes.video_size(9 / 16, "1080p"), "1080x1920")

    def test_async_submit_returns_before_generation_finishes(self):
        release = threading.Event()
        started = threading.Event()

        def blocked_operation(cancel):
            started.set()
            release.wait(5)
            return ("result",)

        task = nodes.jobs.submit("image", blocked_operation)
        try:
            self.assertTrue(started.wait(2))
            self.assertFalse(nodes.jobs._jobs[task].future.done())
        finally:
            release.set()
        self.assertEqual(nodes.jobs.fetch(task, "image", lambda: None), ("result",))

    def test_async_image_submit_fetch_and_repeat_do_not_resubmit(self):
        self.respond({"data": [{"b64_json": self.b64}]})
        task, = nodes.ZiyuanImageSubmitNode().submit(**self.image_options())
        with patch.object(nodes, "check_cancel"):
            tensor, status = nodes.ZiyuanImageFetchNode().fetch(task)
            again, _ = nodes.ZiyuanImageFetchNode().fetch(task)
        self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
        self.assertIs(again, tensor)
        self.assertEqual(len(self.server.calls), 1)
        with self.assertRaisesRegex(ValueError, "任务类型不匹配"):
            nodes.jobs.fetch(task, "video", lambda: None)

    def test_async_timeout_is_propagated_and_fetch_cancel_signals_worker(self):
        def failed(cancel):
            raise TimeoutError("worker timed out")
        task = nodes.jobs.submit("image", failed)
        with self.assertRaisesRegex(TimeoutError, "worker timed out"):
            nodes.jobs.fetch(task, "image", lambda: None)
        release = threading.Event()
        def pending(cancel):
            release.wait(5)
            cancel()
        task = nodes.jobs.submit("video", pending)
        def cancel_fetch():
            raise InterruptedError("cancel")
        try:
            with self.assertRaises(InterruptedError):
                nodes.jobs.fetch(task, "video", cancel_fetch)
            self.assertTrue(nodes.jobs._jobs[task].cancel.is_set())
        finally:
            release.set()
            try:
                nodes.jobs._jobs[task].future.result(timeout=2)
            except Exception:
                pass

    @unittest.skipUnless(os.environ.get("COMFYUI_PATH"), "Set COMFYUI_PATH to test native ComfyUI VIDEO")
    def test_native_video_node_end_to_end(self):
        import av
        import folder_paths
        from comfy_api.input_impl import VideoFromFile

        buffer = io.BytesIO()
        with av.open(buffer, "w", format="mp4") as container:
            stream = container.add_stream("libx264", rate=24)
            stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
            for _ in range(3):
                frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), "blue"))
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        self.respond({"id": "native-video", "status": "queued"}, {"status": "completed"})
        self.server.responses.append((200, {"Content-Type": "video/mp4"}, buffer.getvalue()))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(folder_paths, "get_output_directory", return_value=directory), patch.object(nodes, "check_cancel"), patch.object(api.time, "sleep"):
                video, path, status = nodes.ZiyuanVideoNode().generate("test", "test-key", self.base, 30, 0, api.VIDEO_MODEL, "1280x720", 5,
                                                                    图片1=torch.zeros((2, 8, 8, 3)))
            self.assertIsInstance(video, VideoFromFile)
            self.assertEqual(video.get_dimensions(), (64, 48))
            self.assertTrue(Path(path).is_file())
            self.assertIn("native-video", status)
        request = self.server.calls[0]
        self.assertEqual(request[1], "/v1/videos")
        self.assertEqual(request[2]["seconds"], 5)
        self.assertEqual(len(request[2]["images"]), 2)

        self.respond({"id": "native-video-async", "status": "completed"})
        self.server.responses.append((200, {"Content-Type": "video/mp4"}, buffer.getvalue()))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(folder_paths, "get_output_directory", return_value=directory), patch.object(nodes, "check_cancel"):
                task, = nodes.ZiyuanUnifiedVideoSubmitNode().submit(
                    提示词="test", 模型=api.VIDEO_MODEL, 比例="9:16", 分辨率="720p", 时长秒数=5, 轮询间隔秒=3,
                    种子=0, 最大等待秒数=30, API密钥="test-key", API地址=self.base, 绕过代理=True)
                video, path, status = nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
            self.assertEqual(video.get_dimensions(), (64, 48))
            self.assertTrue(Path(path).is_file())
            self.assertIn("native-video-async", status)
        self.assertEqual(self.server.calls[-2][2]["size"], "720x1280")

        for model in api.QIAOMO_MODELS:
            for asynchronous in (False, True):
                self.respond({"id": "unified-test", "status": "queued"}, {"status": "completed"})
                self.server.responses.append((200, {"Content-Type": "video/mp4"}, buffer.getvalue()))
                options = self.video_options(model) | {"生成声音": False,
                    "参考图1": torch.zeros((2, 4, 4, 3)), "参考视频1": VideoFromFile(io.BytesIO(buffer.getvalue())),
                    "参考音频1": self.audio_input()}
                with tempfile.TemporaryDirectory() as directory:
                    with patch.object(folder_paths, "get_output_directory", return_value=directory), patch.object(nodes, "check_cancel"), patch.object(api.time, "sleep"):
                        if asynchronous:
                            task, = nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                            video, path, status = nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
                            again, _, _ = nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
                            self.assertIs(again, video)
                        else:
                            video, path, status = nodes.ZiyuanUnifiedVideoNode().run(**options)
                    self.assertEqual(video.get_dimensions(), (64, 48))
                    self.assertTrue(Path(path).is_file())
                    self.assertIn(model, status)
                self.assertEqual([call[1] for call in self.server.calls[-3:]],
                    ["/v1/videos", "/v1/videos/unified-test", "/v1/videos/unified-test/content"])
                body = self.server.calls[-3][2]
                self.assertEqual(body["model"], model)
                self.assertEqual(json.loads(body["metadata"]), {"resolution": "720p", "ratio": "16:9", "generate_audio": False})
                self.assertEqual([f[0] for f in body["files"]], ["input_reference", "input_reference", "input_video", "input_audio"])
                with av.open(io.BytesIO(body["files"][-2][3])) as uploaded:
                    self.assertEqual(len(list(uploaded.decode(video=0))), 3)
                self.check_relay_contract([{"model": model, "roles": ["reference_audio", "reference_image", "reference_image", "reference_video"],
                    "body": {"kind": "multipart", "fields": {k: [v] for k, v in body.items() if k != "files"},
                             "files": [{"field": f[0], "size": len(f[3])} for f in body["files"]]}}])
        for duration in (0, 15.01, float("nan")):
            with patch.object(VideoFromFile, "get_duration", return_value=duration), self.assertRaises(ValueError):
                nodes.collect_videos({"参考视频1": VideoFromFile(io.BytesIO(buffer.getvalue()))})


    def audio_input(self, seconds=4, batch=1, channels=1, rate=8000):
        return {"waveform": torch.zeros((batch, channels, int(seconds * rate))), "sample_rate": rate}

    def test_audio_encoding_boundaries_and_invalid_inputs(self):
        import wave
        for maximum in (15, 30):
            for seconds in (2, maximum):
                audio = self.audio_input(seconds, batch=2, channels=2)
                audio["waveform"][1].fill_(0.5)
                files = nodes.collect_audio({"参考音频1": audio}, 3, maximum)
                self.assertEqual(len(files), 2)
                for index, raw in enumerate(files):
                    with wave.open(io.BytesIO(raw), "rb") as wav:
                        self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (2, 2, 8000))
                        self.assertEqual(wav.getnframes(), seconds * 8000)
                        self.assertEqual(int.from_bytes(wav.readframes(1)[:2], "little", signed=True), index * 16384)
            for audio in (self.audio_input(1.99), self.audio_input(maximum + .01), self.audio_input(batch=4),
                          self.audio_input(channels=3), {"waveform": torch.zeros((1, 1, 4)), "sample_rate": 0},
                          {"waveform": torch.full((1, 1, 16000), float("nan")), "sample_rate": 8000}, {}):
                with self.assertRaises(ValueError):
                    nodes.collect_audio({"参考音频1": audio}, 3, maximum)
        with self.assertRaisesRegex(ValueError, "15 MiB"):
            nodes.collect_audio({"参考音频1": self.audio_input(30, channels=2, rate=192000)}, 3, 30)
        self.assertEqual(self.server.calls, [])

    def test_audio_multipart_and_model_specific_reference_rules(self):
        cases = []
        signed = "https://assets.test/api/reference/audio?expires=9999999999&signature=test"
        refs = nodes.collect_references({"参考图1": torch.zeros((1, 4, 4, 3))})
        for model in api.QIAOMO_MODELS:
            limit = nodes.VIDEO_NODES[model].AUDIO_LIMIT
            audio = self.audio_input(batch=limit)
            payload = self.qiaomo_payload(model, refs=refs, 参考音频1=audio)
            self.respond({"status": "completed", "id": "audio-test"})
            self.client.generate("video", payload)
            body = self.server.calls[-1][2]
            self.assertEqual([f[0] for f in body["files"]], ["input_reference"] + ["input_audio"] * limit)
            self.assertEqual([f[2] for f in body["files"]][1:], ["audio/wav"] * limit)
            self.assertNotIn("_audio_files", body)
            cases.append({"model": model, "roles": ["reference_audio"] * limit + ["reference_image"],
                "body": {"kind": "multipart", "fields": {k: [v] for k, v in body.items() if k != "files"},
                         "files": [{"field": f[0], "size": len(f[3])} for f in body["files"]]}})
            for change in ({"参考音频1": self.audio_input(batch=limit + 1)},
                           {"参考音频1": self.audio_input(), "Mini音频链接": signed},
                           {"参考音频1": self.audio_input(), "Mini素材模式": "首尾帧"}):
                with self.assertRaises(ValueError):
                    self.qiaomo_payload(model, refs=refs, **change)
            remote = self.qiaomo_payload(model, Mini图片链接=signed, Mini音频链接=signed)
            cases.append({"model": model, "body": {"kind": "json", "value": remote},
                          "roles": ["reference_image", "reference_audio"]})
            if model != "doubao-seedance-2.5":
                with self.assertRaisesRegex(ValueError, "搭配"):
                    self.qiaomo_payload(model, 参考音频1=self.audio_input())
        for local in (True, False):
            payload = self.qiaomo_payload("doubao-seedance-2.5", **(
                {"参考音频1": self.audio_input(30)} if local else {"Mini音频链接": signed}))
            if not local:
                cases.append({"model": "doubao-seedance-2.5", "body": {"kind": "json", "value": payload},
                              "roles": ["reference_audio"]})
            else:
                self.assertEqual(len(payload["_audio_files"]), 1)
                self.respond({"status": "completed", "id": "audio-only-test"})
                self.client.generate("video", payload)
                body = self.server.calls[-1][2]
                self.assertEqual([f[0] for f in body["files"]], ["input_audio"])
                cases.append({"model": "doubao-seedance-2.5", "roles": ["reference_audio"],
                    "body": {"kind": "multipart", "fields": {k: [v] for k, v in body.items() if k != "files"},
                             "files": [{"field": f[0], "size": len(f[3])} for f in body["files"]]}})
        with self.assertRaisesRegex(ValueError, "合计最多12"):
            self.qiaomo_payload("doubao-seedance-2.5", refs=refs * 3, 参考音频1=self.audio_input(batch=10))
        self.check_relay_contract(cases)

    def test_async_audio_input_is_frozen(self):
        audio = self.audio_input()
        with patch.object(nodes.jobs, "submit", return_value="test-job") as submit:
            nodes.ZiyuanUnifiedVideoSubmitNode().submit(**(self.video_options() | {"参考音频1": audio}))
        audio["waveform"].fill_(1)
        with patch.object(nodes.ZiyuanUnifiedVideoNode, "run", return_value=()) as run:
            submit.call_args.args[1](lambda: None)
        self.assertEqual(run.call_args.kwargs["参考音频1"]["waveform"].sum().item(), 0)

    def video_options(self, model="doubao-seedance-2.0-mini"):
        return dict(提示词="test", 模型=model, 比例="16:9", 分辨率="720p", 时长秒数=4, 轮询间隔秒=3,
                    种子=0, 最大等待秒数=30, API密钥="test-key", API地址=self.base, 绕过代理=True)

    def qiaomo_payload(self, model, **kwargs):
        return nodes.VIDEO_NODES[model]().build_payload("test", model, "", 4, kwargs.pop("refs", []),
            _aspect_ratio="16:9", _resolution="720p", _enable_sound=False, **kwargs)

    def test_qiaomo_profiles_and_removed_models(self):
        expected = [api.VIDEO_MODEL, *api.QIAOMO_MODELS]
        for cls in (nodes.ZiyuanUnifiedVideoNode, nodes.ZiyuanUnifiedVideoSubmitNode):
            model_field = cls.INPUT_TYPES()["required"]["模型"]
            self.assertEqual(model_field[0], expected)
            profiles = model_field[1]["ziyuan_profiles"]
            for model, resolutions, images, videos in (
                    ("doubao-seedance-2.0", ["720p", "480p", "1080p", "4k"], 9, 3),
                    (api.SEEDANCE_MINI_MODEL, ["720p"], 9, 3),
                    ("doubao-seedance-2.5", ["720p", "480p", "1080p"], 12, 12)):
                profile = profiles[model]
                self.assertEqual(profile["resolutions"], resolutions)
                self.assertEqual((profile["images"], profile["videos"], profile["audio_limit"]),
                                 (images, videos, 10 if model == "doubao-seedance-2.5" else 3))
                self.assertEqual(profile["duration"], {"default": 4, "min": 4, "max": 30 if model == "doubao-seedance-2.5" else 15})
                self.assertTrue(profile["sound"])
                self.assertEqual("自动" in profile["ratios"], model == api.SEEDANCE_MINI_MODEL)
        for model in ("seedance-2.5v5", "seedance-2.0-fast-1080p", "jimeng-2.5-480p", "jimeng-2.5-720p"):
            with self.assertRaises(ValueError):
                nodes.ZiyuanUnifiedVideoNode().run(**self.video_options(model))
        for path in (ROOT / "examples").glob("*.json"):
            for node in json.loads(path.read_text(encoding="utf-8"))["nodes"]:
                if node["type"] in ("ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"):
                    self.assertIn(node["widgets_values"][1], expected)
                    self.assertEqual(node["widgets_values"][13:], ["组合参考", "", "", ""])
        self.assertEqual(self.server.calls, [])

    def test_qiaomo25_duration_sync_async_requests_and_boundaries(self):
        def generate(prompt, key, base, timeout, seed, model, size, seconds, **kwargs):
            payload = nodes.VIDEO_NODES[model]().build_payload(prompt, model, size, seconds, [], **kwargs)
            return self.client.generate("video", payload)

        with patch.object(nodes.ZiyuanQiaomo25Node, "generate", side_effect=generate), patch.object(nodes, "check_cancel"):
            for asynchronous in (False, True):
                for seconds in (4, 15, 16, 30):
                    self.respond({"id": "duration-test", "status": "completed"})
                    options = self.video_options("doubao-seedance-2.5") | {"时长秒数": seconds}
                    if asynchronous:
                        task, = nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                        nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
                    else:
                        nodes.ZiyuanUnifiedVideoNode().run(**options)
                    self.assertEqual(self.server.calls[-1][1], "/v1/videos")
                    self.assertEqual(self.server.calls[-1][2]["seconds"], str(seconds))
                for model, seconds in (("doubao-seedance-2.5", 31), ("doubao-seedance-2.0", 30),
                                       (api.SEEDANCE_MINI_MODEL, 30)):
                    options = self.video_options(model) | {"时长秒数": seconds}
                    before = len(self.server.calls)
                    with self.assertRaisesRegex(ValueError, "不支持所选时长"):
                        if asynchronous:
                            task, = nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                            nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
                        else:
                            nodes.ZiyuanUnifiedVideoNode().run(**options)
                    self.assertEqual(len(self.server.calls), before)

    def test_qiaomo_all_controls_json_and_actual_relay_contract(self):
        cases = []
        profiles = nodes.ZiyuanUnifiedVideoNode.INPUT_TYPES()["required"]["模型"][1]["ziyuan_profiles"]
        for model in api.QIAOMO_MODELS:
            for resolution in profiles[model]["resolutions"]:
                for ratio in profiles[model]["ratios"]:
                    for seconds in range(4, 16):
                        for sound in (True, False):
                            options = self.video_options(model) | {"比例": ratio, "分辨率": resolution,
                                                                  "时长秒数": seconds, "生成声音": sound}
                            with patch.object(nodes.VIDEO_NODES[model], "generate", return_value=()) as generate:
                                nodes.ZiyuanUnifiedVideoNode().run(**options)
                            args, kwargs = generate.call_args
                            payload = nodes.VIDEO_NODES[model]().build_payload(args[0], args[5], args[6], args[7], [], **kwargs)
                            expected = {"model": model, "prompt": "test", "seconds": str(seconds), "metadata": {
                                "ratio": "adaptive" if ratio == "自动" else ratio, "resolution": resolution, "generate_audio": sound}}
                            self.assertEqual(payload, expected)
                            cases.append({"model": model, "body": {"kind": "json", "value": payload}})
        self.assertEqual(len(cases), 1176)
        self.check_relay_contract(cases)

    def check_relay_contract(self, cases):
        import subprocess
        if not os.environ.get("QIAOMO_PLUGIN_PATH"):
            self.skipTest("Set QIAOMO_PLUGIN_PATH to verify actual relay 0.4.1")
        script = '''
import fs from 'node:fs';
import assert from 'node:assert/strict';
const relay = await import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync(process.env.QIAOMO_PLUGIN_PATH)).toString('base64'));
assert.equal(relay.meta.version, '0.4.1');
for (const input of JSON.parse(fs.readFileSync(0, 'utf8'))) {
    const decoded = relay.protocols.openai_video.decodeRequest(input);
    const ctx = {requestBody: decoded.requestBody, upstreamModel: input.model, files: input.body.files || [],
                 qiaomoAssetUpload: true, qiaomoAudioUpload: true, baseUrl: 'https://example.test', apiKey: 'fake', action: decoded.action};
    const request = relay.buildSubmitRequest(ctx);
    assert.equal(request.body.model, input.model);
    assert.deepEqual(request.body, decoded.requestBody);
    assert.equal(relay.extractUsage(ctx).seconds, Number(decoded.requestBody.seconds));
    const facts = relay.extractUsage({...ctx, usagePurpose: 'facts'});
    assert.equal(facts.resolution, decoded.requestBody.metadata.resolution);
    const content = request.body.metadata.content || [];
    assert.equal(facts.reference_mode, content.some(x => x.type === 'video_url') ? 'input_video' : 'no_video');
    if (input.roles) assert.deepEqual(content.map(x => x.role), input.roles);
}
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(cases),
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_qiaomo_local_multipart_roles_order_and_relay_contract(self):
        cases = []
        refs = nodes.collect_references({"参考图1": torch.zeros((1, 4, 4, 3)), "参考图2": torch.ones((1, 4, 4, 3))})
        for model in api.QIAOMO_MODELS:
            for mode in ("组合参考", "首尾帧"):
                payload = self.qiaomo_payload(model, refs=refs, Mini素材模式=mode)
                self.respond({"status": "completed", "id": "test"})
                self.client.generate("video", payload)
                body = self.server.calls[-1][2]
                self.assertEqual(set(body), {"model", "prompt", "seconds", "metadata", "files"})
                self.assertEqual(json.loads(body["metadata"])["generate_audio"], False)
                fields = [f[0] for f in body["files"]]
                self.assertEqual(fields, ["input_reference"] * 2 if mode == "组合参考" else ["first_frame", "last_frame"])
                self.assertEqual([Image.open(io.BytesIO(f[3])).getpixel((0, 0)) for f in body["files"]],
                                 [(0, 0, 0), (255, 255, 255)])
                cases.append({"model": model, "roles": ["reference_image"] * 2 if mode == "组合参考" else fields,
                    "body": {"kind": "multipart", "fields": {k: [v] for k, v in body.items() if k != "files"},
                             "files": [{"field": f[0], "size": len(f[3])} for f in body["files"]]}})
        self.check_relay_contract(cases)

    def test_mini_480p_rejected_before_normal_or_async_network_request(self):
        options = self.video_options() | {"分辨率": "480p"}
        with patch.object(nodes, "check_cancel"):
            with self.assertRaisesRegex(ValueError, "不支持所选比例或分辨率"):
                nodes.ZiyuanUnifiedVideoNode().run(**options)
            task, = nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
            with self.assertRaisesRegex(ValueError, "不支持所选比例或分辨率"):
                nodes.ZiyuanUnifiedVideoFetchNode().fetch(task)
        self.assertEqual(self.server.calls, [])

    def test_qiaomo_validation_prevents_submit(self):
        for model in api.QIAOMO_MODELS:
            for change in ({"时长秒数": 3}, {"时长秒数": 31 if model == "doubao-seedance-2.5" else 16}, {"时长秒数": True}, {"时长秒数": 4.5},
                           {"比例": "2:3"}, {"分辨率": "4K"}, {"生成声音": "false"},
                           {"参考图15": torch.zeros((1, 2, 2, 3))}, {"参考音频1": {}}, {"参考音频10": {}},
                           {"Mini素材模式": "首尾帧"}, {"Mini音频链接": "https://assets.test/a.wav"},
                           {"Mini素材模式": "首尾帧", "参考图1": torch.zeros((3, 2, 2, 3))}):
                with self.subTest(model=model, change=list(change)), self.assertRaises(ValueError), patch.object(nodes, "check_cancel"):
                    nodes.ZiyuanUnifiedVideoNode().run(**(self.video_options(model) | change))
        for model in ("doubao-seedance-2.0", "doubao-seedance-2.5"):
            with self.assertRaises(ValueError):
                nodes.ZiyuanUnifiedVideoNode().run(**(self.video_options(model) | {"比例": "自动"}))
        for model in api.QIAOMO_MODELS:
            limit = nodes.VIDEO_NODES[model].REFERENCE_LIMIT
            with self.assertRaises(ValueError), patch.object(nodes, "check_cancel"):
                nodes.ZiyuanUnifiedVideoNode().run(**(self.video_options(model) | {"参考图1": torch.zeros((limit + 1, 2, 2, 3))}))
        self.assertEqual(self.server.calls, [])

    def test_qiaomo_total_material_limit_and_legacy_urls(self):
        signed = "https://assets.test/api/reference/id?expires=9999999999&signature=test"
        for model in api.QIAOMO_MODELS:
            payload = self.qiaomo_payload(model, Mini图片链接=signed, Mini视频链接=signed)
            self.assertEqual([x["role"] for x in payload["metadata"]["content"]], ["reference_image", "reference_video"])
            with self.assertRaises(ValueError):
                self.qiaomo_payload(model, refs=["unused"], Mini图片链接=signed)
        payload = self.qiaomo_payload(api.SEEDANCE_MINI_MODEL, refs=["local"] * 9,
            Mini音频链接="https://assets.test/a.wav\nasset://audio2\nasset://audio3")
        self.assertEqual(len(payload["metadata"]["content"]), 3)
        remote = self.qiaomo_payload(api.SEEDANCE_MINI_MODEL, Mini图片链接="asset://image", Mini音频链接="asset://audio")
        self.check_relay_contract([{"model": api.SEEDANCE_MINI_MODEL, "body": {"kind": "json", "value": remote},
                                    "roles": ["reference_image", "reference_audio"]}])
        with self.assertRaisesRegex(ValueError, "合计最多12"):
            self.qiaomo_payload(api.SEEDANCE_MINI_MODEL, refs=["local"] * 9,
                Mini视频链接="https://assets.test/v.mp4", Mini音频链接="asset://a\nasset://b\nasset://c")
        with self.assertRaisesRegex(ValueError, "合计最多12"):
            self.qiaomo_payload("doubao-seedance-2.5", refs=["local"] * 12, Mini视频链接=signed)
        for url in ("http://assets.test/a.png", "data:image/png;base64,eA==", "https://user@assets.test/a", "https://assets.test/a#frag"):
            with self.assertRaises(ValueError):
                self.qiaomo_payload(api.SEEDANCE_MINI_MODEL, Mini图片链接=url)
        with self.assertRaisesRegex(ValueError, "签名"):
            self.qiaomo_payload("doubao-seedance-2.0", Mini图片链接="https://assets.test/a.png")
        self.assertEqual(self.server.calls, [])

    def test_qiaomo_file_size_limits_before_network(self):
        for source in ("https://example.test/a.png", "data:image/png;base64,",
                       "data:image/png;base64," + base64.b64encode(b'x' * (10 * 1024 * 1024 + 1)).decode()):
            with self.assertRaises(ValueError):
                self.client.generate("video", {"model": api.SEEDANCE_MINI_MODEL, "images": [source]})
        for raw in (b'', b'x' * (50 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                self.client.generate("video", {"model": api.SEEDANCE_MINI_MODEL, "_video_files": [raw]})
        self.assertEqual(self.server.calls, [])


if __name__ == "__main__":
    unittest.main()
