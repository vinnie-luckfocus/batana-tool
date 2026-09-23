// 通用 DOM 构建工具：el() 造节点、card/section/pill/segmented 等规范组件。
// 视觉全部走 tokens.css 变量（见 styles/app.css），此处只搭结构。

type Child = Node | string | null | undefined;
type Attrs = Record<string, unknown>;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false || v === null) continue;
    if (k === "onclick") node.addEventListener("click", v as EventListener);
    else if (k === "oninput") node.addEventListener("input", v as EventListener);
    else if (k === "onchange") node.addEventListener("change", v as EventListener);
    else if (k === "class") node.className = String(v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, String(v));
  }
  for (const c of children) {
    if (c == null) continue;
    node.append(c as Node | string);
  }
  return node;
}

export function card(...children: Child[]): HTMLElement {
  return el("section", { class: "card" }, ...children);
}

export function sectionHeader(text: string): HTMLElement {
  return el("h3", { class: "section-header" }, text);
}

export type PillTone = "accent" | "green" | "red" | "orange" | "yellow" | "dim";

/** 状态 pill：语义色 12% 底 + 语义色文字（规范 4）。 */
export function pill(text: string, tone: PillTone = "dim"): HTMLElement {
  return el("span", { class: `pill pill-${tone}` }, text);
}

export function setPill(node: HTMLElement, text: string, tone: PillTone): void {
  node.textContent = text;
  node.className = `pill pill-${tone}`;
}

export interface SegmentedOption {
  label: string;
  value: string;
}

/** 分段控件：互斥视图切换（规范 4）。返回容器与取值/设值器。 */
export function segmented(
  options: SegmentedOption[],
  initial: string,
  onChange: (value: string) => void,
): { node: HTMLElement; get: () => string; set: (v: string) => void } {
  let current = initial;
  const buttons = new Map<string, HTMLButtonElement>();
  const node = el("div", { class: "segmented", role: "tablist" });
  const refresh = () => {
    for (const [v, b] of buttons) b.classList.toggle("on", v === current);
  };
  for (const opt of options) {
    const b = el(
      "button",
      {
        class: "segment",
        role: "tab",
        type: "button",
        onclick: () => {
          if (current === opt.value) return;
          current = opt.value;
          refresh();
          onChange(opt.value);
        },
      },
      opt.label,
    );
    buttons.set(opt.value, b);
    node.append(b);
  }
  refresh();
  return {
    node,
    get: () => current,
    set: (v: string) => {
      current = v;
      refresh();
    },
  };
}

/** macOS 风格开关（checkbox 语义，键盘可达）。 */
export function switchControl(checked: boolean, onChange: (on: boolean) => void): HTMLLabelElement {
  const input = el("input", { type: "checkbox" });
  input.checked = checked;
  input.addEventListener("change", () => onChange(input.checked));
  return el("label", { class: "switch" }, input, el("span", { class: "switch-track" }));
}

export function button(
  label: string,
  kind: "primary" | "secondary" | "danger" = "secondary",
  onclick: () => void,
): HTMLButtonElement {
  return el("button", { class: `btn btn-${kind}`, type: "button", onclick }, label);
}

/** 遥测数值块：次级小标签 + 等宽数字读数。 */
export function telemetryValue(label: string, accent = false): { node: HTMLElement; set: (v: string) => void } {
  const value = el("div", { class: `tele-value num${accent ? " tele-accent" : ""}` }, "--");
  const node = el("div", { class: "tele" }, el("div", { class: "tele-label" }, label), value);
  return { node, set: (v: string) => (value.textContent = v) };
}

export function fmtLocalTime(isoUtc: string): string {
  const d = new Date(isoUtc);
  if (Number.isNaN(d.getTime())) return isoUtc;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
