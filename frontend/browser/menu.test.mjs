// Context menu: keyboard (Tab and Esc), focus return, placement near the edges, and copy path.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { closeBrowser, keyboardMenu, listingRow, longPress, openApp, pointerMenuAt, showDocuments, startFixture } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const menu = page => page.locator("#menu-ctx");
// The menu lies inside the window and entirely above or below the row it acts on; when it is shorter
// than its items, it scrolls.
async function assertBesideRow(page, row) {
  const {width, height} = page.viewportSize();
  const box = await menu(page).boundingBox(), rowBox = await row.boundingBox();
  assert.ok(box.y + box.height <= rowBox.y + 0.5 || box.y >= rowBox.y + rowBox.height - 0.5,
    `menu from ${box.y} to ${box.y + box.height}, row from ${rowBox.y} to ${rowBox.y + rowBox.height}`);
  assert.ok(box.x >= 8 && box.y >= 8 && box.x + box.width <= width - 8 + 0.5 && box.y + box.height <= height - 8 + 0.5);
  const clipped = await menu(page).evaluate(element => element.scrollHeight > element.clientHeight + 1 ? getComputedStyle(element).overflowY : "fits");
  assert.ok(["fits", "auto", "scroll"].includes(clipped), `clipped menu overflow ${clipped}`);
  return box;
}
const focusedRowName = page => page.evaluate(() => document.activeElement?.closest("tr.item")?.dataset.path?.split("/").pop() ?? null);

test("the keyboard menu takes focus, and Tab closes it and moves on from the row", async () => {
  const {context, page, errors} = await openApp(fixture);
  await keyboardMenu(listingRow(page, "notas"));
  await menu(page).waitFor({state: "visible"});
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("ctx-item")), true);
  await page.keyboard.press("Tab");
  assert.equal(await menu(page).isVisible(), false);
  assert.equal(await page.evaluate(() => Boolean(document.activeElement?.closest("#menu-ctx"))), false);
  assert.notEqual(await page.evaluate(() => document.activeElement), null);
  assert.deepEqual(errors, []);
  await context.close();
});

test("Esc closes the menu and returns focus to the row", async () => {
  const {context, page, errors} = await openApp(fixture);
  await keyboardMenu(listingRow(page, "texto.txt"));
  await menu(page).waitFor({state: "visible"});
  await page.keyboard.press("Escape");
  assert.equal(await menu(page).isVisible(), false);
  assert.equal(await focusedRowName(page), "texto.txt");
  assert.deepEqual(errors, []);
  await context.close();
});

test("near the bottom and right edges the menu flips and never covers the row", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "laptop"});
  await showDocuments(page);
  const row = listingRow(page, "texto.txt");
  const {width} = page.viewportSize();
  const rowBox = await row.boundingBox();
  await page.setViewportSize({width, height: Math.ceil(rowBox.y + rowBox.height + 24)});
  const near = await row.boundingBox();
  await pointerMenuAt(row, width - 30, near.y + near.height / 2);
  await menu(page).waitFor({state: "visible"});
  const box = await assertBesideRow(page, row);
  assert.ok(box.y + box.height <= near.y + 0.5, "with no room below, the menu goes above the row");
  assert.ok(box.x + box.width <= width - 30 + 0.5, `menu ends at x ${box.x + box.width}, pointer at ${width - 30}`);
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

test("the keyboard menu opens next to its row, not at the corner of the window", async () => {
  const {context, page, errors} = await openApp(fixture);
  await showDocuments(page);
  const row = listingRow(page, "notas");
  await keyboardMenu(row);
  await menu(page).waitFor({state: "visible"});
  const box = await assertBesideRow(page, row);
  assert.ok(Math.abs(box.x - (await row.boundingBox()).x) <= 1, "the menu starts at the row's left edge");
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

for (const profile of ["iphone", "ipad-portrait"]) {
  test(`a long press on ${profile} opens the menu next to the row, without covering it`, async () => {
    const {context, page, errors} = await openApp(fixture, {profile});
    await showDocuments(page);
    for (const name of ["notas", "texto.txt"]) {
      const row = listingRow(page, name);
      await longPress(page, row);
      await menu(page).waitFor({state: "visible"});
      await assertBesideRow(page, row);
      await page.keyboard.press("Escape");
      await menu(page).waitFor({state: "hidden"});
    }
    assert.deepEqual(errors, []);
    await context.close();
  });
}

test("on a short phone screen, a long press opens the menu above a row near the bottom, and on the roomier side, scrolling, for a row in the middle", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "iphone"});
  await showDocuments(page);
  const row = listingRow(page, "texto.txt");
  const near = await row.boundingBox();
  await page.setViewportSize({width: 390, height: Math.ceil(near.y + near.height + 40)});
  await longPress(page, row);
  await menu(page).waitFor({state: "visible"});
  const box = await assertBesideRow(page, row);
  assert.ok(box.y + box.height <= (await row.boundingBox()).y + 0.5, "with no room below, the menu goes above the row");
  await page.keyboard.press("Escape");
  await menu(page).waitFor({state: "hidden"});
  const top = await listingRow(page, "notas").boundingBox();
  await page.setViewportSize({width: 390, height: Math.ceil(top.y * 2 + top.height)});
  await longPress(page, listingRow(page, "notas"));
  await menu(page).waitFor({state: "visible"});
  await assertBesideRow(page, listingRow(page, "notas"));
  assert.ok(await menu(page).evaluate(element => element.scrollHeight > element.clientHeight), "a menu taller than either side scrolls");
  // The last visible command is cut at the edge with its name still readable, under a fade that
  // goes away at the end of the list.
  const cut = await menu(page).evaluate(element => {
    const edge = element.getBoundingClientRect().bottom;
    const item = [...element.querySelectorAll(".ctx-item")].find(node => {
      const box = node.getBoundingClientRect();
      return box.top < edge - 4 && box.bottom > edge + 2;
    });
    if (!item) return null;
    const box = item.getBoundingClientRect(), range = document.createRange();
    range.selectNodeContents(item);
    return {shown: (edge - box.top) / box.height, textBottom: range.getBoundingClientRect().bottom, edge, fades: element.classList.contains("rola")};
  });
  assert.ok(cut, "a command is cut at the edge of a scrolling menu");
  assert.ok(cut.shown >= 0.6 && cut.shown < 1, `${Math.round(cut.shown * 100)}% of the cut command shows`);
  assert.ok(cut.textBottom <= cut.edge, "the cut command's name reads in full");
  assert.ok(cut.fades, "the edge fades while more commands are below");
  await menu(page).evaluate(element => { element.scrollTop = element.scrollHeight; element.dispatchEvent(new Event("scroll")); });
  assert.equal(await menu(page).evaluate(element => element.classList.contains("rola")), false, "no fade at the end of the list");
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

test("on an iPhone, the menu stays above Safari's floating toolbar, which covers the bottom of the window", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "iphone"});
  await showDocuments(page);
  // The layout takes the visible height (100dvh); the window goes on under the toolbar.
  const visible = await page.evaluate(() => {
    const layout = document.querySelector("#app-layout"), height = window.innerHeight - 120;
    layout.style.height = `${height}px`;
    return height;
  });
  // The first row: the menu opens below it, toward the toolbar.
  const row = page.locator("#results tr.item").first();
  await longPress(page, row);
  await menu(page).waitFor({state: "visible"});
  const box = await menu(page).boundingBox(), rowBox = await row.boundingBox();
  assert.ok(box.y >= rowBox.y + rowBox.height - 0.5, "the menu opens below the first row");
  assert.ok(box.y + box.height <= visible - 8 + 0.5, `menu ends at ${box.y + box.height}, visible area at ${visible}`);
  assert.ok(box.y + box.height <= rowBox.y + 0.5 || box.y >= rowBox.y + rowBox.height - 0.5, "the menu does not cover the row");
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});

test("the menu of a file copies its absolute path", async () => {
  const {context, page, errors} = await openApp(fixture);
  await listingRow(page, "texto.txt").click({button: "right"});
  await menu(page).getByRole("menuitem", {name: "Copiar caminho"}).click();
  await page.waitForTimeout(200);
  assert.deepEqual(await page.evaluate(() => window.copiedTexts), [`${fixture.documents}/texto.txt`]);
  assert.deepEqual(errors, []);
  await context.close();
});

test("the row of an open menu keeps its highlight with the pointer over it", async () => {
  const {context, page, errors} = await openApp(fixture);
  const row = listingRow(page, "texto.txt");
  const box = await row.boundingBox();
  await page.mouse.click(box.x + 300, box.y + box.height / 2, {button: "right"});
  await menu(page).waitFor({state: "visible"});
  await page.mouse.move(box.x + 280, box.y + box.height / 2);
  const colors = await page.evaluate(() => {
    const probe = document.createElement("div"); probe.style.background = "var(--sel)"; document.body.append(probe);
    const expected = getComputedStyle(probe).backgroundColor; probe.remove();
    const cell = document.querySelector("#results tr.menu-alvo td.c-nome");
    return {expected, actual: cell && getComputedStyle(cell).backgroundColor, hovered: Boolean(document.querySelector("#results tr.menu-alvo:hover"))};
  });
  assert.equal(colors.hovered, true);
  assert.equal(colors.actual, colors.expected);
  await page.keyboard.press("Escape");
  assert.deepEqual(errors, []);
  await context.close();
});
