// 设置页：分组表单卡片（采集模式 / 检测阈值 / 语音 / 存储 / 环境检查）。
// 标签右对齐、控件列弹性拉伸（规范 4 表单）；「保存设置」落盘 settings.json。
import { el, card, sectionHeader, button, switchControl } from "./components";
import {
  AppSettings,
  VideoDevice,
  getSettings,
  listDevices,
  saveSettings,
  store,
} from "./ipc";
import type { AppContext, Page } from "./app";

const RESOLUTIONS: Array<[string, number, number]> = [
  ["1280x400", 1280, 400],
  ["2560x720", 2560, 720],
  ["1600x600", 1600, 600],
];

type NumField = {
  kind: "number";
  key: keyof AppSettings;
  label: string;
  min: number;
  max: number;
  step: number;
  suffix?: string;
  tip?: string;
};
type TextField = { kind: "text"; key: keyof AppSettings; label: string; placeholder?: string };

const DETECT_FIELDS: NumField[] = [
  { kind: "number", key: "presence_ratio", label: "就位占比阈值", min: 0.001, max: 1, step: 0.005 },
  { kind: "number", key: "motion_trigger_pct", label: "挥棒触发（运动占比）", min: 0.1, max: 50, step: 0.1, suffix: "%", tip: "显著运动像素占 ROI 比例超过此值判定挥棒开始" },
  { kind: "number", key: "motion_release_pct", label: "挥棒回落（运动占比）", min: 0.1, max: 50, step: 0.1, suffix: "%", tip: "运动占比低于此值并持续 POST-ROLL 判定挥棒结束" },
  { kind: "number", key: "motion_pix_thresh", label: "运动像素阈值", min: 1, max: 100, step: 1 },
  { kind: "number", key: "pre_roll_seconds", label: "PRE-ROLL（秒）", min: 0, max: 10, step: 0.5 },
  { kind: "number", key: "post_roll_seconds", label: "POST-ROLL（秒）", min: 0.1, max: 10, step: 0.5 },
  { kind: "number", key: "countdown_seconds", label: "倒计时（秒）", min: 0, max: 10, step: 0.5 },
  { kind: "number", key: "buffer_seconds", label: "预录缓冲（秒）", min: 1, max: 30, step: 0.5 },
];

const STORAGE_FIELDS: TextField[] = [
  { kind: "text", key: "storage_root", label: "存储根目录" },
  { kind: "text", key: "pose_model_path", label: "POSE 模型文件", placeholder: "留空 = 未配置骨架估计" },
  { kind: "text", key: "core_repo_path", label: "CORE 仓路径", placeholder: "留空 = 自动探测常见位置" },
];

const ENV_FIELDS: NumField[] = [
  { kind: "number", key: "env_brightness_fail", label: "亮度不合格下限", min: 0, max: 255, step: 1 },
  { kind: "number", key: "env_brightness_warn", label: "亮度警告下限", min: 0, max: 255, step: 1 },
  { kind: "number", key: "env_flicker_warn_pct", label: "频闪警告阈值", min: 0.1, max: 50, step: 0.1, suffix: "%" },
  { kind: "number", key: "env_flicker_fail_pct", label: "频闪不合格阈值", min: 0.5, max: 100, step: 0.1, suffix: "%" },
  { kind: "number", key: "env_sharpness_warn", label: "清晰度阈值", min: 1, max: 1000, step: 1 },
  { kind: "number", key: "env_level_warn_deg", label: "水平警告（度）", min: 0.1, max: 30, step: 0.1, suffix: "°" },
  { kind: "number", key: "env_level_fail_deg", label: "水平不合格（度）", min: 0.5, max: 45, step: 0.1, suffix: "°" },
  { kind: "number", key: "env_planned_clips", label: "计划采集段数", min: 1, max: 100000, step: 1 },
  { kind: "number", key: "env_est_mb_per_clip", label: "单段估算（MB）", min: 1, max: 10000, step: 10 },
];

export function createSettingsPage(_ctx: AppContext): Page {
  let draft: AppSettings | null = null;

  // ---- 控件引用 ----
  const resSelect = el("select", {}) as HTMLSelectElement;
  const fpsInput = el("input", { type: "number", min: "1", max: "240", step: "1" }) as HTMLInputElement;
  const camSelect = el("select", {}) as HTMLSelectElement;
  const numInputs = new Map<keyof AppSettings, HTMLInputElement>();
  const textInputs = new Map<keyof AppSettings, HTMLInputElement>();
  let voiceSwitch: HTMLInputElement | null = null;
  const rateInput = el("input", { type: "number", min: "80", max: "400", step: "10" }) as HTMLInputElement;

  const feedback = el("span", { class: "save-feedback" }, "已保存");
  const saveBtn = button("保存设置", "primary", () => void save());

  // ---- 行构建 ----
  function numRow(f: NumField): HTMLElement {
    const input = el("input", {
      type: "number",
      min: String(f.min),
      max: String(f.max),
      step: String(f.step),
    }) as HTMLInputElement;
    if (f.tip) input.title = f.tip;
    numInputs.set(f.key, input);
    return el(
      "div",
      { class: "form-row" },
      el("label", {}, f.label),
      el("div", { class: "field" }, input, f.suffix ? el("span", { class: "field-suffix" }, f.suffix) : null),
    );
  }

  function textRow(f: TextField): HTMLElement {
    const input = el("input", { type: "text", placeholder: f.placeholder ?? "" }) as HTMLInputElement;
    textInputs.set(f.key, input);
    return el("div", { class: "form-row" }, el("label", {}, f.label), el("div", { class: "field" }, input));
  }

  function row(label: string, ...fieldChildren: (Node | string | null)[]): HTMLElement {
    return el("div", { class: "form-row" }, el("label", {}, label), el("div", { class: "field" }, ...fieldChildren));
  }

  const refreshCamBtn = button("刷新", "secondary", () => void populateCameras());

  const node = el(
    "div",
    { class: "page settings-page" },
    el(
      "div",
      { class: "settings-inner" },
      card(
        sectionHeader("采集模式"),
        row("分辨率", resSelect),
        row("帧率", fpsInput, el("span", { class: "field-suffix" }, "FPS")),
        row("相机设备", camSelect, refreshCamBtn),
      ),
      card(sectionHeader("检测阈值"), ...DETECT_FIELDS.map(numRow)),
      (() => {
        const sw = switchControl(true, () => undefined);
        voiceSwitch = sw.querySelector("input");
        return card(
          sectionHeader("语音"),
          row("语音开关", sw, el("span", { class: "field-suffix" }, "启用语音引导")),
          row("语速", rateInput, el("span", { class: "field-suffix" }, "词/分")),
        );
      })(),
      card(sectionHeader("存储"), ...STORAGE_FIELDS.map(textRow)),
      card(sectionHeader("环境检查"), ...ENV_FIELDS.map(numRow)),
      el("div", { class: "settings-footer" }, feedback, saveBtn),
    ),
  );

  // ---- 数据装载 ----
  function fill(s: AppSettings): void {
    draft = { ...s };
    // 分辨率：不在预设档位时插入当前值
    const cur = `${s.capture_width}x${s.capture_height}`;
    resSelect.textContent = "";
    const options = RESOLUTIONS.map(([label]) => label);
    if (!options.includes(cur)) options.push(cur);
    for (const label of options) {
      const o = document.createElement("option");
      o.value = label;
      o.textContent = label;
      resSelect.append(o);
    }
    resSelect.value = cur;
    fpsInput.value = String(s.capture_fps);
    rateInput.value = String(s.voice_rate);
    if (voiceSwitch) voiceSwitch.checked = s.voice_enabled;
    for (const f of DETECT_FIELDS.concat(ENV_FIELDS)) {
      numInputs.get(f.key)!.value = String(s[f.key]);
    }
    for (const f of STORAGE_FIELDS) {
      textInputs.get(f.key)!.value = String(s[f.key]);
    }
    void populateCameras();
  }

  async function populateCameras(): Promise<void> {
    let devices: VideoDevice[] = [];
    try {
      devices = await listDevices();
    } catch {
      devices = [];
    }
    const prev = draft?.camera_name ?? "";
    camSelect.textContent = "";
    for (const d of devices) {
      const o = document.createElement("option");
      o.value = d.name;
      o.textContent = d.is_stereo_module ? `${d.name}（双目模组）` : d.name;
      o.dataset.index = String(d.index);
      camSelect.append(o);
    }
    const idx = devices.findIndex((d) => d.name === prev);
    if (idx < 0 && prev) {
      // 已配置相机当前未连接：保留为占位项并选中，避免「打开设置页→保存」静默改写相机源
      const o = document.createElement("option");
      o.value = prev;
      o.textContent = `${prev}（未连接）`;
      o.dataset.index = String(draft?.camera_index ?? 0);
      camSelect.append(o);
      camSelect.selectedIndex = camSelect.length - 1;
    } else {
      camSelect.selectedIndex = Math.max(idx, 0);
    }
    if (devices.length === 0 && !prev) {
      const o = document.createElement("option");
      o.value = "";
      o.textContent = "未检测到相机";
      camSelect.append(o);
    }
  }

  // ---- 保存 ----
  async function save(): Promise<void> {
    if (!draft) return;
    const s = draft;
    const [w, h] = resSelect.value.split("x").map(Number);
    if (w && h) {
      s.capture_width = w;
      s.capture_height = h;
    }
    s.capture_fps = Number(fpsInput.value) || s.capture_fps;
    s.voice_rate = Number(rateInput.value) || s.voice_rate;
    s.voice_enabled = voiceSwitch?.checked ?? s.voice_enabled;
    const camOpt = camSelect.selectedOptions[0];
    if (camOpt?.value) {
      s.camera_name = camOpt.value;
      s.camera_index = Number(camOpt.dataset.index ?? s.camera_index);
    }
    for (const f of DETECT_FIELDS.concat(ENV_FIELDS)) {
      const v = Number(numInputs.get(f.key)!.value);
      if (!Number.isNaN(v)) (s[f.key] as number) = v;
    }
    for (const f of STORAGE_FIELDS) {
      (s[f.key] as string) = textInputs.get(f.key)!.value;
    }
    try {
      await saveSettings(s);
      store.settings = s;
      feedback.textContent = "已保存（采集参数下次开始采集生效）";
      feedback.classList.add("show");
      window.setTimeout(() => feedback.classList.remove("show"), 4000);
    } catch (e) {
      feedback.textContent = `保存失败：${String(e)}`;
      feedback.style.color = "var(--red)";
      feedback.classList.add("show");
      window.setTimeout(() => {
        feedback.classList.remove("show");
        feedback.style.color = "";
      }, 3500);
    }
  }

  return {
    node,
    onShow: () => {
      // 每次进入重新拉取（采集页可能刚改了 ROI / 相机源）
      void getSettings()
        .then((s) => {
          store.settings = s;
          fill(s);
        })
        .catch(() => {
          if (store.settings) fill(store.settings);
        });
    },
    onSettings: () => {
      if (store.settings) fill(store.settings);
    },
  };
}
