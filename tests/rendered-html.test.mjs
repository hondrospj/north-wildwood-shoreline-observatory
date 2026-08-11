import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import test from "node:test";

const projectRoot = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("https://shoreline-observatory.example/", {
      headers: { accept: "text/html", host: "shoreline-observatory.example" },
    }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the minimal shoreline logger", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>North Wildwood Shoreline Observatory<\/title>/i);
  assert.match(html, /shoreline logger/i);
  assert.match(html, /Monthly/);
  assert.match(html, /Low tide/);
  assert.match(html, /Set baseline/);
  assert.match(html, /Draw transect/);
  assert.match(html, /Click shoreline/);
  assert.match(html, /Export \.xlsx/);
  assert.match(html, /og:image[^>]+https:\/\/shoreline-observatory\.example\/og\.png/i);
  assert.doesNotMatch(html, /A decade of shoreline movement|Beach sectors|How it works|codex-preview/i);
});

test("ships one best image per month and a strict low-tide catalog", async () => {
  const catalog = JSON.parse(
    await readFile(new URL("../public/data/monthly-catalog.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(catalog.range, ["2015-08", "2026-08"]);
  assert.equal(catalog.monthly.length, 133);
  assert.equal(new Set(catalog.monthly.map((scene) => scene.month)).size, catalog.monthly.length);
  assert.equal(new Set(catalog.monthly.map((scene) => scene.image)).size, catalog.monthly.length);
  assert.ok(catalog.low_tide.length > 80);
  assert.equal(new Set(catalog.low_tide.map((scene) => scene.month)).size, catalog.low_tide.length);
  assert.ok(
    catalog.low_tide.every(
      (scene) =>
        Math.abs(scene.nearest_low_tide.image_offset_minutes) <=
        catalog.selection.low_tide_window_minutes,
    ),
  );
  assert.ok(
    [...catalog.monthly, ...catalog.low_tide].every((scene) =>
      scene.image.startsWith(
        "https://floodmapperv1.b-cdn.net/NorthWildwoodShoreline/scenes/",
      ),
    ),
  );
  assert.deepEqual(await readdir(new URL("../public/data/", import.meta.url)), [
    "monthly-catalog.json",
  ]);
});

test("includes keyboard logging and a true Excel export", async () => {
  const [source, packageRaw] = await Promise.all([
    readFile(new URL("../app/ShorelineApp.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  const packageJson = JSON.parse(packageRaw);
  assert.match(source, /event\.key === "ArrowRight"/);
  assert.match(source, /event\.key === "ArrowLeft"/);
  assert.match(source, /distanceAlongTransect/);
  assert.match(source, /north-wildwood-shoreline-log\.xlsx/);
  assert.equal(packageJson.dependencies.xlsx, "^0.18.5");
});

test("builds the GitHub Pages version with project-scoped assets", async () => {
  const html = await readFile(new URL("../dist-github/index.html", import.meta.url), "utf8");
  assert.match(html, /North Wildwood Shoreline Observatory/);
  assert.match(html, /\/north-wildwood-shoreline-observatory\/assets\//);
  assert.match(html, /hondrospj\.github\.io\/north-wildwood-shoreline-observatory\/og\.png/);
  await Promise.all([
    access(new URL("../dist-github/data/monthly-catalog.json", import.meta.url)),
    access(new URL("../dist-github/og.png", import.meta.url)),
  ]);
  await assert.rejects(access(new URL("public/data/scenes", projectRoot)));
});
