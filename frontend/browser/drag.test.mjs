// Spring-loaded tree folders: holding dragged items over a collapsed folder of the tree opens it.
// Headless WebKit does not run a native drag from mouse moves, so the tests send the drag events
// a browser sends, with one DataTransfer for the whole gesture.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { closeBrowser, listingRow, openApp, startFixture, waitForListing } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const treeRow = (page, relative) => page.locator(`#tree .node-row[data-path="${(fixture.documents + "/" + relative).slice(1)}"]`);
const expanded = async (page, relative) => (await treeRow(page, relative).getAttribute("aria-expanded")) === "true";

// Shows the documents folder in the listing with its ancestors expanded in the tree and
// "arquivo" collapsed: open "arquivo" from the listing (the tree reveals it), then go back.
async function prepare(page) {
  await listingRow(page, "arquivo").locator("button.nome-link").click();
  await page.waitForFunction(() => document.querySelector("#results")?.textContent.includes("2024"));
  await page.goBack();
  await waitForListing(page);
  await treeRow(page, "arquivo").waitFor();
  assert.equal(await expanded(page, "arquivo"), false);
}

async function dragStart(page, name) {
  await listingRow(page, name).evaluate(row => {
    window.dragData = new DataTransfer();
    row.dispatchEvent(new DragEvent("dragstart", {bubbles: true, cancelable: true, dataTransfer: window.dragData}));
  });
}
// dragenter and then dragover every 100 ms, as a browser does while the pointer rests on a row, for
// `milliseconds` of real time: a slow machine does not stretch a short pass past the opening delay.
async function hover(page, locator, milliseconds) {
  const until = Date.now() + milliseconds;
  await locator.evaluate(row => row.dispatchEvent(new DragEvent("dragenter", {bubbles: true, cancelable: true, dataTransfer: window.dragData})));
  do {
    await locator.evaluate(row => row.dispatchEvent(new DragEvent("dragover", {bubbles: true, cancelable: true, dataTransfer: window.dragData})));
    await page.waitForTimeout(Math.min(100, Math.max(0, until - Date.now())));
  } while (Date.now() < until);
}
async function leave(locator) {
  await locator.evaluate(row => row.dispatchEvent(new DragEvent("dragleave", {bubbles: true, cancelable: true, dataTransfer: window.dragData, relatedTarget: document.body})));
}
async function dragEnd(page, name, dropEffect) {
  await listingRow(page, name).evaluate((row, effect) => {
    window.dragData.dropEffect = effect;
    row.dispatchEvent(new DragEvent("dragend", {bubbles: true, cancelable: true, dataTransfer: window.dragData}));
  }, dropEffect);
}

test("holding a dragged item over collapsed folders opens them, and the drop moves the item", async () => {
  const {context, page, errors} = await openApp(fixture);
  await prepare(page);
  await dragStart(page, "texto.txt");
  await hover(page, treeRow(page, "arquivo"), 800);
  assert.equal(await expanded(page, "arquivo"), true);
  await treeRow(page, "arquivo/2024").waitFor();
  await hover(page, treeRow(page, "arquivo/2024"), 800);
  assert.equal(await expanded(page, "arquivo/2024"), true);
  const target = treeRow(page, "arquivo/2024/janeiro");
  await target.waitFor();
  await hover(page, target, 200);
  await target.evaluate(row => row.dispatchEvent(new DragEvent("drop", {bubbles: true, cancelable: true, dataTransfer: window.dragData})));
  await dragEnd(page, "texto.txt", "move");
  await page.waitForFunction(() => !document.querySelector("#results")?.textContent.includes("texto.txt"));
  assert.equal(existsSync(join(fixture.documents, "arquivo/2024/janeiro/texto.txt")), true);
  assert.equal(existsSync(join(fixture.documents, "texto.txt")), false);
  // A successful drop keeps the folders it opened.
  assert.equal(await expanded(page, "arquivo"), true);
  assert.equal(await expanded(page, "arquivo/2024"), true);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a cancelled drag closes again the folders it opened", async () => {
  const {context, page, errors} = await openApp(fixture);
  await page.reload(); await waitForListing(page);
  // The previous test left "arquivo" expanded in this browser: collapse it through its chevron.
  if (await treeRow(page, "arquivo").count() && await expanded(page, "arquivo")) await treeRow(page, "arquivo").locator(".chev").click();
  await prepare(page);
  await dragStart(page, "notas");
  await hover(page, treeRow(page, "arquivo"), 800);
  assert.equal(await expanded(page, "arquivo"), true);
  await leave(treeRow(page, "arquivo"));
  await dragEnd(page, "notas", "none");
  await page.waitForTimeout(100);
  assert.equal(await expanded(page, "arquivo"), false);
  assert.equal(existsSync(join(fixture.documents, "notas")), true);
  assert.deepEqual(errors, []);
  await context.close();
});

test("passing over a collapsed folder quickly does not open it", async () => {
  const {context, page, errors} = await openApp(fixture);
  await page.reload(); await waitForListing(page);
  if (await treeRow(page, "arquivo").count() && await expanded(page, "arquivo")) await treeRow(page, "arquivo").locator(".chev").click();
  await prepare(page);
  await dragStart(page, "notas");
  await hover(page, treeRow(page, "arquivo"), 300);
  await leave(treeRow(page, "arquivo"));
  await page.waitForTimeout(600);
  assert.equal(await expanded(page, "arquivo"), false);
  await dragEnd(page, "notas", "none");
  assert.deepEqual(errors, []);
  await context.close();
});
