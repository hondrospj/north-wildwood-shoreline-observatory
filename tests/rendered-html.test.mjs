import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
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

test("server-renders the complete shoreline observatory", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>North Wildwood Shoreline Observatory<\/title>/i);
  assert.match(html, /A decade of shoreline movement/);
  assert.match(html, /Interactive shoreline explorer/);
  assert.match(html, /−111\.8/);
  assert.match(html, /Wave caveat/);
  assert.match(html, /regional climatology/);
  assert.match(html, /og:image[^>]+https:\/\/shoreline-observatory\.example\/og\.png/i);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships the real data bundle and removes the starter preview", async () => {
  const [metadataRaw, trendRaw, geojsonRaw] = await Promise.all([
    readFile(new URL("../public/data/metadata.json", import.meta.url), "utf8"),
    readFile(new URL("../public/data/trend.json", import.meta.url), "utf8"),
    readFile(new URL("../public/data/shorelines.json", import.meta.url), "utf8"),
  ]);
  const metadata = JSON.parse(metadataRaw);
  const trend = JSON.parse(trendRaw);
  const geojson = JSON.parse(geojsonRaw);

  assert.equal(metadata.scenes.length, 6);
  assert.equal(metadata.scenes.at(-1).wave.source, "regional climatology fallback");
  assert.equal(trend.net_median_change_m, -111.8);
  assert.equal(trend.retreat_share_pct, 100);
  assert.equal(geojson.features.filter((feature) => feature.properties.geometry_kind === "corrected").length, 6);

  await Promise.all([
    access(new URL("../public/data/sentinel-2016.jpg", import.meta.url)),
    access(new URL("../public/data/sentinel-2026.jpg", import.meta.url)),
    access(new URL("../public/og.png", import.meta.url)),
  ]);
  await assert.rejects(access(new URL("../app/_sites-preview/SkeletonPreview.tsx", import.meta.url)));
  await assert.rejects(access(new URL("../app/_sites-preview/preview.css", import.meta.url)));
  await assert.rejects(access(new URL("public/_sites-preview", projectRoot)));
});

test("builds a GitHub Pages app with project-scoped assets", async () => {
  const html = await readFile(new URL("../dist-github/index.html", import.meta.url), "utf8");
  assert.match(html, /North Wildwood Shoreline Observatory/);
  assert.match(html, /\/north-wildwood-shoreline-observatory\/assets\//);
  assert.match(html, /hondrospj\.github\.io\/north-wildwood-shoreline-observatory\/og\.png/);
  await Promise.all([
    access(new URL("../dist-github/data/sentinel-2016.jpg", import.meta.url)),
    access(new URL("../dist-github/data/sentinel-2026.jpg", import.meta.url)),
    access(new URL("../dist-github/data/shorelines.json", import.meta.url)),
    access(new URL("../dist-github/og.png", import.meta.url)),
  ]);
});
