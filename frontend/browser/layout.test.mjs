// Layout audit of the main screens on desktop, laptop, iPad, and iPhone sizes, in the light and dark
// themes: contrast, cut text, wrapped labels, sideways scroll, controls off screen or covered, and
// touch targets.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { auditLayout, closeBrowser, closeSidebar, keyboardMenu, listingRow, openApp, openSidebar, PROFILES, showDocuments, startFixture, waitForListing } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

const SCREENS = {
  listing: async page => { await showDocuments(page); },
  sidebar: async page => { await showDocuments(page); await openSidebar(page); },
  "context menu": async page => { await showDocuments(page); await keyboardMenu(listingRow(page, "notas")); await page.locator("#menu-ctx").waitFor(); },
  "markdown document": async page => {
    await showDocuments(page);
    await listingRow(page, "notas").locator("button.nome-link").click();
    await waitForListing(page);
    await listingRow(page, "projeto.md").locator("button.nome-link").click();
    await page.locator("#seg-cru").click();
    await page.locator(".cm-content").waitFor();
  },
};

for (const profile of ["desktop", "laptop", "ipad-portrait", "iphone"]) {
  for (const dark of [false, true]) {
    test(`${profile}, ${dark ? "dark" : "light"} theme: no layout problems`, async () => {
      const {context, page, errors} = await openApp(fixture, {profile, dark});
      const found = [];
      for (const [name, show] of Object.entries(SCREENS)) {
        await closeSidebar(page);
        await show(page);
        await page.waitForTimeout(250);
        // Tap targets are measured with nothing open over the screen.
        const touch = Boolean(PROFILES[profile].hasTouch) && name !== "context menu";
        for (const problem of await auditLayout(page, {touch})) found.push(`${name}: ${problem.kind} ${problem.target} (${problem.detail})`);
        await page.keyboard.press("Escape");
      }
      assert.deepEqual(found, []);
      assert.deepEqual(errors, []);
      await context.close();
    });
  }
}
