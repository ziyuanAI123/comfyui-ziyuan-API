import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const cj = process.env.MEDIA_LOADER_VARIANT === "CJ";
const loaderClass = cj ? "CJ_MediaLoader" : "ZiyuanMediaLoader";
const splitterClass = cj ? "CJ_MediaSplitter" : "ZiyuanMediaSplitter";
const original = readFileSync(new URL(cj
  ? "../../ComfyUI-Chajian-Toolbox/js/cj_media_loader.js"
  : "../web/media_loader.js", import.meta.url), "utf8");
const source = original
  .replace('import { app } from "../../scripts/app.js";',
    'const app = { registerExtension(extension) { this.extension = extension; } };')
  .replace('import { api } from "../../scripts/api.js";', 'const api = {};');
const { app, readState, writeState, typeForFile } = await import(
  "data:text/javascript;base64," + Buffer.from(source +
    "\nexport { app, readState, writeState, typeForFile };\n").toString("base64"));

test("extension keeps its independent namespace", () => {
  assert.equal(app.extension.name, cj
    ? "ComfyUI-Chajian-Toolbox.MediaLoader" : "comfyui-ziyuan-API.MediaLoader");
  if (cj) return;
  assert.doesNotMatch(original, /CJ_|__cj|cj-ml|cjMl|DtMedia/);
  assert.match(original, /section.dataset.zyMlSection = group.type/);
  assert.match(original, /data-zy-ml-section/);
});

test("state supports saved filenames, caps and same-type ordering", () => {
  const node = { widgets: [{ name: "media_state", value: "" }] };
  writeState(node, { images: ["b.png", "a.png", "b.png"],
    audios: ["a.wav"], videos: ["v.mp4"] });
  assert.deepEqual(readState(node), {
    images: ["b.png", "a.png"], audios: ["a.wav"], videos: ["v.mp4"],
  });
  assert.equal(node.properties.media_loader_state, node.widgets[0].value);
  node.widgets[0].value = JSON.stringify({ images: Array.from({ length: 12 }, (_, i) => `${i}.png`) });
  assert.equal(readState(node).images.length, 9);
  node.widgets[0].value = "invalid-json";
  assert.deepEqual(readState(node), { images: [], audios: [], videos: [] });
});

test("upload recognizes all three media types, including MIME-free files", () => {
  for (const [file, expected] of [
    [{ name: "图片.png", type: "image/png" }, "image"],
    [{ name: "声音.MP3", type: "" }, "audio"],
    [{ name: "视频.MP4", type: "" }, "video"],
    [{ name: "file.bin", type: "application/octet-stream" }, ""],
  ]) assert.equal(typeForFile(file), expected);
});

function connectedSplitter() {
  class Node {
    constructor() {
      this.widgets = ["image_count", "video_count", "audio_count"]
        .map((name) => ({ name, value: name === "image_count" ? 1 : 0 }));
      this.outputs = [...Array(9).fill("IMAGE"), ...Array(3).fill("VIDEO"), ...Array(3).fill("AUDIO")]
        .map((type, i) => ({ type, links: i === 9 ? [42] : null }));
      this.type = splitterClass;
      this.inputs = [{ name: "media_bundle", link: null }];
    }
    computeSize() { return [320, 240]; }
    setSize(size) { this.size = size; }
    setDirtyCanvas() {}
  }
  app.extension.beforeRegisterNodeDef(Node, { name: splitterClass });
  const node = new Node();
  node.onNodeCreated();
  const loader = { type: loaderClass, widgets: [{ name: "media_state", value: "" }],
    outputs: [{ links: [10] }] };
  node.graph = loader.graph = {
    links: { 10: { origin_id: 1, target_id: 2 } },
    getNodeById: (id) => id === 1 ? loader : id === 2 ? node : null,
  };
  node.inputs[0].link = 10;
  node.onConnectionsChange();
  return { node, loader };
}

test("splitter follows asset additions and removals while preserving slots and links", () => {
  const { node, loader } = connectedSplitter();
  const outputs = [...node.outputs];
  const counts = () => node.widgets.map((widget) => widget.value);
  assert.deepEqual(counts(), [0, 0, 0]);
  writeState(loader, { images: ["a.png", "b.png", "c.png"],
    videos: ["a.mp4", "b.mp4"], audios: ["a.wav"] });
  assert.deepEqual(counts(), [3, 2, 1]);
  assert.equal(node.outputs.filter((o) => !o.hidden).length, 6);
  assert.equal(node.outputs[9].type, "VIDEO");
  assert.deepEqual(node.outputs[9].links, [42]);
  for (const widget of node.widgets) {
    assert.equal(widget.disabled, true);
    assert.equal(widget.options.read_only, true);
    widget.value = 0;
    widget.callback();
  }
  assert.deepEqual(counts(), [3, 2, 1]);
  writeState(loader, { images: ["b.png"], audios: ["a.wav"] });
  assert.deepEqual(counts(), [1, 0, 1]);
  writeState(loader, {});
  assert.deepEqual(counts(), [0, 0, 0]);
  assert.equal(node.outputs.filter((o) => !o.hidden).length, 0);
  assert.deepEqual(node.outputs[9].links, [42]);
  node.outputs.forEach((output, i) => assert.equal(output, outputs[i]));
});

test("saved manual counts are replaced on restore and reconnect", async () => {
  const { node, loader } = connectedSplitter();
  loader.widgets[0].value = JSON.stringify({ images: ["a.png", "b.png"],
    videos: ["a.mp4"], audios: ["a.wav", "b.wav"] });
  node.widgets.forEach((widget) => { widget.value = 0; });
  node.onConfigure({});
  node.onGraphConfigured();
  assert.deepEqual(node.widgets.map((widget) => widget.value), [2, 1, 2]);
  node.inputs[0].link = null;
  node.onConnectionsChange();
  await Promise.resolve();
  assert.deepEqual(node.widgets.map((widget) => widget.value), [0, 0, 0]);
  // Connection hooks can run just before LiteGraph sets the input link.
  node.onConnectionsChange();
  node.inputs[0].link = 10;
  await Promise.resolve();
  assert.deepEqual(node.widgets.map((widget) => widget.value), [2, 1, 2]);
});

test("CJ and existing generation node definitions are not patched", () => {
  for (const name of [cj ? "ZiyuanMediaLoader" : "CJ_MediaLoader",
    cj ? "ZiyuanMediaSplitter" : "CJ_MediaSplitter", "ZiyuanUnifiedVideoNode", "ZiyuanImageNode"]) {
    class Unrelated {}
    app.extension.beforeRegisterNodeDef(Unrelated, { name });
    assert.deepEqual(Object.getOwnPropertyNames(Unrelated.prototype), ["constructor"]);
  }
});
