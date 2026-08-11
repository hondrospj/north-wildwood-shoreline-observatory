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
  assert.match(html, /Twice monthly/);
  assert.match(html, />Monthly<\/button>/);
  assert.match(html, /Low tide/);
  assert.match(html, /Draw transect from this baseline/);
  assert.match(html, /Center shore/);
  assert.match(html, /Export \.xlsx/);
  assert.match(html, /og:image[^>]+https:\/\/shoreline-observatory\.example\/og\.png/i);
  assert.doesNotMatch(html, /A decade of shoreline movement|Beach sectors|How it works|codex-preview/i);
});

test("ships at most two regular images per month and every strict low-tide image", async () => {
  const catalog = JSON.parse(
    await readFile(new URL("../public/data/monthly-catalog.json", import.meta.url), "utf8"),
  );
  assert.deepEqual(catalog.range, ["2015-08", "2026-08"]);
  assert.ok(catalog.clear.length > 150);
  assert.equal(new Set(catalog.clear.map((scene) => scene.id)).size, catalog.clear.length);
  assert.equal(new Set(catalog.clear.map((scene) => scene.image)).size, catalog.clear.length);
  assert.ok(catalog.low_tide.length > 80);
  assert.ok(catalog.monthly.length > 100);
  assert.equal(new Set(catalog.monthly.map((scene) => scene.id)).size, catalog.monthly.length);
  const monthlyCounts = new Map();
  for (const scene of catalog.clear) {
    monthlyCounts.set(scene.month, (monthlyCounts.get(scene.month) ?? 0) + 1);
  }
  assert.ok([...monthlyCounts.values()].every((count) => count <= 2));
  const singleMonthlyCounts = new Map();
  for (const scene of catalog.monthly) {
    singleMonthlyCounts.set(scene.month, (singleMonthlyCounts.get(scene.month) ?? 0) + 1);
  }
  assert.ok([...singleMonthlyCounts.values()].every((count) => count === 1));
  assert.equal(catalog.selection.low_tide_window_minutes, 105);
  assert.equal(catalog.selection.maximum_clear_images_per_month, 2);
  assert.equal(catalog.selection.maximum_monthly_images_per_month, 1);
  assert.ok(
    catalog.low_tide.every(
      (scene) =>
        Math.abs(scene.nearest_low_tide.image_offset_minutes) <=
        catalog.selection.low_tide_window_minutes,
    ),
  );
  assert.ok(
    [...catalog.clear, ...catalog.monthly, ...catalog.low_tide].every((scene) =>
      scene.image.startsWith(
        "https://floodmapperv1.b-cdn.net/NorthWildwoodShoreline/scenes/",
      ),
    ),
  );
  assert.ok(
    [...catalog.clear, ...catalog.monthly, ...catalog.low_tide].every(
      (scene) =>
        scene.study_cloud_pixels === 0 &&
        scene.study_snow_pixels === 0 &&
        scene.study_invalid_pixels / scene.study_pixel_count <= 0.005,
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
  assert.match(source, /aria-label="Fast image scrubber"/);
  assert.match(source, /lastWheelSceneRef/);
  assert.match(source, /changeScene\(delta > 0 \? steps : -steps\)/);
  assert.match(source, /\(event\.metaKey \|\| event\.ctrlKey\)/);
  assert.match(source, /event\.key\.toLowerCase\(\) === "z"/);
  assert.match(source, /Last shoreline point undone/);
  assert.match(source, /const activeBaselineId = baselineId \?\? scene\.id/);
  assert.match(source, /setBaselines\(\(current\) => \(\{ \.\.\.current, \[mode\]: scene\.id \}\)\)/);
  assert.match(source, /Click shoreline for baseline/);
  assert.doesNotMatch(source, /Set this or another image as the baseline first/);
  assert.match(source, /const DEFAULT_ZOOM = 7/);
  assert.match(source, /const SHORE_FOCUS: Coordinate = \[-74\.787, 38\.9945\]/);
  assert.match(source, /Clear study area/);
  assert.match(source, /Drag from land to ocean/);
  assert.match(source, /finishTransect\(drag\.startCoordinate, coordinate\)/);
  assert.match(source, /distanceAlongTransect/);
  assert.match(source, /snapToTransect/);
  assert.match(source, /const sourceAspect = sourceWidth \/ sourceHeight/);
  assert.match(source, /initialFocusDoneRef\.current = true/);
  assert.match(source, /\[sourceHeight, sourceWidth, viewportSize\.height, viewportSize\.width\]/);
  assert.match(source, /className="logged-crosshair"/);
  assert.match(source, /\{drawing && <div className="map-instruction">/);
  assert.doesNotMatch(source, /className=\{active \? "logged-point active"/);
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
