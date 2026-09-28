import test from "node:test";
import assert from "node:assert/strict";
import {renderMarkdownUnsanitized} from "../src/editor-runtime.js";

// DOMPurify needs a browser; this checks the HTML that would reach it.
const render = source => renderMarkdownUnsanitized(source);

test("tags in ordinary text become marks with the normalized tag name", () => {
  assert.equal(render("Veja #Projeto-Alfa e #revisão2."),
    '<p>Veja <mark data-tag="projeto-alfa">#Projeto-Alfa</mark> e <mark data-tag="revisão2">#revisão2</mark>.</p>\n');
});

test("tag names follow the backend normalization (NFC and casefold)", () => {
  assert.equal(render("#Straße #ΟΔΟΣ #ırmak #CAFÉ"),
    '<p><mark data-tag="strasse">#Straße</mark> <mark data-tag="οδοσ">#ΟΔΟΣ</mark> ' +
    '<mark data-tag="ırmak">#ırmak</mark> <mark data-tag="café">#CAFÉ</mark></p>\n');
});

test("code, links, URLs, colors and invalid bodies are not tags", () => {
  const source = "`#codigo` [#rotulo](https://x.test/#frag) https://x.test/#frag <https://x.test/#auto> " +
    "#fff #cafe abc#def \\#esc _#sub #fim- #a_b #x² ##duplo #1abc";
  assert.equal(render(source),
    '<p><code>#codigo</code> <a href="https://x.test/#frag">#rotulo</a> https://x.test/#frag ' +
    '<a href="https://x.test/#auto">https://x.test/#auto</a> #fff #cafe abc#def #esc _#sub #fim- #a_b #x² ##duplo #1abc</p>\n');
  assert.equal(render("```\n#dentro\n```"), "<pre><code>#dentro\n</code></pre>\n");
  assert.equal(render("    #recuado"), "<pre><code>#recuado\n</code></pre>\n");
});

test("a tag has at most 80 characters", () => {
  const eighty = "a".repeat(80);
  assert.equal(render(`#${eighty}`), `<p><mark data-tag="${eighty}">#${eighty}</mark></p>\n`);
  assert.equal(render(`#${eighty}a`), `<p>#${eighty}a</p>\n`);
});

test("tags are marked in headings, tables, lists, highlights and emphasis", () => {
  assert.equal(render("# Título #tag"), '<h1>Título <mark data-tag="tag">#tag</mark></h1>\n');
  assert.equal(render("==revisar #urgente== e **#forte**"),
    '<p><mark>revisar <mark data-tag="urgente">#urgente</mark></mark> e <strong><mark data-tag="forte">#forte</mark></strong></p>\n');
  assert.match(render("| A |\n| --- |\n| #celula |"), /<td><mark data-tag="celula">#celula<\/mark><\/td>/);
});

test("task items show a box and keep their tags", () => {
  assert.equal(render("- [ ] aberta #t1\n- [x] feita"),
    '<ul data-marcador="traco">\n<li data-tarefa="aberta">☐ aberta <mark data-tag="t1">#t1</mark></li>\n' +
    '<li data-tarefa="feita">☑ feita</li>\n</ul>\n');
});

test("each bullet marker is reported to the stylesheet", () => {
  assert.equal(render("- a\n* b\n+ c"),
    '<ul data-marcador="traco">\n<li>a</li>\n</ul>\n<ul data-marcador="asterisco">\n<li>b</li>\n</ul>\n' +
    '<ul data-marcador="mais">\n<li>c</li>\n</ul>\n');
});

test("raw HTML stays text and a tag cannot carry markup", () => {
  assert.equal(render('<script>alert(1)</script> #tag"onmouseover=x'),
    '<p>&lt;script&gt;alert(1)&lt;/script&gt; <mark data-tag="tag">#tag</mark>&quot;onmouseover=x</p>\n');
  assert.equal(render("[x](javascript:alert(1))"), "<p>[x](javascript:alert(1))</p>\n");
});
