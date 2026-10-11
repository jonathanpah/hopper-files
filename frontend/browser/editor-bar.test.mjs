// Formatting bars of the Markdown view: the floating bar over a selection (fine pointer only), and
// the fixed toolbar, which wraps with a mouse instead of hiding buttons past the edge.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { closeBrowser, listingRow, openApp, showDocuments, startFixture, waitForListing } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const bar = page => page.locator(".lp-flutuante");
const editorText = page => page.evaluate(() => [...document.querySelectorAll(".cm-content .cm-line")].map(line => line.textContent).join("\n"));
const selected = page => page.evaluate(() => getSelection().toString());

async function openMarkdownView(page) {
  await showDocuments(page);
  await listingRow(page, "notas").locator("button.nome-link").click();
  await waitForListing(page);
  await listingRow(page, "projeto.md").locator("button.nome-link").click();
  await page.locator("#seg-cru").click();
  await page.locator(".cm-content").waitFor();
}
async function selectWord(page, word) {
  const line = page.locator(".cm-line", {hasText: word});
  const box = await line.boundingBox();
  const offset = await line.evaluate((element, text) => {
    const node = [...element.childNodes].find(child => child.textContent.includes(text));
    const range = document.createRange();
    const textNode = node.nodeType === 3 ? node : node.firstChild;
    const start = textNode.textContent.indexOf(text);
    range.setStart(textNode, start); range.setEnd(textNode, start + text.length);
    const rect = range.getBoundingClientRect();
    return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
  }, word);
  assert.ok(box);
  await page.mouse.dblclick(offset.x, offset.y);
}

test("a mouse selection shows the bar above it, and Negrito formats without losing the selection", async () => {
  const {context, page, errors} = await openApp(fixture);
  await openMarkdownView(page);
  assert.equal(await bar(page).isVisible(), false);
  await selectWord(page, "várias");
  await bar(page).waitFor({state: "visible"});
  assert.equal(await selected(page), "várias");
  const barBox = await bar(page).boundingBox();
  const lineBox = await page.locator(".cm-line", {hasText: "várias"}).boundingBox();
  assert.ok(barBox.y + barBox.height <= lineBox.y + 1, "the bar sits above the selected line");
  await bar(page).getByRole("button", {name: "Negrito"}).click();
  assert.match(await editorText(page), /\*\*várias\*\*/);
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("cm-content")), true);
  assert.equal(await bar(page).isVisible(), true);
  assert.equal(await bar(page).getByRole("button", {name: "Negrito"}).getAttribute("aria-pressed"), "true");
  assert.deepEqual(errors, []);
  await context.close();
});

test("Esc hides the bar and keeps the selection; changing the selection shows it again", async () => {
  const {context, page, errors} = await openApp(fixture);
  await openMarkdownView(page);
  await selectWord(page, "palavras");
  await bar(page).waitFor({state: "visible"});
  await page.keyboard.press("Escape");
  assert.equal(await bar(page).isVisible(), false);
  assert.equal(await selected(page), "palavras");
  await page.keyboard.press("Shift+ArrowLeft");
  await bar(page).waitFor({state: "visible"});
  await page.keyboard.press("ArrowRight");
  await bar(page).waitFor({state: "hidden"});
  assert.deepEqual(errors, []);
  await context.close();
});

test("the bar hides when the document leaves the Markdown view and when it closes", async () => {
  const {context, page, errors} = await openApp(fixture);
  await openMarkdownView(page);
  await selectWord(page, "nota");
  await bar(page).waitFor({state: "visible"});
  await page.locator("#seg-visual").click();
  await bar(page).waitFor({state: "hidden"});
  await page.locator("#seg-cru").click();
  await page.keyboard.press("Meta+w").catch(() => {});
  assert.deepEqual(errors, []);
  await context.close();
  // Nothing was saved: the bar does not write by itself.
  assert.equal(readFileSync(join(fixture.documents, "notas/projeto.md"), "utf-8").includes("**nota**"), false);
});

test("touch screens keep the fixed palette and get no floating bar", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "iphone"});
  await showDocuments(page);
  await listingRow(page, "notas").locator("button.nome-link").tap();
  await waitForListing(page);
  await listingRow(page, "projeto.md").locator("button.nome-link").tap();
  await page.locator("#seg-cru").tap();
  await page.locator(".cm-content").waitFor();
  assert.equal(await bar(page).count(), 0);
  assert.equal(await page.locator(".lp-paleta").isVisible(), true);
  assert.deepEqual(errors, []);
  await context.close();
});

test("with a mouse in a narrow editor, the formatting toolbar wraps and shows every button", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "laptop"});
  await page.setViewportSize({width: 760, height: 720});
  await openMarkdownView(page);
  const hidden = await page.locator(".lp-paleta").evaluate(palette => {
    const box = palette.getBoundingClientRect();
    return [...palette.querySelectorAll(".lp-btn")].filter(button => button.offsetParent !== null).filter(button => {
      const own = button.getBoundingClientRect();
      return own.right > box.right + 0.5 || own.left < box.left - 0.5 || own.bottom > box.bottom + 0.5;
    }).map(button => button.getAttribute("aria-label"));
  });
  assert.deepEqual(hidden, []);
  const wrapped = await page.locator(".lp-paleta").evaluate(palette => new Set([...palette.querySelectorAll(".lp-btn")].map(button => Math.round(button.getBoundingClientRect().top))).size);
  assert.ok(wrapped >= 2, "the toolbar takes more than one line at this width");
  assert.deepEqual(errors, []);
  await context.close();
});
