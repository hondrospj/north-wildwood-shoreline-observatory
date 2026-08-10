"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Image from "next/image";

type Scene = {
  year: number;
  scene_id: string;
  datetime: string;
  cloud_mask_aoi_pct: number;
  ndwi_threshold: number;
  tide: { level_m_msl: number; time: string; source: string };
  wave: { height_m: number; dominant_period_s: number; time: string; source: string };
  wave_setup_m: number;
  horizontal_correction_m: number;
  uncertainty_m: number;
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
  scenes: Scene[];
};

type Trend = {
  baseline_year: number;
  latest_year: number;
  net_median_change_m: number;
  retreat_share_pct: number;
  max_retreat_m: number;
  max_advance_m: number;
  latitudes: number[];
  change_m: number[];
  zones: Array<{ name: string; median_change_m: number; min_change_m: number; max_change_m: number }>;
  yearly: Array<{ year: number; median_change_m: number; p10_change_m: number; p90_change_m: number }>;
};

type Feature = {
  properties: { year: number; geometry_kind: string };
  geometry: { type: string; coordinates: number[][] };
};

type GeoJSON = { type: string; features: Feature[] };

const YEAR_COLORS: Record<number, string> = {
  2016: "#f6e8c9",
  2018: "#f4b95f",
  2020: "#e57e68",
  2022: "#d84e63",
  2024: "#a75cd8",
  2026: "#42e2d2",
};

function signed(value: number, digits = 1) {
  if (value === 0) return "0.0";
  return `${value > 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

function cleanDate(date: string) {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(date));
}

function CoastCanvas({ metadata, shorelines, selectedYear, showAll }: { metadata: Metadata; shorelines: GeoJSON; selectedYear: number; showAll: boolean }) {
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
      const lines = showAll ? corrected : corrected.filter((feature) => feature.properties.year === selectedYear);

      for (const feature of lines) {
        const year = feature.properties.year;
        const selected = year === selectedYear;
        ctx.beginPath();
        feature.geometry.coordinates.forEach(([lon, lat], index) => {
          const x = ((lon - minLon) / (maxLon - minLon)) * rect.width;
          const y = ((maxLat - lat) / (maxLat - minLat)) * rect.height;
          if (index === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = "rgba(5, 18, 30, .8)";
        ctx.lineWidth = selected ? 6 : 4;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.stroke();
        ctx.strokeStyle = YEAR_COLORS[year];
        ctx.lineWidth = selected ? 3.5 : 2;
        ctx.stroke();
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [metadata, shorelines, selectedYear, showAll]);

  return <canvas ref={ref} className="coast-canvas" aria-label="Corrected shoreline positions over satellite imagery" />;
}

function TrendChart({ trend }: { trend: Trend }) {
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
      const min = -180;
      const max = 20;
      const y = (value: number) => pad.top + ((max - value) / (max - min)) * h;
      const x = (index: number) => pad.left + (index / (trend.yearly.length - 1)) * w;

      ctx.font = "11px var(--font-geist-mono)";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const tick of [0, -50, -100, -150]) {
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
      trend.yearly.forEach((item, index) => {
        if (index === 0) ctx.moveTo(x(index), y(item.median_change_m));
        else ctx.lineTo(x(index), y(item.median_change_m));
      });
      ctx.lineTo(x(trend.yearly.length - 1), y(min));
      ctx.lineTo(x(0), y(min));
      ctx.closePath();
      const gradient = ctx.createLinearGradient(0, pad.top, 0, rect.height - pad.bottom);
      gradient.addColorStop(0, "rgba(66,226,210,.38)");
      gradient.addColorStop(1, "rgba(66,226,210,0)");
      ctx.fillStyle = gradient;
      ctx.fill();

      ctx.beginPath();
      trend.yearly.forEach((item, index) => {
        if (index === 0) ctx.moveTo(x(index), y(item.median_change_m));
        else ctx.lineTo(x(index), y(item.median_change_m));
      });
      ctx.strokeStyle = "#42e2d2";
      ctx.lineWidth = 3;
      ctx.lineJoin = "round";
      ctx.stroke();

      trend.yearly.forEach((item, index) => {
        ctx.fillStyle = "#0b1d25";
        ctx.beginPath();
        ctx.arc(x(index), y(item.median_change_m), 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#42e2d2";
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = "#b9c9ce";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(String(item.year), x(index), rect.height - pad.bottom + 10);
      });
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [trend]);

  return <canvas ref={ref} className="trend-canvas" aria-label="Median shoreline change by observation year" />;
}

export function ShorelineApp({ metadata, trend, shorelines }: { metadata: Metadata; trend: Trend; shorelines: GeoJSON }) {
  const years = metadata.scenes.map((scene) => scene.year);
  const [selectedYear, setSelectedYear] = useState(trend.latest_year);
  const [compare, setCompare] = useState(52);
  const [showAll, setShowAll] = useState(true);
  const [view, setView] = useState<"lines" | "compare">("lines");
  const scene = useMemo(() => metadata.scenes.find((item) => item.year === selectedYear) ?? metadata.scenes.at(-1)!, [metadata.scenes, selectedYear]);

  return (
    <main>
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
        <span className="data-badge"><i /> Updated Aug 10, 2026</span>
      </header>

      <section id="top" className="hero">
        <div className="hero-copy">
          <p className="eyebrow">Cape May County · New Jersey</p>
          <h1>A decade of shoreline movement, <em>normalized to the same sea state.</em></h1>
          <p className="lede">Sentinel-2 L2A imagery reveals where North Wildwood&apos;s oceanfront has moved after removing the visual influence of tide and wave setup.</p>
        </div>
        <div className="hero-stat">
          <span className="stat-kicker">2016 → 2026</span>
          <strong>{Math.abs(trend.net_median_change_m).toFixed(1)}<small>m</small></strong>
          <span>median landward movement</span>
          <div className="stat-scale"><i /><b>≈ one city block</b></div>
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
              <Image className="base-image" src="/data/sentinel-2026.jpg" alt="Sentinel-2 view of North Wildwood on August 7, 2026" fill sizes="(max-width: 1050px) 100vw, 65vw" style={{ objectFit: "fill" }} priority />
              {view === "compare" && (
                <>
                  <div className="compare-image" style={{ clipPath: `inset(0 ${100 - compare}% 0 0)` }}>
                    <Image src="/data/sentinel-2016.jpg" alt="Sentinel-2 view of North Wildwood on July 20, 2016" fill sizes="(max-width: 1050px) 100vw, 65vw" style={{ objectFit: "fill" }} />
                  </div>
                  <div className="compare-handle" style={{ left: `${compare}%` }}><span>↔</span></div>
                  <input className="compare-range" type="range" min="5" max="95" value={compare} onChange={(event) => setCompare(Number(event.target.value))} aria-label="Compare 2016 and 2026 imagery" />
                  <span className="image-year left">2016</span><span className="image-year right">2026</span>
                </>
              )}
              {view === "lines" && <CoastCanvas metadata={metadata} shorelines={shorelines} selectedYear={selectedYear} showAll={showAll} />}
              <div className="north-arrow"><span>↑</span>N</div>
              <div className="map-attribution">Sentinel-2 L2A · 10 m</div>
            </div>
            <div className="map-controls">
              <div className="timeline" role="group" aria-label="Observation year">
                {years.map((year) => (
                  <button key={year} className={selectedYear === year ? "selected" : ""} style={{ "--year-color": YEAR_COLORS[year] } as React.CSSProperties} onClick={() => setSelectedYear(year)}>
                    <i />{year}
                  </button>
                ))}
              </div>
              <label className="toggle"><input type="checkbox" checked={showAll} onChange={(event) => setShowAll(event.target.checked)} disabled={view === "compare"} /><span />Show all corrected lines</label>
            </div>
          </div>

          <aside className="scene-card">
            <div className="scene-top">
              <p className="eyebrow">Selected acquisition</p>
              <strong>{cleanDate(scene.datetime)}</strong>
              <a href={scene.stac_url} target="_blank" rel="noreferrer">Open Sentinel scene ↗</a>
            </div>
            <div className="correction-figure">
              <span>Observed waterline</span><i className="raw-line" />
              <div className="correction-arrow"><b>{signed(scene.horizontal_correction_m)} m</b><i>→</i></div>
              <i className="corrected-line" />
              <span>MSL-normalized shoreline</span>
            </div>
            <dl className="scene-metrics">
              <div><dt>Tide at capture</dt><dd>{signed(scene.tide.level_m_msl)} m <small>MSL</small></dd></div>
              <div><dt>Wave height</dt><dd>{scene.wave.height_m.toFixed(2)} m</dd></div>
              <div><dt>Wave period</dt><dd>{scene.wave.dominant_period_s.toFixed(1)} s</dd></div>
              <div><dt>Uncertainty</dt><dd>±{scene.uncertainty_m.toFixed(1)} m</dd></div>
              <div><dt>AOI cloud mask</dt><dd>{scene.cloud_mask_aoi_pct.toFixed(2)}%</dd></div>
              <div><dt>Water threshold</dt><dd>{scene.ndwi_threshold.toFixed(3)}</dd></div>
            </dl>
            {scene.wave.source.includes("fallback") && (
              <div className="caveat"><b>Wave caveat</b><span>The 2026 tide is verified NOAA data; its wave term uses a regional climatology because matching buoy observations were unavailable.</span></div>
            )}
          </aside>
        </div>
      </section>

      <section id="change" className="change-section shell">
        <div className="section-heading">
          <div><p className="eyebrow">Measured change</p><h2>Retreat is visible along the full exposed beach.</h2></div>
          <p className="section-note">Negative values indicate landward movement from the 2016 MSL-normalized baseline.</p>
        </div>
        <div className="metric-row">
          <article><span>Median movement</span><strong>{signed(trend.net_median_change_m)} m</strong><small>2016 to 2026</small></article>
          <article><span>Most landward</span><strong>{signed(trend.max_retreat_m)} m</strong><small>single transect</small></article>
          <article><span>Transects retreating</span><strong>{trend.retreat_share_pct.toFixed(0)}%</strong><small>of exposed oceanfront</small></article>
        </div>
        <div className="analysis-grid">
          <article className="chart-card">
            <div className="card-title"><div><span>Median shoreline position</span><strong>Relative to 2016 baseline</strong></div><div className="legend-key"><i /> Landward</div></div>
            <TrendChart trend={trend} />
            <p className="chart-footnote">Six cloud-screened summer observations; whisker ranges are omitted here for legibility and remain reflected in scene uncertainty.</p>
          </article>
          <article className="zones-card">
            <div className="card-title"><div><span>Beach sectors</span><strong>Median 2016–2026 movement</strong></div></div>
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
          <p>Every acquisition is processed with the same reproducible workflow, then shifted to a mean-sea-level reference so high tide does not masquerade as erosion.</p>
        </div>
        <ol className="method-steps">
          <li><span>01</span><div><strong>Screen the imagery</strong><p>Sentinel-2 L2A surface reflectance at 10 m. Scene Classification removes cloud, shadow, snow, and invalid pixels.</p></div></li>
          <li><span>02</span><div><strong>Find the waterline</strong><p>Green and near-infrared bands form NDWI. An adaptive threshold traces the ocean-facing wet/dry boundary.</p></div></li>
          <li><span>03</span><div><strong>Normalize the sea state</strong><p>NOAA tide and offshore wave height/period estimate water-level displacement and Stockdon wave setup.</p></div></li>
          <li><span>04</span><div><strong>Compare alongshore</strong><p>Each line shifts along its local seaward normal using a 0.045 beach slope, then is sampled on consistent transects.</p></div></li>
        </ol>
        <div className="quality-panel">
          <div>
            <span className="quality-icon">!</span>
            <div><strong>Interpret as screening-level evidence.</strong><p>Sentinel&apos;s 10 m pixels, wet-sand ambiguity, georegistration, slope assumptions, and wave estimates create scene uncertainty of roughly ±14–19 m. This is not a survey-grade property boundary.</p></div>
          </div>
          <div className="quality-status"><i /> Tide records verified for all six scenes</div>
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
