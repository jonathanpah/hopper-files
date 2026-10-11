// Live refresh: the open folder, an open tree branch, open documents, and previews follow changes
// that another process makes on disk, without any action in the app (HF-FILE-006, HF-ACC-028).
// This test process makes the changes, only inside the fixture documents folder.
import assert from "node:assert/strict";
import { appendFileSync, mkdirSync, readFileSync, renameSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { crc32, deflateSync } from "node:zlib";
import { closeBrowser, listingRow, openApp, startFixture, waitForListing } from "./harness.mjs";

// A reported change reaches the screen within about 2 seconds.
const LIVE = 2000;

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

// The API names an entry by its absolute path without the leading slash.
const apiPath = absolute => absolute.slice(1);
const inDocuments = name => join(fixture.documents, name);
const rowSelector = name => `#results tr.item[data-path="${apiPath(inDocuments(name))}"]`;

// The folders shown are watched once the first change request is answered; tests change files
// only after that answer.
async function waitForWatch(page) {
  await page.waitForFunction(() => performance.getEntriesByType("resource").some(entry => new URL(entry.name).pathname.endsWith("/api/changes")));
}

// Opens the app on the shared instance. The context also closes if the test fails before its end.
async function openLive(t, options = {}) {
  const opened = await openApp(fixture, options);
  t.after(() => opened.context.close());
  await waitForWatch(opened.page);
  return opened;
}

// Opens the app on an instance of its own, for a test that deletes the open folder or leaves the
// saved folder tab somewhere else. `prepare` receives the documents folder before the app opens.
async function openIsolated(t, prepare) {
  const own = await startFixture();
  let opened = null;
  t.after(async () => { await opened?.context.close(); await own.stop(); });
  prepare(own.documents);
  opened = await openApp(own);
  await waitForWatch(opened.page);
  return {...opened, documents: own.documents};
}

// Removes entries of the documents folder when the test ends.
function removeAfter(t, ...names) {
  t.after(() => { for (const name of names) rmSync(inDocuments(name), {recursive: true, force: true}); });
}

// Creates files in the documents folder and removes them when the test ends.
function createFiles(t, files) {
  for (const [name, content] of Object.entries(files)) writeFileSync(inDocuments(name), content);
  removeAfter(t, ...Object.keys(files));
}

// Writes as editors and generators often do: to a temporary name, then renamed over the file.
function writeBySwap(path, content) {
  const temporary = `${path}.tmp-${process.pid}`;
  writeFileSync(temporary, content);
  renameSync(temporary, path);
}

// The API requests the page sends from now on, as {at, route, path}.
function recordRequests(page) {
  const sent = [];
  page.on("request", request => {
    const url = new URL(request.url());
    const route = url.pathname.match(/\/api\/(.+)$/)?.[1];
    if (route) sent.push({at: Date.now(), route, path: url.searchParams.get("path"), request});
  });
  // A read the server refused with 409 (the folder changed while it was read) is made again by the app.
  page.on("response", response => {
    const entry = sent.find(item => item.request === response.request());
    if (entry) entry.status = response.status();
  });
  return sent;
}

// Resolves once `requests()` has gained no entry for `ms`.
async function quietFor(requests, ms) {
  for (;;) {
    const idle = Date.now() - Math.max(0, ...requests().map(request => request.at));
    if (idle >= ms) return;
    await delay(ms - idle);
  }
}

// Resolves when an answer to a change request names `name` in `folder`, or the whole folder.
function changeReported(page, folder, name, timeout = LIVE) {
  return page.waitForResponse(async response => {
    if (!new URL(response.url()).pathname.endsWith("/api/changes")) return false;
    const body = await response.json().catch(() => null);
    return Boolean(body?.changes?.some(change => change.path === folder && (change.name === name || change.name === null)));
  }, {timeout});
}

// Whether `predicate` becomes true in the page at some moment within `ms`.
async function happensWithin(page, ms, predicate, arg) {
  try {
    await page.waitForFunction(predicate, arg, {timeout: ms, polling: 50});
    return true;
  } catch (error) {
    if (error.name === "TimeoutError") return false;
    throw error;
  }
}

// Console errors other than the failed loads, with one of `statuses`, that a test causes on purpose.
// A test that writes next to a folder while the app reads it may also meet a 409: the server says the
// folder changed during the read, and the app reads it again.
const unexpected = (errors, ...statuses) => errors.filter(error => !(error.includes("Failed to load resource")
  && statuses.some(status => error.includes(`status of ${status}`))));

const markedNames = page => page.evaluate(() => [...document.querySelectorAll("#results tr.item input.check:checked")].map(box => box.closest("tr").dataset.path.split("/").pop()));
const focusedPath = page => page.evaluate(() => document.activeElement?.closest("tr.item")?.dataset.path ?? null);
const listScroll = page => page.evaluate(() => document.querySelector("#results").scrollTop);
const treeRow = (page, path) => page.locator(`#tree .node-row[data-path="${path}"]`);
const tabNamed = (page, name) => page.locator("#tabs .aba-nome", {hasText: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`)});

const SHOWN = "#results article.document-shell:not([hidden])";
const noticeLabel = page => page.locator(`${SHOWN} .ed-erro`);

async function openFromListing(page, name) {
  await listingRow(page, name).locator("button.nome-link").click();
}

// Waits until `selector` of the shown document (the formatted preview, the Markdown source, or the
// plain editor) contains `text`.
function waitForShown(page, selector, text, timeout) {
  return page.waitForFunction(([selector, text]) => {
    const node = document.querySelector(selector);
    return Boolean(node) && (node.value ?? node.textContent).includes(text);
  }, [`${SHOWN} ${selector}`, text], {timeout, polling: 50});
}

const shownText = (page, selector) => page.locator(`${SHOWN} ${selector}`).textContent();

function waitForNotice(page, label) {
  return page.waitForFunction(([selector, label]) => {
    const node = document.querySelector(selector);
    return Boolean(node) && !node.hidden && node.textContent === label;
  }, [`${SHOWN} .ed-erro`, label], {timeout: LIVE, polling: 50});
}

// Opens a Markdown note in the Markdown view and types a line that is not saved.
async function editNote(page, name, line) {
  await openFromListing(page, name);
  await page.locator("#seg-cru").click();
  await page.locator(`${SHOWN} .cm-content`).click();
  await page.keyboard.press("Control+End");
  await page.keyboard.type(`\n${line}`);
  await waitForShown(page, ".cm-content", line);
}

// The app follows document.visibilityState, which a headless page keeps "visible".
async function setVisible(page, visible) {
  await page.evaluate(visible => {
    Object.defineProperty(document, "visibilityState", {configurable: true, get: () => (visible ? "visible" : "hidden")});
    Object.defineProperty(document, "hidden", {configurable: true, get: () => !visible});
    document.dispatchEvent(new Event("visibilitychange"));
  }, visible);
}

// Enters each named folder in turn by clicking it in the listing.
async function enterFolders(page, names) {
  for (const name of names) {
    const path = await listingRow(page, name).getAttribute("data-path");
    await openFromListing(page, name);
    await page.locator(`#results tr.item[data-path^="${path}/"]`).first().waitFor();
  }
}

// A solid-color PNG image.
function png(width, height, color) {
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({length: width}, () => color).flat())]);
  const chunk = (type, data) => {
    const length = Buffer.alloc(4); length.writeUInt32BE(data.length);
    const check = Buffer.alloc(4); check.writeUInt32BE(crc32(Buffer.concat([Buffer.from(type), data])) >>> 0);
    return Buffer.concat([length, Buffer.from(type), data, check]);
  };
  const header = Buffer.alloc(13); header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  return Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.concat(Array.from({length: height}, () => row)))), chunk("IEND", Buffer.alloc(0))]);
}

// A PDF with one page per RGB color, each page a filled square (no fonts to load).
function pdf(colors) {
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", `<< /Type /Pages /Kids [${colors.map((_color, index) => `${3 + index * 2} 0 R`).join(" ")}] /Count ${colors.length} >>`];
  colors.forEach((color, index) => {
    const stream = `${color.join(" ")} rg 72 300 300 300 re f`;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents ${4 + index * 2} 0 R >>`);
    objects.push(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
  });
  let out = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((body, index) => { offsets.push(out.length); out += `${index + 1} 0 obj\n${body}\nendobj\n`; });
  const table = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${table}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

// ---- The open folder ----

test("a file created outside the app appears in the open folder within 2 s", async t => {
  removeAfter(t, "created-outside.txt");
  const {context, page, errors} = await openLive(t);
  writeFileSync(inDocuments("created-outside.txt"), "created by another process\n");
  await listingRow(page, "created-outside.txt").waitFor({timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a file written after the folder was read but before its watch started appears within 2 s", async t => {
  removeAfter(t, "written-before-watch.txt");
  const {context, page, errors} = await openLive(t);
  let written = false;
  // On reload, the first change request is held until the listing is drawn; the file is written
  // then, so the folder was read without it and the watch starts after it.
  await page.route("**/api/changes", async route => {
    if (!written) {
      written = true;
      await page.waitForSelector(rowSelector("texto.txt"));
      assert.equal(await page.locator(rowSelector("written-before-watch.txt")).count(), 0);
      writeFileSync(inDocuments("written-before-watch.txt"), "written before the watch\n");
    }
    await route.continue();
  });
  await page.reload();
  await waitForListing(page);
  await page.waitForFunction(() => performance.getEntriesByType("resource").some(entry => new URL(entry.name).pathname.endsWith("/api/changes")));
  await listingRow(page, "written-before-watch.txt").waitFor({timeout: LIVE});
  await page.unroute("**/api/changes");
  assert.deepEqual(errors, []);
  await context.close();
});

test("a file renamed outside the app shows its new name in the open folder within 2 s", async t => {
  createFiles(t, {"rename-me.txt": "x\n"});
  removeAfter(t, "renamed-outside.txt");
  const {context, page, errors} = await openLive(t);
  renameSync(inDocuments("rename-me.txt"), inDocuments("renamed-outside.txt"));
  await Promise.all([
    listingRow(page, "renamed-outside.txt").waitFor({timeout: LIVE}),
    listingRow(page, "rename-me.txt").waitFor({state: "detached", timeout: LIVE}),
  ]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a file changed outside the app shows its new size in the open folder within 2 s", async t => {
  createFiles(t, {"grows.txt": "x\n"});
  const {context, page, errors} = await openLive(t);
  const sizeCell = `${rowSelector("grows.txt")} td.c-tam`;
  const before = await page.locator(sizeCell).textContent();
  appendFileSync(inDocuments("grows.txt"), "x".repeat(5000));
  await page.waitForFunction(([selector, before]) => document.querySelector(selector)?.textContent !== before, [sizeCell, before], {timeout: LIVE, polling: 50});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a file deleted outside the app leaves the open folder within 2 s", async t => {
  createFiles(t, {"delete-me.txt": "x\n"});
  const {context, page, errors} = await openLive(t);
  unlinkSync(inDocuments("delete-me.txt"));
  await listingRow(page, "delete-me.txt").waitFor({state: "detached", timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a refresh keeps the list scroll position, the focused row, and the marked items that still exist", async t => {
  const names = Array.from({length: 80}, (_value, index) => `entry-${String(index + 1).padStart(3, "0")}.txt`);
  createFiles(t, Object.fromEntries(names.map(name => [name, `${name}\n`])));
  removeAfter(t, "entry-new.txt");
  const {context, page, errors} = await openLive(t);
  await page.evaluate(() => { document.querySelector("#results").scrollTop = 900; });
  for (const name of ["entry-041.txt", "entry-042.txt"]) await listingRow(page, name).locator("input.check").check();
  await listingRow(page, "entry-040.txt").focus();
  const scroll = await listScroll(page);
  const focus = await focusedPath(page);
  assert.ok(scroll > 0, "the listing is scrolled");
  assert.equal(focus, apiPath(inDocuments("entry-040.txt")));

  writeFileSync(inDocuments("entry-new.txt"), "new\n");
  await listingRow(page, "entry-new.txt").waitFor({timeout: LIVE});
  assert.ok(Math.abs(await listScroll(page) - scroll) <= 2, "scroll position kept when a file appears");
  assert.equal(await focusedPath(page), focus);
  assert.deepEqual(await markedNames(page), ["entry-041.txt", "entry-042.txt"]);

  unlinkSync(inDocuments("entry-041.txt"));
  await listingRow(page, "entry-041.txt").waitFor({state: "detached", timeout: LIVE});
  assert.ok(Math.abs(await listScroll(page) - scroll) <= 2, "scroll position kept when a file leaves");
  assert.equal(await focusedPath(page), focus);
  assert.deepEqual(await markedNames(page), ["entry-042.txt"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("an open context menu holds the listing redraw until it closes", async t => {
  removeAfter(t, "while-menu.txt");
  const {context, page, errors} = await openLive(t);
  await listingRow(page, "texto.txt").click({button: "right"});
  await page.locator("#menu-ctx").waitFor({state: "visible"});
  const reported = changeReported(page, apiPath(fixture.documents), "while-menu.txt");
  writeFileSync(inDocuments("while-menu.txt"), "x\n");
  await reported;
  // The app knows about the change; the listing must not redraw under the menu meanwhile.
  assert.equal(await happensWithin(page, 1000, selector => Boolean(document.querySelector(selector)), rowSelector("while-menu.txt")), false);
  assert.equal(await page.locator("#menu-ctx").isVisible(), true);
  await page.keyboard.press("Escape");
  await listingRow(page, "while-menu.txt").waitFor({timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

// A press or a drag whose end the browser never reports (the system took over the gesture, or the
// drag ended where the page does not hear it) holds the redraw for a few seconds at most.
const LOST_END = 5000 + 3 * LIVE;

test("a press whose end never arrives stops holding the listing redraw after a few seconds", async t => {
  removeAfter(t, "after-lost-press.txt");
  const {context, page, errors} = await openLive(t);
  await page.evaluate(() => document.querySelector("#results").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, pointerType: "touch", isPrimary: true})));
  const reported = changeReported(page, apiPath(fixture.documents), "after-lost-press.txt");
  writeFileSync(inDocuments("after-lost-press.txt"), "x\n");
  await reported;
  // The press holds the redraw at first, like a finger on the screen.
  assert.equal(await happensWithin(page, 2500, selector => Boolean(document.querySelector(selector)), rowSelector("after-lost-press.txt")), false);
  await listingRow(page, "after-lost-press.txt").waitFor({timeout: LOST_END});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a drag whose end never arrives stops holding the listing redraw after a few seconds", async t => {
  removeAfter(t, "after-lost-drag.txt");
  const {context, page, errors} = await openLive(t);
  await listingRow(page, "texto.txt").evaluate(row => row.dispatchEvent(new DragEvent("dragstart", {bubbles: true, cancelable: true, dataTransfer: new DataTransfer()})));
  // The pointer moves on, which never happens while the browser still drags.
  await page.evaluate(() => document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, pointerType: "mouse", buttons: 0})));
  const reported = changeReported(page, apiPath(fixture.documents), "after-lost-drag.txt");
  writeFileSync(inDocuments("after-lost-drag.txt"), "x\n");
  await reported;
  assert.equal(await happensWithin(page, 2500, selector => Boolean(document.querySelector(selector)), rowSelector("after-lost-drag.txt")), false);
  await listingRow(page, "after-lost-drag.txt").waitFor({timeout: LOST_END});
  assert.equal(await page.evaluate(() => document.body.classList.contains("arrastando-itens")), false, "the expired drag is cleared");
  assert.deepEqual(errors, []);
  await context.close();
});

test("a drag still going on keeps holding the redraw past a few seconds", async t => {
  removeAfter(t, "during-drag.txt");
  const {context, page, errors} = await openLive(t);
  await listingRow(page, "texto.txt").evaluate(row => row.dispatchEvent(new DragEvent("dragstart", {bubbles: true, cancelable: true, dataTransfer: new DataTransfer()})));
  await page.evaluate(() => document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, pointerType: "mouse", buttons: 0})));
  const reported = changeReported(page, apiPath(fixture.documents), "during-drag.txt");
  writeFileSync(inDocuments("during-drag.txt"), "x\n");
  await reported;
  // The browser keeps sending drag events while the drag goes on.
  for (let second = 0; second < 8; second++) {
    await page.evaluate(() => document.querySelector("#results").dispatchEvent(new DragEvent("dragover", {bubbles: true, cancelable: true, dataTransfer: new DataTransfer()})));
    await delay(1000);
    assert.equal(await listingRow(page, "during-drag.txt").count(), 0, `redrawn during the drag, after ${second + 1} s`);
  }
  await listingRow(page, "texto.txt").evaluate(row => row.dispatchEvent(new DragEvent("dragend", {bubbles: true})));
  await listingRow(page, "during-drag.txt").waitFor({timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a mouse that moves with no button pressed ends a press at once", async t => {
  removeAfter(t, "after-mouse-move.txt");
  const {context, page, errors} = await openLive(t);
  await page.evaluate(() => document.querySelector("#results").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, pointerType: "mouse", buttons: 1})));
  const reported = changeReported(page, apiPath(fixture.documents), "after-mouse-move.txt");
  writeFileSync(inDocuments("after-mouse-move.txt"), "x\n");
  await reported;
  assert.equal(await happensWithin(page, 1000, selector => Boolean(document.querySelector(selector)), rowSelector("after-mouse-move.txt")), false);
  await page.evaluate(() => document.dispatchEvent(new PointerEvent("pointermove", {bubbles: true, pointerType: "mouse", buttons: 0})));
  await listingRow(page, "after-mouse-move.txt").waitFor({timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a folder that changes while it is read is read again instead of showing an error", async t => {
  const {context, page, errors} = await openLive(t);
  let answered = 0;
  await page.route("**/api/list?*", route => {
    if (new URL(route.request().url()).searchParams.get("path")?.endsWith("/notas") && answered++ === 0) {
      return route.fulfill({status: 409, contentType: "application/json", body: JSON.stringify({error: "conflict"})});
    }
    return route.continue();
  });
  await listingRow(page, "notas").locator("button.nome-link").click();
  await listingRow(page, "projeto.md").waitFor({timeout: LIVE});
  assert.equal(await page.locator("#results", {hasText: "A pasta mudou durante a leitura"}).count(), 0);
  assert.equal(answered >= 2, true);
  assert.deepEqual(unexpected(errors, 409), []);
  await context.close();
});

test("an open tree branch shows folders created and deleted outside the app within 2 s", async t => {
  removeAfter(t, "arquivo/created-outside");
  const {context, page, errors} = await openLive(t);
  // Opening a document reveals its folder in the tree; back in the folder tab, "arquivo" opens there.
  await openFromListing(page, "texto.txt");
  await tabNamed(page, "documents").click();
  const branch = apiPath(inDocuments("arquivo"));
  await treeRow(page, branch).locator("button.chev").click();
  await treeRow(page, `${branch}/2024`).waitFor();

  mkdirSync(inDocuments("arquivo/created-outside"));
  await treeRow(page, `${branch}/created-outside`).waitFor({timeout: LIVE});
  rmSync(inDocuments("arquivo/created-outside"), {recursive: true});
  await treeRow(page, `${branch}/created-outside`).waitFor({state: "detached", timeout: LIVE});
  assert.deepEqual(errors, []);
  await context.close();
});

test("a burst of 100 files written outside the app costs a few reads of the open folder", async t => {
  const names = Array.from({length: 100}, (_value, index) => `burst-${String(index + 1).padStart(3, "0")}.txt`);
  removeAfter(t, ...names);
  const {context, page, errors} = await openLive(t);
  const sent = recordRequests(page);
  const start = Date.now();
  for (const name of names) writeFileSync(inDocuments(name), `${name}\n`);
  await page.waitForFunction(() => document.querySelectorAll('#results tr.item[data-path*="/burst-"]').length === 100, null, {timeout: LIVE, polling: 50});
  const reads = () => sent.filter(request => request.at >= start && request.route === "list" && request.path === apiPath(fixture.documents));
  await quietFor(reads, 1000);
  // Reads the server refused because the folder changed meanwhile are made again and do not count.
  const done = reads().filter(request => request.status !== 409);
  assert.ok(done.length >= 1 && done.length <= 5, `${done.length} reads of the open folder`);
  assert.deepEqual(unexpected(errors, 409), []);
  await context.close();
});

// ---- Open documents ----

test("a document open without edits shows the new text by itself, with a short notice", async t => {
  createFiles(t, {"live-note.md": "# Live note\n\nFirst version.\n"});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "live-note.md");
  await waitForShown(page, ".markdown-preview", "First version.");
  writeBySwap(inDocuments("live-note.md"), "# Live note\n\nSecond version, written through a temporary name.\n");
  await waitForShown(page, ".markdown-preview", "Second version", LIVE);
  await page.waitForFunction(() => {
    const toast = document.querySelector("#toast");
    return toast?.classList.contains("mostra") && toast.textContent === "Arquivo atualizado fora do app";
  }, null, {timeout: 1000});
  assert.equal(await noticeLabel(page).isVisible(), false);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a document with unsaved edits keeps them and offers the outside version under \"Mudou fora do app\"", async t => {
  createFiles(t, {"edited-note.md": "# Edited note\n\nFirst version.\n"});
  const {context, page, errors} = await openLive(t);
  await editNote(page, "edited-note.md", "Typed here, not saved.");
  const outside = "# Edited note\n\nVersion written outside the app.\n";
  writeBySwap(inDocuments("edited-note.md"), outside);
  await waitForNotice(page, "Mudou fora do app");
  const text = await shownText(page, ".cm-content");
  assert.ok(text.includes("Typed here, not saved.") && !text.includes("written outside"), text);
  const choices = page.locator(`${SHOWN} .conflict-actions button`);
  assert.deepEqual(await choices.allTextContents(), ["Copiar versão do servidor", "Recarregar versão do servidor"]);
  await choices.filter({hasText: "Copiar versão do servidor"}).click();
  await page.waitForFunction(() => window.copiedTexts.length > 0);
  assert.deepEqual(await page.evaluate(() => window.copiedTexts), [outside]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("reloading the outside version replaces the unsaved edits and clears the notice", async t => {
  createFiles(t, {"reloaded-note.md": "# Reloaded note\n\nFirst version.\n"});
  const {context, page, errors} = await openLive(t);
  await editNote(page, "reloaded-note.md", "Typed here, then discarded.");
  writeBySwap(inDocuments("reloaded-note.md"), "# Reloaded note\n\nVersion written outside the app.\n");
  await waitForNotice(page, "Mudou fora do app");
  await page.locator(`${SHOWN} .conflict-actions button`, {hasText: "Recarregar versão do servidor"}).click();
  await page.locator("#operation-dialog button", {hasText: "Substituir"}).click();
  await waitForShown(page, ".cm-content", "Version written outside the app.");
  assert.equal((await shownText(page, ".cm-content")).includes("then discarded"), false);
  assert.equal(await noticeLabel(page).isVisible(), false);
  assert.equal(await shownText(page, ".ed-estado"), "Salvo");
  assert.deepEqual(errors, []);
  await context.close();
});

test("the app's own save raises no outside-change notice", async t => {
  createFiles(t, {"saved-note.md": "# Saved note\n\nFirst version.\n"});
  const {context, page, errors} = await openLive(t);
  await editNote(page, "saved-note.md", "Saved by the user.");
  const reported = changeReported(page, apiPath(fixture.documents), "saved-note.md", 5000);
  await page.keyboard.press("Control+s");
  await page.waitForFunction(selector => document.querySelector(selector)?.textContent === "Salvo", `${SHOWN} .ed-estado`);
  // The server reports the save like any other write; the app must recognize it as its own.
  await reported;
  const raised = await happensWithin(page, 1500, selector => {
    const toast = document.querySelector("#toast");
    const label = document.querySelector(selector);
    return (toast?.classList.contains("mostra") && toast.textContent.includes("fora do app")) || (label && !label.hidden);
  }, `${SHOWN} .ed-erro`);
  assert.equal(raised, false);
  assert.ok(readFileSync(inDocuments("saved-note.md"), "utf8").includes("Saved by the user."));
  assert.deepEqual(errors, []);
  await context.close();
});

test("a document deleted outside the app stays open under \"Removido fora do app\"", async t => {
  createFiles(t, {"removed-note.md": "# Removed note\n\nText that stays open.\n"});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "removed-note.md");
  await waitForShown(page, ".markdown-preview", "Text that stays open.");
  unlinkSync(inDocuments("removed-note.md"));
  await waitForNotice(page, "Removido fora do app");
  assert.ok((await shownText(page, ".markdown-preview")).includes("Text that stays open."));
  assert.deepEqual(unexpected(errors, 404), []);
  await context.close();
});

test("a document renamed outside the app stays open under \"Removido fora do app\"", async t => {
  createFiles(t, {"moved-note.md": "# Moved note\n\nText that stays open.\n"});
  removeAfter(t, "moved-note-renamed.md");
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "moved-note.md");
  await waitForShown(page, ".markdown-preview", "Text that stays open.");
  renameSync(inDocuments("moved-note.md"), inDocuments("moved-note-renamed.md"));
  await waitForNotice(page, "Removido fora do app");
  assert.ok((await shownText(page, ".markdown-preview")).includes("Text that stays open."));
  assert.deepEqual(unexpected(errors, 404), []);
  await context.close();
});

test("a plain text document open without edits shows the new text by itself", async t => {
  createFiles(t, {"live-text.txt": "plain text, version 1\n"});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "live-text.txt");
  await waitForShown(page, "textarea.plain-editor", "version 1");
  writeBySwap(inDocuments("live-text.txt"), "plain text, version 2\n");
  await waitForShown(page, "textarea.plain-editor", "version 2", LIVE);
  assert.deepEqual(errors, []);
  await context.close();
});

test("an open image preview shows the new version within 2 s", async t => {
  createFiles(t, {"live-image.png": png(100, 50, [200, 30, 30])});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "live-image.png");
  await page.waitForFunction(() => document.querySelector("#results img.raster-preview")?.naturalWidth === 100);
  writeBySwap(inDocuments("live-image.png"), png(220, 80, [30, 60, 200]));
  await page.waitForFunction(() => document.querySelector("#results img.raster-preview")?.naturalWidth === 220, null, {timeout: LIVE, polling: 50});
  assert.deepEqual(errors, []);
  await context.close();
});

// Changes the stored interface state through the API, as another browser would: `change` maps the
// stored document (without its revision) to the next one.
async function changeStoredState(page, change) {
  const {stateRevision, ...saved} = await page.evaluate(async () => (await fetch("api/state", {headers: {Accept: "application/json"}, cache: "no-store"})).json());
  const status = await page.evaluate(async body => (await fetch("api/state", {
    method: "PUT", body, headers: {"Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": document.getElementById("hf-app").dataset.csrf},
  })).status, JSON.stringify({...change(saved), baseRevision: stateRevision}));
  assert.equal(status, 200);
}

test("reading the shared state again leaves an image preview on screen and keyboard focus where it was", async t => {
  createFiles(t, {"ui-image.png": png(100, 50, [200, 30, 30])});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "ui-image.png");
  await page.waitForFunction(() => document.querySelector("#results img.raster-preview")?.naturalWidth === 100);
  await page.locator("#tabs .aba-nome").first().focus();
  const focused = await page.evaluate(() => document.activeElement.textContent);
  const favorite = inDocuments("texto.txt");
  t.after(() => changeStoredState(page, state => ({...state, items: state.items.filter(item => item.path !== favorite)})).catch(() => {}));
  await changeStoredState(page, state => ({...state, items: [...state.items.filter(item => item.path !== favorite), {path: favorite, labelIds: [], favorite: true, emoji: null, inode: null, device: null}]}));
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await page.locator(`#favoritos .node-row[data-path="${apiPath(favorite)}"]`).waitFor({timeout: LIVE});
  assert.equal(await page.locator("#results img.raster-preview").count(), 1, "the preview stays");
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("aba-nome") ? document.activeElement.textContent : null), focused);
  assert.deepEqual(errors, []);
  await context.close();
});

test("an open PDF preview asks for the new version within 2 s and shows it", async t => {
  createFiles(t, {"live-document.pdf": pdf([[1, 0, 0]])});
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "live-document.pdf");
  await page.waitForFunction(() => document.querySelectorAll("#results .visual-preview canvas").length === 1);
  const fetched = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/api/preview")
    && new URL(request.url()).searchParams.get("path") === apiPath(inDocuments("live-document.pdf")), {timeout: LIVE});
  writeBySwap(inDocuments("live-document.pdf"), pdf([[1, 0, 0], [0, 0, 1]]));
  await fetched;
  // Drawing the pages is the PDF viewer's work, which a busy machine slows down.
  await page.waitForFunction(() => document.querySelectorAll("#results .visual-preview canvas").length === 2, null, {polling: 50});
  assert.deepEqual(errors, []);
  await context.close();
});

// ---- Hidden tab, a server without change notices, and an ended session ----

test("a hidden tab asks and reads nothing, and on return reads again what it shows", async t => {
  createFiles(t, {"hidden-note.md": "# Hidden note\n\nFirst version.\n"});
  removeAfter(t, "while-hidden.txt");
  const {context, page, errors} = await openLive(t);
  await openFromListing(page, "hidden-note.md");
  await waitForShown(page, ".markdown-preview", "First version.");
  await tabNamed(page, "documents").click();
  await listingRow(page, "hidden-note.md").waitFor();
  const sent = recordRequests(page);

  await setVisible(page, false);
  const hidden = Date.now();
  writeFileSync(inDocuments("while-hidden.txt"), "x\n");
  writeBySwap(inDocuments("hidden-note.md"), "# Hidden note\n\nVersion written while the tab was hidden.\n");
  // A visible tab would have reacted well within this time.
  await delay(LIVE);
  assert.deepEqual(sent.filter(request => request.at >= hidden && ["changes", "list", "file"].includes(request.route)), []);
  assert.equal(await listingRow(page, "while-hidden.txt").count(), 0);

  const shown = Date.now();
  const noteRead = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/api/file")
    && new URL(request.url()).searchParams.get("path") === apiPath(inDocuments("hidden-note.md")), {timeout: LIVE});
  await setVisible(page, true);
  await Promise.all([noteRead, listingRow(page, "while-hidden.txt").waitFor({timeout: LIVE})]);
  assert.ok(sent.some(request => request.at >= shown && request.route === "changes"), "change requests resume");
  await tabNamed(page, "hidden-note.md").click();
  await waitForShown(page, ".markdown-preview", "while the tab was hidden", LIVE);
  assert.deepEqual(errors, []);
  await context.close();
});

test("without change notices on the server, the app stops asking and reads the folder again on return to the tab", async t => {
  const {context, page, errors} = await openLive(t);
  await page.route("**/api/changes", route => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({supported: false, epoch: "unsupported", seq: 0, resync: false, changes: [], rejected: []}),
  }));
  const sent = recordRequests(page);
  const firstAnswer = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/api/changes"));
  await page.reload();
  await waitForListing(page);
  await firstAnswer;
  await delay(LIVE);
  assert.equal(sent.filter(request => request.route === "changes").length, 1);

  await setVisible(page, false);
  const back = Date.now();
  const folderRead = page.waitForRequest(request => new URL(request.url()).pathname.endsWith("/api/list")
    && new URL(request.url()).searchParams.get("path") === apiPath(fixture.documents), {timeout: LIVE});
  await setVisible(page, true);
  await folderRead;
  await delay(1000);
  assert.deepEqual(sent.filter(request => request.at >= back && request.route === "changes"), []);
  assert.deepEqual(errors, []);
  await context.close();
});

test("after the session ends, the app stops asking about changes", async t => {
  removeAfter(t, "after-session.txt");
  const {context, page, errors} = await openLive(t);
  const sent = recordRequests(page);
  await context.clearCookies();
  // A change answers the request in progress; the requests that follow carry no session.
  writeFileSync(inDocuments("after-session.txt"), "x\n");
  await page.waitForFunction(() => document.querySelector("#operation-dialog")?.open
    && document.querySelector("#operation-dialog h2")?.textContent === "Sua sessão terminou", null, {timeout: 5000});
  const ended = Date.now();
  // Longer than the first retry after a failure (1 s), so a retry would show.
  await delay(3000);
  assert.deepEqual(sent.filter(request => request.at >= ended && request.route === "changes"), []);
  assert.deepEqual(unexpected(errors, 401), []);
  await context.close();
});

// ---- The open folder deleted and created again, each test on an instance of its own ----

const ABSENT = "#results > [data-pasta-sumiu]";

// Opens <documents>/box/output, which holds old.txt.
async function openOutputFolder(t) {
  const opened = await openIsolated(t, documents => {
    mkdirSync(join(documents, "box", "output"), {recursive: true});
    writeFileSync(join(documents, "box", "output", "old.txt"), "old\n");
  });
  await enterFolders(opened.page, ["box", "output"]);
  return {...opened, box: join(opened.documents, "box"), output: join(opened.documents, "box", "output")};
}

async function waitForAbsentFolder(page) {
  await page.locator(ABSENT).waitFor({timeout: LIVE});
  assert.equal(await page.locator("#results tr.item").count(), 0);
}

async function waitForFolderBack(page, name) {
  await Promise.all([listingRow(page, name).waitFor({timeout: LIVE}), page.locator(ABSENT).waitFor({state: "detached", timeout: LIVE})]);
  assert.equal(await listingRow(page, "old.txt").count(), 0);
}

test("the open folder deleted outside the app says so, and shows its new content when created again 1 s later", async t => {
  const {context, page, errors, output} = await openOutputFolder(t);
  const deleted = Date.now();
  rmSync(output, {recursive: true});
  await waitForAbsentFolder(page);
  await delay(Math.max(0, 1000 - (Date.now() - deleted)));
  mkdirSync(output);
  writeFileSync(join(output, "new.txt"), "new\n");
  await waitForFolderBack(page, "new.txt");
  writeFileSync(join(output, "next.txt"), "next\n");
  await listingRow(page, "next.txt").waitFor({timeout: LIVE});
  // A missing folder answers 403 or 404 (HF-API-006).
  assert.deepEqual(unexpected(errors, 403, 404, 409), []);
  await context.close();
});

test("the open folder created again after more than one change request shows its new content", async t => {
  const {context, page, errors, box, output} = await openOutputFolder(t);
  const sent = recordRequests(page);
  rmSync(output, {recursive: true});
  await waitForAbsentFolder(page);
  const absent = Date.now();
  // Each change next to the missing folder answers the request in progress, and the app asks again.
  for (const name of ["unrelated-1.txt", "unrelated-2.txt"]) {
    const reported = changeReported(page, apiPath(box), name);
    writeFileSync(join(box, name), "x\n");
    await reported;
  }
  const asked = () => sent.filter(request => request.at >= absent && request.route === "changes").length;
  for (let waited = 0; asked() < 2; waited += 50) {
    assert.ok(waited < LIVE, `${asked()} change requests while the folder was missing`);
    await delay(50);
  }
  mkdirSync(output);
  writeFileSync(join(output, "new.txt"), "new\n");
  await waitForFolderBack(page, "new.txt");
  assert.deepEqual(unexpected(errors, 403, 404, 409), []);
  await context.close();
});

test("the open folder and its parent deleted outside the app show the new content when created again", async t => {
  const {context, page, errors, box, output} = await openOutputFolder(t);
  const deleted = Date.now();
  rmSync(box, {recursive: true});
  await waitForAbsentFolder(page);
  await delay(Math.max(0, 1000 - (Date.now() - deleted)));
  mkdirSync(output, {recursive: true});
  writeFileSync(join(output, "new.txt"), "new\n");
  await waitForFolderBack(page, "new.txt");
  assert.deepEqual(unexpected(errors, 403, 404, 409), []);
  await context.close();
});

test("a file written right after a folder is read, before its watch starts, still appears", async t => {
  const {context, page, errors, documents} = await openIsolated(t, documents => {
    mkdirSync(join(documents, "gap"));
    writeFileSync(join(documents, "gap", "one.txt"), "1\n");
  });
  const gap = join(documents, "gap");
  // Change requests reach the server 1 s late, so the write lands before the new folder is named.
  await page.route("**/api/changes", async route => { await delay(1000); await route.continue().catch(() => {}); });
  const read = page.waitForResponse(response => new URL(response.url()).pathname.endsWith("/api/list")
    && new URL(response.url()).searchParams.get("path") === apiPath(gap));
  await openFromListing(page, "gap");
  await read;
  writeFileSync(join(gap, "in-the-gap.txt"), "written right after the read\n");
  await listingRow(page, "one.txt").waitFor();
  await listingRow(page, "in-the-gap.txt").waitFor({timeout: 1000 + LIVE});
  await page.unrouteAll({behavior: "ignoreErrors"});
  assert.deepEqual(errors, []);
  await context.close();
});
