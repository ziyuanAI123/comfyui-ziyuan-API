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
        self.clear_jobs()
        self.server.responses = []
        self.server.calls = []
        self.client = api.ZiyuanClient(self.base + "/v1", "test-key", 30)
        self.addCleanup(self.client.close)

    def respond(self, *bodies):
        self.server.responses.extend((200, {}, body) for body in bodies)

    def clear_jobs(self):
        for job in list(nodes.jobs._jobs.values()):
            try:
                job.future.result(timeout=6)
            except Exception:
                pass
        with nodes.jobs._lock:
            nodes.jobs._jobs.clear()
            nodes.jobs._fetch_batches.clear()

    def fetch_image(self, key="test-key"):
        outputs = nodes.ZiyuanImageFetchNode().fetch(key)
        self.assertTrue(all(len(values) == 1 for values in outputs))
        return tuple(values[0] for values in outputs)

    def fetch_video(self, key="test-key"):
        outputs = nodes.ZiyuanUnifiedVideoFetchNode().fetch(key)
        self.assertTrue(all(len(values) == 1 for values in outputs))
        return tuple(values[0] for values in outputs)

    def test_mentions_map_socket_batches_to_actual_upload_ordinals(self):
        inputs = {"参考图1": torch.zeros((2, 6, 8, 3)), "参考图2": torch.ones((1, 6, 8, 3)),
                  "参考音频1": self.audio_input(batch=2), "参考音频2": self.audio_input(),
                  "参考视频2": object()}
        prompt = "@图1 的背景，@图片2 的人物，@音频2 的节奏和@视频2 的动作；email@example.com"
        self.assertEqual(nodes.resolve_prompt_references(prompt, inputs, 3, 1, 3),
                         "（第1至2张参考图片） 的背景，（第3张参考图片） 的人物，（第3段参考音频） 的节奏和（第1段参考视频） 的动作；email@example.com")
        self.assertEqual(nodes.resolve_prompt_references("@图2", {}, 2), "（第2张参考图片）")
        with self.assertRaisesRegex(ValueError, "没有对应"):
            nodes.resolve_prompt_references("@图3", inputs, 3, 1, 3)

    def test_image_mentions_reach_json_and_multipart_with_matching_media(self):
        for model in (api.IMAGE_MODELS[0], "gpt-image-2-super"):
            for asynchronous in (False, True):
                self.respond({"data": [{"b64_json": self.b64}]})
                options = self.image_options() | {"模型": model, "提示词": "让@图2 进入@图1 的场景",
                    "参考图1": torch.zeros((2, 6, 8, 3)), "参考图2": torch.ones((1, 6, 8, 3))}
                with patch.object(nodes, "check_cancel"):
                    if asynchronous:
                        nodes.ZiyuanImageSubmitNode().submit(**options)
                        nodes.ZiyuanImageFetchNode().fetch("test-key")
                    else:
                        nodes.ZiyuanImageNode().run(**options)
                body = self.server.calls[-1][2]
                self.assertEqual(body["prompt"], "让（第3张参考图片） 进入（第1至2张参考图片） 的场景")
                if model == "gpt-image-2-super":
                    images = [raw for _, _, _, raw in body["files"]]
                else:
                    images = [base64.b64decode(url.split(",", 1)[1]) for url in body["image"]]
                self.assertEqual(len(images), 3)
                self.assertEqual(Image.open(io.BytesIO(images[2])).getpixel((0, 0)), (255, 255, 255))

    def test_video_mentions_reach_multipart_and_legacy_url_payload(self):
        model = api.SEEDANCE_MINI_MODEL
        inputs = {"参考图1": torch.zeros((2, 6, 8, 3)), "参考图2": torch.ones((1, 6, 8, 3)),
                  "参考音频1": self.audio_input(batch=2), "参考音频2": self.audio_input()}
        refs = nodes.collect_references(inputs)
        payload = nodes.ZiyuanSeedanceMiniNode().build_payload("@图2 跟随@音频2", model, "", 4, refs,
                    _resolution="720p", _aspect_ratio="16:9", **inputs)
        self.respond({"url": "https://example.test/result.mp4"})
        self.client.generate("video", payload)
        body = self.server.calls[-1][2]
        self.assertEqual(body["prompt"], "（第3张参考图片） 跟随（第3段参考音频）")
        self.assertEqual([field for field, _, _, _ in body["files"]], ["input_reference"] * 3 + ["input_audio"] * 3)
        legacy = nodes.ZiyuanSeedanceMiniNode().build_payload("@图2 与@音频1", model, "", 4, [],
            _resolution="720p", _aspect_ratio="16:9", Mini图片链接="asset://one\nasset://two", Mini音频链接="asset://song")
        self.assertEqual(legacy["prompt"], "（第2张参考图片） 与（第1段参考音频）")
        omni = nodes.ZiyuanVideoNode().build_payload("@图2", api.VIDEO_MODEL, "1280x720", 5, refs, **inputs)
        self.assertEqual(omni["prompt"], "（第3张参考图片）")

    def test_missing_or_disconnected_mentions_never_submit(self):
        for prompt in ("@图0", "@图2", "@视频1", "@音频1", "@已断开(图1)"):
            self.clear_jobs()
            options = self.image_options() | {"提示词": prompt, "参考图1": torch.zeros((1, 6, 8, 3))}
            with patch.object(nodes, "check_cancel"):
                with self.assertRaises(ValueError):
                    nodes.ZiyuanImageNode().run(**options)
                with self.assertRaises(ValueError):
                    nodes.ZiyuanImageSubmitNode().submit(**options)
                with self.assertRaises(ValueError):
                    nodes.ZiyuanUnifiedVideoNode().run(**(self.video_options() | {"提示词": prompt}))
                with self.assertRaises(ValueError):
                    nodes.ZiyuanUnifiedVideoSubmitNode().submit(**(self.video_options() | {"提示词": prompt}))
        self.assertEqual(self.server.calls, [])

    def test_each_image_model_parses_real_png(self):
        for model in api.IMAGE_MODELS:
            with self.subTest(model=model):
                self.respond({"data": [{"b64_json": self.b64}]})
                with patch.object(nodes, "check_cancel"):
                    tensor, status = nodes.ZiyuanImageNode().generate("test", "test-key", self.base, 30, 0, model,
                                                                   "4:3" if model in api.BANANA_MODELS else "8x6", 1)
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
                            nodes.ZiyuanImageSubmitNode().submit(**options)
                            tensor, status = self.fetch_image()
                        else:
                            tensor, status = nodes.ZiyuanImageNode().run(**options)
                    _, path, body, _ = self.server.calls[-1]
                    self.assertEqual(path, "/v1/images/edits" if reference else "/v1/images/generations")
                    self.assertEqual(body["model"], model)
                    self.assertEqual(body["n"], "1" if reference else 1)
                    self.assertNotIn("image", body)
                    self.assertEqual("files" in body, reference)
                    if reference:
                        field, filename, mime, raw = body["files"][0]
                        self.assertEqual(len(body["files"]), 1)
                        self.assertEqual((field, filename, mime), ("image", "reference-1.png", "image/png"))
                        with Image.open(io.BytesIO(raw)) as uploaded:
                            self.assertEqual(uploaded.size, (8, 6))
                            self.assertEqual(uploaded.getpixel((0, 0)), (0, 0, 0))
                    self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
                    self.assertIn(model, status)
        self.assertEqual(len(self.server.calls), 4)

    def test_super_reference_multipart_polling_and_order(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), "blue").save(buffer, "PNG")
        second_png = buffer.getvalue()
        self.respond({"task_id": "super-1", "status": "queued"},
                     {"status": "completed", "data": [{"b64_json": self.b64}]})
        payload = {"model": "gpt-image-2-super", "prompt": "edit", "n": 2,
                   "size": "1024x1024", "quality": "auto", "response_format": "url",
                   "image": ["data:image/png;base64," + base64.b64encode(raw).decode()
                             for raw in (self.png, second_png)]}
        original = json.loads(json.dumps(payload))
        with patch.object(api.time, "sleep"):
            sources, task_id = self.client.generate("image", payload)
        self.assertEqual(payload, original)
        self.assertEqual(task_id, "super-1")
        self.assertEqual(sources, [self.b64])
        method, path, body, auth = self.server.calls[0]
        self.assertEqual((method, path, auth), ("POST", "/v1/images/edits", "Bearer test-key"))
        self.assertEqual({key: value for key, value in body.items() if key != "files"},
                         {key: str(value) for key, value in payload.items() if key != "image"})
        self.assertEqual(body["files"], [("image[]", f"reference-{i}.png", "image/png", raw)
                                          for i, raw in enumerate((self.png, second_png), 1)])
        self.assertEqual([call[:2] for call in self.server.calls],
                         [("POST", "/v1/images/edits"), ("GET", "/v1/images/super-1")])

    def test_super_reference_failure_does_not_resubmit(self):
        self.respond({"error": {"message": "images[].image_url is required"}})
        with self.assertRaisesRegex(RuntimeError, r"image_url is required"):
            self.client.generate("image", {"model": "gpt-image-2-super", "prompt": "edit",
                                          "image": "data:image/png;base64," + self.b64})
        self.assertEqual(len(self.server.calls), 1)
        self.assertIn("files", self.server.calls[0][2])

    def test_flare_sunburst_reference_files_in_normal_and_async_nodes(self):
        for model in ("gpt-image-2.5-flare", "gpt-image-2.5-sunburst"):
            for count in (1, 2):
                for asynchronous in (False, True):
                    with self.subTest(model=model, count=count, asynchronous=asynchronous):
                        self.respond({"data": [{"b64_json": self.b64}]})
                        options = self.image_options() | {"模型": model}
                        options.update({f"参考图{i}": torch.full((1, 6, 8, 3), float(i - 1))
                                        for i in range(1, count + 1)})
                        with patch.object(nodes, "check_cancel"):
                            if asynchronous:
                                nodes.ZiyuanImageSubmitNode().submit(**options)
                                tensor, _ = self.fetch_image()
                            else:
                                tensor, _ = nodes.ZiyuanImageNode().run(**options)
                        _, path, body, _ = self.server.calls[-1]
                        self.assertEqual(path, "/v1/images/edits")
                        self.assertNotIn("image", body)
                        self.assertEqual(body["model"], model)
                        self.assertEqual(body["n"], "1")
                        self.assertEqual([item[0] for item in body["files"]],
                                         ["image" if count == 1 else "image[]"] * count)
                        self.assertEqual([Image.open(io.BytesIO(item[3])).getpixel((0, 0))
                                          for item in body["files"]],
                                         [(0, 0, 0), (255, 255, 255)][:count])
                        self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))

    def test_duplicate_image_results_preserve_batch_count(self):
        self.respond({"data": [{"b64_json": self.b64}, {"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"):
            tensor, _ = nodes.ZiyuanImageNode().generate("test", "test-key", self.base, 30, 0, "gpt-image-2-super", "8x6", 2)
        self.assertEqual(tuple(tensor.shape), (2, 6, 8, 3))

    def test_gpt_image_2_batch_submits_single_images_in_order_normal_and_async(self):
        buffer = io.BytesIO()
        Image.new("RGB", (8, 6), "blue").save(buffer, "PNG")
        blue = base64.b64encode(buffer.getvalue()).decode()
        for count in (2, 10):
            for asynchronous in (False, True):
                with self.subTest(count=count, asynchronous=asynchronous):
                    self.server.calls = []
                    self.respond(*({"id": f"image-{i}", "data": [{"b64_json": self.b64 if i % 2 == 0 else blue}]}
                                   for i in range(count)))
                    options = self.image_options() | {"生成数量": count, "质量": "高", "输出格式": "webp"}
                    if count == 2:
                        options.update({"提示词": "@图3 beside @图1", "参考图1": torch.zeros((2, 6, 8, 3)),
                                        "参考图3": torch.ones((1, 6, 8, 3))})
                    with patch.object(nodes, "check_cancel"):
                        if asynchronous:
                            nodes.ZiyuanImageSubmitNode().submit(**options)
                            images, statuses = nodes.ZiyuanImageFetchNode().fetch("test-key")
                            repeated, _ = nodes.ZiyuanImageFetchNode().fetch("test-key")
                            tensor, status = torch.cat(images), "\n".join(statuses)
                            self.assertTrue(torch.equal(torch.cat(repeated), tensor))
                        else:
                            tensor, status = nodes.ZiyuanImageNode().run(**options)
                    self.assertEqual(tuple(tensor.shape), (count, 6, 8, 3))
                    self.assertEqual(tensor[:, 0, 0].tolist(), [[1., 0., 0.] if i % 2 == 0 else [0., 0., 1.] for i in range(count)])
                    self.assertEqual(len(self.server.calls), count)
                    bodies = [call[2] for call in self.server.calls]
                    self.assertTrue(all(body == bodies[0] for body in bodies))
                    self.assertEqual(bodies[0]["n"], 1)
                    self.assertEqual(bodies[0]["quality"], "high")
                    self.assertEqual(bodies[0]["output_format"], "webp")
                    if count == 2:
                        self.assertEqual(len(bodies[0]["image"]), 3)
                        self.assertEqual(bodies[0]["prompt"], "（第3张参考图片） beside （第1至2张参考图片）")
                    self.assertTrue(all(f"image-{i}" in status for i in range(count)))

    def test_gpt_image_2_batch_failure_stops_without_retry_or_later_submission(self):
        self.respond({"id": "first", "data": [{"b64_json": self.b64}]})
        self.server.responses.append((500, {}, b"upstream failed"))
        self.respond({"data": [{"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"), self.assertRaisesRegex(RuntimeError, "第 2/3 张停止.*此前已解码 1 张") as error:
            nodes.ZiyuanImageNode().run(**(self.image_options() | {"生成数量": 3}))
        self.assertIn("已完成任务 ID：first", str(error.exception))
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(len(self.server.responses), 1)

    def test_gpt_image_2_batch_shares_deadline_and_obeys_cancellation(self):
        original_bytes = api.ZiyuanClient.image_bytes
        for cancelled in (False, True):
            self.server.calls = []
            self.server.responses = []
            self.respond({"data": [{"b64_json": self.b64}]})
            stop = []
            def decode(client, source):
                result = original_bytes(client, source)
                if cancelled:
                    stop.append(True)
                else:
                    client.deadline = 0
                return result
            def check():
                if stop:
                    raise InterruptedError("cancelled")
            with patch.object(api.ZiyuanClient, "image_bytes", decode):
                with self.assertRaises(InterruptedError if cancelled else TimeoutError):
                    nodes.ZiyuanImageNode().run(**(self.image_options() | {"生成数量": 2}), _cancel_check=check)
            self.assertEqual(len(self.server.calls), 1)

    def test_image_short_response_is_an_error_without_resubmission(self):
        self.respond({"id": "short", "data": [{"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"), self.assertRaisesRegex(RuntimeError, "仅返回 1/2 张图片.*short"):
            nodes.ZiyuanImageNode().run(**(self.image_options() | {"模型": "gpt-image-2-super", "生成数量": 2}))
        self.assertEqual(len(self.server.calls), 1)

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
                    nodes.ZiyuanImageSubmitNode().submit(**self.image_options())
                    tensor, _ = self.fetch_image()
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
                tensor, status = nodes.ZiyuanImageNode().run(**(self.image_options() | {"模型": "gpt-image-2-super", "生成数量": count}))
                self.assertEqual(tensor.shape[0], count)
                self.assertIn(f"接口返回 4 条图片结果，按请求数量输出前 {count} 张", status)
        self.assertEqual([x[0] for x in self.server.calls], ["POST", "GET", "POST", "GET", "GET"])

    def test_image_count_invalid_values_do_not_submit(self):
        for count in (0, -1, 11, True, 1.5, "1"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                nodes.ZiyuanImageNode().run(**(self.image_options() | {"生成数量": count}))
        self.assertEqual(self.server.calls, [])

    def test_banana_models_sync_async_parameters_references_and_polling(self):
        for model in api.BANANA_MODELS:
            for asynchronous in (False, True):
                for resolution in ("1K", "2K", "4K"):
                    with self.subTest(model=model, asynchronous=asynchronous, resolution=resolution):
                        self.server.calls = []
                        self.respond({"task_id": "banana/1", "status": "queued"},
                                     {"status": "processing", "url": "/preview.png"},
                                     {"status": "succeeded", "result": {"candidates": [{"content": {"parts": [
                                         {"inlineData": {"mimeType": "image/png", "data": self.b64}}
                                     ]}}]}})
                        options = self.image_options() | {"模型": model, "分辨率": resolution, "比例": "9:16",
                            "提示词": "@图2 的人物和@图1 的背景", "质量": "高", "输出格式": "webp",
                            "参考图1": torch.zeros((2, 6, 8, 3)), "参考图2": torch.ones((1, 6, 8, 3))}
                        with patch.object(nodes, "check_cancel"), patch.object(api.time, "sleep"):
                            if asynchronous:
                                nodes.ZiyuanImageSubmitNode().submit(**options)
                                tensor, status = self.fetch_image()
                                nodes.ZiyuanImageFetchNode().fetch("test-key")
                            else:
                                tensor, status = nodes.ZiyuanImageNode().run(**options)
                        self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
                        self.assertIn(model, status)
                        self.assertEqual([(call[0], call[1]) for call in self.server.calls], [
                            ("POST", "/v1/images/edits"), ("GET", "/v1/images/tasks/banana%2F1"),
                            ("GET", "/v1/images/tasks/banana%2F1")])
                        body = self.server.calls[0][2]
                        self.assertEqual({key: body[key] for key in ("model", "size", "quality", "n")},
                                         dict(model=model, size="9:16", quality=resolution, n=1))
                        self.assertNotIn("output_format", body)
                        self.assertEqual(body["prompt"], "（第3张参考图片） 的人物和（第1至2张参考图片） 的背景")
                        self.assertEqual(len(body["image"]), 3)
                        image = Image.open(io.BytesIO(base64.b64decode(body["image"][2].split(",", 1)[1])))
                        self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))

    def test_banana_text_generation_auto_and_fixed_ratios(self):
        for model in api.BANANA_MODELS:
            for ratio in api.BANANA_RATIOS:
                self.respond({"data": [{"b64_json": self.b64}]})
                with patch.object(nodes, "check_cancel"):
                    nodes.ZiyuanImageNode().run(**(self.image_options() | {"模型": model, "比例": ratio}))
                self.assertEqual(self.server.calls[-1][1], "/v1/images/generations")
                self.assertEqual(self.server.calls[-1][2]["size"], ratio)
        for refs, expected in (({}, "1:1"), ({"参考图1": torch.zeros((1, 16, 9, 3))}, "9:16"),
                               ({"参考图1": torch.zeros((1, 6, 8, 3))}, "4:3")):
            self.respond({"data": [{"b64_json": self.b64}]})
            with patch.object(nodes, "check_cancel"):
                nodes.ZiyuanImageNode().run(**(self.image_options() | {"模型": "T香蕉2", "比例": "自动"} | refs))
            self.assertEqual(self.server.calls[-1][2]["size"], expected)

    def test_banana_invalid_options_and_failed_task_do_not_resubmit(self):
        for model in api.BANANA_MODELS:
            for overrides in ({"生成数量": 2}, {"比例": "21:9"}, {"分辨率": "8K"}):
                with self.assertRaises(ValueError):
                    nodes.ZiyuanImageNode().run(**(self.image_options() | {"模型": model} | overrides))
        self.assertEqual(self.server.calls, [])
        self.respond({"task_id": "failed-banana", "status": "queued"},
                     {"status": "failed", "message": "provider rejected", "url": "/preview.png"})
        with patch.object(api.time, "sleep"), self.assertRaisesRegex(RuntimeError, "provider rejected.*\n任务 ID：failed-banana"):
            self.client.generate("image", {"model": "T香蕉pro", "prompt": "test", "n": 1, "size": "1:1", "quality": "1K"})
        self.assertEqual([call[0] for call in self.server.calls], ["POST", "GET"])

    def test_gemini_image_outputs_exclude_echoed_inputs_thoughts_and_aliases(self):
        candidate = {"content": {"parts": [
            {"thought": True, "inlineData": {"mimeType": "image/png", "data": "preview"}},
            {"text": "Generated image"}, {"inline_data": {"mime_type": "image/png", "data": self.b64}}]}}
        self.assertEqual(api.media_sources({"contents": [candidate]}), [])
        self.assertEqual(api.media_sources({"result": {"candidates": [candidate]}}), ["data:image/png;base64," + self.b64])
        self.assertEqual(api.media_sources({"data": [{"url": "/final.png"}], "candidates": [candidate]}), ["/final.png"])

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
        media_names = {"ZiyuanMediaLoader", "ZiyuanMediaSplitter", "ZiyuanVideoToReferenceFrames"}
        self.assertEqual(set(plugin.NODE_CLASS_MAPPINGS), expected | media_names)
        self.assertEqual(set(plugin.NODE_DISPLAY_NAME_MAPPINGS), expected | media_names)
        for name in media_names:
            self.assertEqual(plugin.NODE_CLASS_MAPPINGS[name].CATEGORY, "ziyuanAI")
        for fetch_node in (nodes.ZiyuanImageFetchNode, nodes.ZiyuanUnifiedVideoFetchNode):
            required = fetch_node.INPUT_TYPES()["required"]
            self.assertEqual(set(required), {"API密钥", "最多等待秒数"})
            self.assertEqual(required["API密钥"][1]["default"], "")
            signature = fetch_node.IS_CHANGED(API密钥="test-key")
            self.assertNotEqual(signature, signature)
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

        task = nodes.jobs.submit("image", blocked_operation, "test-key")
        try:
            self.assertTrue(started.wait(2))
            self.assertFalse(nodes.jobs._jobs[task].future.done())
        finally:
            release.set()
        self.assertEqual(nodes.jobs.fetch(task, "image", lambda: None, "test-key"), ("result",))

    def test_async_image_submit_fetch_and_repeat_do_not_resubmit(self):
        self.respond({"data": [{"b64_json": self.b64}]})
        info, = nodes.ZiyuanImageSubmitNode().submit(**self.image_options())
        self.assertIn("已提交", info)
        task = next(iter(nodes.jobs._jobs))
        with patch.object(nodes, "check_cancel"):
            tensor, status = self.fetch_image()
            again, _ = self.fetch_image()
        self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
        self.assertIs(again, tensor)
        self.assertEqual(len(self.server.calls), 1)
        with self.assertRaisesRegex(ValueError, "任务类型不匹配"):
            nodes.jobs.fetch(task, "video", lambda: None, "test-key")
        with self.assertRaisesRegex(ValueError, "API密钥.*不一致"):
            nodes.jobs.fetch(task, "image", lambda: None, "wrong-key")

    def test_async_posts_on_execution_thread_and_finishes_on_worker(self):
        caller = threading.get_ident()
        original_submit = api.ZiyuanClient.submit_generation
        for kind, submit_type, run_type, options, finish_name in (
                ("image", nodes.ZiyuanImageSubmitNode, nodes.ZiyuanImageNode, self.image_options(), "_finish_images"),
                ("video", nodes.ZiyuanUnifiedVideoSubmitNode, nodes.ZiyuanVideoNode,
                 self.video_options(), "_finish_video")):
            with self.subTest(kind=kind):
                self.clear_jobs()
                release, started = threading.Event(), threading.Event()
                post_threads, finish_threads = [], []
                self.respond({"id": "queued", "status": "queued"})
                def submit(client, *args):
                    post_threads.append(threading.get_ident())
                    return original_submit(client, *args)
                def finish(client, *args):
                    finish_threads.append(threading.get_ident())
                    started.set()
                    try:
                        if not release.wait(5):
                            raise TimeoutError("test worker not released")
                        return ("result",)
                    finally:
                        client.close()
                try:
                    with patch.object(api.ZiyuanClient, "submit_generation", submit), \
                            patch.object(run_type, finish_name, side_effect=finish), patch.object(nodes, "check_cancel"):
                        info, = submit_type().submit(**options)
                        self.assertIn("已提交", info)
                        self.assertTrue(started.wait(2))
                        self.assertEqual(post_threads, [caller])
                        self.assertNotEqual(finish_threads, [caller])
                        self.assertFalse(next(iter(nodes.jobs._jobs.values())).future.done())
                        release.set()
                        self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, "test-key"), [("result",)])
                finally:
                    release.set()

    def test_fetch_deadline_returns_later_success_and_keeps_pending_task(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                self.clear_jobs()
                release = threading.Event()
                def pending(cancel):
                    if not release.wait(5):
                        raise TimeoutError("test worker not released")
                    cancel()
                    return ("first",)
                first = nodes.jobs.submit(kind, pending, "test-key")
                second = nodes.jobs.submit(kind, lambda cancel: ("second",), "test-key")
                nodes.jobs._jobs[second].future.result(timeout=2)
                try:
                    results = nodes.jobs.fetch_batch(kind, lambda: None, "test-key", max_wait=0)
                    self.assertEqual(results, [("second",)])
                    self.assertIn("成功1，失败0，等待中1", results.report)
                    self.assertFalse(nodes.jobs._jobs[first].retrieved)
                    with self.assertRaisesRegex(TimeoutError, "等待中1"):
                        nodes.jobs.fetch_batch(kind, lambda: None, "test-key", max_wait=0)
                    release.set()
                    self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, "test-key"), [("first",)])
                finally:
                    release.set()

    def test_cancelled_batch_fetch_can_be_retried_without_resubmitting(self):
        for kind in ("image", "video"):
            release = threading.Event()
            def pending(cancel):
                release.wait(5)
                cancel()
                return ("kept",)
            task = nodes.jobs.submit(kind, pending, "test-key")
            def cancel_fetch():
                raise InterruptedError("cancel fetch")
            try:
                with self.assertRaisesRegex(InterruptedError, "cancel fetch"):
                    nodes.jobs.fetch_batch(kind, cancel_fetch, "test-key")
                self.assertFalse(nodes.jobs._jobs[task].cancel.is_set())
                self.assertFalse(nodes.jobs._jobs[task].retrieved)
                release.set()
                self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, "test-key"), [("kept",)])
            finally:
                release.set()

    def test_concurrent_fetch_is_rejected_only_for_same_key_and_kind(self):
        nodes.jobs.submit("image", lambda cancel: ("same",), "test-key")
        nodes.jobs.submit("video", lambda cancel: ("video",), "test-key")
        nodes.jobs.submit("image", lambda cancel: ("other",), "other-key")
        checked = False
        def check():
            nonlocal checked
            if checked:
                return
            checked = True
            with self.assertRaisesRegex(RuntimeError, "正在获取"):
                nodes.jobs.fetch_batch("image", lambda: None, "test-key")
            self.assertEqual(nodes.jobs.fetch_batch("video", lambda: None, "test-key"), [("video",)])
            self.assertEqual(nodes.jobs.fetch_batch("image", lambda: None, "other-key"), [("other",)])
        self.assertEqual(nodes.jobs.fetch_batch("image", check, "test-key"), [("same",)])
        self.assertTrue(checked)

    def test_all_failed_batch_is_consumed_and_key_is_redacted(self):
        for kind in ("image", "video"):
            def failed(cancel):
                raise RuntimeError("rejected test-key")
            nodes.jobs.submit(kind, failed, "test-key")
            with self.assertRaisesRegex(RuntimeError, "成功0，失败1") as error:
                nodes.jobs.fetch_batch(kind, lambda: None, "test-key")
            self.assertNotIn("test-key", str(error.exception))
            nodes.jobs.submit(kind, lambda cancel: ("next",), "test-key")
            self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, "test-key"), [("next",)])

    def test_async_later_post_failure_preserves_accepted_gpt_tasks(self):
        self.respond({"id": "accepted", "data": [{"b64_json": self.b64}]})
        self.server.responses.append((500, {}, b"rejected"))
        with patch.object(nodes, "check_cancel"):
            with self.assertRaisesRegex(RuntimeError, "已提交1/3项"):
                nodes.ZiyuanImageSubmitNode().submit(**(self.image_options() | {"生成数量": 3}))
            tensor, status = self.fetch_image()
        self.assertEqual(tuple(tensor.shape), (1, 6, 8, 3))
        self.assertIn("accepted", status)
        self.assertEqual(len(self.server.calls), 2)

    def test_async_gpt_worker_failure_does_not_lose_other_images(self):
        self.respond({"id": "first", "data": [{"b64_json": self.b64}]},
                     {"id": "failed", "status": "failed", "message": "rejected"},
                     {"id": "last", "data": [{"b64_json": self.b64}]})
        with patch.object(nodes, "check_cancel"):
            nodes.ZiyuanImageSubmitNode().submit(**(self.image_options() | {"生成数量": 3}))
            images, statuses = nodes.ZiyuanImageFetchNode().fetch("test-key")
        self.assertEqual(len(images), 2)
        self.assertIn("first", statuses[0])
        self.assertIn("last", statuses[1])
        self.assertTrue(all("成功2，失败1" in status for status in statuses))
        self.assertEqual(len(self.server.calls), 3)

    def test_key_fetch_collects_all_tasks_of_same_key_and_kind(self):
        key = "test-key-select-latest"
        nodes.jobs.submit("image", lambda cancel: ("old-image",), key)
        nodes.jobs.submit("image", lambda cancel: ("new-image",), key)
        nodes.jobs.submit("video", lambda cancel: ("video",), key)
        nodes.jobs.submit("image", lambda cancel: ("other-key",), "other-select-key")
        with patch.object(nodes, "check_cancel"):
            self.assertEqual(nodes.ZiyuanImageFetchNode().fetch(key), (["old-image", "new-image"],))
            self.assertEqual(nodes.ZiyuanUnifiedVideoFetchNode().fetch(key), (["video"],))

    def test_key_fetch_waits_for_entire_batch_instead_of_returning_partial_results(self):
        key = "test-key-pending-latest"
        nodes.jobs.submit("image", lambda cancel: ("old-result",), key)
        release = threading.Event()
        def pending(cancel):
            release.wait(5)
            return ("latest-result",)
        nodes.jobs.submit("image", pending, key)
        def on_wait():
            release.set()
        try:
            with patch.object(nodes, "check_cancel", side_effect=on_wait) as check:
                self.assertEqual(nodes.ZiyuanImageFetchNode().fetch(key), (["old-result", "latest-result"],))
                check.assert_called()
        finally:
            release.set()

    def test_key_fetch_does_not_fall_back_to_other_keys_or_types(self):
        key = "test-key-video-only"
        nodes.jobs.submit("video", lambda cancel: ("video",), key)
        with patch.object(nodes, "check_cancel"):
            with self.assertRaisesRegex(ValueError, "找不到.*图片任务"):
                nodes.ZiyuanImageFetchNode().fetch(key)
            with self.assertRaisesRegex(ValueError, "找不到.*视频任务"):
                nodes.ZiyuanUnifiedVideoFetchNode().fetch("unknown-fetch-key")

    def test_key_fetch_uses_environment_key(self):
        key = "test-key-env-fetch"
        nodes.jobs.submit("image", lambda cancel: ("environment-result",), key)
        with patch.dict(os.environ, {"ZIYUAN_API_KEY": key}), patch.object(nodes, "check_cancel"):
            self.assertEqual(nodes.ZiyuanImageFetchNode().fetch(""), (["environment-result"],))

    def test_three_and_ten_submissions_fetch_exact_image_and_video_batches(self):
        for kind, submit_type, run_type, fetch_type in (
                ("image", nodes.ZiyuanImageSubmitNode, nodes.ZiyuanImageNode, nodes.ZiyuanImageFetchNode),
                ("video", nodes.ZiyuanUnifiedVideoSubmitNode, nodes.ZiyuanUnifiedVideoNode, nodes.ZiyuanUnifiedVideoFetchNode)):
            for count in (3, 10):
                with self.subTest(kind=kind, count=count):
                    self.clear_jobs()
                    expected = []
                    for index in range(count):
                        if kind == "image":
                            # Different sizes and multiple images in one task must survive unchanged.
                            expected.append((torch.full((1 + index % 2, 4 + index, 8, 3), float(index)), f"image-{index}"))
                        else:
                            expected.append((object(), f"video-{index}.mp4", f"video-{index}"))
                    def generate(**inputs):
                        index = int(inputs["提示词"])
                        return (nodes.jobs.submit(kind, lambda cancel: expected[index], "test-key"),)
                    options = self.image_options() if kind == "image" else self.video_options()
                    with patch.object(run_type, "run", side_effect=generate) as run, patch.object(nodes, "check_cancel"):
                        for index in range(count):
                            submit_type().submit(**(options | {"提示词": str(index)}))
                        outputs = fetch_type().fetch("test-key")
                        repeated = fetch_type().fetch("test-key")
                    self.assertEqual(fetch_type.OUTPUT_IS_LIST, (True,) * len(fetch_type.RETURN_TYPES))
                    self.assertEqual([len(values) for values in outputs], [count] * len(expected[0]))
                    for index, result in enumerate(expected):
                        for slot, value in enumerate(result):
                            self.assertIs(outputs[slot][index], value)
                            self.assertIs(repeated[slot][index], value)
                    self.assertEqual(run.call_count, count)
        self.assertEqual(self.server.calls, [])

    def test_next_batch_excludes_previously_retrieved_tasks_for_both_types(self):
        for kind in ("image", "video"):
            key = f"next-batch-{kind}"
            for index in range(3):
                nodes.jobs.submit(kind, lambda cancel, i=index: (i,), key)
            self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, key), [(0,), (1,), (2,)])
            for index in range(10):
                nodes.jobs.submit(kind, lambda cancel, i=index: (i + 3,), key)
            expected = [(index + 3,) for index in range(10)]
            self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, key), expected)
            self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, key), expected)

    def test_submissions_during_fetch_are_reserved_for_next_batch(self):
        for kind in ("image", "video"):
            key = f"snapshot-{kind}"
            nodes.jobs.submit(kind, lambda cancel: ("first",), key)
            nodes.jobs.submit(kind, lambda cancel: ("second",), key)
            added = False
            def submit_during_wait():
                nonlocal added
                if not added:
                    nodes.jobs.submit(kind, lambda cancel: ("later",), key)
                    added = True
            self.assertEqual(nodes.jobs.fetch_batch(kind, submit_during_wait, key), [("first",), ("second",)])
            self.assertEqual(nodes.jobs.fetch_batch(kind, lambda: None, key), [("later",)])

    def test_failed_task_does_not_block_successes_or_next_batch(self):
        key = "failed-batch"
        calls = []
        def generate(cancel, index):
            calls.append(index)
            if index == 1:
                raise ValueError("rejected")
            return (index,)
        tasks = [nodes.jobs.submit("image", lambda cancel, i=i: generate(cancel, i), key) for i in range(3)]
        results = nodes.jobs.fetch_batch("image", lambda: None, key)
        self.assertEqual(results, [(0,), (2,)])
        self.assertIn("成功2，失败1", results.report)
        self.assertIn("第2项失败：rejected", results.report)
        for task in tasks:
            try:
                nodes.jobs._jobs[task].future.result(timeout=2)
            except ValueError:
                pass
            self.assertTrue(nodes.jobs._jobs[task].retrieved)
        self.assertCountEqual(calls, [0, 1, 2])
        nodes.jobs.submit("image", lambda cancel: ("next-batch",), key)
        self.assertEqual(nodes.jobs.fetch_batch("image", lambda: None, key), [("next-batch",)])

    def test_unread_completed_tasks_are_not_evicted_at_capacity(self):
        key = "capacity-batch"
        for index in range(40):
            task = nodes.jobs.submit("video", lambda cancel, i=index: (i,), key)
            nodes.jobs._jobs[task].future.result(timeout=2)
        self.assertEqual(nodes.jobs.fetch_batch("video", lambda: None, key), [(i,) for i in range(40)])
        nodes.jobs.submit("video", lambda cancel: ("next",), key)
        self.assertEqual(nodes.jobs.fetch_batch("video", lambda: None, key), [("next",)])
        self.assertEqual(len(nodes.jobs._jobs), 1)

    def test_async_timeout_is_propagated_and_fetch_cancel_preserves_worker(self):
        def failed(cancel):
            raise TimeoutError("worker timed out")
        task = nodes.jobs.submit("image", failed, "test-key")
        with self.assertRaisesRegex(TimeoutError, "worker timed out"):
            nodes.jobs.fetch(task, "image", lambda: None, "test-key")
        release = threading.Event()
        def pending(cancel):
            release.wait(5)
            cancel()
        task = nodes.jobs.submit("video", pending, "test-key")
        def cancel_fetch():
            raise InterruptedError("cancel")
        try:
            with self.assertRaises(InterruptedError):
                nodes.jobs.fetch(task, "video", cancel_fetch, "test-key")
            self.assertFalse(nodes.jobs._jobs[task].cancel.is_set())
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
                nodes.ZiyuanUnifiedVideoSubmitNode().submit(
                    提示词="test", 模型=api.VIDEO_MODEL, 比例="9:16", 分辨率="720p", 时长秒数=5, 轮询间隔秒=3,
                    种子=0, 最大等待秒数=30, API密钥="test-key", API地址=self.base, 绕过代理=True)
                video, path, status = self.fetch_video()
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
                            nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                            video, path, status = self.fetch_video()
                            again, _, _ = self.fetch_video()
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
        self.respond({"id": "audio-test", "status": "queued"})
        with patch.object(nodes.jobs, "submit", return_value="test-job") as submit, patch.object(nodes, "check_cancel"):
            nodes.ZiyuanUnifiedVideoSubmitNode().submit(**(self.video_options("doubao-seedance-2.5") | {"参考音频1": audio}))
        audio["waveform"].fill_(1)
        uploaded = self.server.calls[-1][2]["files"][0]
        import wave
        with wave.open(io.BytesIO(uploaded[3]), "rb") as wav:
            self.assertEqual(set(wav.readframes(wav.getnframes())), {0})
        # Close the deferred client without invoking a real worker/download.
        with patch.object(api.ZiyuanClient, "finish_generation", side_effect=RuntimeError("stop")):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                submit.call_args.args[1](lambda: None)

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
            submission = self.client.submit_generation("video", payload)
            if kwargs.get("_async"):
                return (nodes.jobs.submit("video", lambda cancel: self.client.finish_generation("video", payload, submission), key),)
            return self.client.finish_generation("video", payload, submission)

        with patch.object(nodes.ZiyuanQiaomo25Node, "generate", side_effect=generate), patch.object(nodes, "check_cancel"):
            for asynchronous in (False, True):
                for seconds in (4, 15, 16, 30):
                    self.respond({"id": "duration-test", "status": "completed"})
                    options = self.video_options("doubao-seedance-2.5") | {"时长秒数": seconds}
                    if asynchronous:
                        nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                        nodes.ZiyuanUnifiedVideoFetchNode().fetch("test-key")
                    else:
                        nodes.ZiyuanUnifiedVideoNode().run(**options)
                    self.assertEqual(self.server.calls[-1][1], "/v1/videos")
                    self.assertEqual(self.server.calls[-1][2]["seconds"], str(seconds))
                for model, seconds in (("doubao-seedance-2.5", 31), ("doubao-seedance-2.0", 30),
                                       (api.SEEDANCE_MINI_MODEL, 30)):
                    self.clear_jobs()
                    options = self.video_options(model) | {"时长秒数": seconds}
                    before = len(self.server.calls)
                    with self.assertRaisesRegex(ValueError, "不支持所选时长"):
                        if asynchronous:
                            nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
                            nodes.ZiyuanUnifiedVideoFetchNode().fetch("test-key")
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
            with self.assertRaisesRegex(ValueError, "不支持所选比例或分辨率"):
                nodes.ZiyuanUnifiedVideoSubmitNode().submit(**options)
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
