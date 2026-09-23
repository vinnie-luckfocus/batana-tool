// 素材复核页：素材列表（筛选 + 状态 pill）+ 视频回放 + 骨架 overlay + 三段式标记 + 删除。
// 交互语义对齐 Python 旧版 review_page.py；FFV1/MKV 母版经后端 ensure_preview_video
// 转码为 H.264 预览 mp4 后由 <video> 经 asset 协议播放（WKWebView 不解 FFV1）。
import { el, card, sectionHeader, button, pill, setPill, segmented, fmtLocalTime } from "./components";
import type { PillTone } from "./components";
import {
  Pose2dFile,
  SessionSummary,
  convertFileSrc,
  deleteSession,
  ensurePreviewVideo,
  listSessions,
  markSession,
  readPose2d,
  STATUS_FAIL,
  STATUS_PASS,
  STATUS_REVIEW,
} from "./ipc";
import type { AppContext, Page } from "./app";

const VERDICT_TONE: Record<string, PillTone> = {
  [STATUS_PASS]: "green",
  [STATUS_FAIL]: "red",
  [STATUS_REVIEW]: "orange",
};

const SPEEDS: Array<[string, number]> = [
  ["0.25×", 0.25],
  ["0.5×", 0.5],
  ["1×", 1.0],
];

// BlazePose 骨架连线（子集：躯干 + 四肢主干）
const BONES: Array<[number, number]> = [
  [11, 12], [11, 13], [13, 15], [12, 14], [14, 16],
  [11, 23], [12, 24], [23, 24],
  [23, 25], [25, 27], [24, 26], [26, 28],
  [0, 11], [0, 12],
];

type Eye = "left" | "right";

export function createReviewPage(ctx: AppContext): Page {
  let sessions: SessionSummary[] = [];
  let current: SessionSummary | null = null;
  let filter = "全部";
  let eye: Eye = "left";
  let pose: Pose2dFile | null = null;
  let poseOn = false;
  let loadSeq = 0; // 防止旧选择异步覆盖新选择
  let deleteArmTimer = 0;

  // ---- DOM ----
  const filterSelect = el(
    "select",
    {},
    ...["全部", STATUS_PASS, STATUS_REVIEW, STATUS_FAIL].map((s) => {
      const o = document.createElement("option");
      o.value = s;
      o.textContent = s;
      return o;
    }),
  ) as HTMLSelectElement;
  filterSelect.addEventListener("change", () => {
    filter = filterSelect.value;
    renderList();
  });

  const clipList = el("div", { class: "clip-list" });
  const listEmpty = el(
    "div",
    { class: "list-empty", style: "display:none" },
    el("div", { class: "t" }, "暂无素材"),
    el("div", { class: "d" }, "先到「采集」页录制挥棒片段，保存后会出现在这里。"),
    button("前往采集页", "primary", () => ctx.navigate("capture")),
  );

  const titleEl = el("span", { class: "player-title dim" }, "未选择素材");
  const verdictPill = pill("—", "dim");
  verdictPill.style.display = "none";
  const eyeSeg = segmented(
    [
      { label: "左目", value: "left" },
      { label: "右目", value: "right" },
    ],
    "left",
    (v) => {
      eye = v as Eye;
      void loadVideo();
    },
  );
  eyeSeg.node.style.display = "none";
  const poseToggle = button("骨架", "secondary", () => {
    poseOn = !poseOn;
    poseToggle.classList.toggle("on", poseOn);
    drawPose();
  });
  poseToggle.style.display = "none";

  const video = el("video", { muted: true, playsinline: true, preload: "auto" }) as HTMLVideoElement;
  video.style.display = "none";
  const poseCv = el("canvas", { class: "pose-cv" }) as HTMLCanvasElement;
  const videoMsg = el("div", { class: "video-empty" }, el("span", {}, "从左侧列表选择一段素材"));
  const videoWrap = el("div", { class: "video-wrap" }, video, poseCv, videoMsg);

  const btnPlay = button("播放", "secondary", togglePlay);
  const btnPrev = button("-1 帧", "secondary", () => step(-1));
  const btnNext = button("+1 帧", "secondary", () => step(1));
  const slider = el("input", { type: "range", min: "0", max: "0", step: "1", value: "0" }) as HTMLInputElement;
  const frameLabel = el("span", { class: "frame-label" }, "帧 --/--");
  const speedSelect = el(
    "select",
    {},
    ...SPEEDS.map(([label]) => {
      const o = document.createElement("option");
      o.value = label;
      o.textContent = label;
      return o;
    }),
  ) as HTMLSelectElement;
  speedSelect.selectedIndex = 2;
  speedSelect.addEventListener("change", () => {
    video.playbackRate = SPEEDS[speedSelect.selectedIndex]?.[1] ?? 1;
  });

  const transport = el(
    "div",
    { class: "transport" },
    btnPlay,
    btnPrev,
    btnNext,
    slider,
    frameLabel,
    speedSelect,
  );

  const markButtons = new Map<string, HTMLButtonElement>();
  const markRow = el("div", { class: "mark-row" }, el("span", { class: "ctl-label" }, "标记"));
  for (const s of [STATUS_PASS, STATUS_REVIEW, STATUS_FAIL]) {
    const b = button(s, "secondary", () => void mark(s));
    markButtons.set(s, b);
    markRow.append(b);
  }
  const btnDelete = button("删除素材", "danger", () => void onDeleteClicked());
  markRow.append(el("span", { class: "spacer" }), btnDelete);

  const node = el(
    "div",
    { class: "page review-page" },
    el(
      "div",
      { class: "review-side" },
      sectionHeader("素材"),
      filterSelect,
      clipList,
      listEmpty,
    ),
    el(
      "div",
      { class: "review-main" },
      (() => {
        const c = card(
          el("div", { class: "player-head" }, titleEl, verdictPill, el("span", { class: "spacer" }), poseToggle, eyeSeg.node),
          videoWrap,
        );
        c.classList.add("player-card");
        return c;
      })(),
      card(transport),
      card(markRow),
    ),
  );

  // ---- 列表 ----
  async function refresh(): Promise<void> {
    try {
      sessions = await listSessions();
    } catch {
      sessions = [];
    }
    // 重拉后 current 指向旧数组对象，重新挂到新数组同 id 记录（标记/筛选才能一致）
    if (current) {
      const rec = sessions.find((x) => x.id === current!.id);
      if (rec) current = rec;
    }
    renderList();
  }

  function renderList(): void {
    clipList.textContent = "";
    const items = sessions.filter((s) => filter === "全部" || s.verdict === filter);
    listEmpty.style.display = items.length === 0 ? "" : "none";
    for (const s of items) {
      const item = el(
        "div",
        { class: `clip-item${current?.id === s.id ? " on" : ""}` },
        el(
          "div",
          { class: "clip-item-top" },
          el("span", { class: "clip-item-title" }, `#${s.seq} · ${fmtLocalTime(s.created_at)}`),
          pill(s.verdict, VERDICT_TONE[s.verdict] ?? "dim"),
        ),
        el(
          "div",
          { class: "clip-item-sub num" },
          `${(s.frame_count / Math.max(s.fps, 1)).toFixed(1)}s · ${s.frame_count} 帧 @ ${s.fps}fps`,
        ),
      );
      item.addEventListener("click", () => select(s));
      clipList.append(item);
    }
  }

  // ---- 选择与播放 ----
  async function select(s: SessionSummary): Promise<void> {
    current = s;
    renderList();
    titleEl.textContent = `${s.id} · ${fmtLocalTime(s.created_at)}`;
    titleEl.classList.remove("dim");
    setPill(verdictPill, s.verdict, VERDICT_TONE[s.verdict] ?? "dim");
    verdictPill.style.display = "";
    eyeSeg.node.style.display = "";
    refreshMarkButtons();
    disarmDelete();
    pose = null;
    poseOn = false;
    poseToggle.classList.remove("on");
    poseToggle.style.display = "none";
    void loadPose(s.id);
    await loadVideo();
  }

  async function loadPose(id: string): Promise<void> {
    try {
      const p = await readPose2d(id);
      if (current?.id !== id) return;
      pose = p;
      if (p && p.frames.length > 0) {
        poseToggle.style.display = "";
        poseOn = true;
        poseToggle.classList.add("on");
        drawPose();
      }
    } catch {
      /* 无骨架数据 */
    }
  }

  async function loadVideo(): Promise<void> {
    if (!current) return;
    const seq = ++loadSeq;
    const id = current.id;
    videoMsg.textContent = "正在准备预览…";
    videoMsg.style.display = "";
    video.style.display = "none";
    try {
      const path = await ensurePreviewVideo(id, eye);
      if (seq !== loadSeq) return; // 已切走
      video.src = convertFileSrc(path);
      video.style.display = "";
      videoMsg.style.display = "none";
      video.load();
      slider.max = String(Math.max(current.frame_count - 1, 0));
      updateFrameUi();
    } catch (e) {
      if (seq !== loadSeq) return;
      videoMsg.textContent = `预览不可用：${String(e)}`;
      videoMsg.style.display = "";
    }
  }

  function currentFrame(): number {
    if (!current) return 0;
    return Math.min(Math.round(video.currentTime * current.fps), current.frame_count - 1);
  }

  function seekFrame(f: number): void {
    if (!current) return;
    const clamped = Math.min(Math.max(f, 0), current.frame_count - 1);
    video.currentTime = (clamped + 0.5) / current.fps;
    updateFrameUi(clamped);
  }

  function updateFrameUi(f?: number): void {
    if (!current) {
      frameLabel.textContent = "帧 --/--";
      return;
    }
    const frame = f ?? currentFrame();
    slider.value = String(frame);
    frameLabel.textContent = `帧 ${frame + 1}/${current.frame_count}`;
    drawPose();
  }

  function togglePlay(): void {
    if (!current || !video.src) return;
    if (video.paused) {
      // 播到末尾后再次播放从头开始
      if (current && video.currentTime >= video.duration - 0.01) video.currentTime = 0;
      void video.play();
    } else {
      video.pause();
    }
  }

  function step(delta: number): void {
    if (!current) return;
    video.pause();
    seekFrame(currentFrame() + delta);
  }

  video.addEventListener("play", () => (btnPlay.textContent = "暂停"));
  video.addEventListener("pause", () => (btnPlay.textContent = "播放"));
  video.addEventListener("timeupdate", () => {
    if (!video.paused) updateFrameUi();
  });
  video.addEventListener("ended", () => (btnPlay.textContent = "播放"));
  video.addEventListener("loadedmetadata", () => updateFrameUi());
  slider.addEventListener("input", () => {
    if (!current) return;
    video.pause();
    seekFrame(Number(slider.value));
  });

  // ---- 骨架 overlay ----
  new ResizeObserver(() => {
    syncPoseCanvas();
    drawPose();
  }).observe(videoWrap);

  function syncPoseCanvas(): void {
    const r = videoWrap.getBoundingClientRect();
    poseCv.width = Math.max(1, Math.round(r.width));
    poseCv.height = Math.max(1, Math.round(r.height));
  }

  /** object-fit: contain 下视频内容的实际显示矩形 */
  function videoContentRect(): { x: number; y: number; w: number; h: number } {
    const cw = poseCv.width;
    const ch = poseCv.height;
    const vw = video.videoWidth || 640;
    const vh = video.videoHeight || 400;
    const scale = Math.min(cw / vw, ch / vh);
    const w = vw * scale;
    const h = vh * scale;
    return { x: (cw - w) / 2, y: (ch - h) / 2, w, h };
  }

  function drawPose(): void {
    const g = poseCv.getContext("2d");
    if (!g) return;
    g.clearRect(0, 0, poseCv.width, poseCv.height);
    if (!poseOn || !pose || !current || eye !== "left") return;
    const frame = currentFrame();
    // pose frames 的 frame_index 与片内帧号一致；找最近帧
    let best: Pose2dFile["frames"][number] | null = null;
    for (const f of pose.frames) {
      if (f.frame_index <= frame) best = f;
      else break;
    }
    if (!best) return;
    const rect = videoContentRect();
    const px = (nx: number) => rect.x + nx * rect.w;
    const py = (ny: number) => rect.y + ny * rect.h;
    g.strokeStyle = "rgba(48, 209, 88, 0.8)";
    g.lineWidth = 1.5;
    for (const [a, b] of BONES) {
      const ka = best.keypoints[a];
      const kb = best.keypoints[b];
      if (!ka || !kb || ka.visibility < 0.3 || kb.visibility < 0.3) continue;
      g.beginPath();
      g.moveTo(px(ka.x), py(ka.y));
      g.lineTo(px(kb.x), py(kb.y));
      g.stroke();
    }
    for (const kp of best.keypoints) {
      if (kp.visibility < 0.3) continue;
      g.beginPath();
      g.arc(px(kp.x), py(kp.y), 2.5, 0, Math.PI * 2);
      g.fillStyle = kp.manual ? "#ff9f0a" : "#30d158";
      g.fill();
    }
  }

  // ---- 标记 / 删除 ----
  function refreshMarkButtons(): void {
    for (const [s, b] of markButtons) b.classList.toggle("on", current?.verdict === s);
  }

  async function mark(status: string): Promise<void> {
    if (!current) return;
    try {
      await markSession(current.id, status);
      current.verdict = status;
      // 列表数据可能已被 refresh() 整体替换（onShow 重拉），同步新数组里的同 id 记录
      const rec = sessions.find((x) => x.id === current!.id);
      if (rec) rec.verdict = status;
      setPill(verdictPill, status, VERDICT_TONE[status] ?? "dim");
      refreshMarkButtons();
      renderList();
    } catch (e) {
      titleEl.textContent = `标记失败：${String(e)}`;
    }
  }

  function disarmDelete(): void {
    window.clearTimeout(deleteArmTimer);
    btnDelete.textContent = "删除素材";
  }

  async function onDeleteClicked(): Promise<void> {
    if (!current) return;
    if (btnDelete.textContent === "删除素材") {
      // 破坏操作二次确认（规范 4）：第一次点击进入待确认态，4 秒后还原
      btnDelete.textContent = "再次点击确认删除";
      deleteArmTimer = window.setTimeout(disarmDelete, 4000);
      return;
    }
    disarmDelete();
    const id = current.id;
    try {
      await deleteSession(id);
      current = null;
      video.pause();
      video.removeAttribute("src");
      video.load();
      video.style.display = "none";
      videoMsg.textContent = "从左侧列表选择一段素材";
      videoMsg.style.display = "";
      titleEl.textContent = "未选择素材";
      titleEl.classList.add("dim");
      verdictPill.style.display = "none";
      eyeSeg.node.style.display = "none";
      poseToggle.style.display = "none";
      pose = null;
      poseOn = false;
      updateFrameUi();
      await refresh();
    } catch (e) {
      titleEl.textContent = `删除失败：${String(e)}`;
    }
  }

  // ---- 快捷键：空格播放/暂停，←/→ 逐帧（仅本页激活时） ----
  function onKeydown(e: KeyboardEvent): void {
    const target = e.target as HTMLElement;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
    if (!current) return;
    if (e.code === "Space") {
      e.preventDefault();
      togglePlay();
    } else if (e.code === "ArrowLeft") {
      e.preventDefault();
      step(-1);
    } else if (e.code === "ArrowRight") {
      e.preventDefault();
      step(1);
    }
  }

  return {
    node,
    onShow: () => {
      window.addEventListener("keydown", onKeydown);
      void refresh();
    },
    onHide: () => {
      window.removeEventListener("keydown", onKeydown);
      video.pause();
    },
  };
}
