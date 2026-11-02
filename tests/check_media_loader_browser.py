"""Headless resize/display regression; uploads only with explicit --upload files."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8188/")
    parser.add_argument("--script", type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--shrink", action="store_true",
                        help="Test narrowing and shortening nodes containing media cards")
    parser.add_argument("--upload", type=Path, nargs="+",
                        help="Also test the upload button with these local files")
    parser.add_argument("--python", required=True, help="ComfyUI Python executable for offline node definitions")
    parser.add_argument("--comfyui-path", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    script = args.script or root / "web" / "media_loader.js"
    exporter = """
import json, os, sys
sys.path.insert(0, os.environ["COMFYUI_PATH"])
import media_loader as media
definitions = {}
for name, cls in media.NODE_CLASS_MAPPINGS.items():
    definitions[name] = {
        "input": cls.INPUT_TYPES(), "input_order": {"required": list(cls.INPUT_TYPES()["required"])},
        "output": list(cls.RETURN_TYPES), "output_name": list(cls.RETURN_NAMES),
        "output_is_list": [False] * len(cls.RETURN_TYPES),
        "name": name, "display_name": media.NODE_DISPLAY_NAME_MAPPINGS[name],
        "description": cls.DESCRIPTION, "category": cls.CATEGORY,
        "python_module": "custom_nodes.comfyui-ziyuan-API", "output_node": False,
    }
print(json.dumps(definitions))
"""
    environment = dict(os.environ, COMFYUI_PATH=args.comfyui_path)
    definitions = json.loads(subprocess.check_output(
        [args.python, "-c", exporter], cwd=root, env=environment, text=True, encoding="utf-8"))
    extension_path = "/extensions/comfyui-ziyuan-API/media_loader.js"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1100})
        uploads = []

        def intercept(route):
            if (args.upload and route.request.method == "POST"
                    and route.request.url in {
                        args.url.rstrip("/") + "/upload/image",
                        args.url.rstrip("/") + "/api/upload/image",
                    }):
                route.continue_()
            elif route.request.method not in ("GET", "HEAD"):
                route.abort()
            elif route.request.url.split("?")[0].endswith(extension_path):
                route.fulfill(path=str(script.resolve()), content_type="text/javascript")
            elif route.request.url.split("?")[0].endswith("/extensions"):
                response = route.fetch()
                extensions = response.json()
                if extension_path not in extensions:
                    extensions.append(extension_path)
                route.fulfill(response=response, json=extensions)
            elif route.request.url.split("?")[0].endswith("/object_info"):
                response = route.fetch()
                info = response.json()
                info.update(definitions)
                route.fulfill(response=response, json=info)
            else:
                route.continue_()

        context.route("**/*", intercept)
        page = context.new_page()
        page.on("dialog", lambda dialog: (print("dialog:", dialog.message, flush=True), dialog.dismiss()))
        page.on("requestfailed", lambda request: print("failed:", request.url, request.failure, flush=True)
                if request.url.endswith("/upload/image") else None)
        page.on("response", lambda response: uploads.append(response)
                if response.request.method == "POST"
                and response.url.endswith("/upload/image") else None)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_function("!!window.LiteGraph?.registered_node_types?.ZiyuanMediaLoader")
        page.wait_for_function("!!window.comfyAPI?.app?.app?.canvas?.graph")
        page.wait_for_timeout(5000)
        page.evaluate("""() => {
            const app = window.comfyAPI.app.app;
            app.graph.clear();
            const node = LiteGraph.createNode('ZiyuanMediaLoader');
            node.pos = [100, 140];
            app.graph.add(node);
            app.canvas.ds.scale = 1;
            app.canvas.ds.offset = [0, 0];
            app.canvas.setDirty(true, true);
            window.resizeTestNode = node;
        }""")
        page.locator(".zy-ml-panel").wait_for(state="visible")
        page.evaluate("window.resizeTestNode.setSize([420, 420])")
        page.wait_for_timeout(300)
        corner = page.evaluate("""() => {
            const app = window.comfyAPI.app.app;
            const n = window.resizeTestNode;
            const canvas = app.canvas.canvas.getBoundingClientRect();
            return [canvas.x + n.pos[0] + n.size[0] - 3,
                    canvas.y + n.pos[1] + n.size[1] - 3];
        }""")
        page.mouse.move(*corner)
        page.mouse.down()
        page.mouse.move(corner[0] + 380, corner[1] + 280, steps=20)
        page.mouse.up()
        page.wait_for_timeout(300)
        dragged = page.evaluate("""() => ({size:[...window.resizeTestNode.size],
            width:window.resizeTestNode.__zyMlWidget.width,
            panel:window.resizeTestNode.__zyMlPanel.getBoundingClientRect().toJSON()})""")
        print("drag:", dragged)
        if args.check:
            assert dragged["size"] == [800, 700], dragged
            assert abs(dragged["panel"]["width"] - 780) < 2, dragged
        # A retained explicit widget width overrides the node width in ComfyUI.
        # Model an existing node with a stale allocation before resizing it.
        page.evaluate("window.resizeTestNode.__zyMlWidget.width = 320")
        results = []
        for width, height, zoom in [(420, 420, 1), (800, 700, 1), (520, 480, 1), (800, 700, 0.7), (800, 700, 1.3)]:
            page.evaluate("""([width, height, zoom]) => {
                const app = window.comfyAPI.app.app;
                window.resizeTestNode.setSize([width, height]);
                app.canvas.ds.scale = zoom;
                app.canvas.setDirty(true, true);
            }""", [width, height, zoom])
            # Wait for the plugin's measurement and the frontend's layout/draw passes.
            page.evaluate("""() => new Promise(resolve => {
                let frames = 12;
                const tick = () => --frames ? requestAnimationFrame(tick) : resolve();
                requestAnimationFrame(tick);
            })""")
            result = page.evaluate("""() => {
                const node = window.resizeTestNode;
                const widget = node.__zyMlWidget;
                const panel = node.__zyMlPanel;
                const bounds = panel.getBoundingClientRect();
                const parent = panel.parentElement.getBoundingClientRect();
                const zoom = window.comfyAPI.app.app.canvas.ds.scale;
                return {
                    node: [...node.size], zoom,
                    widget: {width: widget.width, height: widget.computedHeight, y: widget.y},
                    panel: [bounds.width / zoom, bounds.height / zoom],
                    wrapper: [parent.width / zoom, parent.height / zoom],
                    emptySlots: panel.querySelectorAll('.zy-ml-empty').length,
                };
            }""")
            results.append(result)
            print(json.dumps(result, ensure_ascii=False))
            if args.check:
                margin = page.evaluate("window.resizeTestNode.__zyMlWidget.margin ?? 10")
                assert abs(result["panel"][0] - (result["node"][0] - margin * 2)) < 2, result
                assert abs(result["panel"][1] - result["wrapper"][1]) < 2, result
                assert result["emptySlots"] == 3, result
        if args.check:
            assert results[1]["panel"][1] > results[0]["panel"][1] + 100, results
            assert results[2]["panel"][1] < results[1]["panel"][1] - 100, results
            assert abs(results[3]["panel"][1] - results[4]["panel"][1]) < 2, results
        if args.shrink:
            page.set_viewport_size({"width": 1600, "height": 1600})
            # Only local widget state changes; no upload or generation requests.
            page.evaluate("""() => {
                const node = window.resizeTestNode;
                const files = (count, ext) => Array.from({length:count},
                    (_, i) => ({filename:`__zy_resize_test_${i}.${ext}`}));
                node.__zyMlWidget.value = JSON.stringify({
                    images:files(9, 'png'), audios:files(3, 'wav'), videos:files(3, 'mp4')
                });
            }""")
            for width, height, zoom in [(800, 800, 1), (320, 320, 1), (800, 320, 1),
                                        (320, 320, 0.7), (800, 320, 1.3), (320, 320, 1)]:
                page.evaluate("""([width, height, zoom]) => {
                    window.comfyAPI.app.app.canvas.ds.scale = zoom;
                    window.resizeTestNode.setSize([width, height]);
                }""", [width, height, zoom])
                page.wait_for_timeout(500)
                shrunk = page.evaluate("""() => {
                    const n = window.resizeTestNode;
                    const panel = n.__zyMlPanel;
                    const bounds = panel.getBoundingClientRect();
                    const zoom = window.comfyAPI.app.app.canvas.ds.scale;
                    const cards = [...panel.querySelectorAll('.zy-ml-card')];
                    const contentBottom = Math.max(...cards.map(c => c.getBoundingClientRect().bottom));
                    return {requested:[], node:[...n.size], zoom,
                        panel:[bounds.width / zoom, bounds.height / zoom],
                        minHeight:n.__zyMlMinWidgetHeight,
                        overflow:(contentBottom - bounds.bottom) / zoom,
                        wrapperHeight:panel.parentElement.getBoundingClientRect().height / zoom,
                        cards:cards.length};
                }""")
                shrunk["requested"] = [width, height]
                print("shrink:", json.dumps(shrunk), flush=True)
                if args.check:
                    assert shrunk["cards"] == 15, shrunk
                    assert shrunk["overflow"] <= 1, shrunk
                    assert abs(shrunk["panel"][0] - (shrunk["node"][0] - 20)) < 2, shrunk
                    assert abs(shrunk["panel"][1] - shrunk["wrapperHeight"]) < 2, shrunk
                    # A narrower previous layout must not pin the new height.
                    assert shrunk["node"][1] <= max(height, shrunk["minHeight"] + 54) + 1, shrunk
            for width in (800, 320, 800):
                points = page.evaluate("""width => {
                    const n = window.resizeTestNode;
                    const rect = window.comfyAPI.app.app.canvas.canvas.getBoundingClientRect();
                    return [rect.x+n.pos[0]+n.size[0]-3, rect.y+n.pos[1]+n.size[1]-3,
                            rect.x+n.pos[0]+width-3, rect.y+n.pos[1]+320-3];
                }""", width)
                page.mouse.move(points[0], points[1])
                page.mouse.down()
                page.mouse.move(points[2], points[3], steps=20)
                page.mouse.up()
                page.wait_for_timeout(300)
                dragged = page.evaluate("""() => ({node:[...window.resizeTestNode.size],
                    minHeight:window.resizeTestNode.__zyMlMinWidgetHeight})""")
                print("shrink-drag:", json.dumps(dragged), flush=True)
                if args.check:
                    assert abs(dragged["node"][0] - width) < 2, dragged
                    assert dragged["node"][1] <= dragged["minHeight"] + 55, dragged
        # Verify real LiteGraph socket compatibility without executing a graph.
        connected = page.evaluate("""() => {
            const app = window.comfyAPI.app.app;
            const splitter = LiteGraph.createNode('ZiyuanMediaSplitter');
            const generation = LiteGraph.createNode('ZiyuanUnifiedVideoNode');
            splitter.pos = [1000, 140];
            generation.pos = [1600, 140];
            app.graph.add(splitter);
            app.graph.add(generation);
            for (const widget of splitter.widgets) {
                if (['image_count', 'video_count', 'audio_count'].includes(widget.name)) {
                    widget.value = 1;
                    widget.callback?.(1);
                }
            }
            const loader = window.resizeTestNode;
            loader.connect(0, splitter, 0);
            const connections = [[0, '参考图1'], [9, '参考视频1'], [12, '参考音频1']];
            return connections.map(([slot, name]) => {
                const input = generation.inputs.findIndex(input => input.name === name);
                const link = splitter.connect(slot, generation, input);
                return {name, type:splitter.outputs[slot].type, connected:!!link};
            });
        }""")
        assert all(item["connected"] for item in connected), connected
        print("connections:", json.dumps(connected, ensure_ascii=False), flush=True)
        if args.upload:
            # Keep uploads independent of full-capacity shrink fixtures.
            page.evaluate("""() => {
                window.resizeTestNode.__zyMlWidget.value =
                    JSON.stringify({images:[], audios:[], videos:[]});
                window.comfyAPI.app.app.canvas.ds.scale = 1;
                window.resizeTestNode.setSize([800, 580]);
            }""")
            with page.expect_file_chooser() as chooser:
                page.locator(".zy-ml-upload").filter(has_text="上传").click()
            chooser.value.set_files([str(path.resolve(strict=True)) for path in args.upload])
            page.wait_for_function("""() => {
                const node = window.resizeTestNode;
                return !node.__zyMlUploadQueue;
            }""", timeout=60000)
            for response in uploads:
                assert response.ok, (response.status, response.text())
            assert len(uploads) == len(args.upload), len(uploads)
            uploaded = [response.json() for response in uploads]
            print("uploaded:", json.dumps(uploaded, ensure_ascii=False))
            page.wait_for_function("""() => [...document.querySelectorAll('.zy-ml-thumb img')]
                .every(img => img.complete && img.naturalWidth > 0)""")
            state = page.evaluate("""() => {
                const n = window.resizeTestNode;
                return {
                    count:n.__zyMlCount.textContent,
                    files:[...n.__zyMlPanel.querySelectorAll('.zy-ml-card')].map(c => c.dataset.filename),
                    media:JSON.parse(n.widgets.find(w => w.name === 'media_state').value),
                    saved:JSON.parse(n.properties.media_loader_state),
                };
            }""")
            assert len(state["files"]) == len(args.upload), state
            assert state["media"] == state["saved"], state
            assert sorted(state["files"]) == sorted(
                (item["subfolder"] + "/" if item["subfolder"] else "") + item["name"]
                for item in uploaded), state
            print("displayed:", json.dumps(state, ensure_ascii=False))
            # Restore a serialized node as when reopening a saved workflow.
            page.evaluate("""() => {
                const app = window.comfyAPI.app.app;
                const saved = window.resizeTestNode.serialize();
                app.graph.remove(window.resizeTestNode);
                const node = LiteGraph.createNode('ZiyuanMediaLoader');
                node.configure(saved);
                app.graph.add(node);
                window.resizeTestNode = node;
            }""")
            page.wait_for_function("""count =>
                window.resizeTestNode.__zyMlPanel.querySelectorAll('.zy-ml-card').length === count
            """, arg=len(args.upload))
            restored = page.evaluate("""() => [...window.resizeTestNode.__zyMlPanel
                .querySelectorAll('.zy-ml-card')].map(c => c.dataset.filename)""")
            assert sorted(restored) == sorted(state["files"]), restored
            print("restored:", json.dumps(restored, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
