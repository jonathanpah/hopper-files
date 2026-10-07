import test from "node:test";
import assert from "node:assert/strict";
import {EditorSelection, EditorState} from "@codemirror/state";
import {markdown, markdownLanguage} from "@codemirror/lang-markdown";
import {activeFormats, formatInline, insertLink, setBullet, setHeading} from "../src/editor-runtime.js";

// Notation: "‸" is the cursor; "⟨" and "⟩" mark the start and end of the selection.
function stateOf(marked) {
  let doc = "", anchor = null, head = null;
  for (const character of marked) {
    if (character === "‸") anchor = head = doc.length;
    else if (character === "⟨") anchor = doc.length;
    else if (character === "⟩") head = doc.length;
    else doc += character;
  }
  return EditorState.create({doc, selection: EditorSelection.single(anchor, head), extensions: [markdown({base: markdownLanguage})]});
}

function show(state) {
  const {from, to} = state.selection.main;
  const doc = state.doc.toString();
  if (from === to) return `${doc.slice(0, from)}‸${doc.slice(from)}`;
  return `${doc.slice(0, from)}⟨${doc.slice(from, to)}⟩${doc.slice(to)}`;
}

function run(command, marked, ...args) {
  let state = stateOf(marked);
  command({state, dispatch: transaction => { state = transaction.state; }}, ...args);
  return show(state);
}

test("inline formatting wraps only the selection and toggles it off", () => {
  assert.equal(run(formatInline, "uma ⟨palavra⟩ aqui", "**"), "uma **⟨palavra⟩** aqui");
  assert.equal(run(formatInline, "uma **⟨palavra⟩** aqui", "**"), "uma ⟨palavra⟩ aqui");
  assert.equal(run(formatInline, "uma ⟨**palavra**⟩ aqui", "**"), "uma ⟨palavra⟩ aqui");
  assert.equal(run(formatInline, "uma ⟨palavra ⟩aqui", "~~"), "uma ~~⟨palavra⟩~~ aqui");
  assert.equal(run(formatInline, "- ⟨item inteiro⟩", "=="), "- ==⟨item inteiro⟩==");
  assert.equal(run(formatInline, "⟨- item inteiro⟩", "`"), "- `⟨item inteiro⟩`");
  assert.equal(run(formatInline, "⟨uma palavra\n⟩outra", "**"), "**⟨uma palavra⟩**\noutra");
});

test("without a selection the command uses the word under the cursor", () => {
  assert.equal(run(formatInline, "uma pala‸vra aqui", "**"), "uma **pala‸vra** aqui");
  assert.equal(run(formatInline, "uma **pala‸vra** aqui", "**"), "uma pala‸vra aqui");
  assert.equal(run(formatInline, "uma **pala‸vra** aqui", "*"), "uma ***pala‸vra*** aqui");
  assert.equal(run(formatInline, "***am‸bos***", "**"), "*am‸bos*");
  assert.equal(run(formatInline, "***am‸bos***", "*"), "**am‸bos**");
  assert.equal(run(formatInline, "**uma pala‸vra aqui**", "**"), "uma pala‸vra aqui");
  assert.equal(run(formatInline, "==mar‸ca==", "=="), "mar‸ca");
  assert.equal(run(formatInline, "~~ris‸co~~", "~~"), "ris‸co");
  assert.equal(run(formatInline, "`co‸de`", "`"), "co‸de");
});

test("outside a word the pair is inserted with the cursor inside, and a second press removes it", () => {
  assert.equal(run(formatInline, "‸", "**"), "**‸**");
  assert.equal(run(formatInline, "fim ‸", "=="), "fim ==‸==");
  assert.equal(run(formatInline, "palavra‸", "*"), "palavra*‸*");
  assert.equal(run(formatInline, "**‸**", "**"), "‸");
  assert.equal(run(formatInline, "**‸**", "*"), "***‸***");
  assert.equal(run(formatInline, "~~‸~~", "~~"), "‸");
});

test("indented list items accept formatting, code lines do not", () => {
  assert.equal(run(formatInline, "- item\n    - sub‸item", "**"), "- item\n    - **sub‸item**");
  assert.equal(run(formatInline, "⟨- item\n    - subitem⟩", "**"), "⟨- **item**\n    - **subitem⟩**");
  assert.equal(run(formatInline, "⟨texto\n    codigo⟩", "**"), "**⟨texto**\n    codigo⟩");
  assert.equal(run(formatInline, "    co‸digo", "**"), "    co‸digo");
  assert.equal(run(formatInline, "```\nco‸digo\n```", "**"), "```\nco‸digo\n```");
});

test("inside inline code only the code command acts", () => {
  assert.equal(run(formatInline, "`co‸de`", "**"), "`co‸de`");
  assert.equal(run(formatInline, "`a == ⟨b⟩ == c`", "=="), "`a == ⟨b⟩ == c`");
  assert.equal(run(formatInline, "`a == b == c` e ⟨fim⟩", "=="), "`a == b == c` e ==⟨fim⟩==");
});

test("a multiline selection formats each applicable line", () => {
  assert.equal(run(formatInline, "⟨um\n\ndois⟩", "**"), "**⟨um**\n\n**dois⟩**");
  assert.equal(run(formatInline, "⟨**um**\n**dois**⟩", "**"), "⟨um\ndois⟩");
  assert.equal(run(formatInline, "⟨um  \ndois⟩", "*"), "*⟨um*  \n*dois⟩*");
});

test("the link command labels the word or the address and never nests links", () => {
  assert.equal(run(insertLink, "visite o si‸te hoje"), "visite o [site](⟨url⟩) hoje");
  assert.equal(run(insertLink, "⟨alvo⟩"), "[alvo](⟨url⟩)");
  assert.equal(run(insertLink, "‸"), "[texto](⟨url⟩)");
  assert.equal(run(insertLink, "veja ⟨https://example.com⟩ agora"), "veja [⟨https://example.com⟩](https://example.com) agora");
  assert.equal(run(insertLink, "veja ⟨www.example.com⟩ agora"), "veja [⟨www.example.com⟩](https://www.example.com) agora");
  assert.equal(run(insertLink, "veja https://exam‸ple.com agora"), "veja [⟨https://example.com⟩](https://example.com) agora");
  assert.equal(run(insertLink, "[si‸te](https://a.test)"), "[site](⟨https://a.test⟩)");
  assert.equal(run(insertLink, "[te‸xto]"), "[texto](⟨url⟩)");
  assert.equal(run(insertLink, "<https://a.test/‸c>"), "<⟨https://a.test/c⟩>");
});

test("the image command keeps its label, placeholder and attachment behavior", () => {
  assert.equal(run(insertLink, "⟨alvo⟩", true), "![alvo](⟨url⟩)");
  assert.equal(run(insertLink, "a ‸b", true, "attachments/x.png", "foto [1]"), "a ![foto \\[1\\]](attachments/x.png) ‸b");
});

test("heading levels 1 to 3 replace each other and the same level removes the heading", () => {
  assert.equal(run(setHeading, "tex‸to", 2), "## tex‸to");
  assert.equal(run(setHeading, "## tex‸to", 2), "tex‸to");
  assert.equal(run(setHeading, "## tex‸to", 1), "# tex‸to");
  assert.equal(run(setHeading, "‸", 1), "# ‸");
  assert.equal(run(setHeading, "⟨um\n\ndois⟩", 3), "### ⟨um\n\n### dois⟩");
});

test("bullets use the chosen marker and the same marker removes the list", () => {
  assert.equal(run(setBullet, "tex‸to", "*"), "* tex‸to");
  assert.equal(run(setBullet, "* tex‸to", "*"), "tex‸to");
  assert.equal(run(setBullet, "- tex‸to", "+"), "+ tex‸to");
  assert.equal(run(setBullet, "- [ ] tare‸fa", "*"), "* [ ] tare‸fa");
  assert.equal(run(setBullet, "‸", "-"), "- ‸");
  assert.equal(run(setBullet, "⟨um\n\ndois⟩", "+"), "+ ⟨um\n\n+ dois⟩");
});

test("active formats describe the markup around the main cursor", () => {
  const active = marked => [...activeFormats(stateOf(marked))].sort();
  assert.deepEqual(active("**negr‸ito**"), ["bold"]);
  assert.deepEqual(active("***am‸bos***"), ["bold", "italic"]);
  assert.deepEqual(active("~~ri‸sco~~"), ["strike"]);
  assert.deepEqual(active("==mar‸ca=="), ["highlight"]);
  assert.deepEqual(active("`co‸de`"), ["code"]);
  assert.deepEqual(active("[li‸nk](https://x.test)"), ["link"]);
  assert.deepEqual(active("## Tí‸tulo"), ["heading2"]);
  assert.deepEqual(active("* it‸em"), ["bulletStar"]);
  assert.deepEqual(active("- **it‸em**"), ["bold", "bulletDash"]);
  assert.deepEqual(active("+ ⟨**a**⟩"), ["bold", "bulletPlus"]);
  assert.deepEqual(active("texto‸"), []);
  assert.deepEqual(active("**negrito**‸"), []);
  assert.deepEqual(active("#### qua‸tro"), []);
  assert.deepEqual(active("`a == b‸ == c`"), ["code"]);
});

test("the second shortcut right before the closing marker leaves the pair instead of removing it", () => {
  assert.equal(run(formatInline, "texto **abc‸** fim", "**"), "texto **abc**‸ fim");
  assert.equal(run(formatInline, "texto **ab‸c** fim", "**"), "texto ab‸c fim");
  assert.equal(run(formatInline, "texto **‸** fim", "**"), "texto ‸ fim");
});

test("the link command selects the destination even when the label is an address", () => {
  assert.equal(run(insertLink, "[https://a.test/x‸](https://a.test/x)"), "[https://a.test/x](⟨https://a.test/x⟩)");
});

test("bullets replace a number marker and leave fenced code untouched", () => {
  assert.equal(run(setBullet, "‸1. primeiro", "-"), "‸- primeiro");
  assert.equal(run(setBullet, "⟨texto\n```\ncódigo\n```⟩", "-"), "- ⟨texto\n```\ncódigo\n```⟩");
  assert.equal(run(setHeading, "⟨título\n```\ncódigo\n```⟩", 2), "## ⟨título\n```\ncódigo\n```⟩");
});

test("a selection that crosses a pair of the same kind extends it instead of leaving loose markers", () => {
  assert.equal(run(formatInline, "uma **pala⟨vra** aq⟩ui", "**"), "uma **⟨palavra aq⟩**ui");
  assert.equal(run(formatInline, "⟨a **b** c⟩", "**"), "**⟨a b c⟩**");
  assert.equal(run(formatInline, "x ==ma⟨rca== y⟩", "=="), "x ==⟨marca y⟩==");
  assert.equal(run(formatInline, "a `co⟨de` b⟩", "**"), "a **⟨`code` b⟩**");
});

test("heading and bullet replace each other's prefix, keep quotes and skip code", () => {
  assert.equal(run(setHeading, "- tra‸ço", 2), "## tra‸ço");
  assert.equal(run(setHeading, "- [ ] tare‸fa", 1), "# tare‸fa");
  assert.equal(run(setBullet, "# Marca‸dores", "+"), "+ Marca‸dores");
  assert.equal(run(setBullet, "> cita‸ção", "+"), "> + cita‸ção");
  assert.equal(run(setHeading, "> cita‸ção", 2), "> ## cita‸ção");
  assert.equal(run(setBullet, "texto\n\n    codigo recua‸do", "-"), "texto\n\n    codigo recua‸do");
});

test("an attached image is kept apart from the words around it", () => {
  assert.equal(run(insertLink, "‸Texto", true, "attachments/x.png", "x"), "![x](attachments/x.png) ‸Texto");
  assert.equal(run(insertLink, "- item dois‸", true, "attachments/x.png", "x"), "- item dois ![x](attachments/x.png)‸");
});
