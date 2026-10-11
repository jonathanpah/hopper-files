// Shared kit of the browser suite: a synthetic instance per test file, WebKit (the Safari engine)
// through playwright-core, device profiles, login, and an automatic layout audit.
//
// Environment:
//   HOPPER_FILES_LOCKED_SITE      directory with the locked Python dependencies (docs/development.md)
//   HOPPER_FILES_PYTHON           Python 3.13 interpreter; default "python3"
//   HOPPER_FILES_FIXTURE_ROOT     where fixture directories are created; default the system temporary directory
//   HOPPER_FILES_FIXTURE_WRAPPER  optional command that runs the fixture server, e.g. a sandbox; it receives
//                                 the fixture directory in HOPPER_FILES_FIXTURE_DIR
//   PLAYWRIGHT_BROWSERS_PATH      where `npx playwright-core install webkit` put the browser
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { webkit } from "playwright-core";

const REPOSITORY = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SAFARI_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1";
// Safari on iPad presents itself as the Mac one and uses the window width.
const SAFARI_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15";

export const PROFILES = {
  desktop: {viewport: {width: 1440, height: 900}, userAgent: SAFARI_MAC},
  laptop: {viewport: {width: 1280, height: 720}, userAgent: SAFARI_MAC},
  "ipad-landscape": {viewport: {width: 1180, height: 820}, deviceScaleFactor: 2, hasTouch: true, userAgent: SAFARI_MAC},
  "ipad-portrait": {viewport: {width: 820, height: 1180}, deviceScaleFactor: 2, hasTouch: true, userAgent: SAFARI_MAC},
  iphone: {viewport: {width: 390, height: 844}, deviceScaleFactor: 3, hasTouch: true, isMobile: true, userAgent: SAFARI_IPHONE},
};

let browser = null;
export async function closeBrowser() { if (browser) { await browser.close(); browser = null; } }

// Starts scripts/browser_fixture.py in a new directory and resolves once it serves, with its url,
// username, password, and documents folder. `stop()` ends the server and removes the directory.
export async function startFixture() {
  const root = process.env.HOPPER_FILES_FIXTURE_ROOT || tmpdir();
  mkdirSync(root, {recursive: true});
  const directory = mkdtempSync(join(root, "hf-browser-"));
  const command = [process.env.HOPPER_FILES_PYTHON || "python3", join(REPOSITORY, "scripts", "browser_fixture.py"), directory];
  const wrapper = process.env.HOPPER_FILES_FIXTURE_WRAPPER;
  const [file, ...args] = wrapper ? [wrapper, ...command] : command;
  const child = spawn(file, args, {
    env: {
      ...process.env,
      HOPPER_FILES_FIXTURE_DIR: directory,
      PYTHONPATH: [join(REPOSITORY, "src"), process.env.HOPPER_FILES_LOCKED_SITE].filter(Boolean).join(":"),
      PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1",
    },
    stdio: ["pipe", "pipe", "inherit"],
  });
  const exited = new Promise(resolve => child.once("exit", resolve));
  const line = await Promise.race([
    new Promise(resolve => createInterface({input: child.stdout}).once("line", resolve)),
    exited.then(code => { throw new Error(`browser fixture exited with ${code}`); }),
    new Promise((_resolve, reject) => setTimeout(() => reject(new Error("browser fixture did not start")), 30000)),
  ]);
  const fixture = JSON.parse(line);
  fixture.directory = directory;
  fixture.stop = async () => {
    child.stdin.end();
    const timer = setTimeout(() => child.kill("SIGKILL"), 10000);
    await exited; clearTimeout(timer);
    rmSync(directory, {recursive: true, force: true});
  };
  return fixture;
}

// Opens the application logged in, in a new context of the given profile. The first call per fixture
// logs in through the form; later calls reuse the session, since repeated logins hit the rate limit.
// `errors` collects page errors and console errors.
export async function openApp(fixture, {profile = "desktop", dark = false} = {}) {
  if (!browser) browser = await webkit.launch({headless: true});
  const context = await browser.newContext({
    ...PROFILES[profile], colorScheme: dark ? "dark" : "light", locale: "pt-BR", storageState: fixture.session,
  });
  // The clipboard records what the application writes, in `window.copiedTexts`.
  await context.addInitScript(() => {
    window.copiedTexts = [];
    if (navigator.clipboard) navigator.clipboard.writeText = async text => { window.copiedTexts.push(String(text)); };
  });
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const errors = [];
  // A failed load names its request, so an unexpected status shows which route answered it.
  const failed = [];
  page.on("response", response => { if (response.status() >= 400) failed.push(`${response.request().method()} ${new URL(response.url()).pathname}`); });
  page.on("pageerror", error => errors.push(`pageerror: ${error.message}`));
  page.on("console", message => {
    if (message.type() !== "error") return;
    const request = message.text().includes("Failed to load resource") && failed.length ? ` [${failed.shift()}]` : "";
    errors.push(`console: ${message.text()}${request}`);
  });
  if (!fixture.session) {
    await page.goto(fixture.url);
    await page.fill("#login-form input[name=username]", fixture.username);
    await page.fill("#login-form input[type=password]", fixture.password);
    await page.keyboard.press("Enter");
    await page.waitForURL(/\/app/);
    fixture.session = await context.storageState();
  } else await page.goto(`${fixture.url}app`);
  await waitForListing(page);
  return {context, page, errors};
}

// The context menu as the keyboard's menu key or Shift+F10 opens it: a contextmenu event without a
// pointer (detail 0). Headless WebKit does not generate it from the keys, and Playwright's
// dispatchEvent sends a plain Event, so the page builds the MouseEvent itself.
export async function keyboardMenu(locator) {
  await locator.focus();
  await locator.evaluate(element => element.dispatchEvent(new MouseEvent("contextmenu", {bubbles: true, cancelable: true, detail: 0})));
}

// A secondary click at a given point of the window, delivered to `locator`.
export async function pointerMenuAt(locator, clientX, clientY) {
  await locator.evaluate((element, [x, y]) => element.dispatchEvent(new MouseEvent("contextmenu", {bubbles: true, cancelable: true, detail: 1, button: 2, clientX: x, clientY: y})), [clientX, clientY]);
}

// A long press as Safari on iPhone delivers it: a touch pointerdown, the finger held still, and a
// pointerup. The application opens its menu after 500 ms.
export async function longPress(page, locator, holdMs = 800) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  const point = {x: box.x + Math.min(60, box.width / 2), y: box.y + box.height / 2};
  const send = type => page.evaluate(({type, x, y}) => document.elementFromPoint(x, y)?.dispatchEvent(new PointerEvent(type, {
    bubbles: true, cancelable: true, pointerType: "touch", isPrimary: true, clientX: x, clientY: y,
  })), {type, ...point});
  await send("pointerdown");
  await page.waitForTimeout(holdMs);
  await send("pointerup");
}

export async function waitForListing(page) {
  await page.waitForSelector("#results tr.item");
  await page.waitForTimeout(300);
}

// Shows the fixture's documents folder in the listing, wherever an earlier test of the same fixture
// left the saved tab: the breadcrumb segment named "documents".
export async function showDocuments(page) {
  await page.locator("#breadcrumbs button.seg", {hasText: /^documents$/}).last().click();
  await page.waitForFunction(() => document.querySelector("#breadcrumbs .seg:last-child")?.textContent === "documents");
  await waitForListing(page);
}

export function listingRow(page, name) {
  return page.locator("#results tr.item").filter({has: page.locator("button.nome-link", {hasText: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`)})});
}

export async function openSidebar(page) {
  if ((page.viewportSize()?.width || 0) > 720) return;
  if (await page.locator("#tree").isVisible()) return;
  await page.locator("#sidebar-toggle").click();
  await page.waitForTimeout(300);
}

export async function closeSidebar(page) {
  if ((page.viewportSize()?.width || 0) > 720) return;
  if (!(await page.locator("#tree").isVisible())) return;
  await page.locator("#sidebar-toggle").click();
  await page.waitForTimeout(300);
}

// Layout audit, run inside the page: contrast against the composed background (WCAG AA), text cut
// without an ellipsis, short labels that wrap, sideways page scroll, controls off screen, text covered
// by a visible element that is not a layer (menu, dialog, sticky header), and, on touch profiles, tap
// targets under the WCAG AA minimum of 24 px, measured by hit testing so that the invisible enlarged
// hit areas the touch layout adds count. Returns a list of {kind, target, detail}.
export async function auditLayout(page, {touch = false, ignore = []} = {}) {
  return page.evaluate(({touch, ignore}) => {
    const problems = [];
    const describe = element => {
      const classes = typeof element.className === "string" ? element.className.trim().split(/\s+/).slice(0, 2).join(".") : "";
      const text = (element.value || element.textContent || element.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 40);
      return element.tagName.toLowerCase() + (element.id ? `#${element.id}` : "") + (classes ? `.${classes}` : "") + (text ? ` "${text}"` : "");
    };
    const add = (kind, element, detail) => { if (!ignore.some(selector => element.closest(selector))) problems.push({kind, target: describe(element), detail}); };
    // Elements hidden for screen readers only (1 px, clipped) are not drawn on screen.
    const shown = element => {
      const box = element.getBoundingClientRect();
      if (box.width < 2 || box.height < 2) return false;
      const style = getComputedStyle(element);
      return style.visibility !== "hidden" && style.display !== "none";
    };
    const parse = value => {
      const match = value && value.match(/rgba?\(([^)]+)\)/);
      if (!match) return null;
      const parts = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      return {r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1};
    };
    const luminance = color => {
      const channel = value => { value /= 255; return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
    };
    const over = (top, bottom) => ({r: top.r * top.a + bottom.r * (1 - top.a), g: top.g * top.a + bottom.g * (1 - top.a), b: top.b * top.a + bottom.b * (1 - top.a), a: 1});
    const background = element => {
      const chain = [];
      for (let node = element; node; node = node.parentElement) chain.push(node);
      let color = parse(getComputedStyle(document.documentElement).backgroundColor) || {r: 255, g: 255, b: 255, a: 1};
      if (color.a < 1) color = over(color, {r: 255, g: 255, b: 255, a: 1});
      for (let index = chain.length - 1; index >= 0; index--) {
        const layer = parse(getComputedStyle(chain[index]).backgroundColor);
        if (layer && layer.a > 0) color = over(layer, color);
      }
      return color;
    };
    const opacity = element => { let value = 1; for (let node = element; node; node = node.parentElement) value *= parseFloat(getComputedStyle(node).opacity); return value; };
    const disabled = element => Boolean(element.closest("[disabled],[aria-disabled=true]")) || element.matches?.(":disabled");
    const ownText = element => [...element.childNodes].some(node => node.nodeType === 3 && node.textContent.trim());
    const all = [...document.querySelectorAll("body *")].filter(shown);
    const texts = all.filter(element => ownText(element) || (element.matches("input,textarea") && element.value && !["checkbox", "radio", "range", "color"].includes(element.type)));
    for (const element of texts) {
      // Dimmed on purpose: disabled controls, hidden text, and entries the account cannot open.
      if (disabled(element) || element.closest("[aria-hidden='true'], .inacessivel, .cm-gutters") || opacity(element) < 0.05) continue;
      const style = getComputedStyle(element);
      const back = background(element);
      let fore = parse(style.color);
      if (!fore) continue;
      fore = over({...fore, a: fore.a * opacity(element)}, back);
      const lighter = Math.max(luminance(fore), luminance(back)), darker = Math.min(luminance(fore), luminance(back));
      const ratio = (lighter + 0.05) / (darker + 0.05);
      const size = parseFloat(style.fontSize), bold = parseInt(style.fontWeight, 10) >= 600;
      const minimum = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
      if (ratio < minimum) add("contrast", element, `${ratio.toFixed(2)}:1, minimum ${minimum}`);
    }
    for (const element of texts) {
      if (element.matches("input,textarea") || element.closest(".cm-editor")) continue;
      const style = getComputedStyle(element);
      if (element.scrollWidth > element.clientWidth + 1 && /hidden|clip/.test(style.overflowX) && style.textOverflow !== "ellipsis") add("cut", element, `width ${element.clientWidth} < ${element.scrollWidth}`);
      const text = element.textContent.trim();
      if (text.length <= 24 && text.split(/\s+/).length <= 3 && element.matches("button, button *, [role=menuitem], [role=menuitem] *, th, label")) {
        const node = [...element.childNodes].find(child => child.nodeType === 3 && child.textContent.trim());
        const range = document.createRange(); range.selectNodeContents(node);
        const lines = new Set([...range.getClientRects()].map(rect => Math.round(rect.top)));
        if (lines.size > 1) add("wrap", element, `${lines.size} lines`);
      }
    }
    const width = innerWidth, height = innerHeight;
    if (document.documentElement.scrollWidth > width + 1) problems.push({kind: "sideways-scroll", target: "page", detail: `${document.documentElement.scrollWidth} > ${width}`});
    const scrolls = element => { for (let node = element.parentElement; node; node = node.parentElement) { const style = getComputedStyle(node); if (/auto|scroll/.test(style.overflowX + style.overflowY)) return true; } return false; };
    for (const element of all.filter(node => node.matches("button,input,select,textarea,a,[role=menuitem],[role=treeitem]"))) {
      const box = element.getBoundingClientRect();
      if ((box.right > width + 1 || box.left < -1 || box.bottom > height + 1) && !scrolls(element)) add("off-screen", element, `${Math.round(box.left)},${Math.round(box.top)} ${Math.round(box.width)}x${Math.round(box.height)}`);
    }
    // Text boxes: the rectangles of the element's own text, not of its whole box.
    const textRect = element => {
      if (element.matches("input,textarea")) return element.getBoundingClientRect();
      const range = document.createRange();
      const rects = [...element.childNodes].filter(node => node.nodeType === 3 && node.textContent.trim()).flatMap(node => { range.selectNodeContents(node); return [...range.getClientRects()]; });
      if (!rects.length) return element.getBoundingClientRect();
      const left = Math.min(...rects.map(rect => rect.left)), top = Math.min(...rects.map(rect => rect.top));
      const right = Math.max(...rects.map(rect => rect.right)), bottom = Math.max(...rects.map(rect => rect.bottom));
      return {left, top, right, bottom, width: right - left, height: bottom - top};
    };
    // An element that paints nothing (a resize handle, a transparent overlay) does not hide text.
    const paints = node => {
      if (opacity(node) < 0.05) return false;
      const style = getComputedStyle(node);
      const fill = parse(style.backgroundColor);
      return Boolean(ownText(node) || (fill && fill.a > 0.05) || style.backgroundImage !== "none" || parseFloat(style.borderTopWidth) || parseFloat(style.borderLeftWidth) || node.matches("img,svg,canvas,video,input,select,textarea"));
    };
    const layer = (cover, element) => {
      for (let node = cover; node && node !== document.body; node = node.parentElement) {
        if (node.contains(element)) return false;
        const style = getComputedStyle(node);
        if (style.position === "sticky") return true;
        if (style.position === "fixed" || (style.position === "absolute" && style.zIndex !== "auto" && parseInt(style.zIndex, 10) >= 5)) return true;
      }
      return false;
    };
    const clipped = element => {
      const box = textRect(element);
      let left = box.left, top = box.top, right = box.right, bottom = box.bottom;
      for (let node = element; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.overflowX !== "visible" || style.overflowY !== "visible") {
          const outer = node.getBoundingClientRect();
          left = Math.max(left, outer.left); top = Math.max(top, outer.top); right = Math.min(right, outer.right); bottom = Math.min(bottom, outer.bottom);
        }
      }
      return {left, top, right, bottom, width: right - left, height: bottom - top};
    };
    for (const element of texts) {
      if (getComputedStyle(element).pointerEvents === "none") continue;
      const box = clipped(element);
      if (box.width < 4 || box.height < 4 || box.bottom < 0 || box.top > height || box.right < 0 || box.left > width) continue;
      for (const [x, y] of [[box.left + box.width / 2, box.top + box.height / 2], [box.left + 3, box.top + box.height / 2], [box.right - 3, box.top + box.height / 2]]) {
        if (x < 0 || y < 0 || x >= width || y >= height) continue;
        const cover = document.elementFromPoint(x, y);
        if (!cover || cover === element || element.contains(cover) || cover.contains(element) || layer(cover, element) || !paints(cover)) continue;
        add("covered", element, `by ${describe(cover)}`);
        break;
      }
    }
    if (touch) {
      // A tap at a point counts for a control when it lands on the control, inside it, or on the row or
      // cell around it that acts as a whole: a tree row or a listing row opens its item, and the
      // selection cell toggles its checkbox.
      const hits = (element, x, y) => {
        const node = document.elementFromPoint(x, y);
        if (!node) return false;
        if (node === element || element.contains(node) || node.closest("[role=treeitem], tr.item, td.c-check")?.contains(element)) return true;
        // A control partly scrolled under a sticky header or a fixed bar is not too small. The sidebar
        // resize handle reaches 8 px into the listing on touch on purpose, so it is easy to grab.
        return layer(node, element) || Boolean(node.closest("#sidebar-splitter"));
      };
      for (const element of all.filter(node => node.matches("button,select,a[href],input[type=checkbox],[role=menuitem]") && !disabled(node) && opacity(node) >= 0.05)) {
        const box = element.getBoundingClientRect();
        const x = box.left + box.width / 2, y = box.top + box.height / 2;
        if (x - 12 < 0 || y - 12 < 0 || x + 12 > width || y + 12 > height || !hits(element, x, y)) continue;
        // A control partly scrolled out of its container (the start of a long breadcrumb) is not measured.
        let left = box.left, top = box.top, right = box.right, bottom = box.bottom;
        for (let node = element.parentElement; node && node !== document.body; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.overflowX === "visible" && style.overflowY === "visible") continue;
          const outer = node.getBoundingClientRect();
          left = Math.max(left, outer.left); top = Math.max(top, outer.top); right = Math.min(right, outer.right); bottom = Math.min(bottom, outer.bottom);
        }
        if (right - left < box.width - 1 || bottom - top < box.height - 1) continue;
        const misses = [[-11.5, 0, "left"], [11.5, 0, "right"], [0, -11.5, "top"], [0, 11.5, "bottom"]].filter(([dx, dy]) => !hits(element, x + dx, y + dy));
        if (misses.length) add("small-touch-target", element, `${Math.round(box.width)}x${Math.round(box.height)}, misses ${misses.map(([dx, dy, side]) => `${side} on ${describe(document.elementFromPoint(x + dx, y + dy) || document.body)}`).join(", ")}`);
      }
    }
    return problems;
  }, {touch, ignore});
}
