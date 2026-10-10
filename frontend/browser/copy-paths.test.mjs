// Copy path from the context menu: a folder, a file from the keyboard, a tree node, the "/" node,
// several checked items, entries that accept no other action, and the touch long press.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { chmodSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { closeBrowser, keyboardMenu, listingRow, openApp, startFixture, waitForListing } from "./harness.mjs";

let fixture;
// Root opens any folder, so the entries the account cannot open do not exist for it.
const asRoot = process.getuid?.() === 0 && "root opens folders without permission";

// Next to the fixture files: links, a folder without permission (with a note inside), a special
// file, and a name that is not UTF-8.
before(async () => {
  fixture = await startFixture();
  const place = name => join(fixture.documents, name);
  symlinkSync("notas", place("atalho-notas"));
  symlinkSync("texto.txt", place("atalho-texto"));
  symlinkSync("nada-aqui", place("quebrado"));
  mkdirSync(place("fechada"));
  writeFileSync(place("fechada/dentro.md"), "# Dentro\n");
  chmodSync(place("fechada"), 0o000);
  execFileSync("mkfifo", [place("fila")]);
  writeFileSync(Buffer.concat([Buffer.from(place("nome-")), Buffer.from([0xff]), Buffer.from(".txt")]), "texto\n");
});
after(async () => {
  await closeBrowser();
  try { chmodSync(join(fixture.documents, "fechada"), 0o755); } catch { /* not created */ }
  await fixture?.stop();
});

const at = name => `${fixture.documents}/${name}`;
const menu = page => page.locator("#menu-ctx");
const entryRow = (page, name) => page.locator(`#results tr.item[data-path="${at(name).slice(1)}"]`);
const choose = (page, label) => menu(page).getByRole("menuitem", {name: label, exact: true}).click();
const focusedRowPath = page => page.evaluate(() => document.activeElement?.closest("tr.item")?.dataset.path ?? null);

// The open menu as shown: entries, titles, and notes, with "—" for each separator.
const menuContents = page => menu(page).evaluate(node => [...node.querySelectorAll(".item-actions > *")]
  .map(child => child.classList.contains("ctx-sep") ? "—" : child.textContent.trim()));
const firstGroup = contents => contents.slice(0, contents.includes("—") ? contents.indexOf("—") : contents.length);

// A secondary click with the mouse. Rows of entries that do not open are aria-disabled, which
// Playwright's own click refuses.
async function secondaryClick(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  await page.mouse.click(box.x + Math.min(160, box.width / 2), box.y + box.height / 2, {button: "right"});
  await menu(page).waitFor({state: "visible"});
}

// Runs `action` and returns what it wrote to the clipboard.
async function copied(page, action) {
  const count = await page.evaluate(() => window.copiedTexts.length);
  await action();
  await page.waitForFunction(count => window.copiedTexts.length > count, count);
  return page.evaluate(count => window.copiedTexts.slice(count), count);
}
async function toastText(page) {
  await page.locator("#toast.mostra").waitFor();
  return page.locator("#toast").textContent();
}

// A finger held still on `locator`. Headless WebKit has no touch-and-hold input, so the page receives
// the touch pointer events itself; the mouse waits on the same point, since a mouse move would cancel
// the hold. Returns whether the menu was still closed right after the touch began.
async function holdFinger(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  const point = {x: box.x + Math.min(120, box.width / 2), y: box.y + box.height / 2};
  await page.mouse.move(point.x, point.y);
  const touch = type => page.evaluate(({type, x, y}) => {
    document.elementFromPoint(x, y).dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerType: "touch", isPrimary: true, clientX: x, clientY: y}));
    return document.querySelector("#menu-ctx").hidden;
  }, {type, ...point});
  const closedAtTouch = await touch("pointerdown");
  await menu(page).waitFor({state: "visible"});
  await touch("pointerup");
  return closedAtTouch;
}

test("a folder's menu offers Copiar caminho after its open actions, and it copies the folder's absolute path", async () => {
  const {context, page, errors} = await openApp(fixture);
  await secondaryClick(page, listingRow(page, "notas"));
  assert.deepEqual(firstGroup(await menuContents(page)), ["Abrir em nova aba", "Calcular tamanho", "Copiar caminho"]);
  assert.deepEqual(await copied(page, () => choose(page, "Copiar caminho")), [at("notas")]);
  assert.equal(await toastText(page), "Caminho copiado");
  assert.equal(await menu(page).isVisible(), false);
  assert.deepEqual(errors, []);
  await context.close();
});

test("in the keyboard menu of a file, the arrows reach Copiar caminho, and Enter copies the path and returns focus to the row", async () => {
  const {context, page, errors} = await openApp(fixture);
  await keyboardMenu(listingRow(page, "texto.txt"));
  await menu(page).waitFor({state: "visible"});
  assert.deepEqual(firstGroup(await menuContents(page)), ["Abrir", "Baixar", "Copiar caminho"]);
  const focusedEntry = () => page.evaluate(() => document.activeElement?.closest("#menu-ctx") ? document.activeElement.textContent.trim() : null);
  assert.equal(await focusedEntry(), "Abrir");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  assert.equal(await focusedEntry(), "Copiar caminho");
  assert.deepEqual(await copied(page, () => page.keyboard.press("Enter")), [at("texto.txt")]);
  assert.equal(await menu(page).isVisible(), false);
  assert.equal(await focusedRowPath(page), at("texto.txt").slice(1));
  assert.deepEqual(errors, []);
  await context.close();
});

test("a tree node has the same menu as its listing row, and Copiar caminho copies its path", async () => {
  const {context, page, errors} = await openApp(fixture);
  await secondaryClick(page, listingRow(page, "notas"));
  const listingMenu = await menuContents(page);
  await page.keyboard.press("Escape");
  // An opened document is revealed in the tree, which expands down to the fixture folder. Unlike a
  // folder, it does not change the folder tab that later tests open on.
  await listingRow(page, "texto.txt").locator("button.nome-link").click();
  const node = page.locator(`#tree .node-row[data-path="${at("notas").slice(1)}"]`);
  await node.waitFor();
  await secondaryClick(page, node);
  assert.deepEqual(await menuContents(page), listingMenu);
  assert.deepEqual(await copied(page, () => choose(page, "Copiar caminho")), [at("notas")]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("the / node has a menu with only Copiar caminho, which copies /", async () => {
  const {context, page, errors} = await openApp(fixture);
  await secondaryClick(page, page.locator("#tree .raiz-vault"));
  assert.deepEqual(await menuContents(page), ["Copiar caminho"]);
  assert.deepEqual(await copied(page, () => choose(page, "Copiar caminho")), ["/"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("with several checked items, the menu copies all their paths, one per line, in listing order", async () => {
  const {context, page, errors} = await openApp(fixture);
  for (const name of ["texto.txt", "notas", "app"]) await listingRow(page, name).locator("input.check").click();
  const checkedInListing = await page.evaluate(() => [...document.querySelectorAll("#results tr.item")]
    .filter(row => row.querySelector("input.check")?.checked).map(row => `/${row.dataset.path}`));
  assert.deepEqual(checkedInListing, [at("app"), at("notas"), at("texto.txt")]);
  await secondaryClick(page, listingRow(page, "notas"));
  assert.deepEqual((await menuContents(page)).slice(0, 3), ["3 itens marcados", "Copiar os 3 caminhos", "—"]);
  assert.deepEqual(await copied(page, () => choose(page, "Copiar os 3 caminhos")), [checkedInListing.join("\n")]);
  assert.equal(await toastText(page), "3 caminhos copiados");
  assert.deepEqual(errors, []);
  await context.close();
});

for (const [name, entry, skip] of [
  ["atalho-notas", "a link to a folder"],
  ["atalho-texto", "a link to a file"],
  ["quebrado", "a broken link"],
  ["fechada", "a folder the account cannot open", asRoot],
  ["fila", "a special file"],
]) {
  test(`${entry} has a menu with only Copiar caminho, which copies its own path`, {skip}, async () => {
    const {context, page, errors} = await openApp(fixture);
    await secondaryClick(page, entryRow(page, name));
    assert.deepEqual(await menuContents(page), ["Copiar caminho"]);
    assert.deepEqual(await copied(page, () => choose(page, "Copiar caminho")), [at(name)]);
    assert.deepEqual(errors, []);
    await context.close();
  });
}

test("an entry without an address has no context menu", async () => {
  const {context, page, errors} = await openApp(fixture);
  const row = page.locator("#results tr.item").filter({has: page.locator("button.nome-link", {hasText: /^nome-/})});
  assert.equal(await row.count(), 1);
  // The menu of a row opens while the event is dispatched, so its state right after tells.
  const menuHidden = await row.evaluate(element => {
    const box = element.getBoundingClientRect();
    element.dispatchEvent(new MouseEvent("contextmenu", {bubbles: true, cancelable: true, detail: 1, button: 2, clientX: box.left + 100, clientY: box.top + box.height / 2}));
    return document.querySelector("#menu-ctx").hidden;
  });
  assert.equal(menuHidden, true);
  assert.deepEqual(errors, []);
  await context.close();
});

test("an item whose parent folder cannot be listed has a menu with only Copiar caminho", {skip: asRoot}, async () => {
  const {context, page, errors} = await openApp(fixture);
  // A label reaches the item without listing its folder; the page sets it through its own state API.
  const status = await page.evaluate(async path => {
    const csrf = document.getElementById("hf-app").dataset.csrf;
    const {stateRevision, ...saved} = await (await fetch("api/state", {headers: {Accept: "application/json"}, cache: "no-store"})).json();
    const items = [...saved.items.filter(item => item.path !== path), {path, labelIds: ["vermelho"], favorite: false, emoji: null, inode: null, device: null}];
    const response = await fetch("api/state", {
      method: "PUT", headers: {"Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrf},
      body: JSON.stringify({...saved, items, baseRevision: stateRevision}),
    });
    return response.status;
  }, at("fechada/dentro.md"));
  assert.equal(status, 200);
  await page.reload();
  await waitForListing(page);
  await page.locator("#etiquetas .et-cab", {hasText: "Vermelho"}).click();
  const item = page.locator("#results .res-item", {hasText: "dentro.md"});
  await item.waitFor();
  await secondaryClick(page, item);
  assert.deepEqual(await menuContents(page), ["Copiar caminho"]);
  assert.equal(await page.locator("#app-status").textContent(), "Este item não está mais disponível neste caminho.");
  assert.deepEqual(await copied(page, () => choose(page, "Copiar caminho")), [at("fechada/dentro.md")]);
  // WebKit reports the refused listing of the parent folder (403) as a console error.
  assert.deepEqual(errors.filter(error => !/status of 403/.test(error)), []);
  await context.close();
});

test("on touch, holding a finger on a row opens its menu, and tapping Copiar caminho copies the path", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "iphone"});
  assert.equal(await holdFinger(page, listingRow(page, "texto.txt")), true);
  assert.deepEqual(firstGroup(await menuContents(page)), ["Abrir", "Baixar", "Copiar caminho"]);
  assert.deepEqual(await copied(page, () => menu(page).getByRole("menuitem", {name: "Copiar caminho", exact: true}).tap()), [at("texto.txt")]);
  assert.equal(await toastText(page), "Caminho copiado");
  assert.equal(await menu(page).isVisible(), false);
  assert.deepEqual(errors, []);
  await context.close();
});
