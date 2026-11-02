import { app } from "../../scripts/app.js";

const referencePattern = /@(图片|图|视频|音频)(\d+)/g;
const promptWidget = (node) => node.widgets?.find((widget) => widget.name === "提示词");

function referenceItems(node) {
    return (node.inputs || []).filter((input) => input.link != null && /^参考(图|视频|音频)\d+$/.test(input.name))
        .map((input) => {
            const link = node.graph?.getLink?.(input.link) ?? node.graph?.links?.[input.link];
            const source = node.graph?.getNodeById?.(link?.origin_id);
            const media = source?.widgets?.find((widget) => ["image", "video", "audio"].includes(widget.name))?.value;
            return { token: "@" + input.name.slice(2), link: input.link,
                label: [source?.title, typeof media === "string" ? media : ""].filter(Boolean).join(" · "),
                disabled: input.label?.includes("请断开") };
        });
}

function setPrompt(node, value) {
    const widget = promptWidget(node);
    if (!widget) return;
    widget.value = value;
    const element = node.ziyuanPromptTextarea ?? widget.inputEl ?? widget.element;
    const textarea = element?.matches?.("textarea") ? element : element?.querySelector?.("textarea");
    if (textarea && textarea.value !== value) {
        textarea.value = value;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
    widget.callback?.(value);
    node.setDirtyCanvas(true, true);
}

function remapReferences(node, previous) {
    const items = referenceItems(node);
    const current = Object.fromEntries(items.map((item) => [item.token, item.link]));
    const widget = promptWidget(node);
    if (widget && previous) {
        const value = String(widget.value ?? "").replace(referencePattern, (token, kind, index) => {
            const canonical = "@" + (kind === "图片" ? "图" : kind) + Number(index);
            if (!(canonical in previous)) return token;
            return items.find((item) => item.link === previous[canonical])?.token ?? `@已断开(${kind}${index})`;
        });
        if (value !== widget.value) setPrompt(node, value);
    }
    node.ziyuanReferenceLinks = current;
}

function mentionQuery(text, cursor) {
    const match = /@([^\s@（）(),，。]*)$/.exec(text.slice(0, cursor));
    return match ? { start: cursor - match[0].length, query: match[1] } : null;
}

function insertMention(node, textarea, token) {
    const cursor = textarea.selectionStart;
    const match = mentionQuery(textarea.value, cursor);
    if (!match) return;
    const value = textarea.value.slice(0, match.start) + token + " " + textarea.value.slice(cursor);
    textarea.value = value;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    setPrompt(node, value);
    textarea.focus();
    textarea.setSelectionRange(match.start + token.length + 1, match.start + token.length + 1);
}

const promptBindings = new WeakMap();
function bindPrompt(node, textarea) {
    if (!textarea?.matches?.("textarea") || promptBindings.has(textarea)) return;
    let popup, choices = [], selected = 0;
    const close = () => { popup?.remove(); popup = null; };
    const scroll = (event) => { if (!popup?.contains(event.target)) close(); };
    function render(reset = true) {
        if (reset) selected = 0;
        const match = mentionQuery(textarea.value, textarea.selectionStart);
        close();
        if (!match || document.activeElement !== textarea) return;
        choices = referenceItems(node).filter((item) => !item.disabled &&
            (item.token.slice(1) + " " + item.label).toLowerCase().includes(match.query.replace(/^图片/, "图").toLowerCase()));
        selected = Math.min(selected, Math.max(choices.length - 1, 0));
        popup = document.createElement("div");
        popup.className = "ziyuan-reference-picker";
        popup.setAttribute("role", "listbox");
        popup.setAttribute("aria-label", "选择已连接的素材");
        const rect = textarea.getBoundingClientRect();
        const width = Math.min(Math.max(rect.width, 260), 520, window.innerWidth - 16);
        const below = window.innerHeight - rect.bottom;
        const height = Math.min(240, Math.max(below, rect.top) - 12);
        Object.assign(popup.style, { position: "fixed", zIndex: "100000", background: "#222", color: "#eee",
            border: "1px solid #666", borderRadius: "6px", padding: "4px", boxSizing: "border-box",
            font: "13px sans-serif", width: `${width}px`, maxHeight: `${Math.max(60, height)}px`, overflowY: "auto",
            left: `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`,
            ...(below >= 120 ? { top: `${rect.bottom + 4}px` } : { bottom: `${window.innerHeight - rect.top + 4}px` }) });
        if (!choices.length) {
            popup.textContent = referenceItems(node).some((item) => !item.disabled) ? "没有匹配的已连接素材" : "请先连接图片、视频或音频素材";
        }
        choices.forEach((item, index) => {
            const row = document.createElement("div");
            row.setAttribute("role", "option");
            row.setAttribute("aria-selected", String(index === selected));
            row.textContent = `${item.token}  ${item.label || "已连接素材"}`;
            Object.assign(row.style, { padding: "7px 8px", cursor: "pointer", overflowWrap: "anywhere",
                background: index === selected ? "#43556d" : "transparent", borderRadius: "3px" });
            row.addEventListener("pointerdown", (event) => {
                event.preventDefault(); event.stopPropagation();
                insertMention(node, textarea, item.token); close();
            });
            popup.append(row);
        });
        document.body.append(popup);
        popup.children[selected]?.scrollIntoView?.({ block: "nearest" });
    }
    const input = (event) => { if (!event.isComposing) render(); };
    const keydown = (event) => {
        if (!popup || event.isComposing) return;
        if (["ArrowDown", "ArrowUp", "Enter", "Tab", "Escape"].includes(event.key)) {
            if (!choices.length && event.key !== "Escape") return;
            event.preventDefault(); event.stopImmediatePropagation();
            if (event.key === "Escape") close();
            else if (event.key === "Enter" || event.key === "Tab") { insertMention(node, textarea, choices[selected].token); close(); }
            else { selected = (selected + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length; render(false); }
        }
    };
    textarea.addEventListener("input", input);
    textarea.addEventListener("click", input);
    textarea.addEventListener("compositionend", input);
    textarea.addEventListener("keydown", keydown, true);
    textarea.addEventListener("blur", close);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", scroll, true);
    const cleanup = () => {
        close();
        textarea.removeEventListener("input", input);
        textarea.removeEventListener("click", input);
        textarea.removeEventListener("compositionend", input);
        textarea.removeEventListener("keydown", keydown, true);
        textarea.removeEventListener("blur", close);
        window.removeEventListener("resize", close);
        window.removeEventListener("scroll", scroll, true);
        if (node.ziyuanPromptTextarea === textarea) delete node.ziyuanPromptTextarea;
        promptBindings.delete(textarea);
    };
    node.ziyuanPromptCleanup?.();
    node.ziyuanPromptTextarea = textarea;
    node.ziyuanPromptCleanup = cleanup;
    promptBindings.set(textarea, { refresh: () => { if (popup) render(); } });
}

function attachPrompt(node) {
    const widget = promptWidget(node);
    const element = widget?.inputEl ?? widget?.element;
    const textarea = element?.matches?.("textarea") ? element : element?.querySelector?.("textarea");
    bindPrompt(node, textarea);
}

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
    const previous = node.ziyuanReferenceLinks ?? Object.fromEntries(referenceItems(node).map((item) => [item.token, item.link]));
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
    remapReferences(node, previous);
    attachPrompt(node);
    promptBindings.get(node.ziyuanPromptTextarea)?.refresh();
}

app.registerExtension({
    name: "ziyuanAI.UnifiedVideo",
    setup() {
        // Vue nodes mount textareas separately from the legacy widget.element.
        document.addEventListener("focusin", (event) => {
            const textarea = event.target;
            if (!textarea.matches?.("textarea")) return;
            const id = textarea.closest("[data-node-id]")?.dataset.nodeId;
            const graph = app.canvas?.graph ?? app.graph;
            const node = graph?.getNodeById?.(id) ?? graph?.getNodeById?.(Number(id));
            if (node?.ziyuanPromptEnabled && Array.from(textarea.labels || []).some((label) => label.textContent.trim() === "提示词")) {
                bindPrompt(node, textarea);
            }
        });
    },
    beforeRegisterNodeDef(nodeType, nodeData) {
        const imageNode = ["ZiyuanImageNode", "ZiyuanImageSubmitNode"].includes(nodeData.name);
        if (!imageNode && !["ZiyuanUnifiedVideoNode", "ZiyuanUnifiedVideoSubmitNode"].includes(nodeData.name)) return;
        const profiles = nodeData.input.required["模型"][1]?.ziyuan_profiles;
        const bananaModels = nodeData.input.required["模型"][1]?.ziyuan_banana_models ?? [];
        const bananaRatios = nodeData.input.required["模型"][1]?.ziyuan_banana_ratios ?? [];
        const imageLimit = Object.keys(nodeData.input.optional).filter((name) => /^参考图\d+$/.test(name)).length;

        function update(node) {
            const widget = (name) => node.widgets.find((item) => item.name === name);
            if (imageNode) {
                const banana = bananaModels.includes(widget("模型").value);
                for (const name of ["质量", "输出格式"]) {
                    const item = widget(name);
                    if (!item) continue;
                    if (!item.ziyuanOriginal) item.ziyuanOriginal = { type: item.type, computeSize: item.computeSize };
                    item.type = banana ? "ziyuan_hidden" : item.ziyuanOriginal.type;
                    item.computeSize = banana ? () => [0, -4] : item.ziyuanOriginal.computeSize;
                    item.hidden = banana;
                }
                const count = widget("生成数量");
                if (count) {
                    count.options.max = banana ? 1 : 10;
                    if (banana) count.value = 1;
                }
                const ratio = widget("比例");
                if (ratio && bananaRatios.length) {
                    ratio.options.values = ["自动", ...bananaRatios, ...(banana ? [] : ["21:9"])];
                    if (!ratio.options.values.includes(ratio.value)) ratio.value = "自动";
                }
                updateReferences(node, imageLimit, 0);
                node.setSize([node.size[0], node.computeSize()[1]]);
                node.setDirtyCanvas(true, true);
                return;
            }
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
            this.ziyuanPromptEnabled = true;
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
            delete this.ziyuanReferenceLinks;
            schedule(this);
            return result;
        };
        const connections = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const result = connections?.apply(this, arguments);
            schedule(this);
            return result;
        };
        const draw = nodeType.prototype.onDrawForeground;
        nodeType.prototype.onDrawForeground = function () {
            const result = draw?.apply(this, arguments);
            attachPrompt(this);
            return result;
        };
        const removed = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            this.ziyuanPromptCleanup?.();
            return removed?.apply(this, arguments);
        };
    },
});
