import { app } from "../../scripts/app.js";

function compactReferences(node, prefix, type, limit) {
    const pattern = new RegExp(`^${prefix}\\d+$`);
    const slots = node.inputs.filter((input) => pattern.test(input.name));
    const connected = slots.filter((input) => input.link != null);
    const spare = connected.length < limit ? slots.find((input) => input.link == null) : null;
    for (let i = node.inputs.length - 1; i >= 0; i--) {
        const input = node.inputs[i];
        if (pattern.test(input.name) && input.link == null && input !== spare) node.removeInput(i);
    }
    connected.forEach((input, index) => {
        input.name = `${prefix}${index + 1}`;
        input.label = index < limit ? input.name : `${input.name}（请断开，当前模型不支持）`;
    });
    if (connected.length < limit) {
        const name = `${prefix}${connected.length + 1}`;
        if (spare) {
            spare.name = name;
            spare.label = name;
        } else node.addInput(name, type);
    }
}

function updateReferences(node, images, audio, videos = 0) {
    compactReferences(node, "参考图", "IMAGE", images);
    compactReferences(node, "参考音频", "AUDIO", audio);
    compactReferences(node, "参考视频", "VIDEO", videos);
    // Keep each family's connected slots in order, followed by its single spare.
    const rank = (input) => /^参考图\d+$/.test(input.name) ? 0 : /^参考音频\d+$/.test(input.name) ? 1 : /^参考视频\d+$/.test(input.name) ? 2 : 3;
    node.inputs.sort((a, b) => rank(a) - rank(b) || (rank(a) < 3 ? Number(a.link == null) - Number(b.link == null) : 0));
    // Slot movement must also update the graph links used by serialization/execution.
    node.inputs.forEach((input, index) => {
        if (input.link == null) return;
        const link = node.graph?.getLink ? node.graph.getLink(input.link) : node.graph?.links?.[input.link];
        if (link) link.target_slot = index;
    });
}

app.registerExtension({
    name: "ziyuanAI.UnifiedVideo",
    beforeRegisterNodeDef(nodeType, nodeData) {
        const imageNode = ["ZiyuanImageNode", "ZiyuanImageSubmitNode"].includes(nodeData.name);
        if (!imageNode && !["ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"].includes(nodeData.name)) return;
        const profiles = nodeData.input.required["模型"][1]?.ziyuan_profiles;
        const imageLimit = Object.keys(nodeData.input.optional).filter((name) => /^参考图\d+$/.test(name)).length;

        function update(node) {
            if (imageNode) {
                updateReferences(node, imageLimit, 0);
                node.setSize([node.size[0], node.computeSize()[1]]);
                node.setDirtyCanvas(true, true);
                return;
            }
            const widget = (name) => node.widgets.find((item) => item.name === name);
            const profile = profiles[widget("模型").value];
            if (!profile) return;
            for (const [name, values] of [["比例", profile.ratios], ["分辨率", profile.resolutions]]) {
                const item = widget(name);
                item.options.values = [...values];
                if (!values.includes(item.value)) item.value = values.includes("16:9") ? "16:9" : values[0];
            }
            const duration = widget("时长秒数");
            duration.options.values = profile.seconds ? [...profile.seconds] :
                Array.from({ length: profile.duration.max - profile.duration.min + 1 }, (_, i) => i + profile.duration.min);
            if (!duration.options.values.includes(duration.value)) duration.value = profile.duration.default;
            const sound = widget("生成声音");
            if (!sound.ziyuanOriginal) sound.ziyuanOriginal = { type: sound.type, computeSize: sound.computeSize };
            sound.type = profile.sound ? sound.ziyuanOriginal.type : "ziyuan_hidden";
            sound.computeSize = profile.sound ? sound.ziyuanOriginal.computeSize : () => [0, -4];
            sound.hidden = !profile.sound;

            for (const name of ["Mini素材模式", "Mini图片链接", "Mini视频链接", "Mini音频链接"]) {
                const item = widget(name);
                if (!item) continue;
                const visible = profile.qiaomo && (name === "Mini素材模式" || Boolean(item.value?.trim()));
                item.label = name.replace("Mini", "");
                if (!item.ziyuanOriginal) item.ziyuanOriginal = { type: item.type, computeSize: item.computeSize };
                item.type = visible ? item.ziyuanOriginal.type : "ziyuan_hidden";
                item.computeSize = visible ? item.ziyuanOriginal.computeSize : () => [0, -4];
                item.hidden = !visible;
                if (item.inputEl) item.inputEl.style.display = visible ? "" : "none";
            }

            const frames = profile.qiaomo && widget("Mini素材模式")?.value === "首尾帧";
            updateReferences(node, frames ? 2 : profile.images, frames ? 0 : profile.audio_limit, frames ? 0 : profile.videos);
            node.setSize([node.size[0], node.computeSize()[1]]);
            node.setDirtyCanvas(true, true);
        }

        const pending = new WeakSet();
        function schedule(node) {
            if (pending.has(node)) return;
            pending.add(node);
            // Wait until connect/disconnect or graph restoration has finished.
            queueMicrotask(() => {
                try { update(node); }
                finally { pending.delete(node); }
            });
        }

        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const result = created?.apply(this, arguments);
            for (const name of ["模型", "Mini素材模式"]) {
                const item = this.widgets.find((item) => item.name === name);
                if (!item) continue;
                const changed = item.callback;
                item.callback = (...args) => {
                    changed?.apply(item, args);
                    schedule(this);
                };
            }
            // Let workflow configuration restore values and input links first.
            schedule(this);
            return result;
        };
        const configured = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const result = configured?.apply(this, arguments);
            schedule(this);
            return result;
        };
        const connections = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const result = connections?.apply(this, arguments);
            schedule(this);
            return result;
        };
    },
});
