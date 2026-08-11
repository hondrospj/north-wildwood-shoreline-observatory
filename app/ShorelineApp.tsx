"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type Scene = {
  year: number;
  scene_id: string;
  datetime: string;
  cloud_mask_aoi_pct: number;
  ndwi_threshold: number;
  wet_dry_point_count: number;
  wet_dry_median_ndwi_contrast: number;
  wet_dry_median_dry_side_ndwi: number;
  wet_dry_median_wet_side_ndwi: number;
  tide: { level_m_msl: number; time: string; source: string };
  high_tide: { level_m_msl: number; time: string; source: string; image_offset_minutes: number };
  wave: { height_m: number; dominant_period_s: number; time: string; source: string };
  wave_setup_m: number;
  horizontal_correction_m: number;
  uncertainty_m: number;
  image: string;
  stac_url: string;
};

type Metadata = {
  generated: string;
  aoi: number[];
  reference: string;
  beach_slope: number;
  sentinel_resolution_m: number;
  tide_station: string;
  wave_station: string;
  suitability: {
    catalog_cloud_max_pct: number;
    aoi_cloud_mask_max_pct: number;
    minimum_shoreline_points: number;
    high_tide_window_minutes: number;
    minimum_wet_dry_ndwi_contrast: number;
    catalog_candidate_count: number;
    accepted_count: number;
    rejected_count: number;
    high_tide_rejected_count: number;
  };
  scenes: Scene[];
};

type Trend = {
  baseline_year: number;
  latest_year: number;
  baseline_datetime: string;
  latest_datetime: string;
  observation_count: number;
  net_median_change_m: number;
  retreat_share_pct: number;
  max_retreat_m: number;
  max_advance_m: number;
  latitudes: number[];
  change_m: number[];
  zones: Array<{ name: string; median_change_m: number; min_change_m: number; max_change_m: number }>;
  observations: Array<{ datetime: string; year: number; median_change_m: number; p10_change_m: number; p90_change_m: number }>;
  yearly: Array<{ year: number; median_change_m: number; p10_change_m: number; p90_change_m: number }>;
};

type Feature = {
  properties: { year: number; datetime: string; geometry_kind: string };
  geometry: { type: string; coordinates: number[][] };
};

type GeoJSON = { type: string; features: Feature[] };

function signed(value: number, digits = 1) {
  if (value === 0) return "0.0";
  return `${value > 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

function cleanDate(date: string) {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(date));
}

function cleanTime(date: string) {
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York", timeZoneName: "short" }).format(new Date(date));
}

function highTideOffset(minutes: number) {
  if (Math.abs(minutes) < 0.5) return "At high tide";
  return `${Math.abs(minutes).toFixed(0)} min ${minutes < 0 ? "before" : "after"}`;
}

function assetPath(path: string) {
  return path.startsWith("/") ? `.${path}` : path;
}

function CoastCanvas({ metadata, shorelines, selectedDatetime, showAll }: { metadata: Metadata; shorelines: GeoJSON; selectedDatetime: string; showAll: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const [minLon, minLat, maxLon, maxLat] = metadata.aoi;
      const corrected = shorelines.features.filter((feature) => feature.properties.geometry_kind === "corrected");
      const visible = showAll ? corrected : corrected.filter((feature) => feature.properties.datetime === selectedDatetime);
      const lines = [...visible].sort((a, b) => Number(a.properties.datetime === selectedDatetime) - Number(b.properties.datetime === selectedDatetime));

      for (const feature of lines) {
        const selected = feature.properties.datetime === selectedDatetime;
        ctx.beginPath();
        feature.geometry.coordinates.forEach(([lon, lat], index) => {
          const x = ((lon - minLon) / (maxLon - minLon)) * rect.width;
          const y = ((maxLat - lat) / (maxLat - minLat)) * rect.height;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = selected ? "rgba(5, 18, 30, .9)" : "rgba(5, 18, 30, .2)";
        ctx.lineWidth = selected ? 6 : 2.4;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
        ctx.strokeStyle = selected ? "#42e2d2" : "rgba(246,232,201,.13)";
        ctx.lineWidth = selected ? 3.5 : 1;
        ctx.stroke();
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [metadata, shorelines, selectedDatetime, showAll]);

  return <canvas ref={ref} className="coast-canvas" aria-label="Corrected wet/dry-line positions over satellite imagery" />;
}

function TrendChart({ trend, selectedDatetime }: { trend: Trend; selectedDatetime: string }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const draw = () => {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(rect.width * dpr);
      canvas.height = Math.round(rect.height * dpr);
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, rect.width, rect.height);
      const pad = { top: 20, right: 18, bottom: 34, left: 44 };
      const w = rect.width - pad.left - pad.right;
      const h = rect.height - pad.top - pad.bottom;
      const observations = trend.observations;
      if (observations.length < 2) return;
      const bounds = observations.flatMap((item) => [item.p10_change_m, item.p90_change_m, item.median_change_m, 0]);
      const min = Math.floor((Math.min(...bounds) - 10) / 25) * 25;
      const max = Math.ceil((Math.max(...bounds) + 10) / 25) * 25;
      const y = (value: number) => pad.top + ((max - value) / (max - min)) * h;
      const start = new Date(observations[0].datetime).getTime();
      const end = new Date(observations.at(-1)!.datetime).getTime();
      const x = (item: Trend["observations"][number]) => pad.left + ((new Date(item.datetime).getTime() - start) / (end - start)) * w;

      ctx.font = "11px var(--font-geist-mono)";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      const tickStep = max - min > 250 ? 100 : 50;
      const firstTick = Math.ceil(min / tickStep) * tickStep;
      for (let tick = firstTick; tick <= max; tick += tickStep) {
        ctx.strokeStyle = tick === 0 ? "rgba(66,226,210,.45)" : "rgba(255,255,255,.1)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(pad.left, y(tick));
        ctx.lineTo(rect.width - pad.right, y(tick));
        ctx.stroke();
        ctx.fillStyle = "#8ba3ac";
        ctx.fillText(`${tick} m`, pad.left - 8, y(tick));
      }

      ctx.beginPath();
      observations.forEach((item, index) => {
        if (index === 0) ctx.moveTo(x(item), y(item.p90_change_m));
        else ctx.lineTo(x(item), y(item.p90_change_m));
      });
      [...observations].reverse().forEach((item) => ctx.lineTo(x(item), y(item.p10_change_m)));
      ctx.closePath();
      ctx.fillStyle = "rgba(66,226,210,.12)";
      ctx.fill();

      ctx.beginPath();
      observations.forEach((item, index) => {
        if (index === 0) ctx.moveTo(x(item), y(item.median_change_m));
        else ctx.lineTo(x(item), y(item.median_change_m));
      });
      ctx.strokeStyle = "#42e2d2";
      ctx.lineWidth = 1.8;
      ctx.lineJoin = "round";
      ctx.stroke();

      const selected = observations.find((item) => item.datetime === selectedDatetime);
      if (selected) {
        ctx.strokeStyle = "rgba(232,114,97,.7)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x(selected), pad.top);
        ctx.lineTo(x(selected), rect.height - pad.bottom);
        ctx.stroke();
        ctx.fillStyle = "#e87261";
        ctx.beginPath();
        ctx.arc(x(selected), y(selected.median_change_m), 4.5, 0, Math.PI * 2);
        ctx.fill();
      }

      const firstByYear = observations.filter((item, index) => index === 0 || observations[index - 1].year !== item.year);
      firstByYear.forEach((item) => {
        ctx.fillStyle = "#b9c9ce";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(String(item.year), x(item), rect.height - pad.bottom + 10);
      });
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [trend, selectedDatetime]);

  return <canvas ref={ref} className="trend-canvas" aria-label="Median shoreline change for every suitable acquisition" />;
}

export function ShorelineApp({ metadata, trend, shorelines }: { metadata: Metadata; trend: Trend; shorelines: GeoJSON }) {
  const [selectedIndex, setSelectedIndex] = useState(metadata.scenes.length - 1);
  const [compare, setCompare] = useState(52);
  const [showAll, setShowAll] = useState(false);
  const [view, setView] = useState<"lines" | "compare">("lines");
  const scene = metadata.scenes[selectedIndex] ?? metadata.scenes.at(-1)!;
  const baselineScene = metadata.scenes[0];
  const selectedObservation = useMemo(() => trend.observations.find((item) => item.datetime === scene.datetime), [scene.datetime, trend.observations]);
  const fallbackCount = useMemo(() => metadata.scenes.filter((item) => item.wave.source.includes("fallback")).length, [metadata.scenes]);

  return (
    <main style={{ "--hero-image": "url('./data/sentinel-latest.jpg')" } as React.CSSProperties}>
      <header className="topbar">
        <a className="brand" href="#top" aria-label="North Wildwood Shoreline Observatory home">
          <span className="brand-mark"><i /><i /><i /></span>
          <span><strong>North Wildwood</strong><small>Shoreline Observatory</small></span>
        </a>
        <nav aria-label="Primary navigation">
          <a href="#explore">Explore</a>
          <a href="#change">Change</a>
          <a href="#method">Method</a>
        </nav>
        <span className="data-badge"><i /> Updated {cleanDate(metadata.generated)}</span>
      </header>

      <section id="top" className="hero">
        <div className="hero-copy">
          <p className="eyebrow">Cape May County · New Jersey</p>
          <h1>A decade of shoreline movement, <em>normalized to the same sea state.</em></h1>
          <p className="lede">Every suitable Sentinel-2 L2A acquisition is captured within ±1 hr 30 min of NOAA high tide. Its ocean-facing wet/dry line is then normalized to remove the visual influence of tide and wave setup.</p>
        </div>
        <div className="hero-stat">
          <span className="stat-kicker">{cleanDate(trend.baseline_datetime)} → {cleanDate(trend.latest_datetime)}</span>
          <strong>{Math.abs(trend.net_median_change_m).toFixed(1)}<small>m</small></strong>
          <span>median landward movement</span>
          <div className="stat-scale"><i /><b>{trend.observation_count} suitable acquisitions</b></div>
        </div>
      </section>

      <section id="explore" className="explorer shell">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Interactive shoreline explorer</p>
            <h2>See the coast move.</h2>
          </div>
          <div className="segmented" aria-label="Map view">
            <button className={view === "lines" ? "active" : ""} onClick={() => setView("lines")}>Shorelines</button>
            <button className={view === "compare" ? "active" : ""} onClick={() => setView("compare")}>Swipe imagery</button>
          </div>
        </div>

        <div className="explorer-grid">
          <div className="map-card">
            <div className="map-stage">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img className="base-image" src={assetPath(scene.image)} alt={`Sentinel-2 view of North Wildwood on ${cleanDate(scene.datetime)}`} />
              {view === "compare" && (
                <>
                  <div className="compare-image" style={{ clipPath: `inset(0 ${100 - compare}% 0 0)` }}>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={assetPath(baselineScene.image)} alt={`Sentinel-2 view of North Wildwood on ${cleanDate(baselineScene.datetime)}`} />
                  </div>
                  <div className="compare-handle" style={{ left: `${compare}%` }}><span>↔</span></div>
                  <input className="compare-range" type="range" min="5" max="95" value={compare} onChange={(event) => setCompare(Number(event.target.value))} aria-label={`Compare ${cleanDate(baselineScene.datetime)} and ${cleanDate(scene.datetime)} imagery`} />
                  <span className="image-year left">{cleanDate(baselineScene.datetime)}</span><span className="image-year right">{cleanDate(scene.datetime)}</span>
                </>
              )}
              {view === "lines" && <CoastCanvas metadata={metadata} shorelines={shorelines} selectedDatetime={scene.datetime} showAll={showAll} />}
              <div className="north-arrow"><span>↑</span>N</div>
              <div className="map-attribution">Sentinel-2 L2A · 10 m</div>
            </div>
            <div className="map-controls">
              <div className="timeline" role="group" aria-label="Suitable Sentinel-2 acquisition">
                <div className="acquisition-nav">
                  <button onClick={() => setSelectedIndex((value) => Math.max(0, value - 1))} disabled={selectedIndex === 0} aria-label="Previous acquisition">←</button>
                  <strong>{cleanDate(scene.datetime)}</strong>
                  <button onClick={() => setSelectedIndex((value) => Math.min(metadata.scenes.length - 1, value + 1))} disabled={selectedIndex === metadata.scenes.length - 1} aria-label="Next acquisition">→</button>
                </div>
                <input className="acquisition-range" type="range" min="0" max={metadata.scenes.length - 1} step="1" value={selectedIndex} onChange={(event) => setSelectedIndex(Number(event.target.value))} aria-label={`Acquisition ${selectedIndex + 1} of ${metadata.scenes.length}`} />
                <span className="acquisition-count">{selectedIndex + 1} / {metadata.scenes.length}</span>
              </div>
              <label className="toggle"><input type="checkbox" checked={showAll} onChange={(event) => setShowAll(event.target.checked)} disabled={view === "compare"} /><span />All {metadata.scenes.length} corrected wet/dry lines</label>
            </div>
          </div>

          <aside className="scene-card">
            <div className="scene-top">
              <p className="eyebrow">Selected acquisition</p>
              <strong>{cleanDate(scene.datetime)}</strong>
              <a href={scene.stac_url} target="_blank" rel="noreferrer">Open Sentinel scene ↗</a>
              <span className="tide-window-badge">✓ {highTideOffset(scene.high_tide.image_offset_minutes)} high tide</span>
            </div>
            <div className="correction-figure">
              <span>Observed wet/dry line</span><i className="raw-line" />
              <div className="correction-arrow"><b>{signed(scene.horizontal_correction_m)} m</b><i>→</i></div>
              <i className="corrected-line" />
              <span>MSL-normalized wet/dry line</span>
            </div>
            <dl className="scene-metrics">
              <div><dt>Tide at capture</dt><dd>{signed(scene.tide.level_m_msl)} m <small>MSL</small></dd></div>
              <div><dt>Nearest high tide</dt><dd>{cleanTime(scene.high_tide.time)}</dd></div>
              <div><dt>Capture offset</dt><dd>{highTideOffset(scene.high_tide.image_offset_minutes)}</dd></div>
              <div><dt>Wave height</dt><dd>{scene.wave.height_m.toFixed(2)} m</dd></div>
              <div><dt>Wave period</dt><dd>{scene.wave.dominant_period_s.toFixed(1)} s</dd></div>
              <div><dt>Uncertainty</dt><dd>±{scene.uncertainty_m.toFixed(1)} m</dd></div>
              <div><dt>AOI cloud mask</dt><dd>{scene.cloud_mask_aoi_pct.toFixed(2)}%</dd></div>
              <div><dt>Water threshold</dt><dd>{scene.ndwi_threshold.toFixed(3)}</dd></div>
              <div><dt>Wet/dry samples</dt><dd>{scene.wet_dry_point_count}</dd></div>
              <div><dt>Median wet/dry contrast</dt><dd>{scene.wet_dry_median_ndwi_contrast.toFixed(3)}</dd></div>
            </dl>
            {scene.wave.source.includes("fallback") && (
              <div className="caveat"><b>Wave caveat</b><span>This scene uses regional wave climatology because a matching buoy observation was unavailable. Its tide term still comes from NOAA.</span></div>
            )}
          </aside>
        </div>
      </section>

      <section id="change" className="change-section shell">
        <div className="section-heading">
          <div><p className="eyebrow">Measured change</p><h2>Retreat dominates the exposed beach.</h2></div>
          <p className="section-note">Negative values indicate landward movement from the {cleanDate(trend.baseline_datetime)} MSL-normalized wet/dry-line baseline.</p>
        </div>
        <div className="metric-row">
          <article><span>Median movement</span><strong>{signed(trend.net_median_change_m)} m</strong><small>{trend.baseline_year} to {trend.latest_year}</small></article>
          <article><span>Most landward</span><strong>{signed(trend.max_retreat_m)} m</strong><small>single transect</small></article>
          <article><span>Transects retreating</span><strong>{trend.retreat_share_pct.toFixed(0)}%</strong><small>of exposed oceanfront</small></article>
        </div>
        <div className="analysis-grid">
          <article className="chart-card">
            <div className="card-title"><div><span>Median shoreline position</span><strong>Every suitable acquisition · relative to baseline</strong></div><div className="legend-key"><i /> Median</div></div>
            <TrendChart trend={trend} selectedDatetime={scene.datetime} />
            <p className="chart-footnote">{trend.observation_count} accepted acquisitions; the shaded band shows the 10th–90th percentile range across the oceanfront. Selected scene: {selectedObservation ? `${signed(selectedObservation.median_change_m)} m` : "—"}.</p>
          </article>
          <article className="zones-card">
            <div className="card-title"><div><span>Beach sectors</span><strong>Median {trend.baseline_year}–{trend.latest_year} movement</strong></div></div>
            <div className="zones">
              {trend.zones.map((zone, index) => (
                <div className="zone" key={zone.name}>
                  <div className="zone-label"><span><i>{String(index + 1).padStart(2, "0")}</i>{zone.name}</span><strong>{signed(zone.median_change_m)} m</strong></div>
                  <div className="zone-track"><i style={{ width: `${Math.abs(zone.median_change_m) / 1.6}%` }} /></div>
                  <small>Range {signed(zone.min_change_m)} to {signed(zone.max_change_m)} m</small>
                </div>
              ))}
            </div>
            <div className="zones-insight"><span>Largest median retreat</span><strong>Central beach · {Math.abs(trend.zones[1].median_change_m).toFixed(1)} m</strong></div>
          </article>
        </div>
      </section>

      <section id="method" className="method-section shell">
        <div className="method-intro">
          <p className="eyebrow">How it works</p>
          <h2>From satellite pixels to a comparable shoreline.</h2>
          <p>Every catalog acquisition is tested with the same reproducible workflow. Suitable scenes are shifted to a mean-sea-level reference so high tide does not masquerade as erosion.</p>
        </div>
        <ol className="method-steps">
          <li><span>01</span><div><strong>Screen every acquisition</strong><p>Every retained capture must fall within ±1 hr 30 min of NOAA-predicted high tide. Tile cloud must also be below {metadata.suitability.catalog_cloud_max_pct}% and the local mask below {metadata.suitability.aoi_cloud_mask_max_pct}%. No yearly sampling.</p></div></li>
          <li><span>02</span><div><strong>Sample the wet/dry line</strong><p>Green and near-infrared bands form NDWI. The ocean mask anchors a local search for the strongest dry-to-wet transition. Three clear pixels are sampled on each side; the landward median must be below the adaptive threshold, the seaward median above it, and their NDWI contrast at least {metadata.suitability.minimum_wet_dry_ndwi_contrast.toFixed(2)}. Traces need at least {metadata.suitability.minimum_shoreline_points} validated points.</p></div></li>
          <li><span>03</span><div><strong>Normalize the sea state</strong><p>The exact NOAA water level at capture and offshore wave height/period estimate displacement and Stockdon wave setup.</p></div></li>
          <li><span>04</span><div><strong>Compare alongshore</strong><p>Each wet/dry line shifts along its local seaward normal using a 0.045 beach slope, passes a temporal-coherence screen, then is sampled on consistent transects.</p></div></li>
        </ol>
        <div className="quality-panel">
          <div>
            <span className="quality-icon">!</span>
            <div><strong>Interpret as screening-level evidence.</strong><p>Sentinel&apos;s 10 m pixels, wet-sand ambiguity, georegistration, slope assumptions, and wave estimates create scene uncertainty of roughly ±14–19 m. This is not a survey-grade property boundary.</p></div>
          </div>
          <div className="quality-status"><i /> {metadata.suitability.accepted_count} accepted · {metadata.suitability.rejected_count} rejected · {fallbackCount} wave fallbacks</div>
        </div>
      </section>

      <footer>
        <div className="footer-brand"><span className="brand-mark"><i /><i /><i /></span><div><strong>North Wildwood Shoreline Observatory</strong><span>Open coastal-change evidence</span></div></div>
        <div className="sources"><span>Data & methods</span><a href="https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a" target="_blank" rel="noreferrer">Sentinel-2 L2A ↗</a><a href={`https://tidesandcurrents.noaa.gov/stationhome.html?id=${metadata.tide_station}`} target="_blank" rel="noreferrer">NOAA Cape May tide ↗</a><a href={`https://www.ndbc.noaa.gov/station_page.php?station=${metadata.wave_station}`} target="_blank" rel="noreferrer">NDBC buoy 44009 ↗</a><a href="https://pubs.usgs.gov/publication/70030520" target="_blank" rel="noreferrer">Stockdon method ↗</a></div>
        <p>Processed {cleanDate(metadata.generated)} · Mean Sea Level reference · All distances approximate</p>
      </footer>
    </main>
  );
}
