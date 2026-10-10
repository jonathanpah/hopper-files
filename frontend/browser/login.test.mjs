// Sign-in page: user name and password, ready for the browser's password manager.
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { webkit } from "playwright-core";
import { PROFILES, startFixture } from "./harness.mjs";

let fixture, browser;
before(async () => { fixture = await startFixture(); browser = await webkit.launch({headless: true}); });
after(async () => { await browser?.close(); await fixture?.stop(); });

async function signInPage(profile = "desktop") {
  const context = await browser.newContext({...PROFILES[profile], locale: "pt-BR"});
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
