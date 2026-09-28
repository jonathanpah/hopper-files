import {Compartment, EditorState, Prec, RangeSetBuilder, Text} from "@codemirror/state";
import {EditorView, Decoration, ViewPlugin, keymap, lineNumbers, highlightActiveLine, drawSelection, dropCursor} from "@codemirror/view";
import {defaultKeymap, history, historyKeymap, undo, redo} from "@codemirror/commands";
import {markdown, markdownLanguage} from "@codemirror/lang-markdown";
import {syntaxTree} from "@codemirror/language";
import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

const markdownIt = new MarkdownIt({html: false, linkify: false, typographer: false, breaks: false});
let imageRenderSequence = 0;
// Highlight syntax ("==mark=="): content is parsed as a link label so tags and emphasis inside it render normally.
markdownIt.inline.ruler.before("emphasis", "highlight", (state, silent) => {
  const start = state.pos;
  if (state.src.slice(start, start + 2) !== "==") return false;
  const end = state.src.indexOf("==", start + 2);
  if (end < 0 || end === start + 2 || end + 2 > state.posMax) return false;
  if (!silent) {
    const max = state.posMax;
    state.push("mark_open", "mark", 1).markup = "==";
    state.pos = start + 2;
    state.posMax = end;
    state.md.inline.tokenize(state);
    state.posMax = max;
    state.push("mark_close", "mark", -1).markup = "==";
  }
  state.pos = end + 2;
  return true;
});

// Bullet marker sets the preview's list-point style: "-" filled, "*" outlined, "+" square, at any nesting level.
const BULLET_NAMES = {"-": "traco", "*": "asterisco", "+": "mais"};
markdownIt.core.ruler.push("bullet_markers", state => {
  for (const token of state.tokens) {
    if (token.type === "bullet_list_open" && BULLET_NAMES[token.markup]) token.attrSet("data-marcador", BULLET_NAMES[token.markup]);
  }
});

// GFM task items ("- [ ] x", "- [x] x") render as ☐/☑ text; data-tarefa lets the CSS drop the
// list bullet. No new HTML tag reaches the sanitizer.
markdownIt.core.ruler.push("task_items", state => {
  const tokens = state.tokens;
  for (let index = 2; index < tokens.length; index++) {
    const inline = tokens[index];
    if (inline.type !== "inline" || tokens[index - 1].type !== "paragraph_open" || tokens[index - 2].type !== "list_item_open") continue;
    const match = /^\[([ xX])\][ \t]+/.exec(inline.content);
    const first = inline.children?.[0];
    if (!match || !first || first.type !== "text" || !first.content.startsWith(match[0])) continue;
    const done = match[1] !== " ";
    first.content = `${done ? "☑" : "☐"} ${first.content.slice(match[0].length)}`;
    tokens[index - 2].attrSet("data-tarefa", done ? "feita" : "aberta");
  }
});

// Tags (HF-META-001) mirror the backend's extract_markdown_tags (src/hopper_files/search.py): URLs,
// code spans and link destinations are masked out before scanning text for "#".
const TAG_URL = /(?:https?:\/\/|mailto:)[^\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000<>()]+/giu;
const TAG_HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

function maskTagText(text) {
  const blocked = new Uint8Array(text.length);
  for (const match of text.matchAll(TAG_URL)) blocked.fill(1, match.index, match.index + match[0].length);
  for (let index = 0; index < text.length;) {
    if (text[index] === "`") {
      let end = index;
      while (text[end] === "`") end++;
      const close = text.indexOf(text.slice(index, end), end);
      if (close < 0) { index = end; continue; }
      blocked.fill(1, index, close + end - index);
      index = close + end - index;
    } else if (text[index] === "]" && text[index + 1] === "(") {
      let depth = 1;
      let end = index + 2;
      for (; end < text.length && depth; end++) {
        if (text[end] === "(") depth++;
        else if (text[end] === ")") depth--;
      }
      if (!depth) blocked.fill(1, index + 2, end - 1);
      index = end;
    } else index++;
  }
  let masked = "";
  for (let index = 0; index < text.length;) {
    let end = index;
    while (end < text.length && blocked[end] === blocked[index]) end++;
    masked += blocked[index] ? " ".repeat(end - index) : text.slice(index, end);
    index = end;
  }
  return masked;
}

function codePointBefore(text, index) {
  const low = text.charCodeAt(index - 1);
  const high = text.charCodeAt(index - 2);
  return low >= 0xdc00 && low <= 0xdfff && high >= 0xd800 && high <= 0xdbff ? text.slice(index - 2, index) : text.slice(index - 1, index);
}

// Mirrors the backend's normalize_search_text (NFC, casefold, NFC). Upper- then lower-casing each
// character matches Python's casefold except for ı, ẞ, and Cherokee, handled explicitly.
function normalizeTag(text) {
  if (/^[\x00-\x7f]*$/.test(text)) return text.toLowerCase();
  let folded = "";
  for (const character of text.normalize("NFC")) {
    if (character === "\u0131") folded += character;
    else if (character === "\u1e9e") folded += "ss";
    else if (/[\u13a0-\u13f5\u13f8-\u13fd\uab70-\uabbf]/u.test(character)) folded += character.toUpperCase();
    else folded += character.toUpperCase().toLowerCase();
  }
  return folded.normalize("NFC");
}

// Tag matching scans NFC text like the backend, including combining marks, so composed
// characters match consistently.
const TAG_RUN = /[\p{L}\p{Nd}\p{M}-]*/uy;

function tagAt(masked, position) {
  if (masked[position] !== "#") return null;
  let before = masked[position - 1];
  if (before !== undefined && before.charCodeAt(0) >= 0x80) {
    let start = position;
    while (start > 0) {
      const previous = codePointBefore(masked, start);
      start -= previous.length;
      if (!/\p{M}/u.test(previous)) break;
    }
    before = Array.from(masked.slice(start, position).normalize("NFC")).at(-1);
  }
  if (before !== undefined && /[\p{L}\p{N}_#\\]/u.test(before)) return null;
  TAG_RUN.lastIndex = position + 1;
  const end = position + 1 + TAG_RUN.exec(masked)[0].length;
  const body = masked.slice(position + 1, end).normalize("NFC");
  if (!/^\p{L}(?:[\p{L}\p{Nd}-]*[\p{L}\p{Nd}])?$/u.test(body) || Array.from(body).length > 80) return null;
  if (/^[\p{N}_]/u.test(masked.slice(end, end + 2)) || TAG_HEX_COLOR.test(`#${body}`)) return null;
  return {length: end - position, name: normalizeTag(body)};
}

markdownIt.inline.ruler.push("tag", (state, silent) => {
  if (state.src.charCodeAt(state.pos) !== 0x23 || state.linkLevel > 0) return false;
  const masks = state.env.hfTagMasks ??= new Map();
  if (!masks.has(state.src)) masks.set(state.src, maskTagText(state.src));
  const tag = tagAt(masks.get(state.src), state.pos);
  if (!tag || state.pos + tag.length > state.posMax) return false;
  if (!silent) {
    state.push("mark_open", "mark", 1).attrSet("data-tag", tag.name);
    state.push("text", "", 0).content = state.src.slice(state.pos, state.pos + tag.length);
    state.push("mark_close", "mark", -1);
  }
  state.pos += tag.length;
  return true;
});

// markdown-it output before DOMPurify, for tests without a browser. The UI always calls
// renderMarkdown/renderPreviewHtml, which sanitize.
export function renderMarkdownUnsanitized(source, imageReferences = [], markerPrefix = "") {
  const previousImageRule = markdownIt.renderer.rules.image;
  if (markerPrefix) {
    markdownIt.renderer.rules.image = (tokens, index) => {
      const token = tokens[index];
      const slot = imageReferences.length;
      imageReferences.push({
        destination: token.attrGet("src") || "",
        alt: token.content || "",
        title: token.attrGet("title") || "",
      });
      return `${markerPrefix}${slot}\uE001`;
    };
  }
  try {
    return markdownIt.render(source);
  } finally {
    markdownIt.renderer.rules.image = previousImageRule;
  }
}

let annotateLines = false;
markdownIt.core.ruler.push("source_lines", state => {
  if (!annotateLines) return;
  for (const token of state.tokens) {
    if (token.map && token.level === 0 && (token.nesting === 1 || ["fence", "code_block", "hr"].includes(token.type))) {
      token.attrSet("data-linha", String(token.map[0] + 1));
    }
  }
});

export function renderMarkdown(rawSource, imageReferences = [], markerPrefix = "") {
  const source = typeof rawSource === "string" ? rawSource.replace(/^\uFEFF/, "") : rawSource;
  return DOMPurify.sanitize(renderMarkdownUnsanitized(source, imageReferences, markerPrefix), {
    ALLOWED_TAGS: ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "strong", "em", "s", "del", "code", "pre", "a", "mark", "table", "thead", "tbody", "tr", "th", "td"],
    ALLOWED_ATTR: ["href", "title"],
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
    FORBID_TAGS: ["img", "style", "iframe", "svg", "math", "form", "input", "video", "audio"],
    FORBID_ATTR: ["style"],
  });
}

// With a proportional font, "ch" differs from the real width of spaces and hyphens and would
// misalign wrapped list items, so the prefix is measured on a canvas, once per font and prefix.
const prefixWidths = new Map();
let measureCanvas = null;
function prefixIndent(view, text) {
  try {
    // Before the editor is attached to the page, computed style isn't valid yet; falls back to "ch"
    // until the next rebuild.
    if (!view.contentDOM.isConnected) throw new Error("fora da página");
    const style = getComputedStyle(view.contentDOM);
    if (!style.fontSize) throw new Error("sem estilo");
    const font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
    const spacing = parseFloat(style.letterSpacing) || 0;
    const key = `${font}\u0000${spacing}\u0000${view.state.tabSize}\u0000${text}`;
    if (!prefixWidths.has(key)) {
      measureCanvas ||= document.createElement("canvas");
      const context = measureCanvas.getContext("2d");
      context.font = font;
      const expanded = text.replace(/\t/g, " ".repeat(view.state.tabSize));
      // The canvas ignores letter-spacing, so it's added per character to match the DOM.
      const width = context.measureText(expanded).width + spacing * expanded.length;
      if (!(width > 0)) throw new Error("sem medida");
      prefixWidths.set(key, `${width.toFixed(2)}px`);
    }
    return prefixWidths.get(key);
  } catch (_error) {
    return `${text.length}ch`;
  }
}

function inlineRanges(view) {
  const state = view.state;
  const doc = state.doc;
  const tree = syntaxTree(state);
  const visible = view.visibleRanges;
  const decorations = [];
  const codeRanges = [];
  const seenLines = new Set();
  const mark = (name, from, to) => { if (to > from) decorations.push(Decoration.mark({class: name}).range(from, to)); };
  // HF-EDIT-002/HF-NAV-008: the Markdown view never hides syntax markers; hide() only styles them,
  // whatever the cursor position.
  const hide = (from, to) => mark("lp-marca", from, to);
  const lines = (name, from, to, attributes = {}) => {
    if (to < from) return;
    const first = doc.lineAt(from).number;
    const last = doc.lineAt(Math.min(to, doc.length)).number;
    for (let number = first; number <= last; number++) {
      const line = doc.line(number);
      const key = `${name}:${line.from}`;
      if (seenLines.has(key)) continue;
      seenLines.add(key);
      decorations.push(Decoration.line({class: name, attributes}).range(line.from));
    }
  };
  const inCode = pos => {
    let node = tree.resolveInner(pos, 1);
    while (node) {
      if (["FencedCode", "InlineCode", "CodeText"].includes(node.name)) return true;
      node = node.parent;
    }
    return false;
  };

  for (const range of visible) {
    tree.iterate({from: range.from, to: range.to, enter(node) {
      const from = Math.max(range.from, node.from);
      const to = Math.min(range.to, node.to);
      if (to <= from) return;
      const name = node.name;
      if (["InlineCode", "FencedCode", "CodeText"].includes(name)) codeRanges.push([from, to]);
      const heading = /^(?:ATX|Setext)Heading([1-6])$/.exec(name);
      if (heading) lines(`lp-ln-h${heading[1]}`, from, to);
      else if (name === "FencedCode") lines("lp-ln-code", from, to);
      else if (name === "Blockquote") lines("lp-ln-quote", from, to);
      else if (name === "Table") lines("lp-ln-tabela", from, to);
      else if (name === "HorizontalRule") lines("lp-ln-hr", from, to);
      else if (name === "StrongEmphasis") mark("lp-strong", from, to);
      else if (name === "Emphasis") mark("lp-em", from, to);
      else if (name === "Strikethrough") mark("lp-strike", from, to);
      else if (name === "InlineCode") mark("lp-code", from, to);
      else if (name === "Link") mark("lp-link-md", from, to);
      else if (name === "Image") mark("lp-img-md", from, to);

      if (name === "HeaderMark") {
        const line = doc.lineAt(node.from);
        const content = doc.sliceString(line.from, line.to);
        if (/^ {0,3}#{1,6}/.test(content)) {
          const opening = /^[ \t]*$/.test(doc.sliceString(line.from, node.from));
          let start = node.from, end = node.to;
          if (opening) while (end < line.to && /[ \t]/.test(doc.sliceString(end, end + 1))) end++;
          else while (start > line.from && /[ \t]/.test(doc.sliceString(start - 1, start))) start--;
          hide(start, end);
        } else mark("lp-marca", from, to);
      } else if (name === "QuoteMark") {
        const line = doc.lineAt(node.from);
        const end = node.to < line.to && doc.sliceString(node.to, node.to + 1) === " " ? node.to + 1 : node.to;
        hide(node.from, end);
      } else if (["EmphasisMark", "StrikethroughMark", "CodeMark", "LinkMark", "LinkTitle", "TaskMarker"].includes(name)) {
        hide(node.from, node.to);
      } else if (name === "URL" && ["Link", "Image", "Autolink", "LinkReference"].includes(node.node.parent?.name)) {
        // A bare address (GFM autolink) is plain text; only a link's destination is a syntax marker.
        hide(node.from, node.to);
      } else if (name === "Escape") {
        hide(node.from, Math.min(node.to, node.from + 1));
      }
    }});
  }

  const excluded = (from, to) => codeRanges.some(([start, end]) => from < end && to > start);
  for (const range of visible) {
    const first = doc.lineAt(range.from).number;
    const last = doc.lineAt(range.to).number;
    for (let number = first; number <= last; number++) {
      const line = doc.line(number);
      const bullet = /^([ \t]*)([-+*])([ \t]+)(.*)$/.exec(line.text);
      if (bullet) {
        const marker = line.from + bullet[1].length;
        if (!inCode(marker)) {
          const width = bullet[1].length + bullet[2].length + bullet[3].length;
          const key = `lp-ln-item:${line.from}`;
          if (!seenLines.has(key)) {
            seenLines.add(key);
            const indent = prefixIndent(view, line.text.slice(0, width));
            decorations.push(Decoration.line({
              class: "lp-ln-item", attributes: {style: `padding-left:${indent};text-indent:-${indent}`},
            }).range(line.from));
          }
          const markerEnd = marker + bullet[2].length;
          hide(marker, markerEnd);
        }
      }
      const highlight = /==([^=\n]+)==/g;
      let match;
      while ((match = highlight.exec(line.text))) {
        const from = line.from + match.index;
        const to = from + match[0].length;
        if (from >= range.from && to <= range.to && !excluded(from, to)) {
          mark("lp-mark", from + 2, to - 2);
          hide(from, from + 2); hide(to - 2, to);
        }
      }
    }
  }
  const pipeRow = text => /^\s*\|?.+\|.+\|?\s*$/.test(text);
  const tableSeparator = text => /^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(text);
  for (const range of visible) {
    const first = doc.lineAt(range.from).number;
    const last = doc.lineAt(range.to).number;
    for (let number = first; number <= last; number++) {
      const separator = doc.line(number);
      if (!tableSeparator(separator.text) || inCode(separator.from)) continue;
      let tableStart = number;
      let tableEnd = number;
      while (tableStart > 1 && pipeRow(doc.line(tableStart - 1).text) && !inCode(doc.line(tableStart - 1).from)) tableStart--;
      while (tableEnd < doc.lines && pipeRow(doc.line(tableEnd + 1).text) && !inCode(doc.line(tableEnd + 1).from)) tableEnd++;
      if (tableStart === number || tableEnd === number) continue;
      for (let row = tableStart; row <= tableEnd; row++) {
        const line = doc.line(row);
        lines("lp-ln-tabela", line.from, line.to);
      }
    }
  }
  return {decorations: Decoration.set(decorations, true)};
}

const stableMarks = ViewPlugin.fromClass(class {
  constructor(view) { this.rebuild(view); }
  rebuild(view) {
    this.decorations = inlineRanges(view).decorations;
  }
  update(update) { if (update.docChanged || update.viewportChanged || update.geometryChanged) this.rebuild(update.view); }
}, {decorations: value => value.decorations});

// Editing commands take {state, dispatch} (an EditorView qualifies), matching CodeMirror's
// command signature so they also run against a bare EditorState in tests.

// A selection ending at the start of a line excludes that line, matching a triple click.
function selectedLines(state) {
  const selection = state.selection.main;
  const first = state.doc.lineAt(selection.from).number;
  let last = state.doc.lineAt(selection.to).number;
  if (last > first && selection.to === state.doc.line(last).from) last--;
  const lines = [];
  for (let number = first; number <= last; number++) lines.push(state.doc.line(number));
  return {lines, selection};
}

// Applied as one undo step; without an explicit selection, the cursor moves past inserted text.
function applyChanges(target, changes, selection = null) {
  const {state} = target;
  const set = state.changes(changes);
  target.dispatch(state.update({changes: set, selection: selection || state.selection.map(set, 1), userEvent: "input"}));
  return true;
}

// Replaces only the changed span of each line, so the cursor tracks the text instead of jumping
// to the line boundary.
function replaceLines(target, lines, transform) {
  const changes = [];
  for (const line of lines) {
    const next = transform(line.text, line);
    if (next === line.text) continue;
    let start = 0;
    while (start < line.text.length && start < next.length && line.text[start] === next[start]) start++;
    let end = 0;
    while (end < line.text.length - start && end < next.length - start &&
      line.text[line.text.length - 1 - end] === next[next.length - 1 - end]) end++;
    changes.push({from: line.from + start, to: line.to - end, insert: next.slice(start, next.length - end)});
  }
  return changes.length ? applyChanges(target, changes) : true;
}

function contentStart(line) {
  let index = /^[ \t]*/.exec(line)[0].length;
  for (let pass = 0; pass < 4; pass++) {
    const rest = line.slice(index);
    const prefix = /^(?:#{1,6}[ \t]+|>[ \t]*|(?:[-+*]|\d+(?:\.\d+)*[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)/.exec(rest);
    if (!prefix) break;
    index += prefix[0].length;
  }
  return index;
}

function fencedLines(doc, maximum) {
  const result = new Set();
  let open = null;
  for (let n = 1; n <= maximum; n++) {
    const text = doc.line(n).text;
    const marker = /^\s{0,3}(`{3,}|~{3,})/.exec(text);
    if (open) {
      result.add(n);
      if (marker && marker[1][0] === open[0] && marker[1].length >= open.length) open = null;
    } else if (marker) { result.add(n); open = marker[1]; }
  }
  return result;
}

// Splits a quote prefix ("> ", "> > ") from the rest of the line; headings and bullets act
// after it.
function splitQuote(text) {
  const quote = /^[ \t]*(?:>[ \t]?)+/.exec(text)?.[0] || "";
  return [quote, text.slice(quote.length)];
}
// A line inside a fenced or indented code block is skipped by heading and bullet commands.
function codeLine(state, line) {
  const offset = /^[ \t]*/.exec(line.text)[0].length;
  if (offset >= line.text.length) return false;
  return Boolean(enclosingNode(state, line.from + offset, line.to, node => ["FencedCode", "CodeBlock"].includes(node.name)));
}
// A line indented 4+ columns is indented code, which formatting skips, unless it is a list item
// ("    - item", "    1. item").
function indentedCode(text) {
  const indent = /^[ \t]*/.exec(text)[0];
  let columns = 0;
  for (const character of indent) columns = character === "\t" ? columns + 4 - (columns % 4) : columns + 1;
  return columns >= 4 && !/^(?:[-+*]|\d+(?:\.\d+)*[.)])(?:[ \t]|$)/.test(text.slice(indent.length));
}

function marked(content, open, close) {
  if (!content.startsWith(open) || !content.endsWith(close) || content.length < open.length + close.length + 1) return false;
  if ((open === "*" || open === "_") && content.startsWith(open.repeat(3)) && content.endsWith(close.repeat(3)) &&
      !content.startsWith(open.repeat(4)) && !content.endsWith(close.repeat(4))) return true;
  if ((open === "*" || open === "_") && (content.startsWith(open.repeat(2)) || content.endsWith(close.repeat(2)))) return false;
  return true;
}

// Node containing the selection: with a cursor, only strictly-inside counts; with a range, the
// node may match it exactly.
function enclosingNode(state, from, to, accept) {
  for (let node = syntaxTree(state).resolveInner(from, from === to ? 0 : 1); node; node = node.parent) {
    if (accept(node) && (from === to ? node.from < from && from < node.to : node.from <= from && to <= node.to)) return node;
  }
  return null;
}

// The "==...==" pairs on a line, matching this file's markdown-it rule: the next "==" closes it.
function highlightPairs(text) {
  const pairs = [];
  for (let index = text.indexOf("=="); index >= 0;) {
    const end = text.indexOf("==", index + 2);
    if (end < 0) break;
    if (end === index + 2) { index = text.indexOf("==", index + 1); continue; }
    pairs.push([index, end + 2]);
    index = text.indexOf("==", end + 2);
  }
  return pairs;
}

const INLINE_NODES = {"**": "StrongEmphasis", "*": "Emphasis", "~~": "Strikethrough", "`": "InlineCode"};
const insideCode = (state, from, to = from) =>
  Boolean(enclosingNode(state, from, to, node => ["InlineCode", "FencedCode", "CodeBlock"].includes(node.name)));

// Markup of the given kind around the selection or cursor. Matches the toolbar's active-state
// check, so a highlighted button means clicking it removes the markup.
function enclosingInline(state, from, to, open) {
  if (open === "==") {
    if (insideCode(state, from, to)) return null;
    const line = state.doc.lineAt(from);
    for (const [start, end] of highlightPairs(line.text)) {
      const pairFrom = line.from + start, pairTo = line.from + end;
      if (from === to ? pairFrom < from && from < pairTo : pairFrom <= from && to <= pairTo) {
        return {openFrom: pairFrom, openTo: pairFrom + 2, closeFrom: pairTo - 2, closeTo: pairTo};
      }
    }
    return null;
  }
  const node = enclosingNode(state, from, to, candidate => candidate.name === INLINE_NODES[open]);
  const first = node?.firstChild, last = node?.lastChild;
  if (!first || !last || first.from === last.from) return null;
  return {openFrom: first.from, openTo: first.to, closeFrom: last.from, closeTo: last.to};
}

// For runs of "*": length 1 or 3 is italic, 2 or 3 is bold, so "**" isn't read as italic.
function pairAround(state, line, from, to, open, close) {
  const before = state.doc.sliceString(line.from, from), after = state.doc.sliceString(to, line.to);
  let present;
  if (open === "*" || open === "**") {
    const left = /\**$/.exec(before)[0].length, right = /^\**/.exec(after)[0].length;
    const sizes = open === "*" ? [1, 3] : [2, 3];
    present = sizes.includes(left) && sizes.includes(right);
  } else present = before.endsWith(open) && after.startsWith(close);
  return present ? {openFrom: from - open.length, openTo: from, closeFrom: to, closeTo: to + close.length} : null;
}

// Pairs and code spans the selection cuts through (without fully wrapping them) get extended
// to avoid leaving stray markers.
function crossedPairs(state, line, from, to, open) {
  const crossed = (start, end) => start < to && end > from && !(start <= from && to <= end);
  if (open === "==") {
    return highlightPairs(line.text)
      .map(([start, end]) => ({from: line.from + start, openTo: line.from + start + 2, closeFrom: line.from + end - 2, to: line.from + end}))
      .filter(pair => crossed(pair.from, pair.to));
  }
  const pairs = [];
  syntaxTree(state).iterate({from, to, enter: node => {
    if (node.name !== INLINE_NODES[open] || !crossed(node.from, node.to)) return;
    const first = node.node.firstChild, last = node.node.lastChild;
    if (first && last && first.from !== last.from) pairs.push({from: node.from, openTo: first.to, closeFrom: last.from, to: node.to});
  }});
  return pairs;
}
function crossedCode(state, from, to) {
  const spans = [];
  syntaxTree(state).iterate({from, to, enter: node => {
    if (node.name === "InlineCode" && node.from < to && node.to > from && (node.from < from || node.to > to)) spans.push({from: node.from, to: node.to});
  }});
  return spans;
}

function unwrap(target, pair) {
  return applyChanges(target, [{from: pair.openFrom, to: pair.openTo}, {from: pair.closeFrom, to: pair.closeTo}]);
}

// Bold, italic, strike, highlight and code (HF-EDIT-002): applies to the selection, or the word
// at the cursor, or inserts an empty pair; the same command removes existing markup.
export function formatInline(target, open, close = open) {
  const {state} = target;
  const {lines, selection} = selectedLines(state);
  if (lines.length > 1) return formatLines(target, lines, open, close);
  const line = lines[0];
  const lonePair = line.text.trim() === open + close;
  if (!lonePair && (indentedCode(line.text) || fencedLines(state.doc, line.number).has(line.number))) return true;
  const contentFrom = line.from + contentStart(line.text);
  let from = Math.max(selection.from, contentFrom), to = Math.min(selection.to, line.to);
  while (from < to && /\s/.test(state.sliceDoc(from, from + 1))) from++;
  while (to > from && /\s/.test(state.sliceDoc(to - 1, to))) to--;
  if (from === to) from = to = Math.min(Math.max(selection.head, contentFrom), line.to);
  // Inside inline code only the code command acts (to remove it); other pairs would be literal text.
  if (open !== "`" && enclosingNode(state, from, to, node => node.name === "InlineCode")) return true;
  if (from < to) return formatRange(target, line, from, to, selection, open, close);
  return formatAtCursor(target, line, from, open, close);
}

function formatRange(target, line, from, to, selection, open, close) {
  const {state} = target;
  const forward = selection.head >= selection.anchor;
  const pick = (start, end) => forward ? {anchor: start, head: end} : {anchor: end, head: start};
  const enclosing = enclosingInline(state, from, to, open);
  if (enclosing) return unwrap(target, enclosing);
  if (marked(state.sliceDoc(from, to), open, close)) {
    return applyChanges(target, [{from, to: from + open.length}, {from: to - close.length, to}],
      pick(from, to - open.length - close.length));
  }
  const around = pairAround(state, line, from, to, open, close);
  if (around) return unwrap(target, around);
  const pairs = crossedPairs(state, line, from, to, open);
  const code = open === "`" ? [] : crossedCode(state, from, to);
  if (pairs.length || code.length) {
    const start = Math.min(from, ...pairs.map(pair => pair.from), ...code.map(span => span.from));
    const end = Math.max(to, ...pairs.map(pair => pair.to), ...code.map(span => span.to));
    const removed = pairs.reduce((sum, pair) => sum + (pair.openTo - pair.from) + (pair.to - pair.closeFrom), 0);
    const changes = [{from: start, insert: open}, {from: end, insert: close}];
    for (const pair of pairs) changes.push({from: pair.from, to: pair.openTo}, {from: pair.closeFrom, to: pair.to});
    return applyChanges(target, changes, pick(start + open.length, end + open.length - removed));
  }
  return applyChanges(target, [{from, insert: open}, {from: to, insert: close}], pick(from + open.length, to + open.length));
}

function formatAtCursor(target, line, position, open, close) {
  const {state} = target;
  const enclosing = enclosingInline(state, position, position, open);
  // Cursor right before a non-empty pair's closing marker steps out of it instead of removing it
  // (as in Typora and Bear).
  if (enclosing && enclosing.closeFrom === position && enclosing.openTo < position) {
    target.dispatch(state.update({selection: {anchor: enclosing.closeTo}, userEvent: "select"}));
    return true;
  }
  if (enclosing) return unwrap(target, enclosing);
  const word = state.wordAt(position);
  if (word && word.from < position && position < word.to) {
    return applyChanges(target, [{from: word.from, insert: open}, {from: word.to, insert: close}], {anchor: position + open.length});
  }
  const empty = pairAround(state, line, position, position, open, close);
  if (empty) return unwrap(target, empty);
  return applyChanges(target, {from: position, insert: open + close}, {anchor: position + open.length});
}

// Multi-line selection: removes the pair when every applicable line already has it, otherwise
// adds it only where missing, never covering line prefixes or trailing whitespace.
function formatLines(target, lines, open, close) {
  const {state} = target;
  const fenced = fencedLines(state.doc, lines.at(-1).number);
  const parts = lines
    .filter(line => line.text.trim() && !fenced.has(line.number) && !indentedCode(line.text))
    .map(line => {
      const start = contentStart(line.text), end = Math.max(start, line.text.trimEnd().length);
      return {from: line.from + start, to: line.from + end, text: line.text.slice(start, end)};
    })
    .filter(part => part.text);
  if (!parts.length) return true;
  const remove = parts.every(part => marked(part.text, open, close));
  const changes = [];
  for (const part of parts) {
    if (remove) changes.push({from: part.from, to: part.from + open.length}, {from: part.to - close.length, to: part.to});
    else if (!marked(part.text, open, close)) changes.push({from: part.from, insert: open}, {from: part.to, insert: close});
  }
  return changes.length ? applyChanges(target, changes) : true;
}

// Applying the same bullet marker again removes the list; a different marker converts it,
// keeping indent and any task checkbox.
export function setBullet(target, marker) {
  const {lines, selection} = selectedLines(target.state);
  const singleEmpty = selection.empty && lines.length === 1 && !lines[0].text.trim();
  const targets = lines.filter(line => line.text.trim() || singleEmpty);
  if (!targets.length) return true;
  const pattern = /^([ \t]*)([-+*])([ \t]+)(.*)$/;
  if (singleEmpty) {
    const line = lines[0];
    const indent = /^[ \t]*/.exec(line.text)[0];
    const insert = `${indent}${marker} ${line.text.slice(indent.length)}`;
    return applyChanges(target, {from: line.from, to: line.to, insert}, {anchor: line.from + insert.length});
  }
  const fenced = fencedLines(target.state.doc, lines.at(-1).number);
  const editable = targets.filter(line => !fenced.has(line.number) && !codeLine(target.state, line));
  if (!editable.length) return true;
  const remove = editable.every(line => pattern.exec(splitQuote(line.text)[1])?.[2] === marker);
  const numbered = /^([ \t]*)\d+[.)]([ \t]+)(.*)$/;
  const heading = /^([ \t]*)#{1,6}[ \t]+(.*)$/;
  return replaceLines(target, editable, text => {
    const [quote, body] = splitQuote(text);
    const match = pattern.exec(body);
    if (remove) return `${quote}${match[1]}${match[4]}`;
    if (match) return match[2] === marker ? text : `${quote}${match[1]}${marker}${match[3]}${match[4]}`;
    const number = numbered.exec(body);
    if (number) return `${quote}${number[1]}${marker}${number[2]}${number[3]}`;
    // A heading becomes a list item: the bullet replaces the heading marker.
    const title = heading.exec(body);
    if (title) return `${quote}${title[1]}${marker} ${title[2]}`;
    const indent = /^[ \t]*/.exec(body)[0];
    return `${quote}${indent}${marker} ${body.slice(indent.length)}`;
  });
}

// Heading level 1 to 3; applying the same level again removes the heading.
export function setHeading(target, level = 1) {
  const marks = "#".repeat(level);
  const {lines} = selectedLines(target.state);
  const fenced = fencedLines(target.state.doc, lines.at(-1).number);
  const skip = line => fenced.has(line.number) || codeLine(target.state, line);
  const filled = lines.filter(line => line.text.trim() && !skip(line));
  const same = text => { const match = /^\s*(#{1,6})[ \t]+/.exec(splitQuote(text)[1]); return Boolean(match) && match[1].length === level; };
  const remove = filled.length > 0 && filled.every(line => same(line.text));
  return replaceLines(target, lines, (text, line) => {
    if (skip(line)) return text;
    const [quote, body] = splitQuote(text);
    const match = /^(\s*)(#{1,6})[ \t]+(.*)$/.exec(body);
    if (remove) return match ? `${quote}${match[1]}${match[3]}` : text;
    if (!text.trim() && lines.length > 1) return text;
    if (match) return `${quote}${match[1]}${marks} ${match[3]}`;
    // A list item becomes a heading: the bullet marker is dropped.
    const item = /^[ \t]*(?:[-+*]|\d+[.)])[ \t]+(?:\[[ xX]\][ \t]+)?(.*)$/.exec(body);
    if (item) return `${quote}${marks} ${item[1]}`;
    const prefix = /^[ \t]*/.exec(body)[0];
    return `${quote}${prefix}${marks} ${body.slice(prefix.length)}`;
  });
}

const LINK_PARENTS = ["Link", "Image", "Autolink", "LinkReference"];

// A bare address (GFM autolink) at the cursor, outside of a link.
function bareUrlAt(state, position) {
  for (const side of [1, -1]) {
    for (let node = syntaxTree(state).resolveInner(position, side); node; node = node.parent) {
      if (node.name === "URL") return LINK_PARENTS.includes(node.parent?.name) ? null : node;
    }
  }
  return null;
}

function selectRange(target, anchor, head) {
  target.dispatch(target.state.update({selection: {anchor, head}, scrollIntoView: true, userEvent: "select"}));
  return true;
}

// Inside a link, the Link button selects the destination for replacement instead of nesting
// another link.
function selectLinkDestination(target, link) {
  const {state} = target;
  // The destination is the URL node after "("; GFM can also read a label address as a URL node.
  const opening = link.getChildren("LinkMark").find(mark => state.sliceDoc(mark.from, mark.to) === "(");
  const url = link.getChildren("URL").find(node => !opening || node.from >= opening.to) || null;
  if (url) return selectRange(target, url.from, url.to);
  const label = link.getChild("LinkLabel");
  if (label && label.to - label.from > 2) return selectRange(target, label.from + 1, label.to - 1);
  const paren = link.getChildren("LinkMark").find(mark => state.sliceDoc(mark.from, mark.to) === "(");
  if (paren) return applyChanges(target, {from: paren.to, insert: "url"}, {anchor: paren.to, head: paren.to + 3});
  const at = label ? label.from : link.to;
  return applyChanges(target, {from: at, to: link.to, insert: "(url)"}, {anchor: at + 1, head: at + 4});
}

// Link: selection (or the word/address at the cursor) becomes the label; "url" is left selected.
// An address also becomes the destination. An image uses the same form; with a given destination
// (an attachment), the cursor moves to the end.
export function insertLink(target, image = false, destination = null, requestedLabel = null) {
  const {state} = target;
  const range = state.selection.main;
  let from = range.from, to = range.to;
  if (!image) {
    // The label may contain an address that GFM reads as an autolink; the outer link takes precedence.
    const link = enclosingNode(state, from, to, node => node.name === "Link") ||
      enclosingNode(state, from, to, node => node.name === "Autolink");
    if (link) return selectLinkDestination(target, link);
    if (range.empty) {
      const around = bareUrlAt(state, range.head) || state.wordAt(range.head);
      if (around) ({from, to} = around);
    }
    while (from < to && /\s/.test(state.sliceDoc(from, from + 1))) from++;
    while (to > from && /\s/.test(state.sliceDoc(to - 1, to))) to--;
    const address = state.sliceDoc(from, to);
    if (/^(?:https?:\/\/|www\.)\S+$/i.test(address)) {
      const href = /^www\./i.test(address) ? `https://${address}` : address;
      return applyChanges(target, {from, to, insert: `[${address}](${href})`}, {anchor: from + 1, head: from + 1 + address.length});
    }
  }
  const selected = state.sliceDoc(from, to);
  const label = requestedLabel || selected || (image ? "descrição" : "texto");
  const escapedLabel = image ? label.replace(/[\\\[\]]/g, "\\$&") : label;
  const href = destination || "url";
  // An inserted image gets a space on each side so it doesn't run into surrounding words.
  const before = image && from > 0 && /\S/.test(state.sliceDoc(from - 1, from)) ? " " : "";
  const after = image && /\S/.test(state.sliceDoc(to, to + 1)) ? " " : "";
  const markup = image ? `![${escapedLabel}](${href})` : `[${escapedLabel}](${href})`;
  const insert = `${before}${markup}${after}`;
  const urlStart = from + before.length + (image ? escapedLabel.length + 4 : escapedLabel.length + 3);
  const selection = destination ? {anchor: from + before.length + markup.length + after.length} : {anchor: urlStart, head: urlStart + 3};
  return applyChanges(target, {from, to, insert}, selection);
}

const INLINE_FORMATS = [["bold", "**"], ["italic", "*"], ["strike", "~~"], ["highlight", "=="], ["code", "`"]];
const BULLET_COMMANDS = {"-": "bulletDash", "*": "bulletStar", "+": "bulletPlus"};

// Active toolbar commands at the main cursor: inline markup around the selection, heading level,
// and bullet marker of the line.
export function activeFormats(state) {
  const active = new Set();
  const {from, to, head} = state.selection.main;
  for (const [name, open] of INLINE_FORMATS) if (enclosingInline(state, from, to, open)) active.add(name);
  if (enclosingNode(state, from, to, node => node.name === "Link" || node.name === "Autolink")) active.add("link");
  const text = state.doc.lineAt(head).text;
  const heading = /^\s*(#{1,6})[ \t]+/.exec(text);
  if (heading && heading[1].length <= 3) active.add(`heading${heading[1].length}`);
  const bullet = /^[ \t]*([-+*])[ \t]+/.exec(text);
  if (bullet) active.add(BULLET_COMMANDS[bullet[1]]);
  return active;
}

function indentSelection(target, remove = false) {
  const {lines} = selectedLines(target.state);
  const changes = [];
  for (const line of lines) {
    // Indenting only applies to list items; in a paragraph it would become a code block.
    if (!remove) { if (/^[ \t]*(?:[-+*]|\d+[.)])[ \t]/.test(splitQuote(line.text)[1])) changes.push({from: line.from, insert: "    "}); }
    else {
      const prefix = /^[ \t]{1,4}/.exec(line.text)?.[0];
      if (prefix) changes.push({from: line.from, to: line.from + prefix.length});
    }
  }
  return changes.length ? applyChanges(target, changes) : true;
}

function rawEnter(view) {
  const range = view.state.selection.main;
  view.dispatch({
    changes: {from: range.from, to: range.to, insert: view.state.lineBreak},
    selection: {anchor: range.from + 1},
    userEvent: "input",
  });
  view.focus();
  return true;
}

// onActiveFormats(set), optional: called once after setup and again whenever the active formats
// change with the selection or text.
export function createMarkdownEditor({parent, initial, nonce, onChange, onSave, onTypingChange, onActiveFormats}) {
  let reportedFormats = null;
  const reportFormats = state => {
    if (!onActiveFormats) return;
    const active = activeFormats(state);
    const key = [...active].sort().join(" ");
    if (key === reportedFormats) return;
    reportedFormats = key;
    onActiveFormats(active);
  };
  const custom = [
    {key: "Mod-s", run: view => { onSave(); return true; }},
    {key: "Mod-b", run: view => formatInline(view, "**")},
    {key: "Mod-i", run: view => formatInline(view, "*")},
    {key: "Mod-Shift-s", run: view => formatInline(view, "~~")},
    {key: "Enter", run: rawEnter},
    {key: "Tab", run: view => indentSelection(view)},
    {key: "Shift-Tab", run: view => indentSelection(view, true)},
    {key: "Mod-z", run: undo},
    {key: "Mod-Shift-z", run: redo},
  ];
  const editable = new Compartment();
  const baseExtensions = [
    EditorView.cspNonce.of(nonce || ""),
    editable.of(EditorView.editable.of(true)),
    // GFM (strike, table, task) in the syntax tree; the raw Enter handler below takes precedence.
    lineNumbers(), highlightActiveLine(), drawSelection(), dropCursor(), history(), EditorView.lineWrapping,
    markdown({base: markdownLanguage}), stableMarks,
    Prec.highest(keymap.of(custom)),
    EditorView.inputHandler.of((editor, from, to, text) => {
      const line = editor.state.doc.lineAt(from);
      const end = line.to;
      if (from === end - 1 && to === end && /^[ \t]*[-+*] $/.test(line.text) && text && !/^\s/.test(text)) {
        editor.dispatch({changes: {from: end, insert: text}, selection: {anchor: end + text.length}, userEvent: "input.type"});
        return true;
      }
      return false;
    }),
    keymap.of([...defaultKeymap, ...historyKeymap]),
    EditorView.updateListener.of(update => {
      if (update.docChanged) onChange();
      if (update.docChanged || update.selectionSet || syntaxTree(update.state) !== syntaxTree(update.startState)) reportFormats(update.state);
    }),
    // All colors are theme tokens, so the editor follows the light and dark themes automatically.
    EditorView.theme({
      "&": {height: "100%", fontSize: "12.5px", backgroundColor: "var(--panel)", color: "var(--text)"},
      ".cm-scroller": {overflow: "auto", fontFamily: "var(--font-sans)", lineHeight: "1.65"},
      ".cm-content": {padding: "34px 32px 60px", minHeight: "18rem", caretColor: "var(--accent)"},
      ".cm-gutters": {backgroundColor: "var(--bg)", color: "var(--text2)", border: "0", borderRight: "1px solid var(--hair)"},
      ".cm-activeLine": {backgroundColor: "var(--hover)"},
      ".cm-activeLineGutter": {backgroundColor: "var(--hover)", color: "var(--text)"},
      ".cm-cursor, .cm-dropCursor": {borderLeftColor: "var(--accent)"},
      // Repeats the .cm-editor class to outrank the specificity of CodeMirror's base selection style.
      "&.cm-editor.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, &.cm-editor > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-content ::selection": {backgroundColor: "var(--selecao-texto)"},
    }),
  ];
  let baselineRaw = initial;
  const createState = text => EditorState.create({doc: normalizeLineEndings(text), extensions: baseExtensions});
  const initialState = createState(initial);
  const view = new EditorView({state: initialState, parent});
  let baselineDoc = initialState.doc;
  view.contentDOM.setAttribute("aria-label", "Fonte Markdown");
  view.contentDOM.setAttribute("spellcheck", "true");
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  let typingEnabled = !coarse;
  const updateKeyboard = () => { view.contentDOM.setAttribute("inputmode", typingEnabled ? "text" : "none"); };
  updateKeyboard();
  view.contentDOM.addEventListener("blur", () => {
    if (coarse) { typingEnabled = false; updateKeyboard(); onTypingChange?.(false); }
  });
  let destroyed = false;
  queueMicrotask(() => { if (!destroyed) reportFormats(view.state); });
  return {
    view,
    getValue: () => preserveLineEndings(baselineRaw, view.state.doc.toString()),
    isDirty: () => !view.state.doc.eq(baselineDoc),
    setBaseline(text) {
      baselineRaw = text;
      baselineDoc = Text.of(normalizeLineEndings(text).split("\n"));
    },
    replace(text) {
      baselineRaw = text;
      const nextState = createState(text);
      view.setState(nextState);
      baselineDoc = nextState.doc;
      view.contentDOM.setAttribute("aria-label", "Fonte Markdown");
      view.contentDOM.setAttribute("spellcheck", "true");
      updateKeyboard();
      onChange();
      reportedFormats = null;
      reportFormats(view.state);
    },
    focus: () => view.focus(),
    topLine() {
      const block = view.lineBlockAtHeight(Math.max(0, view.scrollDOM.scrollTop));
      return view.state.doc.lineAt(block.from).number;
    },
    revealLine(number, {cursor = false} = {}) {
      const line = view.state.doc.line(Math.min(Math.max(1, number), view.state.doc.lines));
      view.dispatch({
        ...(cursor ? {selection: {anchor: line.from}} : {}),
        effects: EditorView.scrollIntoView(line.from, {y: "start", yMargin: 12}),
      });
    },
    destroy: () => { destroyed = true; view.destroy(); },
    setTypingEnabled(enabled) {
      typingEnabled = Boolean(enabled); updateKeyboard(); onTypingChange?.(typingEnabled);
      if (typingEnabled) view.focus();
    },
    setReadOnly(readOnly) {
      view.dispatch({effects: editable.reconfigure(EditorView.editable.of(!readOnly))});
      if (readOnly) view.contentDOM.setAttribute("aria-readonly", "true");
      else view.contentDOM.removeAttribute("aria-readonly");
    },
    isTypingEnabled: () => typingEnabled,
    coarse,
    command(name, destination = null, label = null) {
      const run = ({heading: () => setHeading(view, 1), heading1: () => setHeading(view, 1), heading2: () => setHeading(view, 2), heading3: () => setHeading(view, 3), bold: () => formatInline(view, "**"), italic: () => formatInline(view, "*"), strike: () => formatInline(view, "~~"), highlight: () => formatInline(view, "=="), code: () => formatInline(view, "`"), link: () => insertLink(view), image: () => insertLink(view, true, destination, label), bulletDash: () => setBullet(view, "-"), bulletStar: () => setBullet(view, "*"), bulletPlus: () => setBullet(view, "+"), tab: () => indentSelection(view), outdent: () => indentSelection(view, true), undo: () => undo(view), redo: () => redo(view)})[name];
      if (!run) return;
      run();
      view.focus();
    },
  };
}

// The textarea already wraps long lines by default; no EditorView is needed here.
export function createPlainEditor({parent, initial, onChange, onSave, onTypingChange}) {
  const area = document.createElement("textarea");
  area.className = "plain-editor";
  area.setAttribute("aria-label", "Texto do arquivo");
  area.spellcheck = false;
  let baselineRaw = initial;
  let baselineValue = normalizeLineEndings(initial);
  area.value = baselineValue;
  area.addEventListener("input", () => onChange());
  // Cmd+S (Mac) or Ctrl+S saves and blocks the browser's save-page action; the event still bubbles
  // with defaultPrevented set.
  const mac = /Mac/.test(navigator.platform);
  area.addEventListener("keydown", event => {
    if (!onSave || event.altKey || event.shiftKey || (mac ? !event.metaKey || event.ctrlKey : !event.ctrlKey || event.metaKey)) return;
    if (event.key.toLowerCase() !== "s" && event.keyCode !== 83) return;
    event.preventDefault();
    onSave();
  });
  parent.append(area);
  const coarse = window.matchMedia("(pointer: coarse)").matches;
  let typingEnabled = !coarse;
  const updateKeyboard = () => area.setAttribute("inputmode", typingEnabled ? "text" : "none");
  updateKeyboard();
  area.addEventListener("blur", () => {
    if (coarse) { typingEnabled = false; updateKeyboard(); onTypingChange?.(false); }
  });
  return {
    element: area,
    getValue: () => preserveLineEndings(baselineRaw, area.value),
    isDirty: () => area.value !== baselineValue,
    setBaseline(text) { baselineRaw = text; baselineValue = normalizeLineEndings(text); },
    replace(text) {
      baselineRaw = text;
      baselineValue = normalizeLineEndings(text);
      area.value = baselineValue;
      onChange();
    },
    focus: () => area.focus(), destroy: () => area.remove(), coarse,
    isTypingEnabled: () => typingEnabled,
    setTypingEnabled(enabled) {
      typingEnabled = Boolean(enabled); updateKeyboard(); onTypingChange?.(typingEnabled);
      if (typingEnabled) area.focus();
    },
    setReadOnly(readOnly) { area.readOnly = Boolean(readOnly); area.setAttribute("aria-readonly", String(Boolean(readOnly))); },
  };
}

function normalizeLineEndings(text) {
  return text.replace(/\r\n?/g, "\n");
}

export function preserveLineEndings(original, normalizedCurrent) {
  const normalizedOriginal = normalizeLineEndings(original);
  if (normalizedOriginal === normalizedCurrent) return original;
  let prefix = 0;
  const shared = Math.min(normalizedOriginal.length, normalizedCurrent.length);
  while (prefix < shared && normalizedOriginal.charCodeAt(prefix) === normalizedCurrent.charCodeAt(prefix)) prefix++;
  let oldTail = normalizedOriginal.length;
  let currentTail = normalizedCurrent.length;
  while (oldTail > prefix && currentTail > prefix &&
    normalizedOriginal.charCodeAt(oldTail - 1) === normalizedCurrent.charCodeAt(currentTail - 1)) {
    oldTail--; currentTail--;
  }
  const oldEndings = original.match(/\r\n|\r|\n/g) || [];
  const countBreaks = (text, end) => {
    let count = 0;
    for (let index = 0; index < end; index++) if (text.charCodeAt(index) === 10) count++;
    return count;
  };
  const oldPrefix = countBreaks(normalizedOriginal, prefix);
  const oldSuffix = countBreaks(normalizedOriginal, oldTail);
  const currentPrefix = countBreaks(normalizedCurrent, prefix);
  const currentSuffix = countBreaks(normalizedCurrent, currentTail);
  const changedEndings = oldEndings.slice(oldPrefix, oldSuffix);
  const frequencies = new Map();
  for (const ending of oldEndings) frequencies.set(ending, (frequencies.get(ending) || 0) + 1);
  let fallback = oldEndings[0] || "\n";
  for (const [ending, count] of frequencies) if (count > (frequencies.get(fallback) || 0)) fallback = ending;
  const lines = normalizedCurrent.split("\n");
  const output = [];
  let offset = 0;
  for (let index = 0; index < lines.length; index++) {
    output.push(lines[index]);
    if (index === lines.length - 1) break;
    const newlinePosition = offset + lines[index].length;
    const breakIndex = index;
    let ending;
    if (breakIndex < currentPrefix) ending = oldEndings[breakIndex];
    else if (newlinePosition >= currentTail) {
      ending = oldEndings[oldSuffix + breakIndex - currentSuffix];
    } else {
      ending = changedEndings[breakIndex - currentPrefix];
    }
    output.push(ending || fallback);
    offset = newlinePosition + 1;
  }
  return output.join("");
}

export function renderPreviewHtml(source, target, options = {}) {
  const references = [];
  const markerPrefix = `\uE000hf-image-${++imageRenderSequence}-${Math.random().toString(36).slice(2)}-`;
  annotateLines = true;
  try { target.innerHTML = renderMarkdown(source, references, markerPrefix); }
  finally { annotateLines = false; }
  // A table wider than the text scrolls sideways in its own focusable container, added after
  // DOMPurify since sanitized output can't include a wrapping div.
  for (const table of target.querySelectorAll("table")) {
    const box = document.createElement("div");
    box.className = "tab-rolagem";
    box.tabIndex = 0;
    box.setAttribute("role", "region");
    box.setAttribute("aria-label", "Tabela");
    table.replaceWith(box);
    box.append(table);
  }
  if (!references.length) return;
  const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
  const markers = [];
  const replacements = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    const text = node.nodeValue;
    const parts = [];
    let cursor = 0;
    let searchFrom = 0;
    while (searchFrom < text.length) {
      const start = text.indexOf(markerPrefix, searchFrom);
      if (start < 0) break;
      const end = text.indexOf("\uE001", start + markerPrefix.length);
      if (end < 0) {
        searchFrom = start + markerPrefix.length;
        continue;
      }
      const indexText = text.slice(start + markerPrefix.length, end);
      if (!/^\d+$/.test(indexText)) {
        searchFrom = end + 1;
        continue;
      }
      const index = Number(indexText);
      if (!Number.isSafeInteger(index) || index >= references.length) {
        searchFrom = end + 1;
        continue;
      }
      if (start > cursor) parts.push(document.createTextNode(text.slice(cursor, start)));
      const marker = document.createElement("span");
      marker.setAttribute("aria-hidden", "true");
      parts.push(marker);
      markers.push([marker, references[index]]);
      cursor = end + 1;
      searchFrom = cursor;
    }
    if (!parts.length) continue;
    if (cursor < text.length) parts.push(document.createTextNode(text.slice(cursor)));
    replacements.push([node, parts]);
  }
  for (const [node, parts] of replacements) node.replaceWith(...parts);
  for (const [marker, reference] of markers) {
    const fallback = () => {
      if (marker.isConnected) marker.replaceWith(document.createTextNode(reference.alt));
    };
    if (typeof options.resolveImage !== "function" || typeof options.previewUrl !== "function") {
      fallback();
      continue;
    }
    Promise.resolve(options.resolveImage(reference.destination)).then(identity => {
      if (!marker.isConnected || identity?.resolved !== true ||
          typeof identity.rootId !== "string" || typeof identity.path !== "string") return fallback();
      const image = document.createElement("img");
      image.alt = reference.alt;
      if (reference.title) image.title = reference.title;
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      const url = new URL(options.previewUrl(identity), document.baseURI);
      if (url.origin !== window.location.origin) return fallback();
      image.src = url.href;
      marker.replaceWith(image);
    }).catch(fallback);
  }
}

export function saveBlob(name, text, type = "text/plain;charset=utf-8") {
  const blob = new Blob([text], {type});
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function cleanCopy(source, markdownMode = true) {
  let text = source.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
  const protectedParts = [];
  const protectedBlocks = [];
  const protect = (value, block = false) => {
    const key = `\uE000HF${block ? "B" : "I"}${protectedParts.length}\uE001`;
    protectedParts.push(value);
    protectedBlocks.push(block);
    return key;
  };
  if (markdownMode) {
    text = protectFencedCode(text, protect);
    text = text.replace(/(`+)([^`\n]+)\1/g, (_whole, _ticks, body) => protect(body));
  }
  const lines = text.split("\n");
  const normalizedLines = [];
  for (let start = 0; start < lines.length;) {
    if (!lines[start].trim()) { normalizedLines.push(lines[start++]); continue; }
    let end = start + 1;
    while (end < lines.length && lines[end].trim()) end++;
    const block = lines.slice(start, end);
    const margin = commonMargin(block);
    for (const line of block) normalizedLines.push(line.slice(margin));
    start = end;
  }
  if (markdownMode) text = recompose(normalizedLines, true);
  else text = recomposePlain(normalizedLines);
  if (markdownMode) {
    text = text
      .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
      .replace(/^[ \t]{0,3}>[ \t]?/gm, "")
      .replace(/^[ \t]*(?:[-*_][ \t]*){3,}$/gm, "")
      // The table separator line is removed with its line break, so header and data end up adjacent.
      .replace(/^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$\n?/gm, "")
      .replace(/^[ \t]*\|(.+)\|[ \t]*$/gm, (_whole, row) => row.split("|").map(cell => cell.trim()).filter(Boolean).join("  "))
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/\[([^\]]+)\]\[[^\]]*\]/g, "$1")
      .replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2")
      .replace(/\[\[([^\]]+)\]\]/g, "$1")
      .replace(/<((?:https?:\/\/|mailto:)[^>]+)>/gi, "$1")
      .replace(/\\([\\`*_{}\[\]()#+.\-!>])/g, "$1")
      .replace(/(\*\*|__|~~|==)(?=\S)(.+?\S?)\1/g, "$2")
      .replace(/(?<![\w*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?![\w*])/g, "$1")
      .replace(/(?<![\w_])_([^\s_](?:[^_\n]*?[^\s_])?)_(?![\w_])/g, "$1")
      .replace(/[ \t]*\\$/gm, "")
      .replace(/^([ \t]*)([-+*]|\d+(?:\.\d+)*\.)([ \t]+)(.*)$/gm, (_whole, _indent, marker, spacing, body) => `${marker}${spacing}${body}`);
  }
  text = cleanup(text);
  return restoreProtectedParts(text, protectedParts);
}

function restoreProtectedParts(text, protectedParts) {
  const marker = /\uE000HF([BI])(\d+)\uE001/g;
  const restored = [];
  let cursor = 0;
  for (const match of text.matchAll(marker)) {
    const index = Number(match[2]);
    if (index >= protectedParts.length) continue;
    restored.push(text.slice(cursor, match.index), protectedParts[index]);
    cursor = match.index + match[0].length;
  }
  return cursor ? restored.concat(text.slice(cursor)).join("") : text;
}

function protectFencedCode(text, protect) {
  const lines = text.split("\n");
  const output = [];
  for (let i = 0; i < lines.length; i++) {
    const opening = /^[ \t]{0,3}(`{3,}|~{3,})/.exec(lines[i]);
    if (!opening) { output.push(lines[i]); continue; }
    const marker = opening[1];
    const closing = new RegExp(`^[ \\t]{0,3}(${marker[0]}{${marker.length},})[ \\t]*$`);
    const body = [];
    let end = i + 1;
    while (end < lines.length) {
      const close = closing.exec(lines[end]);
      if (close && close[1].length >= marker.length) break;
      body.push(lines[end]);
      end++;
    }
    output.push(protect(body.join("\n"), true));
    if (end < lines.length) i = end;
    else i = lines.length;
  }
  return output.join("\n");
}

function commonMargin(lines) {
  const nonempty = lines.filter(line => line.trim());
  if (!nonempty.length) return 0;
  let prefix = /^[ \t]*/.exec(nonempty[0])[0];
  for (const line of nonempty.slice(1)) {
    const current = /^[ \t]*/.exec(line)[0];
    let n = 0;
    while (n < prefix.length && n < current.length && prefix[n] === current[n]) n++;
    prefix = prefix.slice(0, n);
  }
  return prefix.length;
}

function recompose(lines, markdownMode = true) {
  const isCodeLine = line => /^\s{4,}/.test(line) || /\uE000HFB\d+\uE001/.test(line);
  let width = 0;
  for (const line of lines) if (line && !isCodeLine(line) && line.length > width) width = line.length;
  const threshold = Math.max(50, Math.floor(0.88 * width));
  const yaml = new Set();
  if (markdownMode && lines[0]?.trim() === "---") {
    yaml.add(0);
    for (let i = 1; i < lines.length; i++) { yaml.add(i); if (lines[i].trim() === "---") break; }
  }
  const protectedLine = (line, index) => {
    if (yaml.has(index) || !line.trim() || /(?: {2,}|\\)$/.test(line)) return true;
    if (/\S {3,}\S/.test(line)) return true;
    if (markdownMode && /^\s{0,3}(?:#{1,6}\s|>|\|)/.test(line)) return true;
    if (/^\s{4,}/.test(line)) return true;
    if (markdownMode && (/^\s{0,3}(?:[-*_][ \t]*){3,}$/.test(line) || /^\s{0,3}(?:`{3,}|~{3,})/.test(line))) return true;
    if (markdownMode && /^\s{0,3}(?:[-+*]|\d+(?:\.\d+)*\.)(?:\s|$)/.test(line)) return true;
    if (/\uE000HFB\d+\uE001/.test(line)) return true;
    return false;
  };
  const canJoin = (left, right, i) => {
    if (protectedLine(left, i) || protectedLine(right, i + 1)) return false;
    return left.length >= threshold || left.length + 1 + firstWord(right).length > width;
  };
  let candidateCount = 0;
  for (let i = 0; i < lines.length - 1; i++) if (canJoin(lines[i], lines[i + 1], i)) candidateCount++;
  if (candidateCount < 2) return lines.join("\n");
  const output = [];
  if (!lines.length) return "";
  let current = [lines[0]];
  for (let i = 0; i < lines.length - 1; i++) {
    if (canJoin(lines[i], lines[i + 1], i)) {
      current[current.length - 1] = current[current.length - 1].trimEnd();
      current.push(" ", lines[i + 1].trimStart());
    } else {
      output.push(current.join(""));
      current = [lines[i + 1]];
    }
  }
  output.push(current.join(""));
  return output.join("\n");
}

function recomposePlain(lines) {
  return recompose(lines, false);
}

function firstWord(line) { return /^\s*([^\s]+)/.exec(line)?.[1] || ""; }
function cleanup(text) {
  return text.split("\n").map(line => line.replace(/[ \t]+$/g, "")).join("\n")
    .replace(/\n{3,}/g, "\n\n").replace(/^\n+/, "").replace(/[ \t\n]+$/g, "");
}
