// Context menu: keyboard (Tab and Esc), focus return, placement near the edges, and copy path.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { closeBrowser, keyboardMenu, listingRow, openApp, pointerMenuAt, startFixture } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const menu = page => page.locator("#menu-ctx");
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

test("near the bottom and right edges the menu flips instead of covering the pointer", async () => {
  const {context, page, errors} = await openApp(fixture, {profile: "laptop"});
  const {width, height} = page.viewportSize();
  await pointerMenuAt(listingRow(page, "texto.txt"), width - 30, height - 20);
  await menu(page).waitFor({state: "visible"});
  const box = await menu(page).boundingBox();
  assert.ok(box.y + box.height <= height - 20 + 0.5, `menu ends at ${box.y + box.height}, pointer at ${height - 20}`);
  assert.ok(box.x + box.width <= width - 30 + 0.5, `menu ends at x ${box.x + box.width}, pointer at ${width - 30}`);
  assert.ok(box.y >= 8 && box.x >= 8);
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
