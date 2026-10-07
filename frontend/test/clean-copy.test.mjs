import test from "node:test";
import assert from "node:assert/strict";
import {cleanCopy, preserveLineEndings} from "../src/editor-runtime.js";

const markdownInput = `# Título **forte**

Este parágrafo sintético demonstra uma quebra automática de coluna
e esta segunda linha continua a mesma frase sem novo bloco
antes de terminar aqui.

   - item com ==marca==
      1.1. subitem *itálico*

\`\`\`js
const raw = "**fica**";
\`\`\`

[Guia](https://example.test) e ![Foto](img.png).`;

const expectedMarkdown = `Título forte

Este parágrafo sintético demonstra uma quebra automática de coluna e esta segunda linha continua a mesma frase sem novo bloco antes de terminar aqui.

- item com marca
1.1. subitem itálico

const raw = "**fica**";

Guia e Foto.`;

test("Markdown clean-copy matches the normative reconstruction fixture", () => {
  assert.equal(cleanCopy(markdownInput, true), expectedMarkdown);
});

test("plain text recomposes without interpreting Markdown markers", () => {
  const input = `    **Texto** sintético demonstra quebra automática e preserva os sinais
    e esta segunda linha continua a mesma frase com ==destaque==
    antes de terminar com - marcador literal.`;
  assert.equal(cleanCopy(input, false), "**Texto** sintético demonstra quebra automática e preserva os sinais e esta segunda linha continua a mesma frase com ==destaque== antes de terminar com - marcador literal.");
  assert.equal(cleanCopy("Rua A\nSala 2", false), "Rua A\nSala 2");
});

test("recomposition evaluates each source line once and does not let joined text absorb short lines", () => {
  const long1 = "Esta linha sintética é longa o bastante para atingir o limiar de junção agora";
  const long2 = "e esta segunda linha também é longa o bastante para seguir o mesmo parágrafo";
  assert.equal(cleanCopy(`${long1}\n${long2}\nRua A\nSala 2\n`, false), `${long1} ${long2} Rua A\nSala 2`);
});

test("editing text preserves unchanged CRLF and mixed line-ending bytes", () => {
  assert.equal(preserveLineEndings("first\r\nsecond\r\n", "changed first\nsecond\n"), "changed first\r\nsecond\r\n");
  assert.equal(preserveLineEndings("one\r\ntwo\nthree\r\n", "ONE\ntwo\nthree\n"), "ONE\r\ntwo\nthree\r\n");
  assert.equal(preserveLineEndings("one\r\ntwo\n", "one\nnew\ntwo\n"), "one\r\nnew\r\ntwo\n");
});

test("fenced and inline code remain literal while surrounding markup is removed", () => {
  const tick = "`";
  const source = `${tick}**inline**${tick} and **bold**\n\n~~~js\nconst x = ${tick}**raw**${tick};  \n~~~`;
  assert.equal(cleanCopy(source, true), `**inline** and bold\n\nconst x = ${tick}**raw**${tick};  `);
});

test("a table separator row leaves no empty line between header and data", () => {
  assert.equal(cleanCopy("| A | B |\n| --- | --- |\n| 1 | 2 |", true), "A  B\n1  2");
  assert.equal(cleanCopy("Antes\n\n| Nome | Valor |\n|:---|---:|\n| x | 10 |\n| y | 20 |\n\nDepois", true),
    "Antes\n\nNome  Valor\nx  10\ny  20\n\nDepois");
});

test("unclosed fenced code is still restored without Markdown interpretation", () => {
  assert.equal(cleanCopy("~~~js\nconst x = **literal**;", true), "const x = **literal**;");
});

test("large inline-code clean-copy restores all protected runs in one pass", () => {
  const count = 8000;
  const source = Array.from({length: count}, (_, index) => `line ${index} \`code-${index}\` tail`.padEnd(73, "x")).join("\n");
  const started = performance.now();
  const output = cleanCopy(source, true);
  const elapsed = performance.now() - started;
  assert.equal((output.match(/\bcode-\d+\b/g) || []).length, count);
  assert.ok(output.includes("line 0 code-0 tail"));
  assert.ok(output.includes(`line ${count - 1} code-${count - 1} tail`));
  assert.ok(elapsed < 5000, `cleanCopy of ${count} inline-code lines took ${elapsed.toFixed(0)} ms`);
});

test("clean copy drops a table separator without outer pipes", async () => {
  const {cleanCopy} = await import("../src/editor-runtime.js");
  assert.equal(cleanCopy("A | B\n--- | ---\n1 | 2", true).includes("---"), false);
});
