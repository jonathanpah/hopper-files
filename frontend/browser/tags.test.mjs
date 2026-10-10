// Tags in the sidebar: the list built from the monitored folder, and the "Ignorar tag" menu of a tag
// row by secondary click, keyboard, and touch long-press, with saving, restoring in Settings, the open
// tag, and the limit of ignored tags.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { closeBrowser, keyboardMenu, openApp, openSidebar, startFixture, waitForListing } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const ALL_TAGS = [["alfa", "2"], ["beta", "2"], ["delta-longo-para-testar", "1"], ["epsilon", "1"], ["gama", "1"]];
const MAX_IGNORED_TAGS = 256;

const menu = page => page.locator("#menu-ctx");
const ignoreEntry = page => menu(page).getByRole("menuitem", {name: "Ignorar tag"});
const tagRow = (page, tag) => page.locator(`#tags-nav .tg-row[data-tag="${tag}"]`);
const shownTags = page => page.locator("#tags-nav .tg-row").evaluateAll(rows => rows.map(row => [row.dataset.tag, row.querySelector(".tg-n").textContent]));
const shownTagNames = async page => (await shownTags(page)).map(([tag]) => tag);
const menuEntries = async page => (await menu(page).locator(".ctx-item").allTextContents()).map(text => text.trim());
const breadcrumbs = async page => (await page.locator("#breadcrumbs").textContent()).trim();
const statusText = page => page.locator("#app-status").textContent();
const focusedTag = page => page.evaluate(() => document.activeElement?.closest?.("#tags-nav .tg-row")?.dataset.tag ?? null);
const isApi = (request, route) => new URL(request.url()).pathname.endsWith(`/api/${route}`);
const isSave = request => request.method() === "PUT" && isApi(request, "state");

const menuInsideWindow = page => menu(page).evaluate(element => {
  const box = element.getBoundingClientRect();
  return box.left >= 0 && box.top >= 0 && box.right <= innerWidth && box.bottom <= innerHeight;
});

// Reads the API with the page's session.
const readApi = (page, route) => page.evaluate(async route => {
  const base = document.getElementById("hf-app").dataset.basePath;
  return (await fetch(`${base}${route}`, {headers: {Accept: "application/json"}})).json();
}, route);
const savedState = async page => {
  const state = await readApi(page, "api/state");
  return {revision: state.stateRevision, ignoredTags: state.preferences.ignoredTags ?? null};
};

// Writes the ignored tags through the request the application uses (PUT api/state with baseRevision).
// The page's own copy of the state is stale afterwards, so callers reload it.
async function writeIgnoredTags(page, tags) {
  const status = await page.evaluate(async tags => {
    const app = document.getElementById("hf-app");
    const base = app.dataset.basePath;
    const {stateRevision, ...saved} = await (await fetch(`${base}api/state`, {headers: {Accept: "application/json"}})).json();
    if (tags.length) saved.preferences.ignoredTags = tags; else delete saved.preferences.ignoredTags;
    const response = await fetch(`${base}api/state`, {
      method: "PUT", headers: {"Content-Type": "application/json", "X-CSRF-Token": app.dataset.csrf, Accept: "application/json"},
      body: JSON.stringify({baseRevision: stateRevision, ...saved}),
    });
    return response.status;
  }, tags);
  assert.equal(status, 200, "writing the ignored tags");
}

async function reloadWithTags(page, count) {
  await page.reload();
  await waitForListing(page);
  await page.waitForFunction(count => document.querySelectorAll("#tags-nav .tg-row").length === count, count);
}

// Opens the application with the tag list loaded and no tag left ignored by an earlier test.
async function openTags(options) {
  const opened = await openApp(fixture, options);
  if ((await savedState(opened.page)).ignoredTags) {
    await writeIgnoredTags(opened.page, []);
    await reloadWithTags(opened.page, ALL_TAGS.length);
  } else await opened.page.waitForFunction(count => document.querySelectorAll("#tags-nav .tg-row").length === count, ALL_TAGS.length);
  return opened;
}

// Records each contextmenu event: the tag row it reached and whether a handler canceled it, which is
// what keeps the browser's own menu from opening.
const recordContextMenus = page => page.evaluate(() => {
  window.contextMenus = [];
  addEventListener("contextmenu", event => {
    const tag = event.target.closest?.(".tg-row")?.dataset.tag ?? null;
    setTimeout(() => window.contextMenus.push({tag, prevented: event.defaultPrevented}));
  }, true);
});

// A long press as Safari on iPhone delivers it: a touch pointerdown, the finger held still, and a
// pointerup. The application opens its menu after 500 ms.
async function longPress(page, locator, holdMs = 800) {
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

async function ignoreWithMenu(page, tag) {
  await tagRow(page, tag).click({button: "right"});
  await ignoreEntry(page).click();
}

async function openTagView(page, tag, files) {
  await tagRow(page, tag).click();
  await page.waitForFunction(({tag, files}) => document.querySelector("#results .busca-cab strong")?.textContent === `#${tag}`
    && document.querySelectorAll("#results .res-item").length === files, {tag, files});
}

test("the tag list shows the five note tags with counts, leaving out node_modules and hidden folders", async () => {
  const {context, page, errors} = await openTags();
  assert.deepEqual(await shownTags(page), ALL_TAGS);
  const index = await readApi(page, "api/tags");
  assert.deepEqual(index.tags.map(item => item.tag).sort(), ALL_TAGS.map(([tag]) => tag));
  const excluded = index.exclusions.map(item => item.category);
  assert.ok(excluded.includes("dependency_directory") && excluded.includes("hidden_directory"), JSON.stringify(index.exclusions));
  assert.equal(index.complete, true);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a secondary click on a tag opens the app menu with only \"Ignorar tag\" instead of the browser menu", async () => {
  const {context, page, errors} = await openTags();
  await recordContextMenus(page);
  await tagRow(page, "beta").click({button: "right"});
  await menu(page).waitFor({state: "visible"});
  assert.deepEqual(await menuEntries(page), ["Ignorar tag"]);
  assert.equal(await menuInsideWindow(page), true);
  await page.waitForFunction(() => window.contextMenus.length > 0);
  assert.deepEqual(await page.evaluate(() => window.contextMenus), [{tag: "beta", prevented: true}]);
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

test("opening the tag menu neither opens the tag nor saves anything", async () => {
  const {context, page, errors} = await openTags();
  const before = await savedState(page);
  const requests = [];
  page.on("request", request => { if (isSave(request) || (isApi(request, "tags") && new URL(request.url()).searchParams.has("tag"))) requests.push(request.url()); });
  await tagRow(page, "beta").click({button: "right"});
  await menu(page).waitFor({state: "visible"});
  await page.keyboard.press("Escape");
  // Reading the state afterwards also orders this check after any request the menu could have sent.
  assert.deepEqual(await savedState(page), before);
  assert.deepEqual(requests, []);
  assert.ok(!(await breadcrumbs(page)).includes("#beta"));
  assert.ok(await page.locator("#results tr.item").count() > 0, "the folder listing stays on screen");
  assert.deepEqual(errors, []);
  await context.close();
});

test("Esc closes the tag menu", async () => {
  const {context, page, errors} = await openTags();
  await tagRow(page, "beta").click({button: "right"});
  await menu(page).waitFor({state: "visible"});
  await page.keyboard.press("Escape");
  assert.equal(await menu(page).isVisible(), false);
  assert.deepEqual(await shownTags(page), ALL_TAGS);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a click outside closes the tag menu without ignoring the tag", async () => {
  const {context, page, errors} = await openTags();
  await tagRow(page, "gama").click({button: "right"});
  await menu(page).waitFor({state: "visible"});
  await page.locator("#app-status").click();
  assert.equal(await menu(page).isVisible(), false);
  assert.deepEqual(await shownTags(page), ALL_TAGS);
  assert.equal((await savedState(page)).ignoredTags, null);
  assert.deepEqual(errors, []);
  await context.close();
});

test("\"Ignorar tag\" removes the tag from the list at once and keeps the other counts", async () => {
  const {context, page, errors} = await openTags();
  await ignoreWithMenu(page, "beta");
  await tagRow(page, "beta").waitFor({state: "detached", timeout: 2000});
  assert.deepEqual(await shownTags(page), ALL_TAGS.filter(([tag]) => tag !== "beta"));
  assert.equal(await menu(page).isVisible(), false);
  assert.match(await statusText(page), /#beta deixou de ser mostrada/);
  assert.deepEqual(errors, []);
  await context.close();
});

test("an ignored tag is saved through PUT api/state with the base revision and stays out of the index", async () => {
  const {context, page, errors} = await openTags();
  const before = await savedState(page);
  const saves = [];
  page.on("request", request => { if (isSave(request)) saves.push(request.postDataJSON()); });
  const saved = page.waitForResponse(response => isSave(response.request()));
  await ignoreWithMenu(page, "beta");
  assert.equal((await saved).status(), 200);
  assert.equal(saves.length, 1);
  assert.equal(saves[0].baseRevision, before.revision);
  assert.deepEqual(saves[0].preferences.ignoredTags, ["beta"]);
  assert.deepEqual(await savedState(page), {revision: before.revision + 1, ignoredTags: ["beta"]});
  assert.ok(!(await readApi(page, "api/tags")).tags.some(item => item.tag === "beta"));
  assert.deepEqual((await readApi(page, "api/tags?tag=beta")).items ?? [], []);
  await reloadWithTags(page, ALL_TAGS.length - 1);
  assert.ok(!(await shownTagNames(page)).includes("beta"));
  assert.deepEqual(errors, []);
  await context.close();
});

test("\"Voltar a mostrar\" in Settings brings an ignored tag back with its count", async () => {
  const {context, page, errors} = await openTags();
  await writeIgnoredTags(page, ["beta"]);
  await reloadWithTags(page, ALL_TAGS.length - 1);
  await page.locator("#btn-config").click();
  const dialog = page.locator("#operation-dialog");
  await dialog.waitFor({state: "visible"});
  assert.deepEqual((await dialog.locator(".opt-tag-ign").allTextContents()).map(text => text.trim()), ["#beta"]);
  await dialog.getByRole("button", {name: "Voltar a mostrar #beta"}).click();
  await dialog.getByRole("button", {name: "Aplicar"}).click();
  await tagRow(page, "beta").waitFor({state: "attached"});
  assert.deepEqual(await shownTags(page), ALL_TAGS);
  assert.equal((await savedState(page)).ignoredTags, null);
  assert.deepEqual(errors, []);
  await context.close();
});

test("ignoring the open tag returns to the tag list without errors, and another tag still opens", async () => {
  const {context, page, errors} = await openTags();
  await openTagView(page, "alfa", 2);
  assert.equal(await breadcrumbs(page), "#alfa");
  await ignoreWithMenu(page, "alfa");
  await page.locator("#results", {hasText: "Escolha uma tag para ver os arquivos"}).waitFor();
  assert.ok(!(await breadcrumbs(page)).includes("alfa"));
  assert.equal(await page.locator("#results .res-item").count(), 0);
  assert.equal(await tagRow(page, "alfa").count(), 0);
  await openTagView(page, "gama", 1);
  assert.equal(await breadcrumbs(page), "#gama");
  assert.deepEqual(errors, []);
  await context.close();
});

test("going back in history to an ignored tag shows it without files", async () => {
  const {context, page, errors} = await openTags();
  await openTagView(page, "alfa", 2);
  const saved = page.waitForResponse(response => isSave(response.request()));
  await ignoreWithMenu(page, "alfa");
  await page.locator("#results", {hasText: "Escolha uma tag para ver os arquivos"}).waitFor();
  await saved;
  await page.goBack();
  await page.locator("#results", {hasText: "Nenhum arquivo contém esta tag"}).waitFor();
  assert.equal(await breadcrumbs(page), "#alfa");
  assert.equal(await page.locator("#results .res-item").count(), 0);
  assert.deepEqual(errors, []);
  await context.close();
});

test("the keyboard menu of a tag focuses \"Ignorar tag\", and Esc returns focus to the tag row", async () => {
  const {context, page, errors} = await openTags();
  await keyboardMenu(tagRow(page, "alfa"));
  await menu(page).waitFor({state: "visible"});
  assert.deepEqual(await menuEntries(page), ["Ignorar tag"]);
  assert.equal(await page.evaluate(() => document.activeElement?.closest("#menu-ctx") ? document.activeElement.textContent.trim() : null), "Ignorar tag");
  await page.keyboard.press("Escape");
  assert.equal(await menu(page).isVisible(), false);
  assert.equal(await focusedTag(page), "alfa");
  assert.deepEqual(errors, []);
  await context.close();
});

test("Enter on a focused tag row opens the tag", async () => {
  const {context, page, errors} = await openTags();
  await tagRow(page, "alfa").focus();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector("#results .busca-cab strong")?.textContent === "#alfa");
  assert.equal(await breadcrumbs(page), "#alfa");
  assert.deepEqual(errors, []);
  await context.close();
});

test("Enter on \"Ignorar tag\" ignores the tag, saves it, and moves focus to the next row", async () => {
  const {context, page, errors} = await openTags();
  // After saving, the index reloads and the list is drawn again. The reload is held until the rows
  // drawn first are marked, so the test can tell when the second drawing has happened.
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const tagIndex = url => url.pathname.endsWith("/api/tags") && !url.searchParams.has("tag");
  await page.route(tagIndex, async route => { await held; await route.continue(); });
  await keyboardMenu(tagRow(page, "beta"));
  await menu(page).waitFor({state: "visible"});
  const saved = page.waitForResponse(response => isSave(response.request()));
  const reindexed = page.waitForResponse(response => tagIndex(new URL(response.url())));
  await page.keyboard.press("Enter");
  assert.deepEqual(await shownTagNames(page), ["alfa", "delta-longo-para-testar", "epsilon", "gama"]);
  assert.equal(await focusedTag(page), "delta-longo-para-testar");
  await page.locator("#tags-nav .tg-row").evaluateAll(rows => { for (const row of rows) row.dataset.drawnBefore = ""; });
  release();
  await saved; await (await reindexed).finished();
  await page.waitForFunction(() => document.querySelector("#tags-nav .tg-row") && !document.querySelector("#tags-nav .tg-row[data-drawn-before]"));
  assert.equal(await focusedTag(page), "delta-longo-para-testar");
  assert.deepEqual((await savedState(page)).ignoredTags, ["beta"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("the 256th ignored tag is saved, and a 257th is refused with a warning, without hiding or saving it", async () => {
  const {context, page, errors} = await openTags();
  const filler = Array.from({length: MAX_IGNORED_TAGS - 1}, (_, index) => `x${String(index).padStart(4, "0")}`);
  await writeIgnoredTags(page, filler);
  await reloadWithTags(page, ALL_TAGS.length);
  const saved = page.waitForResponse(response => isSave(response.request()));
  await ignoreWithMenu(page, "alfa");
  assert.equal((await saved).status(), 200);
  assert.equal((await savedState(page)).ignoredTags.length, MAX_IGNORED_TAGS);
  const saves = [];
  page.on("request", request => { if (isSave(request)) saves.push(request.url()); });
  await ignoreWithMenu(page, "beta");
  await page.locator("#app-status", {hasText: `Não é possível ignorar mais de ${MAX_IGNORED_TAGS} tags`}).waitFor();
  assert.ok((await shownTagNames(page)).includes("beta"));
  const after = await savedState(page);
  assert.equal(after.ignoredTags.length, MAX_IGNORED_TAGS);
  assert.ok(!after.ignoredTags.includes("beta"));
  assert.deepEqual(saves, []);
  assert.deepEqual(errors, []);
  await context.close();
});

test("on iPhone, a long press on a tag opens the app menu with only \"Ignorar tag\", without opening the tag", async () => {
  const {context, page, errors} = await openTags({profile: "iphone"});
  await openSidebar(page);
  await recordContextMenus(page);
  await longPress(page, tagRow(page, "beta"));
  await menu(page).waitFor({state: "visible"});
  assert.deepEqual(await menuEntries(page), ["Ignorar tag"]);
  assert.equal(await menuInsideWindow(page), true);
  assert.ok(!(await breadcrumbs(page)).includes("#beta"));
  await page.waitForFunction(() => window.contextMenus.length > 0);
  assert.deepEqual(await page.evaluate(() => window.contextMenus), [{tag: "beta", prevented: true}]);
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

test("on iPhone, the tag menu entry has the touch height of the entries in the menu of the folder rows", async () => {
  const {context, page, errors} = await openTags({profile: "iphone"});
  await openSidebar(page);
  const entryHeight = async target => {
    await longPress(page, target);
    await menu(page).waitFor({state: "visible"});
    const height = await menu(page).locator(".ctx-item").first().evaluate(element => element.getBoundingClientRect().height);
    await page.keyboard.press("Escape");
    await menu(page).waitFor({state: "hidden"});
    return Math.round(height);
  };
  const reference = await entryHeight(page.locator("#tree .node-row").first());
  const tag = await entryHeight(tagRow(page, "beta"));
  assert.equal(tag, reference);
  assert.ok(tag >= 40, `entry height ${tag}`);
  assert.deepEqual(errors, []);
  await context.close();
});

test("on iPhone, tapping \"Ignorar tag\" removes the tag and saves it", async () => {
  const {context, page, errors} = await openTags({profile: "iphone"});
  await openSidebar(page);
  await longPress(page, tagRow(page, "beta"));
  await menu(page).waitFor({state: "visible"});
  const saved = page.waitForResponse(response => isSave(response.request()));
  await ignoreEntry(page).tap();
  await tagRow(page, "beta").waitFor({state: "detached", timeout: 2000});
  assert.deepEqual(await shownTagNames(page), ["alfa", "delta-longo-para-testar", "epsilon", "gama"]);
  assert.equal((await saved).status(), 200);
  assert.deepEqual((await savedState(page)).ignoredTags, ["beta"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("on iPhone, tapping outside closes the tag menu without ignoring the tag", async () => {
  const {context, page, errors} = await openTags({profile: "iphone"});
  await openSidebar(page);
  await longPress(page, tagRow(page, "gama"));
  await menu(page).waitFor({state: "visible"});
  // The empty top corner of the open sidebar: a spot that does nothing on its own.
  await page.locator("#sidebar").tap({position: {x: 5, y: 5}});
  await menu(page).waitFor({state: "hidden"});
  assert.deepEqual(await shownTags(page), ALL_TAGS);
  assert.equal((await savedState(page)).ignoredTags, null);
  assert.deepEqual(errors, []);
  await context.close();
});

test("on iPhone, a short tap on a tag opens it", async () => {
  const {context, page, errors} = await openTags({profile: "iphone"});
  await openSidebar(page);
  await tagRow(page, "gama").tap();
  await page.waitForFunction(() => document.querySelector("#results .busca-cab strong")?.textContent === "#gama");
  assert.equal(await breadcrumbs(page), "#gama");
  assert.deepEqual(errors, []);
  await context.close();
});
