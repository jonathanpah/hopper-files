// Viewers: a PDF whose text uses one of the 14 standard PDF fonts (not embedded) shows without
// errors; the viewer loads the bundled replacement font from the application itself.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { closeBrowser, listingRow, openApp, showDocuments, startFixture } from "./harness.mjs";

let fixture;
before(async () => { fixture = await startFixture(); });
after(async () => { await closeBrowser(); await fixture?.stop(); });

// A one-page PDF with a line of text in Helvetica, which the file does not embed.
function standardFontPdf(text) {
  const stream = `BT /F1 24 Tf 40 100 Td (${text}) Tj ET`;
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
    `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let out = "%PDF-1.4\n";
  const offsets = [];
  objects.forEach((body, index) => { offsets.push(out.length); out += `${index + 1} 0 obj\n${body}\nendobj\n`; });
  const table = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${table}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}

test("a PDF in a standard font that it does not embed shows without errors", async () => {
  writeFileSync(join(fixture.documents, "fonte-padrao.pdf"), standardFontPdf("Texto em Helvetica"));
  const {context, page, errors} = await openApp(fixture);
  await showDocuments(page);
  await listingRow(page, "fonte-padrao.pdf").locator("button.nome-link").click();
  const canvas = page.locator("canvas").first();
  await canvas.waitFor();
  await page.waitForFunction(() => {
    const element = document.querySelector("canvas");
    if (!element || !element.width) return false;
    const data = element.getContext("2d").getImageData(0, 0, element.width, element.height).data;
    for (let index = 0; index < data.length; index += 4) if (data[index + 3] && data[index] < 128) return true;
    return false;
  });
  await page.waitForTimeout(500);
  assert.deepEqual(errors, []);
  await context.close();
});
