// Favorites in the sidebar: each favorite folder is the first node of its own tree, a file favorite is
// a single row, expanded branches return after a reload in the same browser, and items dropped on
// Favorites become favorites.
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { after, before, test } from "node:test";
import { closeBrowser, listingRow, openApp, startFixture, waitForListing } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

// Absolute path of a fixture entry; rows keep it without the leading "/" in data-path.
const at = name => `${fixture.documents}/${name}`;
const favoriteRow = (page, name) => page.locator(`#favoritos .node-row[data-path="${at(name).slice(1)}"]`);
const currentFavoriteRow = (page, name) => page.locator(`#favoritos .node-row.ativo[data-path="${at(name).slice(1)}"]`);
const rootTreeRows = page => page.locator("#tree .node-row");
const lastCrumb = page => page.locator("#breadcrumbs .seg").last().textContent();

// Visible rows under Favorites as "name level [open|closed] [current]", names relative to the documents folder.
function favoriteRows(page) {
  return page.evaluate(prefix => [...document.querySelectorAll("#favoritos .node-row")].filter(row => row.offsetParent !== null).map(row => {
    const expanded = row.getAttribute("aria-expanded");
    return [row.dataset.path.slice(prefix.length), row.getAttribute("aria-level"), expanded && (expanded === "true" ? "open" : "closed"), row.classList.contains("ativo") && "current"].filter(Boolean).join(" ");
  }), `${fixture.documents.slice(1)}/`);
}

// Leaves exactly these favorites in the instance state, and a single folder tab on the fixture
// documents (navigation in an earlier test moves the saved tab), through the API the page itself
// uses; then reloads so the page starts from that state.
async function resetState(page, names) {
  const status = await page.evaluate(async ({documents, paths}) => {
    const csrf = document.getElementById("hf-app").dataset.csrf;
    for (let attempt = 0; attempt < 3; attempt++) {
      const {stateRevision, ...saved} = await (await fetch("api/state", {headers: {Accept: "application/json"}, cache: "no-store"})).json();
      const wanted = new Set(paths);
      const items = saved.items.map(item => ({...item, favorite: wanted.has(item.path)}))
        .filter(item => item.favorite || item.labelIds.length || item.emoji !== null);
      for (const path of wanted) if (!items.some(item => item.path === path)) items.push({path, labelIds: [], favorite: true, emoji: null, inode: null, device: null});
      const response = await fetch("api/state", {
        method: "PUT", headers: {"Content-Type": "application/json", Accept: "application/json", "X-CSRF-Token": csrf},
        body: JSON.stringify({...saved, items, tabs: [{path: documents, mode: "navegar"}], baseRevision: stateRevision}),
      });
      if (response.status !== 409) return response.status;
    }
    return 409;
  }, {documents: fixture.documents, paths: names.map(at)});
  assert.equal(status, 200);
  await page.reload();
  await waitForListing(page);
}

// The page saves its state shortly after a change: waits until the server holds exactly these favorites.
async function expectSavedFavorites(page, names) {
  const expected = names.map(at).sort();
  let saved = null;
  for (const deadline = Date.now() + 10000; Date.now() < deadline; await page.waitForTimeout(100)) {
    saved = await page.evaluate(async () => (await (await fetch("api/state", {headers: {Accept: "application/json"}, cache: "no-store"})).json())
      .items.filter(item => item.favorite).map(item => item.path).sort());
    if (JSON.stringify(saved) === JSON.stringify(expected)) return;
  }
  assert.deepEqual(saved, expected);
}

// A mouse drag in small steps: WebKit delivers dragenter and dragover only to targets the pointer crosses.
async function startDrag(page, source) {
  const box = await source.boundingBox();
  const x = box.x + Math.min(120, box.width / 2), y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + 20, y + 20, {steps: 4});
  await page.locator("body.arrastando-itens").waitFor({state: "attached"});
}
// Drops on `target` once the drop area `area` (a selector) shows it accepts the items: WebKit
// counts a drop only after a dragover that the page accepted.
async function dropOn(page, target, area) {
  const box = await target.boundingBox();
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  await page.mouse.move(x, y, {steps: 8});
  await page.mouse.move(x + 2, y, {steps: 2});
  await page.locator(`${area}.drop-alvo-item`).waitFor({state: "attached"});
  await page.mouse.up();
}

test("adding a folder from its menu shows Favorites above the tree, with the folder as the collapsed first node of its own tree", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, []);
  assert.equal(await page.locator("#favoritos > *").count(), 0);
  await listingRow(page, "notas").click({button: "right"});
  await page.locator("#menu-ctx").getByRole("menuitem", {name: "Adicionar aos favoritos"}).click();
  const row = favoriteRow(page, "notas");
  await row.locator("button.chev").waitFor();
  assert.equal(await page.locator("#favoritos .fav-titulo").textContent(), "Favoritos");
  const [favoritesTop, treeTop] = await page.evaluate(() => ["#favoritos", "#tree-heading"].map(selector => document.querySelector(selector).getBoundingClientRect().top));
  assert.ok(favoritesTop < treeTop, `Favorites at ${favoritesTop}, tree heading at ${treeTop}`);
  assert.equal(await row.getAttribute("role"), "treeitem");
  assert.deepEqual(await favoriteRows(page), ["notas 1 closed"]);
  assert.equal(await rootTreeRows(page).count(), 1);
  await expectSavedFavorites(page, ["notas"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a file favorite is a single row without a chevron, and activating it opens the file", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["texto.txt"]);
  const row = favoriteRow(page, "texto.txt");
  // Until the parent folder's listing tells its type, the row stays neutral, without an icon.
  await row.locator(".icone").waitFor();
  await row.locator(".icone-vazio").waitFor({state: "detached"});
  assert.equal(await row.locator("button.chev").count(), 0);
  assert.deepEqual(await favoriteRows(page), ["texto.txt 1"]);
  await row.locator(".tree-entry-name").click();
  await page.locator("#tabs .aba.ativa .aba-nome", {hasText: /^texto\.txt$/}).waitFor();
  await currentFavoriteRow(page, "texto.txt").waitFor();
  assert.equal(await rootTreeRows(page).count(), 1);
  assert.deepEqual(errors, []);
  await context.close();
});

test("the chevrons of a favorite tree expand and collapse its branches without changing the listing", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["arquivo"]);
  await favoriteRow(page, "arquivo").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024/janeiro").waitFor();
  assert.deepEqual(await favoriteRows(page), ["arquivo 1 open", "arquivo/2024 2 open", "arquivo/2024/janeiro 3 closed"]);
  assert.equal(await lastCrumb(page), "documents");
  assert.equal(await rootTreeRows(page).count(), 1);
  await favoriteRow(page, "arquivo/2024").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024/janeiro").waitFor({state: "detached"});
  assert.deepEqual(await favoriteRows(page), ["arquivo 1 open", "arquivo/2024 2 closed"]);
  assert.equal(await lastCrumb(page), "documents");
  assert.deepEqual(errors, []);
  await context.close();
});

test("activating a favorite folder's name opens it in the listing and expands it, and activating it again keeps it expanded", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["notas"]);
  const name = favoriteRow(page, "notas").locator(".tree-entry-name");
  await name.click();
  await listingRow(page, "ideias.md").waitFor();
  await favoriteRow(page, "notas/ideias.md").waitFor();
  const expanded = ["notas 1 open current", "notas/ideias.md 2", "notas/projeto.md 2", "notas/reuniao.md 2"];
  assert.deepEqual(await favoriteRows(page), expanded);
  assert.equal(await lastCrumb(page), "notas");
  assert.equal(await rootTreeRows(page).count(), 1);
  const listed = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname.endsWith("/api/list") && url.searchParams.get("path") === at("notas").slice(1);
  });
  await name.click();
  await listed;
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.deepEqual(await favoriteRows(page), expanded);
  assert.deepEqual(errors, []);
  await context.close();
});

test("navigating the listing inside an expanded favorite reveals the item in that favorite, not in the / tree", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["arquivo"]);
  await favoriteRow(page, "arquivo").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024").waitFor();
  for (const [folder, child] of [["arquivo", "2024"], ["2024", "janeiro"], ["janeiro", "relatorio.md"]]) {
    await listingRow(page, folder).locator("button.nome-link").click();
    await listingRow(page, child).waitFor();
  }
  await currentFavoriteRow(page, "arquivo/2024/janeiro").waitFor();
  assert.deepEqual(await favoriteRows(page), ["arquivo 1 open", "arquivo/2024 2 open", "arquivo/2024/janeiro 3 closed current"]);
  assert.equal(await rootTreeRows(page).count(), 1);
  assert.deepEqual(errors, []);
  await context.close();
});

test("navigating the listing inside a collapsed favorite expands no tree", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["arquivo"]);
  await favoriteRow(page, "arquivo").locator("button.chev").waitFor();
  for (const [folder, child] of [["arquivo", "2024"], ["2024", "janeiro"]]) {
    await listingRow(page, folder).locator("button.nome-link").click();
    await listingRow(page, child).waitFor();
  }
  assert.equal(await lastCrumb(page), "2024");
  assert.deepEqual(await favoriteRows(page), ["arquivo 1 closed"]);
  assert.equal(await rootTreeRows(page).count(), 1);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a favorite tree's expanded branches come back after a reload in the same browser, and not in another browser", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["arquivo"]);
  await favoriteRow(page, "arquivo").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024").locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024/janeiro").waitFor();
  const expanded = ["arquivo 1 open", "arquivo/2024 2 open", "arquivo/2024/janeiro 3 closed"];
  assert.deepEqual(await favoriteRows(page), expanded);
  await page.reload();
  await waitForListing(page);
  await favoriteRow(page, "arquivo/2024/janeiro").waitFor();
  assert.deepEqual(await favoriteRows(page), expanded);
  assert.equal(await rootTreeRows(page).count(), 1);
  const other = await openApp(fixture);
  await favoriteRow(other.page, "arquivo").locator("button.chev").waitFor();
  assert.deepEqual(await favoriteRows(other.page), ["arquivo 1 closed"]);
  assert.deepEqual(other.errors, []);
  await other.context.close();
  assert.deepEqual(errors, []);
  await context.close();
});

test("the × of a favorite removes it", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["arquivo", "notas"]);
  const row = favoriteRow(page, "arquivo");
  await row.locator("button.chev").click();
  await favoriteRow(page, "arquivo/2024").waitFor();
  await row.hover();
  await row.locator(".fav-x").click();
  await row.waitFor({state: "detached"});
  assert.deepEqual(await favoriteRows(page), ["notas 1 closed"]);
  await expectSavedFavorites(page, ["notas"]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("dropping a listing item on a favorite folder makes it a favorite without moving it", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, ["notas"]);
  const fileRequests = [];
  page.on("request", request => { if (new URL(request.url()).pathname.includes("/api/files")) fileRequests.push(`${request.method()} ${request.url()}`); });
  const target = favoriteRow(page, "notas");
  await target.locator("button.chev").waitFor();
  await startDrag(page, listingRow(page, "app"));
  await dropOn(page, target, "#favoritos");
  await favoriteRow(page, "app").waitFor();
  await expectSavedFavorites(page, ["app", "notas"]);
  assert.deepEqual(fileRequests, []);
  assert.equal(existsSync(at("app")), true);
  assert.equal(existsSync(at("notas/app")), false);
  assert.deepEqual(errors, []);
  await context.close();
});

test("with no favorites, a drag shows a drop zone below the Trash without shifting the tree, and dropping there makes a favorite", async () => {
  const {context, page, errors} = await openApp(fixture);
  await resetState(page, []);
  const zone = page.locator("#favoritos-soltar");
  assert.equal(await zone.isVisible(), false);
  const treeTop = () => page.locator("#tree-heading").evaluate(node => node.getBoundingClientRect().top);
  const treeTopBefore = await treeTop();
  await startDrag(page, listingRow(page, "texto.txt"));
  await zone.waitFor({state: "visible"});
  const [zoneTop, trashBottom] = await page.evaluate(() => [
    document.querySelector("#favoritos-soltar").getBoundingClientRect().top, document.querySelector("#btn-lixeira").getBoundingClientRect().bottom,
  ]);
  assert.ok(zoneTop >= trashBottom - 1, `drop zone at ${zoneTop}, Trash ends at ${trashBottom}`);
  assert.equal(await treeTop(), treeTopBefore);
  await dropOn(page, zone, "#favoritos-soltar");
  await favoriteRow(page, "texto.txt").waitFor();
  await expectSavedFavorites(page, ["texto.txt"]);
  assert.equal(existsSync(at("texto.txt")), true);
  assert.deepEqual(errors, []);
  await context.close();
});
