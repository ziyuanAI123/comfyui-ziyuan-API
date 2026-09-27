import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(new URL("../web/unified_video.js", import.meta.url), "utf8")
    .replace('import { app } from "../../scripts/app.js";', "const app = { registerExtension(extension) { this.extension = extension; } };");
const { app } = await import("data:text/javascript;base64," + Buffer.from(source + "\nexport { app };").toString("base64"));
const profiles = {
    omni: { ratios: ["自动", "16:9", "9:16", "1:1", "4:3"], resolutions: ["720p", "1080p"], duration: { min: 1, max: 60, default: 5 }, images: 14, audio_limit: 0, videos: 0, sound: false },
    full: { ratios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"], resolutions: ["720p", "480p", "1080p", "4k"], duration: { min: 4, max: 15, default: 4 }, images: 9, audio_limit: 3, videos: 3, sound: true, qiaomo: true },
    mini: { ratios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "自动"], resolutions: ["720p"], duration: { min: 4, max: 15, default: 4 }, images: 9, audio_limit: 3, videos: 3, sound: true, qiaomo: true },
    newer: { ratios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"], resolutions: ["720p", "480p", "1080p"], duration: { min: 4, max: 15, default: 4 }, images: 12, audio_limit: 10, videos: 12, sound: true, qiaomo: true },
};

function createNode(nodeName) {
    const imageNode = nodeName.startsWith("ZiyuanImage");
    const optional = Object.fromEntries(Array.from({ length: imageNode ? 14 : 30 }, (_, i) => [`参考图${i + 1}`, ["IMAGE"]]));
    if (!imageNode) {
        for (let i = 1; i <= 10; i++) optional[`参考音频${i}`] = ["AUDIO"];
        for (let i = 1; i <= 12; i++) optional[`参考视频${i}`] = ["VIDEO"];
    }
    class Node {
        constructor() {
            this.size = [420, 720];
            this.inputs = Object.entries(optional).map(([name, [type]]) => ({ name, type, link: null }));
            this.graph = { links: {}, getLink(id) { return this.links[id]; } };
            this.widgets = [["模型", "omni"], ["比例", "4:3"], ["分辨率", "720p"], ["时长秒数", 30], ["生成声音", false]]
                .map(([name, value]) => ({ name, value, type: name === "生成声音" ? "toggle" : "combo", options: {} }));
            if (!imageNode) this.widgets.push(...["Mini素材模式", "Mini图片链接", "Mini视频链接", "Mini音频链接"]
                .map((name) => ({ name, value: name === "Mini素材模式" ? "组合参考" : "", type: "text",
                                 options: {}, inputEl: { style: {} } })));
        }
        addInput(name, type) { this.inputs.push({ name, type, link: null }); }
        removeInput(i) {
            assert.equal(this.inputs[i].link, null, "must never remove a connected input");
            this.inputs.splice(i, 1);
            this.inputs.forEach((input, index) => {
                if (input.link != null) this.graph.links[input.link].target_slot = index;
            });
        }
        setSize(size) { this.size = size; }
        computeSize() { return [420, 600]; }
        setDirtyCanvas() {}
    }
    app.extension.beforeRegisterNodeDef(Node, { name: nodeName, input: {
        required: { 模型: [Object.keys(profiles), { ziyuan_profiles: profiles }] }, optional,
    } });
    const node = new Node();
    node.onNodeCreated();
    return node;
}
const widget = (node, name) => node.widgets.find((w) => w.name === name);
const names = (node) => node.inputs.map((input) => input.name);
async function select(node, model) {
    widget(node, "模型").value = model;
    widget(node, "模型").callback(model);
    await Promise.resolve();
}
async function connect(node, name, id) {
    const slot = node.inputs.findIndex((i) => i.name === name);
    assert.notEqual(slot, -1);
    node.inputs[slot].link = id;
    node.graph.links[id] = { id, origin_id: id + 100, origin_slot: 0, target_slot: slot };
    node.onConnectionsChange();
    await Promise.resolve();
    checkLinks(node);
}
async function disconnect(node, name) {
    const input = node.inputs.find((i) => i.name === name);
    delete node.graph.links[input.link];
    input.link = null;
    node.onConnectionsChange();
    await Promise.resolve();
    checkLinks(node);
}
function checkLinks(node) {
    assert.equal(new Set(names(node)).size, node.inputs.length, "names remain unique");
    node.inputs.forEach((input, index) => {
        if (input.link == null) return;
        assert.equal(node.graph.links[input.link].target_slot, index);
        assert.equal(node.graph.links[input.link].origin_id, input.link + 100);
    });
}

for (const type of ["ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"]) {
    test(`${type}: all model controls, native inputs and frame mode`, async () => {
        const node = createNode(type);
        await Promise.resolve();
        assert.deepEqual(names(node), ["参考图1"]);
        assert.equal(widget(node, "生成声音").hidden, true);
        for (const model of ["full", "mini", "newer"]) {
            await select(node, model);
            assert.deepEqual(names(node), ["参考图1", "参考音频1", "参考视频1"]);
            assert.deepEqual(widget(node, "分辨率").options.values, profiles[model].resolutions);
            assert.deepEqual(widget(node, "比例").options.values, profiles[model].ratios);
            assert.deepEqual(widget(node, "时长秒数").options.values, Array.from({length: 12}, (_, i) => i + 4));
            assert.equal(widget(node, "时长秒数").value, 4);
            assert.equal(widget(node, "生成声音").hidden, false);
            assert.equal(widget(node, "Mini素材模式").hidden, false);
            assert.equal(widget(node, "Mini音频链接").hidden, true);
            assert.equal(widget(node, "Mini图片链接").hidden, true);
            assert.equal(widget(node, "Mini视频链接").hidden, true);
        }
        await select(node, "full");
        widget(node, "分辨率").value = "4k";
        await select(node, "mini");
        assert.equal(widget(node, "分辨率").value, "720p");
        widget(node, "比例").value = "自动";
        await select(node, "newer");
        assert.equal(widget(node, "比例").value, "16:9");
        widget(node, "Mini素材模式").value = "首尾帧";
        widget(node, "Mini素材模式").callback("首尾帧");
        await Promise.resolve();
        assert.deepEqual(names(node), ["参考图1"]);
        await connect(node, "参考图1", 1);
        await connect(node, "参考图2", 2);
        assert.deepEqual(names(node), ["参考图1", "参考图2"]);
        node.onConfigure();
        await Promise.resolve();
        checkLinks(node);
    });
}

for (const type of ["ZiyuanImageNode", "ZiyuanImageSubmitNode", "ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"]) {
    test(`${type}: deleting a middle image compacts links and numbering`, async () => {
        const node = createNode(type);
        await Promise.resolve();
        for (let i = 1; i <= 3; i++) await connect(node, `参考图${i}`, i);
        assert.deepEqual(names(node), ["参考图1", "参考图2", "参考图3", "参考图4"]);
        await disconnect(node, "参考图2");
        assert.deepEqual(node.inputs.map((i) => [i.name, i.link]), [["参考图1", 1], ["参考图2", 3], ["参考图3", null]]);
        assert.equal(node.inputs[1].label, "参考图2");
        await disconnect(node, "参考图1");
        assert.deepEqual(node.inputs.map((i) => [i.name, i.link]), [["参考图1", 3], ["参考图2", null]]);
        await disconnect(node, "参考图1");
        assert.deepEqual(names(node), ["参考图1"]);
    });
}

test("image and video compact independently without changing sources", async () => {
    const node = createNode("ZiyuanUnifiedVideoNode");
    await select(node, "mini");
    for (let i = 1; i <= 3; i++) {
        await connect(node, `参考图${i}`, i);
        await connect(node, `参考视频${i}`, i + 10);
    }
    assert.deepEqual(names(node), ["参考图1", "参考图2", "参考图3", "参考图4", "参考音频1", "参考视频1", "参考视频2", "参考视频3"]);
    await disconnect(node, "参考图2");
    await disconnect(node, "参考视频2");
    assert.deepEqual(node.inputs.map((i) => [i.name, i.link]), [
        ["参考图1", 1], ["参考图2", 3], ["参考图3", null],
        ["参考音频1", null], ["参考视频1", 11], ["参考视频2", 13], ["参考视频3", null],
    ]);
    // Simulate workflow serialization/restoration, including graph link endpoints.
    const saved = JSON.parse(JSON.stringify({ inputs: node.inputs, links: node.graph.links }));
    const restored = createNode("ZiyuanUnifiedVideoNode");
    widget(restored, "模型").value = "mini";
    restored.inputs = saved.inputs;
    restored.graph.links = saved.links;
    restored.onConfigure();
    await Promise.resolve();
    assert.deepEqual(restored.inputs, node.inputs);
    checkLinks(restored);
});

test("model changes retain and mark excess connections, then compact after disconnect", async () => {
    const node = createNode("ZiyuanUnifiedVideoNode");
    await select(node, "newer");
    for (let i = 1; i <= 10; i++) await connect(node, `参考图${i}`, i);
    await select(node, "mini");
    assert.equal(node.inputs.filter((i) => i.type === "IMAGE").length, 10);
    assert.match(node.inputs.find((i) => i.name === "参考图10").label, /请断开/);
    await connect(node, "参考视频1", 40);
    await disconnect(node, "参考图2");
    assert.equal(node.inputs.find((i) => i.name === "参考图9").link, 10);
    assert.equal(node.inputs.find((i) => i.name === "参考图9").label, "参考图9");
    assert.equal(node.inputs.some((i) => i.name === "参考图10"), false);
    await select(node, "omni");
    assert.match(node.inputs.find((i) => i.name === "参考视频1").label, /请断开/);
    await disconnect(node, "参考视频1");
    assert.equal(node.inputs.some((i) => i.type === "VIDEO"), false);
    checkLinks(node);
});

for (const [type, model, limit] of [["ZiyuanImageNode", "omni", 14], ["ZiyuanUnifiedVideoNode", "omni", 14],
    ["ZiyuanUnifiedVideoNode", "newer", 12], ["ZiyuanUnifiedVideoNode", "mini", 9]]) {
    test(`${type} ${model}: stop growing at ${limit} images`, async () => {
        const node = createNode(type);
        await select(node, model);
        for (let i = 1; i <= limit; i++) await connect(node, `参考图${i}`, i);
        assert.equal(node.inputs.filter((i) => i.type === "IMAGE").length, limit);
        await disconnect(node, "参考图1");
        const images = node.inputs.filter((i) => i.type === "IMAGE");
        assert.equal(images.length, limit);
        assert.equal(images.at(-1).link, null);
        assert.equal(images[0].link, 2);
    });
}

test("restore sparse older workflow and preserve unrelated widget input links", async () => {
    const node = createNode("ZiyuanUnifiedVideoNode");
    // onConfigure runs before the queued initial update, as in graph loading.
    node.inputs[4].link = 5;
    node.inputs[11].link = 12;
    node.inputs.push({ name: "提示词", type: "STRING", link: 50 });
    for (const [index, input] of node.inputs.entries()) {
        if (input.link != null) node.graph.links[input.link] = { id: input.link, origin_id: input.link + 100, origin_slot: 0, target_slot: index };
    }
    node.onConfigure();
    await Promise.resolve();
    assert.deepEqual(node.inputs.map((i) => [i.name, i.link]), [["参考图1", 5], ["参考图2", 12], ["参考图3", null], ["提示词", 50]]);
    checkLinks(node);
});

for (const type of ["ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"]) {
    test(`${type}: saved URL values remain visible and connected frames are retained`, async () => {
        const node = createNode(type);
        await select(node, "mini");
        widget(node, "Mini图片链接").value = "https://assets.test/a.png";
        widget(node, "Mini音频链接").value = "asset://audio";
        node.onConfigure();
        await Promise.resolve();
        assert.equal(widget(node, "Mini图片链接").hidden, false);
        await select(node, "full");
        assert.equal(widget(node, "Mini音频链接").hidden, false, "allow clearing an incompatible saved audio URL");
        await connect(node, "参考图1", 1);
        await connect(node, "参考图2", 2);
        await connect(node, "参考图3", 3);
        await connect(node, "参考视频1", 4);
        widget(node, "Mini素材模式").value = "首尾帧";
        widget(node, "Mini素材模式").callback("首尾帧");
        await Promise.resolve();
        assert.match(node.inputs.find(i => i.name === "参考图3").label, /请断开/);
        assert.match(node.inputs.find(i => i.name === "参考视频1").label, /请断开/);
        await select(node, "omni");
        assert.equal(widget(node, "Mini图片链接").hidden, true);
        assert.equal(widget(node, "Mini图片链接").value, "https://assets.test/a.png");
        checkLinks(node);
    });
}

test("2.5 video inputs grow to 12; Mini retains excess links until disconnected", async () => {
    const node = createNode("ZiyuanUnifiedVideoNode");
    await select(node, "newer");
    for (let i = 1; i <= 12; i++) await connect(node, `参考视频${i}`, i);
    assert.equal(node.inputs.filter(i => i.type === "VIDEO").length, 12);
    await select(node, "mini");
    assert.match(node.inputs.find(i => i.name === "参考视频12").label, /请断开/);
    await select(node, "newer");
    await disconnect(node, "参考视频5");
    assert.equal(node.inputs.find(i => i.name === "参考视频5").link, 6);
    assert.equal(node.inputs.find(i => i.name === "参考视频12").link, null);
    checkLinks(node);
});

for (const nodeType of ["ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"]) {
    test(`${nodeType}: restore legacy Mini 480p as 720p and preserve other model resolutions`, async () => {
        const node = createNode(nodeType);
        widget(node, "模型").value = "mini";
        widget(node, "分辨率").value = "480p";
        node.onConfigure();
        await Promise.resolve();
        assert.equal(widget(node, "分辨率").value, "720p");
        assert.deepEqual(widget(node, "分辨率").options.values, ["720p"]);
        for (const model of ["full", "newer"]) {
            await select(node, model);
            widget(node, "分辨率").value = "480p";
            node.onConfigure();
            await Promise.resolve();
            assert.equal(widget(node, "分辨率").value, "480p");
            await select(node, "mini");
            assert.equal(widget(node, "分辨率").value, "720p");
        }
    });
    test(`${nodeType}: audio expands, compacts and survives mode/model changes`, async () => {
        const node = createNode(nodeType);
        await select(node, "newer");
        for (let i = 1; i <= 10; i++) await connect(node, `参考音频${i}`, i);
        assert.equal(node.inputs.filter(i => i.type === "AUDIO").length, 10);
        await disconnect(node, "参考音频2");
        assert.equal(node.inputs.find(i => i.name === "参考音频2").link, 3);
        assert.equal(node.inputs.find(i => i.name === "参考音频10").link, null);
        await select(node, "full");
        assert.match(node.inputs.find(i => i.name === "参考音频4").label, /请断开/);
        widget(node, "Mini素材模式").value = "首尾帧";
        widget(node, "Mini素材模式").callback("首尾帧");
        await Promise.resolve();
        assert.match(node.inputs.find(i => i.name === "参考音频1").label, /请断开/);
        const saved = JSON.parse(JSON.stringify({inputs: node.inputs, links: node.graph.links}));
        node.inputs = saved.inputs;
        node.graph.links = saved.links;
        node.onConfigure();
        await Promise.resolve();
        checkLinks(node);
        await select(node, "omni");
        while (node.inputs.some(i => i.type === "AUDIO")) await disconnect(node, "参考音频1");
        await select(node, "mini");
        assert.equal(node.inputs.some(i => i.type === "AUDIO"), false);
        widget(node, "Mini素材模式").value = "组合参考";
        widget(node, "Mini素材模式").callback("组合参考");
        await Promise.resolve();
        for (let i = 1; i <= 3; i++) await connect(node, `参考音频${i}`, i);
        assert.equal(node.inputs.filter(i => i.type === "AUDIO").length, 3);
    });
}
