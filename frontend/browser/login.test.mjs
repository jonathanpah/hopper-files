// Sign-in page: user name and password, ready for the browser's password manager.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { webkit } from "playwright-core";
import { auditLayout, PROFILES, startFixture } from "./harness.mjs";

let fixture, browser;
before(async () => { fixture = await startFixture(); browser = await webkit.launch({headless: true}); });
after(async () => { await browser?.close(); await fixture?.stop(); });

async function signInPage(profile = "desktop", dark = false) {
  const context = await browser.newContext({...PROFILES[profile], locale: "pt-BR", colorScheme: dark ? "dark" : "light"});
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(fixture.url);
  await page.locator("#login-form").waitFor();
  return {context, page, errors};
}

test("the sign-in form asks for the user name and the password, with password-manager hints", async () => {
  const {context, page, errors} = await signInPage();
  const user = page.locator("#login-form input[name=username]");
  const password = page.locator("#login-form input[name=password]");
  assert.equal(await user.getAttribute("autocomplete"), "username");
  assert.equal(await user.getAttribute("autocapitalize"), "none");
  assert.equal(await password.getAttribute("type"), "password");
  assert.equal(await password.getAttribute("autocomplete"), "current-password");
  assert.equal(await page.locator("label[for=username]").textContent(), "Usuário");
  assert.equal(await page.evaluate(() => document.activeElement?.name), "username");
  // Both fields look the same: same width and height, one above the other.
  const [userBox, passwordBox] = [await user.boundingBox(), await password.boundingBox()];
  assert.ok(userBox.height >= 36, `user name field is ${userBox.height} px tall`);
  assert.equal(Math.round(userBox.width), Math.round(passwordBox.width));
  assert.equal(Math.round(userBox.height), Math.round(passwordBox.height));
  assert.ok(userBox.y + userBox.height <= passwordBox.y);
  assert.deepEqual(errors, []);
  await context.close();
});

test("a wrong user name is refused like a wrong password, and the right pair signs in", async () => {
  const {context, page, errors} = await signInPage();
  await page.fill("#login-form input[name=username]", "someone-else");
  await page.fill("#login-form input[name=password]", fixture.password);
  await page.keyboard.press("Enter");
  await page.locator("#login-status", {hasText: "Usuário ou senha incorretos."}).waitFor();
  await page.fill("#login-form input[name=username]", fixture.username);
  await page.fill("#login-form input[name=password]", fixture.password);
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/app/);
  await page.locator("#results").waitFor();
  assert.deepEqual(errors, []);
  await context.close();
});

test("the password change page asks for the user name as well", async () => {
  const {context, page, errors} = await signInPage("iphone");
  await page.locator(".login-trocar a").tap();
  await page.locator("#password-form").waitFor();
  const user = page.locator("#password-form input[name=username]");
  assert.equal(await user.getAttribute("autocomplete"), "username");
  assert.equal(await page.locator("#password-form input[name=current]").getAttribute("autocomplete"), "current-password");
  assert.equal(await page.locator("#password-form input[name=new]").getAttribute("autocomplete"), "new-password");
  assert.deepEqual(errors, []);
  await context.close();
});

test("the sign-in and password change pages pass the layout audit on every size and theme", async () => {
  const found = [];
  for (const profile of ["desktop", "ipad-portrait", "iphone"]) {
    for (const dark of [false, true]) {
      const {context, page, errors} = await signInPage(profile, dark);
      for (const [name, show] of [["sign-in", async () => {}], ["password change", async () => {
        await page.locator(".login-trocar a").click();
        await page.locator("#password-form").waitFor();
      }]]) {
        await show();
        for (const problem of await auditLayout(page, {touch: Boolean(PROFILES[profile].hasTouch)})) {
          found.push(`${profile} ${dark ? "dark" : "light"} ${name}: ${problem.kind} ${problem.target} (${problem.detail})`);
        }
      }
      found.push(...errors);
      await context.close();
    }
  }
  assert.deepEqual(found, []);
});
