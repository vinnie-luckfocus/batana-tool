// 应用骨架：左侧导航（图标+文字）+ 三页（采集 / 素材复核 / 设置）。
// 窗口为透明 + NSVisualEffectView 侧栏材质，侧栏不铺底色让毛玻璃透出（规范第 2 节）。
import { el } from "./components";
import { loadSettings } from "./ipc";
import { createCapturePage } from "./capture";
import { createReviewPage } from "./review";
import { createSettingsPage } from "./settings";

export interface Page {
  node: HTMLElement;
  onShow?: () => void;
  onHide?: () => void;
  /** 设置异步加载完成后回调（页面先于设置挂载，settings 到达时刷新依赖项） */
  onSettings?: () => void;
}

export interface AppContext {
  navigate: (id: string) => void;
}

// 16px 描边图标（几何线条，不引外部素材）
const ICONS: Record<string, string> = {
  capture:
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="4" width="13" height="9" rx="2"/><circle cx="8" cy="8.5" r="2.6"/><circle cx="12" cy="6.2" r="0.6" fill="currentColor" stroke="none"/></svg>',
  review:
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><rect x="1.5" y="2.5" width="13" height="11" rx="2"/><path d="M6.5 5.8v4.4l4-2.2z" fill="currentColor" stroke="none"/></svg>',
  settings:
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="8" r="2.2"/><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.9 3.9l1.4 1.4M10.7 10.7l1.4 1.4M12.1 3.9l-1.4 1.4M5.3 10.7l-1.4 1.4"/></svg>',
};

const NAV = [
  { id: "capture", label: "采集" },
  { id: "review", label: "素材复核" },
  { id: "settings", label: "设置" },
];

export function mountApp(root: HTMLElement): void {
  const sidebar = el(
    "nav",
    { class: "sidebar" },
    el("div", { class: "sidebar-drag", "data-tauri-drag-region": "" }),
    el(
      "div",
      { class: "brand", "data-tauri-drag-region": "" },
      el("div", { class: "brand-name", "data-tauri-drag-region": "" }, "BatanaTool"),
      el("div", { class: "brand-sub", "data-tauri-drag-region": "" }, "挥棒素材采集与标注"),
    ),
  );
  const navList = el("div", { class: "nav-list" });
  sidebar.append(navList);

  const pagesHost = el("main", { class: "pages" });
  const shell = el("div", { class: "shell" }, sidebar, el("div", { class: "content" }, el("div", { class: "drag-strip", "data-tauri-drag-region": "" }), pagesHost));
  root.append(shell);

  const pages = new Map<string, Page>();
  const navButtons = new Map<string, HTMLButtonElement>();
  let currentId = "";

  const navigate = (id: string) => {
    if (id === currentId || !pages.has(id)) return;
    const prev = currentId ? pages.get(currentId) : undefined;
    prev?.onHide?.();
    prev?.node.classList.remove("active");
    currentId = id;
    const next = pages.get(id)!;
    next.node.classList.add("active");
    next.onShow?.();
    for (const [k, b] of navButtons) b.classList.toggle("on", k === id);
  };
  const ctx: AppContext = { navigate };

  for (const item of NAV) {
    const b = el(
      "button",
      { class: "nav-item", type: "button", onclick: () => navigate(item.id) },
      el("span", { class: "nav-icon" }),
      el("span", {}, item.label),
    );
    (b.firstElementChild as HTMLElement).innerHTML = ICONS[item.id];
    navButtons.set(item.id, b);
    navList.append(b);
  }

  // 页面立即挂载（不阻塞在设置加载上：WebView 重载后 IPC 可能延迟，
  // 各页对 store.settings 为 null 均有兜底）；设置到达后回调各页刷新依赖项。
  const capture = createCapturePage(ctx);
  const review = createReviewPage(ctx);
  const settings = createSettingsPage(ctx);
  pages.set("capture", capture);
  pages.set("review", review);
  pages.set("settings", settings);
  for (const p of pages.values()) pagesHost.append(p.node);
  navigate("capture");

  loadSettings()
    .catch(() => null)
    .then((s) => {
      if (!s) return;
      for (const p of pages.values()) p.onSettings?.();
    });
}
