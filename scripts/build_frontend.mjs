import {cp, mkdir, readFile, readdir, rm, writeFile} from "node:fs/promises";
import {createHash} from "node:crypto";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {build} from "../frontend/node_modules/esbuild/lib/main.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const frontend = path.join(root, "frontend");
const staticRoot = path.join(root, "src/hopper_files/static");
const assets = path.join(staticRoot, "assets");
await rm(assets, {recursive: true, force: true});
await mkdir(assets, {recursive: true});

const result = await build({
  absWorkingDir: root,
  entryPoints: ["frontend/src/entry.js"],
  bundle: true,
  splitting: true,
  format: "esm",
  platform: "browser",
  target: ["es2022"],
  sourcemap: false,
  legalComments: "none",
  minify: true,
  metafile: true,
  outdir: "src/hopper_files/static",
  entryNames: "app.bundle",
  chunkNames: "assets/[name]-[hash]",
  assetNames: "assets/[name]-[hash]",
  publicPath: "",
  loader: {".wasm": "file"},
});

const pdfRoot = path.join(frontend, "node_modules/pdfjs-dist");
await cp(path.join(pdfRoot, "build/pdf.worker.min.mjs"), path.join(assets, "pdf.worker.min.mjs"));
for (const [sourceName, destName, suffixes] of [
  ["cmaps", "cmaps", [".bcmap"]],
  ["standard_fonts", "standard_fonts", [".pfb", ".ttf"]],
  ["wasm", "wasm", [".wasm", ".js"]],
  ["iccs", "iccs", [".icc"]],
]) {
  const from = path.join(pdfRoot, sourceName);
  const to = path.join(assets, destName);
  await rm(to, {recursive: true, force: true});
  await mkdir(to, {recursive: true});
  for (const name of await readdir(from)) {
    if (suffixes.some(suffix => name.endsWith(suffix))) await cp(path.join(from, name), path.join(to, name));
  }
}

const lock = JSON.parse(await readFile(path.join(frontend, "package-lock.json"), "utf8"));
const packageNames = new Set();
for (const output of Object.values(result.metafile.outputs)) {
  for (const input of Object.keys(output.inputs)) {
    const marker = "frontend/node_modules/";
    if (!input.startsWith(marker)) continue;
    const rest = input.slice(marker.length).split("/");
    packageNames.add(rest[0].startsWith("@") ? `${rest[0]}/${rest[1]}` : rest[0]);
  }
}
packageNames.add("pdfjs-dist");
const notices = [
  "Hopper Files includes the following third-party browser libraries. Package archives were obtained from https://registry.npmjs.org/ and are integrity-pinned in frontend/package-lock.json. Their source licenses are reproduced below.",
  "",
];
const licenseText = text => text.trimEnd().replace(/[\t ]+$/gm, "");
for (const name of [...packageNames].sort()) {
  const lockEntry = Object.entries(lock.packages).find(([key]) => key === `node_modules/${name}`)?.[1];
  if (!lockEntry) throw new Error(`No lockfile entry for ${name}`);
  const packageJson = JSON.parse(await readFile(path.join(frontend, "node_modules", name, "package.json"), "utf8"));
  const packageDirectory = path.join(frontend, "node_modules", name);
  const files = await readdir(packageDirectory);
  let candidates = files.filter(file => /^(?:LICENSE|COPYING|NOTICE)(?:$|[._-])/i.test(file) && !file.endsWith(".map"));
  if (name === "pdfjs-dist") candidates = candidates.filter(file => file === "LICENSE");
  if (name === "dompurify") candidates = candidates.filter(file => file === "LICENSE" || file === "LICENSE-MPL");
  if (!candidates.length) throw new Error(`No license file found for ${name}`);
  notices.push(`## ${name}@${lockEntry.version} — ${packageJson.license || "license listed in package metadata"}`);
  for (const filename of candidates.sort()) {
    notices.push(`### ${filename}`);
    notices.push(licenseText(await readFile(path.join(packageDirectory, filename), "utf8")));
  }
  notices.push("");
}
const pdfExtras = [
  ["cmaps", "LICENSE"], ["iccs", "LICENSE"], ["standard_fonts", "LICENSE_FOXIT"],
  ["standard_fonts", "LICENSE_LIBERATION"], ["wasm", "LICENSE_PDFJS_JBIG2"],
  ["wasm", "LICENSE_PDFJS_OPENJPEG"], ["wasm", "LICENSE_PDFJS_QCMS"],
  ["wasm", "LICENSE_JBIG2"], ["wasm", "LICENSE_OPENJPEG"], ["wasm", "LICENSE_QCMS"],
];
for (const [directory, filename] of pdfExtras) {
  const file = path.join(pdfRoot, directory, filename);
  try {
    notices.push(`## pdfjs-dist/${directory}/${filename}`);
    notices.push(licenseText(await readFile(file, "utf8")));
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
}
await writeFile(path.join(staticRoot, "THIRD_PARTY_NOTICES.txt"), `${notices.join("\n\n")}\n`);
const generatedPaths = ["app.bundle.js", "THIRD_PARTY_NOTICES.txt"];
async function collectFiles(directory, prefix = "") {
  const collected = [];
  for (const entry of (await readdir(directory, {withFileTypes: true})).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) collected.push(...await collectFiles(path.join(directory, entry.name), relative));
    else if (entry.isFile()) collected.push(relative);
  }
  return collected;
}
generatedPaths.push(...(await collectFiles(assets)).map(name => `assets/${name}`));
const manifest = {};
for (const relative of generatedPaths.sort()) {
  const bytes = await readFile(path.join(staticRoot, relative));
  manifest[relative] = createHash("sha256").update(bytes).digest("hex");
}
await writeFile(path.join(staticRoot, "frontend-bundle-manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`Frontend bundle generated: ${Object.keys(result.metafile.outputs).length} outputs; licenses: ${packageNames.size} packages.`);
