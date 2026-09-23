// 采集页（默认页）：预览 + ROI 框选 + 状态横幅 + 遥测 + 运输控件。
// 交互语义对齐 Python 旧版 capture_page.py，视觉按 Batana UI 规范 v1.0 重做。
import { el, card, sectionHeader, button, segmented, switchControl, telemetryValue } from "./components";
import {
  CaptureEvent,
  Channel,
  SmStateName,
  VideoDevice,
  discardClip,
  listDevices,
  listSessions,
  manualToggle,
  saveSettings,
  setMuted,
  startCapture,
  stopCapture,
  store,
} from "./ipc";
import type { AppContext, Page } from "./app";

type ViewMode = "left" | "right" | "sbs";

const PREVIEW_W = 320;
const PREVIEW_H = 200;
/** 运动占比细条满格 = 10%（对齐旧版 MiniBar 量纲） */
const METER_MAX_PCT = 10;

const STATE_LABEL: Record<SmStateName, string> = {
  IDLE: "等待就位",
  READY: "请准备",
  ARMED: "请挥棒",
  SWING: "挥棒中",
  SAVING: "保存中",
  ERROR: "异常",
};
const STATE_TONE: Record<SmStateName, string> = {
  IDLE: "state-dim",
  READY: "state-green",
  ARMED: "state-red",
  SWING: "state-accent",
  SAVING: "state-accent",
  ERROR: "state-red",
};

function errorAdvice(message: string): string {
  if (/相机|ffmpeg|avfoundation|帧/.test(message)) return "检查相机连接与「相机源」选择，然后重新开始采集。";
  if (/落盘|存储|磁盘|写/.test(message)) return "检查设置页存储根目录的可用空间，然后重新开始采集。";
  return "重新开始采集；反复出现请到设置页检查采集参数。";
}

export function createCapturePage(_ctx: AppContext): Page {
  // ---- 状态 ----
  let capturing = false;
  let smState: SmStateName | null = null;
  let viewMode: ViewMode = "left";
  let latestFrame: { data: Uint8ClampedArray<ArrayBuffer>; width: number; height: number } | null = null;
  let readySince = 0;
  let countdownTimer = 0;
  let savedFlashTimer = 0;
  let roiDraft: [number, number, number, number] | null = null; // 框选中的归一化矩形

  // ---- DOM ----
  const banner = el("div", { class: "state-banner state-dim" }, "未采集");
  const errText = el("span", {});
  const errBanner = el(
    "div",
    { class: "banner banner-error", style: "display:none" },
    errText,
    button("重新开始采集", "secondary", () => void restart()),
  );
  const roiHint = el("span", { class: "roi-hint", style: "display:none" }, "请先在画面上框选打击区");
  const clearRoiBtn = el("button", { class: "link-btn", type: "button", style: "display:none", onclick: clearRoi }, "清除框选");

  const previewCv = el("canvas", { class: "preview-cv" }) as HTMLCanvasElement;
  const overlayCv = el("canvas", { class: "overlay-cv can-draw" }) as HTMLCanvasElement;
  const previewEmpty = el(
    "div",
    { class: "preview-empty" },
    el("span", { style: "font-size:13px" }, "预览未启动"),
    el("span", {}, "点击「开始采集」连接相机"),
  );
  const previewWrap = el("div", { class: "preview-wrap" }, previewCv, overlayCv, previewEmpty);

  const viewSeg = segmented(
    [
      { label: "左目", value: "left" },
      { label: "右目", value: "right" },
      { label: "双目", value: "sbs" },
    ],
    "left",
    (v) => {
      viewMode = v as ViewMode;
      render();
    },
  );

  const mTotal = telemetryValue("已采集");
  const mPass = telemetryValue("合格");
  const mFps = telemetryValue("帧率");
  const mPresence = telemetryValue("就位占比");
  const mMotion = telemetryValue("运动占比", true);
  const meterFill = el("div", { class: "meter-fill" });
  const meterTick = el("div", { class: "meter-tick" });
  const meter = el("div", { class: "meter" }, meterFill, meterTick);

  const btnStart = button("开始采集", "primary", () => void toggleCapture());
  btnStart.classList.add("btn-block");
  const btnManualStart = button("手动开始挥棒", "secondary", () => void doManualToggle());
  const btnManualStop = button("手动结束", "secondary", () => void doManualToggle());
  const btnDiscard = button("丢弃重拍", "danger", () => void doDiscard());
  btnManualStart.title = "快捷键：空格";
  btnManualStop.title = "快捷键：空格";
  btnDiscard.title = "快捷键：D";

  const camSelect = el("select", {}) as HTMLSelectElement;
  camSelect.addEventListener("mousedown", () => void populateCameras());
  camSelect.addEventListener("change", () => void onCameraChanged());

  const muteSwitch = switchControl(false, (muted) => void onMute(muted));

  const statusLine = el("div", { class: "status-line" }, "待命");

  const previewCard = card(
    el("div", { class: "preview-head" }, roiHint, clearRoiBtn, el("span", { class: "spacer" }), viewSeg.node),
    previewWrap,
  );
  previewCard.classList.add("preview-card");

  const node = el(
    "div",
    { class: "page capture-page" },
    el(
      "div",
      { class: "cap-body" },
      el("div", { class: "cap-left" }, previewCard),
      el(
        "div",
        { class: "cap-side" },
        banner,
        errBanner,
        card(
          sectionHeader("遥测"),
          el(
            "div",
            { class: "tele-grid" },
            mTotal.node,
            mPass.node,
            mFps.node,
            mPresence.node,
            mMotion.node,
          ),
          meter,
          el("div", { class: "meter-caption" }, el("span", {}, "0%"), el("span", {}, "触发阈值"), el("span", {}, `${METER_MAX_PCT}%`)),
        ),
        card(
          sectionHeader("采集控制"),
          el("div", { class: "controls-card" },
            btnStart,
            el("div", { class: "ctl-row" }, btnManualStart, btnManualStop),
            btnDiscard,
            el("div", { class: "ctl-row" }, el("span", { class: "ctl-label" }, "静音"), muteSwitch),
            el("div", { class: "ctl-row" }, el("span", { class: "ctl-label" }, "相机源"), camSelect),
          ),
        ),
      ),
    ),
    statusLine,
  );

  // card() 返回 section；预览卡片布局类已在创建时挂载
  function setStatus(text: string): void {
    statusLine.textContent = text;
  }

  // ---- 预览渲染 ----
  function syncCanvasSize(): void {
    const w = viewMode === "sbs" ? PREVIEW_W * 2 : PREVIEW_W;
    if (previewCv.width !== w) {
      previewCv.width = w;
      previewCv.height = PREVIEW_H;
      // overlay 与预览同一坐标系（均经 object-fit: contain 等比显示，letterbox 一致）
      overlayCv.width = w;
      overlayCv.height = PREVIEW_H;
    }
  }

  function render(): void {
    syncCanvasSize();
    const g = previewCv.getContext("2d")!;
    if (viewMode === "right") {
      g.fillStyle = "#2a2a2c";
      g.fillRect(0, 0, previewCv.width, previewCv.height);
      g.fillStyle = "rgba(255,255,255,0.35)";
      g.font = "12px -apple-system, sans-serif";
      g.textAlign = "center";
      g.fillText("预览流仅含左目，右目不可用", previewCv.width / 2, PREVIEW_H / 2);
    } else if (latestFrame) {
      const img = new ImageData(latestFrame.data, latestFrame.width, latestFrame.height);
      g.putImageData(img, 0, 0);
      if (viewMode === "sbs") {
        g.fillStyle = "#2a2a2c";
        g.fillRect(PREVIEW_W, 0, PREVIEW_W, PREVIEW_H);
        g.fillStyle = "rgba(255,255,255,0.35)";
        g.font = "12px -apple-system, sans-serif";
        g.textAlign = "center";
        g.fillText("右目", PREVIEW_W + PREVIEW_W / 2, PREVIEW_H / 2);
      }
    } else {
      g.fillStyle = "#000";
      g.fillRect(0, 0, previewCv.width, previewCv.height);
    }
    drawOverlay();
  }

  /** overlay 画布像素尺寸（与预览同坐标系） */
  function overlayRect(): { w: number; h: number } {
    return { w: overlayCv.width, h: overlayCv.height };
  }

  /** wrap 内 object-fit: contain 的实际内容矩形（client 坐标） */
  function contentClientRect(): { x: number; y: number; w: number; h: number } {
    const r = previewWrap.getBoundingClientRect();
    const aspect = overlayCv.width / Math.max(overlayCv.height, 1);
    let w = r.width;
    let h = w / aspect;
    if (h > r.height) {
      h = r.height;
      w = h * aspect;
    }
    return { x: r.left + (r.width - w) / 2, y: r.top + (r.height - h) / 2, w, h };
  }

  function drawOverlay(): void {
    const { w, h } = overlayRect();
    if (w === 0) return;
    const g = overlayCv.getContext("2d")!;
    g.clearRect(0, 0, w, h);
    const roiNorm = roiDraft ?? currentRoiNorm();
    if (!roiNorm) return;
    const half = viewMode === "sbs" ? w / 2 : w;
    const [fx, fy, fw, fh] = roiNorm;
    const x = fx * half;
    const y = fy * h;
    g.fillStyle = "rgba(10, 132, 255, 0.12)";
    g.fillRect(x, y, fw * half, fh * h);
    g.strokeStyle = "#0a84ff";
    g.lineWidth = 1.5;
    g.setLineDash([6, 4]);
    g.strokeRect(x + 0.75, y + 0.75, fw * half - 1.5, fh * h - 1.5);
    g.setLineDash([]);
    g.font = "11px -apple-system, sans-serif";
    g.textAlign = "left";
    g.fillStyle = "#0a84ff";
    g.fillText("打击区", x + 6, y + 16);
  }

  function currentRoiNorm(): [number, number, number, number] | null {
    const s = store.settings;
    if (!s?.roi) return null;
    const eyeW = s.capture_width / 2;
    const eyeH = s.capture_height;
    return [s.roi[0] / eyeW, s.roi[1] / eyeH, s.roi[2] / eyeW, s.roi[3] / eyeH];
  }

  function refreshRoiHint(): void {
    const has = store.settings?.roi != null;
    roiHint.style.display = has ? "none" : "";
    clearRoiBtn.style.display = has ? "" : "none";
  }

  function clearRoi(): void {
    if (!store.settings) return;
    store.settings.roi = null;
    void saveSettings(store.settings).catch(() => setStatus("ROI 清除失败：设置保存出错"));
    refreshRoiHint();
    drawOverlay();
    setStatus("已清除框选，重新拖拽画面框选打击区");
  }

  // ---- ROI 框选（指针拖拽，落笔即归一化，松手写 settings.roi 并落盘） ----
  let dragStart: { x: number; y: number } | null = null;

  function eventNorm(e: PointerEvent): { x: number; y: number } {
    const r = contentClientRect();
    const half = viewMode === "sbs" ? r.w / 2 : r.w;
    return {
      x: Math.min(Math.max((e.clientX - r.x) / half, 0), 1),
      y: Math.min(Math.max((e.clientY - r.y) / r.h, 0), 1),
    };
  }

  overlayCv.addEventListener("pointerdown", (e) => {
    if (viewMode === "right") return;
    overlayCv.setPointerCapture(e.pointerId);
    dragStart = eventNorm(e);
    roiDraft = [dragStart.x, dragStart.y, 0, 0];
    drawOverlay();
  });
  overlayCv.addEventListener("pointermove", (e) => {
    if (!dragStart) return;
    const p = eventNorm(e);
    roiDraft = [
      Math.min(dragStart.x, p.x),
      Math.min(dragStart.y, p.y),
      Math.abs(p.x - dragStart.x),
      Math.abs(p.y - dragStart.y),
    ];
    drawOverlay();
  });
  overlayCv.addEventListener("pointerup", (e) => {
    if (!dragStart || !roiDraft || !store.settings) return;
    dragStart = null;
    const [fx, fy, fw, fh] = roiDraft;
    roiDraft = null;
    if (fw < 0.03 || fh < 0.03) {
      drawOverlay(); // 误触：保持原框选
      return;
    }
    const s = store.settings;
    const eyeW = s.capture_width / 2;
    const eyeH = s.capture_height;
    s.roi = [
      Math.round(fx * eyeW),
      Math.round(fy * eyeH),
      Math.round(fw * eyeW),
      Math.round(fh * eyeH),
    ];
    void saveSettings(s)
      .then(() => setStatus(`ROI 已保存 (${s.roi![0]},${s.roi![1]} ${s.roi![2]}x${s.roi![3]})，下次开始采集生效`))
      .catch(() => setStatus("ROI 保存失败：设置写入出错"));
    refreshRoiHint();
    drawOverlay();
    void e;
  });

  // overlay 与预览同分辨率缓冲，经 object-fit 等比缩放，无需随窗口重设尺寸

  // ---- 状态横幅 ----
  function setBanner(state: SmStateName | null): void {
    smState = state;
    window.clearInterval(countdownTimer);
    banner.classList.remove("counting");
    if (state === null) {
      banner.textContent = "未采集";
      banner.className = "state-banner state-dim";
    } else {
      banner.textContent = STATE_LABEL[state];
      banner.className = `state-banner ${STATE_TONE[state]}`;
      if (state === "READY") {
        readySince = performance.now();
        tickCountdown();
        countdownTimer = window.setInterval(tickCountdown, 100);
      }
    }
    updateButtons();
  }

  function tickCountdown(): void {
    const total = store.settings?.countdown_seconds ?? 3;
    const remaining = total - (performance.now() - readySince) / 1000;
    const n = Math.ceil(remaining);
    if (n >= 1 && n <= 9) {
      banner.textContent = String(n);
      banner.classList.add("counting");
    } else {
      banner.textContent = STATE_LABEL.READY;
      banner.classList.remove("counting");
    }
  }

  function flashSaved(seq: number): void {
    window.clearTimeout(savedFlashTimer);
    window.clearInterval(countdownTimer);
    banner.classList.remove("counting");
    banner.textContent = `已保存 #${seq}`;
    banner.className = "state-banner state-green";
    savedFlashTimer = window.setTimeout(() => {
      if (capturing && smState) setBanner(smState);
    }, 1500);
  }

  function updateButtons(): void {
    btnStart.textContent = capturing ? "停止采集" : "开始采集";
    btnManualStart.disabled = !capturing || smState !== "ARMED";
    btnManualStop.disabled = !capturing || smState !== "SWING";
    btnDiscard.disabled = !capturing || (smState !== "ARMED" && smState !== "SWING");
    previewEmpty.style.display = capturing ? "none" : "";
  }

  // ---- 采集事件 ----
  function handleEvent(raw: unknown): void {
    const ev = raw as CaptureEvent;
    switch (ev.type) {
      case "preview": {
        const data: Uint8ClampedArray<ArrayBuffer> =
          ev.rgba instanceof ArrayBuffer
            ? new Uint8ClampedArray(ev.rgba)
            : new Uint8ClampedArray(ev.rgba as number[]);
        latestFrame = { data, width: ev.width, height: ev.height };
        render();
        break;
      }
      case "telemetry": {
        mFps.set(ev.fps.toFixed(1));
        mPresence.set(`${(ev.presence_ratio * 100).toFixed(1)}%`);
        const pct = ev.motion_ratio * 100;
        mMotion.set(`${pct.toFixed(2)}%`);
        meterFill.style.width = `${Math.min((pct / METER_MAX_PCT) * 100, 100)}%`;
        break;
      }
      case "state_changed": {
        setBanner(ev.next);
        setStatus(`${ev.prev} → ${ev.next}（${ev.reason}）`);
        break;
      }
      case "clip_saved": {
        flashSaved(ev.seq);
        setStatus(`片段 #${ev.seq} 已保存（${ev.frame_count} 帧）：${ev.session_id}`);
        void refreshCounts();
        break;
      }
      case "error": {
        showError(ev.message);
        break;
      }
    }
  }

  function showError(message: string): void {
    errText.textContent = `${message}。${errorAdvice(message)}`;
    errBanner.style.display = "";
    setBanner("ERROR");
    setStatus(message);
  }

  function hideError(): void {
    errBanner.style.display = "none";
  }

  // ---- 采集控制 ----
  async function toggleCapture(): Promise<void> {
    if (capturing) {
      await stop();
    } else {
      await start();
    }
  }

  async function start(): Promise<void> {
    hideError();
    try {
      const ch = new Channel<unknown>();
      ch.onmessage = handleEvent;
      await startCapture(ch);
      capturing = true;
      latestFrame = null;
      setBanner("IDLE");
      setStatus("采集运行中");
      render();
    } catch (e) {
      showError(String(e));
    }
    updateButtons();
  }

  async function stop(): Promise<void> {
    try {
      await stopCapture();
    } catch {
      /* 幂等 */
    }
    capturing = false;
    setBanner(null);
    hideError();
    setStatus("已停止采集");
    mFps.set("--");
    mPresence.set("--");
    mMotion.set("--");
    meterFill.style.width = "0%";
    updateButtons();
  }

  async function restart(): Promise<void> {
    await stop();
    await start();
  }

  async function doManualToggle(): Promise<void> {
    try {
      await manualToggle();
    } catch (e) {
      setStatus(String(e));
    }
  }

  async function doDiscard(): Promise<void> {
    try {
      await discardClip();
      setStatus("已丢弃，重新倒计时待挥棒");
    } catch (e) {
      setStatus(String(e));
    }
  }

  // ---- 相机源 ----
  async function populateCameras(): Promise<void> {
    let devices: VideoDevice[] = [];
    try {
      devices = await listDevices();
    } catch {
      devices = [];
    }
    const prev = store.settings?.camera_name ?? "";
    camSelect.textContent = "";
    for (const d of devices) {
      const opt = document.createElement("option");
      opt.value = d.name;
      opt.textContent = d.is_stereo_module ? `${d.name}（双目模组）` : d.name;
      opt.dataset.index = String(d.index);
      camSelect.append(opt);
    }
    if (devices.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "未检测到相机";
      camSelect.append(opt);
    }
    const idx = devices.findIndex((d) => d.name === prev);
    if (idx < 0 && prev) {
      // 已配置相机未连接：占位项保留显示，不静默改成别的设备
      const opt = document.createElement("option");
      opt.value = prev;
      opt.textContent = `${prev}（未连接）`;
      opt.dataset.index = String(store.settings?.camera_index ?? 0);
      camSelect.append(opt);
      camSelect.selectedIndex = camSelect.length - 1;
    } else {
      camSelect.selectedIndex =
        idx >= 0 ? idx : Math.max(0, devices.findIndex((d) => d.is_stereo_module));
    }
  }

  async function onCameraChanged(): Promise<void> {
    const opt = camSelect.selectedOptions[0];
    if (!opt || !opt.value || !store.settings) return;
    if (opt.value === store.settings.camera_name) return;
    store.settings.camera_name = opt.value;
    store.settings.camera_index = Number(opt.dataset.index ?? 0);
    await saveSettings(store.settings).catch(() => setStatus("相机源保存失败"));
    setStatus(`相机源：${opt.value}`);
    if (capturing) await restart();
  }

  // ---- 静音 ----
  async function onMute(muted: boolean): Promise<void> {
    if (store.settings) {
      store.settings.voice_enabled = !muted;
      void saveSettings(store.settings).catch(() => undefined);
    }
    try {
      await setMuted(muted);
    } catch {
      /* 未在采集时无语音通道，设置已落盘下次生效 */
    }
    setStatus(muted ? "语音已静音" : "语音已开启");
  }

  // ---- 快捷键（空格=手动开始/结束，D=丢弃重拍；仅本页激活时） ----
  function onKeydown(e: KeyboardEvent): void {
    const target = e.target as HTMLElement;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
    if (e.code === "Space") {
      e.preventDefault();
      void doManualToggle();
    } else if (e.code === "KeyD") {
      void doDiscard();
    }
  }

  // ---- 数据 ----
  async function refreshCounts(): Promise<void> {
    try {
      const items = await listSessions();
      mTotal.set(String(items.length));
      mPass.set(String(items.filter((s) => s.verdict === "合格").length));
    } catch {
      /* 素材库不可达时保持旧值 */
    }
  }

  // ---- 初始化 ----
  function syncFromSettings(): void {
    const s = store.settings;
    if (!s) return;
    muteSwitch.querySelector("input")!.checked = !s.voice_enabled;
    meterTick.style.left = `${Math.min((s.motion_trigger_pct / METER_MAX_PCT) * 100, 100)}%`;
    refreshRoiHint();
    drawOverlay();
    void populateCameras();
  }

  function init(): void {
    updateButtons();
    render();
    syncFromSettings();
    void refreshCounts();
  }
  init();

  return {
    node,
    onShow: () => {
      window.addEventListener("keydown", onKeydown);
      void refreshCounts();
    },
    onHide: () => {
      window.removeEventListener("keydown", onKeydown);
    },
    onSettings: syncFromSettings,
  };
}
