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

test("server-renders the complete shoreline observatory", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>North Wildwood Shoreline Observatory<\/title>/i);
  assert.match(html, /A decade of shoreline movement/);
  assert.match(html, /Interactive shoreline explorer/);
  assert.match(html, /suitable acquisitions/);
  assert.match(html, /Every suitable acquisition/);
  assert.match(html, /±1 hr 30 min/);
  assert.match(html, /No yearly sampling/);
  assert.match(html, /Sample the wet\/dry line/);
  assert.match(html, /Observed wet\/dry line/);
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

  assert.ok(metadata.scenes.length > 50);
  assert.equal(metadata.scenes.length, metadata.suitability.accepted_count);
  assert.equal(metadata.suitability.catalog_item_count, metadata.suitability.catalog_candidate_count + metadata.suitability.duplicate_product_count);
  assert.equal(metadata.suitability.catalog_candidate_count, metadata.suitability.accepted_count + metadata.suitability.rejected_count);
  assert.equal(new Set(metadata.scenes.map((scene) => scene.datetime)).size, metadata.scenes.length);
  assert.equal(trend.observation_count, metadata.scenes.length);
  assert.equal(trend.observations.length, metadata.scenes.length);
  assert.ok(trend.net_median_change_m < 0);
  assert.ok(trend.retreat_share_pct > 80);
  assert.equal(geojson.features.filter((feature) => feature.properties.geometry_kind === "corrected").length, metadata.scenes.length);
  assert.ok(geojson.features.every((feature) => feature.properties.shoreline_proxy === "wet/dry line"));
  assert.ok(metadata.scenes.every((scene) => scene.cloud_mask_aoi_pct <= metadata.suitability.aoi_cloud_mask_max_pct));
  assert.ok(metadata.scenes.every((scene) => scene.wet_dry_point_count >= metadata.suitability.minimum_shoreline_points));
  assert.ok(metadata.scenes.every((scene) => scene.wet_dry_median_ndwi_contrast >= metadata.suitability.minimum_wet_dry_ndwi_contrast));
  assert.ok(metadata.scenes.every((scene) => scene.wet_dry_median_dry_side_ndwi < scene.ndwi_threshold));
  assert.ok(metadata.scenes.every((scene) => scene.wet_dry_median_wet_side_ndwi > scene.ndwi_threshold));
  assert.ok(metadata.scenes.every((scene) => scene.geometry_p90_deviation_m <= metadata.suitability.geometry_p90_max_deviation_m));
  assert.ok(metadata.scenes.every((scene) => scene.geometry_max_deviation_m <= metadata.suitability.geometry_max_deviation_m));
  assert.ok(metadata.scenes.every((scene) => Math.abs(scene.high_tide.image_offset_minutes) <= metadata.suitability.high_tide_window_minutes));
  assert.equal((await readdir(new URL("../public/data/scenes/", import.meta.url))).filter((name) => name.endsWith(".jpg")).length, metadata.scenes.length);

  await Promise.all([
    ...metadata.scenes.map((scene) => access(new URL(`../public${scene.image}`, import.meta.url))),
    access(new URL("../public/data/sentinel-baseline.jpg", import.meta.url)),
    access(new URL("../public/data/sentinel-latest.jpg", import.meta.url)),
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
    access(new URL("../dist-github/data/sentinel-baseline.jpg", import.meta.url)),
    access(new URL("../dist-github/data/sentinel-latest.jpg", import.meta.url)),
    access(new URL("../dist-github/data/shorelines.json", import.meta.url)),
    access(new URL("../dist-github/og.png", import.meta.url)),
  ]);
});
