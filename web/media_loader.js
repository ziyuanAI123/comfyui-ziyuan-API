import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const LOADER_CLASS = "ZiyuanMediaLoader";
const SPLITTER_CLASS = "ZiyuanMediaSplitter";
const INTERNAL_DRAG_TYPE = "application/x-zy-media-loader-type";

const GROUPS = Object.freeze([
  { type: "image", key: "images", max: 9, title: "图片" },
  { type: "audio", key: "audios", max: 3, title: "音频" },
  { type: "video", key: "videos", max: 3, title: "视频" },
]);

const SPLITTER_GROUPS = Object.freeze([
  { type: "IMAGE", prefix: "image", count: "image_count", max: 9 },
  { type: "VIDEO", prefix: "video", count: "video_count", max: 3 },
  { type: "AUDIO", prefix: "audio", count: "audio_count", max: 3 },
]);

const TEXT = {
  all: "全部素材",
  upload: "上传",
  clear: "清空",
  clearConfirm: "清空全部素材？文件仍留在 input 目录。",
  drop: "拖入图片、音频或视频",
  empty: "暂无",
  remove: "删除",
  unsupported: "不支持的文件",
  limit: "已达数量上限",
};

let internalDrag = null;

function getWidget(node, name) {
  return node?.widgets?.find((widget) => widget.name === name);
}

function hideWidget(widget) {
  if (!widget) return;
  widget.hidden = true;
  widget.computeSize = () => [0, -4];
  if (widget.type && widget.type !== "hidden") widget.origType = widget.type;
  widget.type = "hidden";
}

function isLoader(node) {
  return node && (node.comfyClass === LOADER_CLASS || node.type === LOADER_CLASS);
}

function readState(node) {
  let value = getWidget(node, "media_state")?.value || "";
  if (typeof value === "string") {
    try {
      value = JSON.parse(value || "{}");
    } catch {
      value = {};
    }
  }
  const state = { images: [], audios: [], videos: [] };
  for (const group of GROUPS) {
    const values = Array.isArray(value?.[group.key]) ? value[group.key] : [];
    const normalized = values
      .map((entry) => {
        const filename = typeof entry === "object" ? entry?.filename : entry;
        return String(filename || "")
          .replaceAll("\\", "/")
          .replace(/^\/+/, "")
          .trim();
      })
      .filter((filename) => filename && !filename.split("/").includes(".."))
      .filter((filename, index, list) => list.indexOf(filename) === index)
      .slice(0, group.max);
    state[group.key] = normalized;
  }
  return state;
}

function writeState(node, state) {
  const unique = (values) => [
    ...new Set(
      (Array.isArray(values) ? values : [])
        .map((value) => String(value || "").trim())
        .filter(Boolean)
    ),
  ];
  const value = JSON.stringify({
    images: unique(state.images).slice(0, 9).map((filename) => ({ filename })),
    audios: unique(state.audios).slice(0, 3).map((filename) => ({ filename })),
    videos: unique(state.videos).slice(0, 3).map((filename) => ({ filename })),
  });
  const widget = getWidget(node, "media_state");
  if (!widget) return;
  widget.value = value;
  if (widget._state) widget._state.value = value;
  node.properties ||= {};
  node.properties.media_loader_state = value;
  syncConnectedSplitters(node);
  node.setDirtyCanvas?.(true, true);
  app.graph?.setDirtyCanvas?.(true, true);
  app.graph?.change?.();
}

function entryUrl(filename, _type) {
  if (!filename) return "";
  const params = new URLSearchParams({ filename: String(filename), type: "input" });
  return `/view?${params.toString()}`;
}

let hoverTimer = 0;
let hoverLayer = null;
let hoverCard = null;

function getHoverLayer() {
  if (hoverLayer) return hoverLayer;
  hoverLayer = document.createElement("div");
  hoverLayer.className = "zy-ml-hover";
  hoverLayer.hidden = true;
  document.body.append(hoverLayer);
  return hoverLayer;
}

function hideHoverPreview() {
  window.clearTimeout(hoverTimer);
  hoverTimer = 0;
  hoverCard = null;
  const layer = hoverLayer;
  if (!layer) return;
  layer.querySelectorAll("video, audio").forEach((media) => {
    media.pause();
    media.removeAttribute("src");
    media.load();
  });
  layer.replaceChildren();
  layer.hidden = true;
}

function positionHover(anchor, layer) {
  const rect = anchor.getBoundingClientRect();
  const pad = 12;
  const width = layer.offsetWidth || 420;
  const height = layer.offsetHeight || 280;
  let left = rect.right + pad;
  let top = rect.top;
  if (left + width > window.innerWidth - 8) left = rect.left - width - pad;
  if (left < 8) left = 8;
  if (top + height > window.innerHeight - 8) top = window.innerHeight - height - 8;
  if (top < 8) top = 8;
  layer.style.left = `${Math.round(left)}px`;
  layer.style.top = `${Math.round(top)}px`;
}

function showHoverPreview(card) {
  if (!card || internalDrag) return;
  const filename = card.dataset.filename;
  const type = card.dataset.mediaType;
  if (!filename || !type) return;
  const layer = getHoverLayer();
  layer.replaceChildren();
  layer.hidden = false;
  layer.dataset.mediaType = type;

  const caption = document.createElement("div");
  caption.className = "zy-ml-hover-caption";
  caption.textContent = filename.split(/[\\/]/).pop() || filename;

  const url = entryUrl(filename, type);
  if (type === "image") {
    const image = document.createElement("img");
    image.alt = filename;
    image.src = url;
    layer.append(image, caption);
  } else if (type === "video") {
    const video = document.createElement("video");
    video.src = url;
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    video.autoplay = true;
    video.controls = false;
    video.preload = "auto";
    video.addEventListener("loadeddata", () => video.play?.().catch(() => {}), { once: true });
    layer.append(video, caption);
    video.play?.().catch(() => {});
  } else {
    const visual = document.createElement("div");
    visual.className = "zy-ml-hover-audio";
    visual.textContent = "试听中";
    const audio = document.createElement("audio");
    audio.src = url;
    audio.preload = "auto";
    audio.autoplay = true;
    layer.append(visual, caption, audio);
    audio.play?.().catch(() => {
      visual.textContent = "点击卡片播放";
    });
  }
  positionHover(card, layer);
}

function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function itemPreview(filename, type) {
  const thumb = document.createElement("div");
  thumb.className = `zy-ml-thumb is-${type}`;
  thumb.setAttribute("aria-hidden", "true");
  if (type === "audio") {
    const wave = document.createElement("span");
    wave.className = "zy-ml-audio-wave";
    for (const height of [38, 68, 100, 68, 38]) {
      const bar = document.createElement("i");
      bar.style.setProperty("--zy-ml-bar", `${height}%`);
      wave.append(bar);
    }
    thumb.append(wave);
    return thumb;
  }

  const media = document.createElement(type === "video" ? "video" : "img");
  media.alt = "";
  media.draggable = false;
  media.src = entryUrl(filename, type);
  if (type === "video") {
    media.muted = true;
    media.playsInline = true;
    media.preload = "metadata";
    const duration = document.createElement("span");
    duration.className = "zy-ml-duration";
    duration.hidden = true;
    media.addEventListener(
      "loadedmetadata",
      () => {
        const label = formatDuration(media.duration);
        if (!label) return;
        duration.textContent = label;
        duration.hidden = false;
      },
      { once: true }
    );
    thumb.append(duration);
  }
  media.addEventListener(
    "error",
    () => {
      thumb.replaceChildren();
      const fallback = document.createElement("span");
      fallback.className = "zy-ml-fallback";
      fallback.textContent = type === "video" ? "视频" : "图片";
      thumb.append(fallback);
    },
    { once: true }
  );
  thumb.append(media);
  return thumb;
}

function filenameFromUpload(response) {
  const name = String(response?.name || response?.filename || "").trim();
  if (!name) return "";
  const subfolder = String(response?.subfolder || "")
    .replaceAll("\\", "/")
    .replace(/^\/+|\/+$/g, "");
  return subfolder ? `${subfolder}/${name}` : name;
}

function typeForFile(file) {
  const mime = String(file?.type || "").toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("video/")) return "video";
  const filename = String(file?.name || "").toLowerCase();
  const extension = filename.includes(".") ? filename.slice(filename.lastIndexOf(".") + 1) : "";
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff", "avif", "heic"].includes(extension)) {
    return "image";
  }
  if (["mp3", "wav", "flac", "ogg", "oga", "m4a", "aac", "opus", "wma"].includes(extension)) {
    return "audio";
  }
  if (["mp4", "webm", "mov", "mkv", "avi", "m4v", "mpeg", "mpg", "wmv", "flv"].includes(extension)) {
    return "video";
  }
  return "";
}

async function uploadOne(file) {
  if (!file) return "";
  const form = new FormData();
  const mime = String(file.type || "").toLowerCase();
  const fallbackExtension = mime.includes("/")
    ? mime.split("/").pop().replace(/[^a-z0-9]+/g, "")
    : "";
  const fallbackId = globalThis.crypto?.randomUUID?.().slice(0, 8) || Math.random().toString(36).slice(2, 10);
  const fallbackName = `pasted-media-${Date.now()}-${fallbackId}${fallbackExtension ? `.${fallbackExtension}` : ""}`;
  form.append("image", file, String(file.name || "").trim() || fallbackName);
  form.append("type", "input");
  const response = await api.fetchApi("/upload/image", { method: "POST", body: form });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
  const filename = filenameFromUpload(data);
  if (!filename) throw new Error("上传没有返回文件名");
  return filename;
}

async function uploadFilesNow(node, fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const state = readState(node);
  const pending = new Map(GROUPS.map((group) => [group.type, state[group.key].length]));
  const errors = [];
  let unsupported = 0;
  let skippedFull = 0;
  let changed = false;
  for (const file of files) {
    const type = typeForFile(file);
    const group = GROUPS.find((entry) => entry.type === type);
    if (!group) {
      unsupported += 1;
      continue;
    }
    if ((pending.get(type) || 0) >= group.max) {
      skippedFull += 1;
      continue;
    }
    try {
      const filename = await uploadOne(file);
      if (!state[group.key].includes(filename)) {
        state[group.key].push(filename);
        pending.set(type, (pending.get(type) || 0) + 1);
        changed = true;
      }
    } catch (error) {
      errors.push(`${file.name || type}: ${error?.message || error}`);
    }
  }
  if (changed) {
    writeState(node, state);
    render(node);
  }
  const notices = [];
  if (unsupported) notices.push(`${TEXT.unsupported} (${unsupported})`);
  if (skippedFull) notices.push(`${TEXT.limit} (${skippedFull})`);
  if (errors.length) notices.push(`${TEXT.upload}: ${errors.join("; ")}`);
  if (notices.length) globalThis.alert?.(notices.join("\n"));
}

function uploadFiles(node, fileList) {
  const files = Array.from(fileList || []);
  if (!node || !files.length) return Promise.resolve();
  const previous = node.__zyMlUploadQueue || Promise.resolve();
  const queued = Promise.resolve(previous)
    .catch(() => {})
    .then(() => uploadFilesNow(node, files));
  node.__zyMlUploadQueue = queued;
  const clear = () => {
    if (node.__zyMlUploadQueue === queued) delete node.__zyMlUploadQueue;
  };
  queued.then(clear, clear);
  return queued;
}

function dataTransferHasFiles(dataTransfer) {
  if (!dataTransfer) return false;
  if (Array.from(dataTransfer.types || []).some((type) => String(type).toLowerCase() === "files")) {
    return true;
  }
  if (Array.from(dataTransfer.items || []).some((item) => item?.kind === "file")) return true;
  return Boolean(dataTransfer.files?.length);
}

function filesFromDataTransfer(dataTransfer) {
  const direct = Array.from(dataTransfer?.files || []).filter(Boolean);
  if (direct.length) return direct;
  return Array.from(dataTransfer?.items || [])
    .filter((item) => item?.kind === "file")
    .map((item) => item.getAsFile?.())
    .filter(Boolean);
}

function selectedLoader() {
  const canvas = app.canvas;
  const graph = canvas?.graph || app.graph;
  const selected = (graph?._nodes || []).filter(
    (node) =>
      node?.selected === true ||
      node?.is_selected === true ||
      canvas?.selectedItems?.has?.(node) ||
      canvas?.selected_nodes?.[node?.id]
  );
  const current = canvas?.current_node;
  if (!selected.length && current?.is_selected) selected.push(current);
  if (selected.length !== 1 || !isLoader(selected[0])) return null;
  return selected[0];
}

function pasteTargetsEditor(event) {
  const path = typeof event?.composedPath === "function" ? event.composedPath() : [event?.target];
  return path.some((target) => {
    if (!(target instanceof Element)) return false;
    if (target.matches?.("input, textarea, select, [contenteditable], [role='textbox']")) return true;
    return Boolean(target.closest?.("input, textarea, select, [contenteditable], [role='textbox']"));
  });
}

function ownsPasteFocus(node, event) {
  const panel = node?.__zyMlPanel;
  const canvas = app.canvas?.canvas;
  const owns = (target) =>
    Boolean(
      !target ||
        target === document ||
        target === document.body ||
        target === document.documentElement ||
        target === canvas ||
        (target instanceof Node && panel?.contains?.(target))
    );
  return owns(event?.target) && owns(document.activeElement);
}

function installClipboardPaste() {
  if (document.__zyMlClipboardPasteInstalled) return;
  document.__zyMlClipboardPasteInstalled = true;
  document.addEventListener(
    "paste",
    (event) => {
      if (event.defaultPrevented || event.shiftKey || pasteTargetsEditor(event)) return;
      const selectedText = String(globalThis.getSelection?.()?.toString?.() || "").trim();
      if (selectedText) return;
      const node = selectedLoader();
      if (!node || !ownsPasteFocus(node, event)) return;
      const files = filesFromDataTransfer(event.clipboardData).filter((file) => Boolean(typeForFile(file)));
      if (!files.length) return;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      uploadFiles(node, files).catch((error) => console.error("[ziyuanAI统一素材加载] 粘贴失败", error));
    },
    true
  );
}

function installFileDrop(node, panel) {
  const stop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation?.();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  };
  panel.addEventListener("dragenter", (event) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    internalDrag = null;
    stop(event);
    panel.classList.add("is-file-drop-target");
  });
  panel.addEventListener("dragover", (event) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    stop(event);
    panel.classList.add("is-file-drop-target");
  });
  panel.addEventListener("dragleave", (event) => {
    if (!panel.classList.contains("is-file-drop-target")) return;
    const related = event.relatedTarget;
    if (related instanceof Node && panel.contains(related)) return;
    panel.classList.remove("is-file-drop-target");
  });
  panel.addEventListener("drop", (event) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    stop(event);
    panel.classList.remove("is-file-drop-target");
    uploadFiles(node, filesFromDataTransfer(event.dataTransfer)).catch((error) =>
      console.error("[ziyuanAI统一素材加载] 拖入失败", error)
    );
  });
}

function updateCard(card, group, filename, index) {
  card.dataset.index = String(index);
  card.dataset.filename = filename;
  card.dataset.mediaType = group.type;
  card.querySelector(".zy-ml-tag").textContent = String(index + 1);
  const label = card.querySelector(".zy-ml-label");
  label.textContent = filename.split(/[\\/]/).pop() || filename;
}

function createCard(node, group, filename) {
  const card = document.createElement("div");
  card.className = `zy-ml-card is-${group.type}`;
  card.draggable = true;
  card.tabIndex = 0;
  card.append(itemPreview(filename, group.type));

  const tag = document.createElement("span");
  tag.className = "zy-ml-tag";
  const label = document.createElement("span");
  label.className = "zy-ml-label";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "zy-ml-remove";
  remove.textContent = "×";
  remove.title = TEXT.remove;
  remove.addEventListener("pointerdown", (event) => event.stopPropagation());
  remove.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    hideHoverPreview();
    const index = Number(card.dataset.index);
    if (!Number.isInteger(index)) return;
    const next = readState(node);
    next[group.key].splice(index, 1);
    writeState(node, next);
    syncGroup(node, group, next);
    resize(node);
  });
  card.append(tag, label, remove);

  card.addEventListener("pointerenter", () => {
    if (internalDrag) return;
    window.clearTimeout(hoverTimer);
    hoverTimer = window.setTimeout(() => {
      hoverCard = card;
      showHoverPreview(card);
    }, 160);
  });
  card.addEventListener("pointerleave", () => {
    window.clearTimeout(hoverTimer);
    hoverTimer = 0;
    if (hoverCard === card) hideHoverPreview();
  });
  card.addEventListener("click", (event) => {
    if (event.target === remove || remove.contains(event.target)) return;
    if (group.type !== "audio") return;
    const layer = getHoverLayer();
    const audio = layer.querySelector("audio");
    if (hoverCard === card && audio) audio.play?.().catch(() => {});
  });

  card.addEventListener("dragstart", (event) => {
    hideHoverPreview();
    internalDrag = { node, groupType: group.type, card };
    event.dataTransfer?.setData(INTERNAL_DRAG_TYPE, group.type);
    event.dataTransfer?.setData("text/plain", String(card.dataset.index));
    if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    card.classList.add("is-dragging");
  });
  card.addEventListener("dragend", () => {
    if (internalDrag?.card === card) internalDrag = null;
    card.classList.remove("is-dragging");
    card.parentElement?.querySelectorAll(".is-drop-target").forEach((item) => item.classList.remove("is-drop-target"));
  });
  card.addEventListener("dragover", (event) => {
    if (dataTransferHasFiles(event.dataTransfer)) return;
    const drag = internalDrag;
    if (!drag || drag.node !== node || drag.groupType !== group.type) return;
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
    card.classList.add("is-drop-target");
  });
  card.addEventListener("dragleave", () => card.classList.remove("is-drop-target"));
  card.addEventListener("drop", (event) => {
    if (dataTransferHasFiles(event.dataTransfer)) return;
    const drag = internalDrag;
    if (!drag || drag.node !== node || drag.groupType !== group.type) return;
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation?.();
    card.classList.remove("is-drop-target");
    const from = Number(drag.card?.dataset?.index);
    const index = Number(card.dataset.index);
    internalDrag = null;
    if (!Number.isInteger(from) || !Number.isInteger(index) || from === index) return;
    const next = readState(node);
    [next[group.key][from], next[group.key][index]] = [next[group.key][index], next[group.key][from]];
    writeState(node, next);
    syncGroup(node, group, next);
  });
  return card;
}

function syncGroup(node, group, state = readState(node)) {
  const panel = node?.__zyMlPanel;
  const list = panel?.querySelector?.(`[data-zy-ml-section="${group.type}"] .zy-ml-list`);
  if (!list) return;
  const entries = state[group.key] || [];
  const cards = new Map(
    [...list.querySelectorAll(".zy-ml-card")].map((card) => [card.dataset.filename, card])
  );
  list.querySelectorAll(".zy-ml-empty").forEach((empty) => empty.remove());

  for (const [filename, card] of cards) {
    if (!entries.includes(filename)) {
      if (hoverCard === card) hideHoverPreview();
      card.remove();
    }
  }
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "zy-ml-empty";
    empty.textContent = TEXT.empty;
    list.append(empty);
    return;
  }
  entries.forEach((filename, index) => {
    const card = cards.get(filename) || createCard(node, group, filename);
    updateCard(card, group, filename, index);
    list.append(card);
  });
}

function render(node) {
  const panel = node?.__zyMlPanel;
  if (!panel) return;
  const state = readState(node);
  const total = GROUPS.reduce((sum, group) => sum + (state[group.key]?.length || 0), 0);
  const capacity = GROUPS.reduce((sum, group) => sum + group.max, 0);
  if (node.__zyMlCount) node.__zyMlCount.textContent = `${total}/${capacity}`;
  for (const group of GROUPS) syncGroup(node, group, state);
  syncConnectedSplitters(node);
  resize(node);
}

function cssPixels(style, property) {
  const value = Number.parseFloat(style?.getPropertyValue?.(property) || "0");
  return Number.isFinite(value) ? value : 0;
}

function intrinsicPanelHeight(panel) {
  const panelStyle = getComputedStyle(panel);
  const toolbar = panel.querySelector(".zy-ml-toolbar");
  const groups = panel.querySelector(".zy-ml-groups");
  const sections = [...(groups?.querySelectorAll?.(":scope > .zy-ml-section") || [])];
  const groupStyle = groups ? getComputedStyle(groups) : null;
  const padding =
    cssPixels(panelStyle, "padding-top") +
    cssPixels(panelStyle, "padding-bottom") +
    cssPixels(panelStyle, "border-top-width") +
    cssPixels(panelStyle, "border-bottom-width");
  const toolbarHeight = Math.ceil(
    toolbar?.scrollHeight || toolbar?.getBoundingClientRect?.().height || 28
  );
  const sectionsHeight = sections.reduce(
    (sum, section) =>
      sum + Math.ceil(section.scrollHeight || section.getBoundingClientRect?.().height || 0),
    0
  );
  const groupGap = cssPixels(groupStyle, "row-gap") || cssPixels(groupStyle, "gap");
  const groupsHeight = sectionsHeight + Math.max(0, sections.length - 1) * groupGap;
  const panelGap = cssPixels(panelStyle, "row-gap") || cssPixels(panelStyle, "gap");
  return Math.max(180, Math.ceil(padding + toolbarHeight + panelGap + groupsHeight));
}

function resize(node) {
  const panel = node?.__zyMlPanel;
  const domWidget = node?.__zyMlWidget;
  if (!panel || !domWidget) return;

  const measure = () => {
    // ComfyUI frontend 1.55+ owns DOM widget visibility and layout. Never
    // switch the widget to type "hidden" for measurement: the Vue wrapper can
    // retain display:none. Measure intrinsic children so a 100%-height wrapper
    // cannot feed its allocated height back into the next resize pass.
    const currentWidth = Number(node.size?.[0]) || 300;
    const currentHeight = Number(node.size?.[1]) || 0;
    const margin = Number(domWidget.margin ?? domWidget.options?.margin ?? 10);
    // Measure wrapping at the new width, not the Vue wrapper's previous frame.
    domWidget.width = Math.max(currentWidth, 320);
    panel.style.width = `${domWidget.width - margin * 2}px`;
    const contentHeight = intrinsicPanelHeight(panel);
    panel.style.width = "100%";
    const widgetHeight = contentHeight + 8;
    node.__zyMlMinWidgetHeight = widgetHeight;

    panel.style.height = "100%";
    panel.style.minHeight = `${contentHeight}px`;
    panel.style.maxHeight = "100%";
    panel.style.flex = "1 1 auto";
    panel.style.setProperty("--comfy-widget-min-height", `${widgetHeight}px`);

    domWidget.options ||= {};
    domWidget.options.getMinHeight = () =>
      Math.max(1, Number(node.__zyMlMinWidgetHeight) || widgetHeight);
    delete domWidget.options.getMaxHeight;
    delete domWidget.options.getHeight;

    const widgetTop = Math.max(48, Number(domWidget.y) || 0);
    const minimumHeight = Math.ceil(widgetTop + widgetHeight + 6);
    if (
      typeof node.setSize === "function" &&
      (currentWidth < 320 || currentHeight + 1 < minimumHeight)
    ) {
      node.setSize([
        Math.max(currentWidth, 320),
        Math.max(currentHeight, minimumHeight),
      ]);
    }

    node._widgetSlotsDirty = true;
    node.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
  };

  // Update the minimum before LiteGraph lays out widgets; a deferred update can
  // grow the node to the old minimum and leave it stuck there after narrowing.
  if (panel.isConnected) measure();
  else if (typeof requestAnimationFrame === "function") requestAnimationFrame(measure);
  else setTimeout(measure, 0);
}

function installStyles() {
  let style = document.getElementById("zy-media-loader-styles");
  if (!style) {
    style = document.createElement("style");
    style.id = "zy-media-loader-styles";
    document.head.append(style);
  }
  style.textContent = `
    .zy-ml-panel { position:relative; display:flex; flex-direction:column; gap:8px; width:100%; height:100%; min-height:0; max-height:100%; flex:1 1 auto; align-self:stretch; box-sizing:border-box; padding:8px; border:1px solid #111; border-radius:6px; background:#222; color:#ddd; font-size:12px; overflow:hidden; transition:border-color .14s ease, box-shadow .14s ease; }
    .zy-ml-panel.is-file-drop-target { border-color:#718ca6; box-shadow:inset 0 0 0 1px rgba(113,140,166,.34); }
    .zy-ml-panel.is-file-drop-target::after { content:attr(data-drop-label); position:absolute; inset:8px; z-index:20; display:flex; align-items:center; justify-content:center; box-sizing:border-box; border:1px dashed rgba(146,177,207,.62); border-radius:5px; background:rgba(25,28,32,.9); color:#d7e2ec; font-size:12px; font-weight:600; pointer-events:none; }
    .zy-ml-toolbar { display:flex; align-items:center; justify-content:space-between; gap:8px; min-height:28px; }
    .zy-ml-heading { display:flex; align-items:baseline; gap:7px; min-width:0; }
    .zy-ml-title { font-weight:650; color:#eee; }
    .zy-ml-count { color:#888; font-size:10px; font-variant-numeric:tabular-nums; }
    .zy-ml-upload { border:1px solid #444; border-radius:4px; background:#2a2a2a; color:#ddd; padding:4px 8px; cursor:pointer; font-size:11px; line-height:1.35; }
    .zy-ml-upload:hover { background:#333; border-color:#666; color:#fff; }
    .zy-ml-actions { display:flex; align-items:center; gap:6px; }
    .zy-ml-clear:hover { background:#8b4242; border-color:#a35a5a; color:#fff; }
    .zy-ml-groups { display:flex; flex:1 1 auto; flex-direction:column; gap:8px; min-width:0; min-height:0; }
    .zy-ml-section { min-width:0; border-top:1px solid #353535; padding-top:7px; }
    .zy-ml-header { display:flex; align-items:center; justify-content:space-between; gap:6px; margin:0 0 5px; color:#bdbdbd; font-size:10px; font-weight:650; line-height:1.2; }
    .zy-ml-list { display:grid; grid-template-columns:repeat(auto-fill,92px); gap:8px; min-height:26px; }
    .zy-ml-hover { position:fixed; z-index:100000; pointer-events:none; max-width:min(480px,72vw); padding:8px; border:1px solid #4a5560; border-radius:8px; background:#111; box-shadow:0 16px 40px rgba(0,0,0,.55); }
    .zy-ml-hover[hidden] { display:none !important; }
    .zy-ml-hover img, .zy-ml-hover video { display:block; max-width:min(460px,70vw); max-height:min(420px,62vh); object-fit:contain; background:#000; }
    .zy-ml-hover-caption { margin-top:6px; color:#ddd; font-size:11px; line-height:1.3; word-break:break-all; }
    .zy-ml-hover-audio { display:flex; align-items:center; justify-content:center; min-width:220px; min-height:88px; color:#35cdb9; font-size:13px; font-weight:650; background:#10201d; border-radius:4px; }
    .zy-ml-empty { display:flex; grid-column:1 / -1; align-items:center; min-height:26px; padding:0 5px; border:1px dashed #3c3c3c; border-radius:4px; color:#6f6f6f; font-size:10px; }
    .zy-ml-card { position:relative; display:block; min-width:0; aspect-ratio:1; overflow:visible; border:1px solid #393939; border-radius:5px; background:#161616; cursor:grab; outline:none; transition:border-color .14s ease, background .14s ease, transform .14s ease; }
    .zy-ml-card:hover, .zy-ml-card:focus-visible { border-color:#6b829b; background:#1a1d20; }
    .zy-ml-card:active { cursor:grabbing; }
    .zy-ml-card.is-dragging { opacity:.4; transform:scale(.96); }
    .zy-ml-card.is-drop-target { border-color:#7ea6cb; box-shadow:0 0 0 1px rgba(126,166,203,.45); }
    .zy-ml-thumb { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; overflow:hidden; border-radius:4px; background:#101010; }
    .zy-ml-thumb.is-audio { background:#10201d; }
    .zy-ml-thumb.is-audio::before { content:""; position:absolute; inset:7px; border:1px solid rgba(70,206,182,.18); border-radius:3px; pointer-events:none; }
    .zy-ml-thumb > img, .zy-ml-thumb > video { position:relative; z-index:1; width:100%; height:100%; display:block; object-fit:contain; object-position:center; }
    .zy-ml-audio-wave { position:relative; z-index:1; display:flex; align-items:center; justify-content:center; gap:4px; width:48%; height:42%; }
    .zy-ml-audio-wave i { display:block; width:4px; height:var(--zy-ml-bar); border-radius:3px; background:#35cdb9; }
    .zy-ml-fallback { color:#777; font-size:10px; }
    .zy-ml-card.is-audio::after { content:""; position:absolute; inset:48% 0 0; border-radius:0 0 4px 4px; pointer-events:none; background:linear-gradient(180deg,transparent,rgba(0,0,0,.86)); }
    .zy-ml-tag { position:absolute; bottom:4px; left:4px; z-index:2; min-width:12px; padding:2px 4px; border-radius:3px; background:rgba(8,8,8,.7); color:#e7e7e7; font-size:9px; line-height:1.15; text-align:center; font-variant-numeric:tabular-nums; pointer-events:none; }
    .zy-ml-duration { position:absolute; right:4px; bottom:4px; z-index:2; padding:2px 4px; border-radius:3px; background:rgba(8,8,8,.35); color:#f1f1f1; font-size:9px; line-height:1.15; font-variant-numeric:tabular-nums; pointer-events:none; }
    .zy-ml-label { position:absolute; right:4px; bottom:4px; left:25px; z-index:1; overflow:hidden; color:#eee; font-size:9px; line-height:1.2; text-overflow:ellipsis; white-space:nowrap; pointer-events:none; }
    .zy-ml-card.is-image .zy-ml-label, .zy-ml-card.is-video .zy-ml-label { display:none; }
    .zy-ml-remove { position:absolute; top:-7px; right:-7px; z-index:3; width:17px; height:17px; padding:0; border:2px solid #222; border-radius:50%; background:#3b434a; color:#f1f1f1; cursor:pointer; display:flex; align-items:center; justify-content:center; font-family:Arial,sans-serif; font-size:15px; font-weight:400; line-height:1; opacity:0; visibility:hidden; transform:scale(.82); transition:opacity .15s ease, visibility .15s ease, transform .15s ease, background .15s ease; }
    .zy-ml-card:hover .zy-ml-remove, .zy-ml-card:focus-within .zy-ml-remove { opacity:1; visibility:visible; transform:scale(1); }
    .zy-ml-remove:hover { background:#8b4242; color:#fff; }
  `;
}

function setupLoader(node) {
  if (!node || node.__zyMlSetup || typeof node.addDOMWidget !== "function") return;
  node.__zyMlSetup = true;
  hideWidget(getWidget(node, "media_state"));
  const panel = document.createElement("div");
  panel.className = "zy-ml-panel";
  panel.dataset.dropLabel = TEXT.drop;
  installFileDrop(node, panel);
  panel.addEventListener("pointerdown", (event) => event.stopPropagation());
  panel.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation?.();
      app.canvas?.processMouseWheel?.(event);
    },
    { passive: false, capture: true }
  );

  const toolbar = document.createElement("div");
  toolbar.className = "zy-ml-toolbar";
  const heading = document.createElement("div");
  heading.className = "zy-ml-heading";
  const title = document.createElement("span");
  title.className = "zy-ml-title";
  title.textContent = TEXT.all;
  const count = document.createElement("span");
  count.className = "zy-ml-count";
  heading.append(title, count);
  const upload = document.createElement("button");
  upload.type = "button";
  upload.className = "zy-ml-upload";
  upload.textContent = TEXT.upload;
  upload.addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*,audio/*,video/*";
    input.multiple = true;
    input.addEventListener(
      "change",
      () => {
        uploadFiles(node, input.files || []).finally(() => {
          input.value = "";
        });
      },
      { once: true }
    );
    input.click();
  });
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "zy-ml-upload zy-ml-clear";
  clear.textContent = TEXT.clear;
  clear.addEventListener("click", () => {
    const state = readState(node);
    const total = GROUPS.reduce((sum, group) => sum + (state[group.key]?.length || 0), 0);
    if (!total) return;
    if (!globalThis.confirm(TEXT.clearConfirm)) return;
    hideHoverPreview();
    writeState(node, { images: [], audios: [], videos: [] });
    render(node);
  });
  const actions = document.createElement("div");
  actions.className = "zy-ml-actions";
  actions.append(upload, clear);
  toolbar.append(heading, actions);

  const groups = document.createElement("div");
  groups.className = "zy-ml-groups";
  panel.append(toolbar, groups);
  node.__zyMlCount = count;
  for (const group of GROUPS) {
    const section = document.createElement("section");
    section.className = "zy-ml-section";
    section.dataset.zyMlSection = group.type;
    const header = document.createElement("div");
    header.className = "zy-ml-header";
    const sectionTitle = document.createElement("span");
    sectionTitle.textContent = group.title;
    header.append(sectionTitle);
    const list = document.createElement("div");
    list.className = "zy-ml-list";
    section.append(header, list);
    groups.append(section);
  }
  node.__zyMlPanel = panel;
  const domWidget = node.addDOMWidget("zy_media_loader", "zy_media_loader", panel, {
    getValue: () => String(getWidget(node, "media_state")?.value || ""),
    setValue: (value) => {
      const widget = getWidget(node, "media_state");
      if (widget) widget.value = String(value || "");
      render(node);
    },
    serialize: false,
    getMinHeight: () => Math.max(1, Number(node.__zyMlMinWidgetHeight) || 48),
    afterResize: () => resize(node),
  });
  if (!domWidget) return;
  domWidget.serialize = false;
  domWidget.options ||= {};
  domWidget.options.serialize = false;
  node.__zyMlWidget = domWidget;
  render(node);
  resize(node);
}

function installLoader(nodeType, nodeData) {
  if (nodeData?.name !== LOADER_CLASS) return;
  if (nodeType.prototype.__zyMediaLoaderInstalled) return;
  nodeType.prototype.__zyMediaLoaderInstalled = true;
  installStyles();
  const originalPasteFiles = nodeType.prototype.pasteFiles;
  nodeType.prototype.pasteFiles = function pasteFilesZiyuanMediaLoader(files) {
    const list = Array.from(files || []);
    if (!list.length && typeof originalPasteFiles === "function") {
      return originalPasteFiles.apply(this, arguments);
    }
    return uploadFiles(this, list);
  };
  const originalCreated = nodeType.prototype.onNodeCreated;
  nodeType.prototype.onNodeCreated = function onNodeCreatedZiyuanMediaLoader() {
    const result = originalCreated?.apply(this, arguments);
    setupLoader(this);
    return result;
  };
  const originalAdded = nodeType.prototype.onAdded;
  nodeType.prototype.onAdded = function onAddedZiyuanMediaLoader(graph) {
    const result = originalAdded?.apply(this, arguments);
    setupLoader(this);
    render(this);
    return result;
  };
  const originalConfigure = nodeType.prototype.onConfigure;
  nodeType.prototype.onConfigure = function onConfigureZiyuanMediaLoader(info) {
    const result = originalConfigure?.apply(this, arguments);
    setupLoader(this);
    render(this);
    return result;
  };
  const originalResize = nodeType.prototype.onResize;
  nodeType.prototype.onResize = function onResizeZiyuanMediaLoader(size) {
    const result = originalResize?.apply(this, arguments);
    if (this.__zyMlSetup) resize(this);
    return result;
  };
}

function mediaLink(graph, id) {
  return graph?.getLink?.(id) || graph?.links?.[id];
}

function syncConnectedSplitters(loader) {
  for (const id of loader?.outputs?.[0]?.links || []) {
    const link = mediaLink(loader.graph, id);
    const target = loader.graph?.getNodeById?.(link?.target_id);
    if (target?.comfyClass === SPLITTER_CLASS || target?.type === SPLITTER_CLASS) {
      updateSplitterOutputs(target);
    }
  }
}

function updateSplitterOutputs(node) {
  if (!node?.outputs) return;
  const input = node.inputs?.find((input) => input.name === "media_bundle");
  const link = input?.link != null ? mediaLink(node.graph, input.link) : null;
  const source = node.graph?.getNodeById?.(link?.origin_id);
  const state = isLoader(source) ? readState(source) : { images: [], videos: [], audios: [] };
  let cursor = 0;
  for (const group of SPLITTER_GROUPS) {
    const count = state[`${group.prefix}s`].length;
    const widget = getWidget(node, group.count);
    if (widget) {
      widget.value = count;
      widget.disabled = true;
      widget.options ||= {};
      widget.options.read_only = true;
    }
    for (let index = 1; index <= group.max; index += 1) {
      const output = node.outputs[cursor];
      cursor += 1;
      if (!output) continue;
      output.hidden = index > count;
    }
  }
  node.setSize?.(node.computeSize?.() || node.size);
  node.setDirtyCanvas?.(true, true);
}

function installSplitter(nodeType, nodeData) {
  if (nodeData?.name !== SPLITTER_CLASS) return;
  if (nodeType.prototype.__zyMediaSplitterInstalled) return;
  nodeType.prototype.__zyMediaSplitterInstalled = true;

  const setup = (node) => {
    if (!node || node.__zyMlSplitterSetup) return;
    node.__zyMlSplitterSetup = true;
    for (const name of ["image_count", "video_count", "audio_count"]) {
      const widget = getWidget(node, name);
      if (!widget) continue;
      widget.callback = () => updateSplitterOutputs(node);
    }
    updateSplitterOutputs(node);
  };

  const originalCreated = nodeType.prototype.onNodeCreated;
  nodeType.prototype.onNodeCreated = function onNodeCreatedZiyuanMediaSplitter() {
    const result = originalCreated?.apply(this, arguments);
    setup(this);
    return result;
  };
  const originalAdded = nodeType.prototype.onAdded;
  nodeType.prototype.onAdded = function onAddedZiyuanMediaSplitter(graph) {
    const result = originalAdded?.apply(this, arguments);
    setup(this);
    updateSplitterOutputs(this);
    return result;
  };
  const originalConfigure = nodeType.prototype.onConfigure;
  nodeType.prototype.onConfigure = function onConfigureZiyuanMediaSplitter(info) {
    const result = originalConfigure?.apply(this, arguments);
    setup(this);
    updateSplitterOutputs(this);
    return result;
  };
  const originalConnectionsChange = nodeType.prototype.onConnectionsChange;
  nodeType.prototype.onConnectionsChange = function () {
    const result = originalConnectionsChange?.apply(this, arguments);
    updateSplitterOutputs(this);
    // LiteGraph may notify before committing the new input link.
    queueMicrotask(() => updateSplitterOutputs(this));
    return result;
  };
  const originalGraphConfigured = nodeType.prototype.onGraphConfigured;
  nodeType.prototype.onGraphConfigured = function () {
    const result = originalGraphConfigured?.apply(this, arguments);
    updateSplitterOutputs(this);
    return result;
  };
}

app.registerExtension({
  name: "comfyui-ziyuan-API.MediaLoader",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    installLoader(nodeType, nodeData);
    installSplitter(nodeType, nodeData);
  },
  async setup() {
    installClipboardPaste();
  },
});
