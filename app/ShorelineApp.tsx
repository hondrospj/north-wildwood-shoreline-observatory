"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type Mode = "clear" | "low_tide";
type Coordinate = [number, number];
type NormalizedPoint = { x: number; y: number };

type Scene = {
  id: string;
  month: string;
  datetime: string;
  image: string;
  image_shape: number[];
  catalog_cloud_pct: number;
  estimated_aoi_cloud_pct: number;
  quality_source: string;
  nearest_low_tide: {
    time: string;
    level_m_msl: number;
    image_offset_minutes: number;
  };
  stac_url: string;
};

type Catalog = {
  generated: string;
  bounds: number[];
  resolution_m: number;
  range: string[];
  selection: { low_tide_window_minutes: number; maximum_images_per_month: number };
  clear: Scene[];
  low_tide: Scene[];
};

type Transect = { start: Coordinate; end: Coordinate };

type Observation = {
  mode: Mode;
  sceneId: string;
  date: string;
  month: string;
  latitude: number;
  longitude: number;
  distanceAlongTransectM: number;
  shorelineWidthM: number;
};

type SavedWork = {
  transect: Transect | null;
  baselines: Partial<Record<Mode, string>>;
  observations: Observation[];
};

const STORAGE_KEY = "north-wildwood-shoreline-logger-v2";
const DEFAULT_ZOOM = 7;
const SHORE_FOCUS: Coordinate = [-74.787, 38.9945];

function cleanDate(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(value));
}

function tideOffset(minutes: number) {
  if (Math.abs(minutes) < 0.5) return "at low tide";
  return `${Math.abs(minutes).toFixed(0)} min ${minutes < 0 ? "before" : "after"} low`;
}

function toMeters(point: Coordinate, origin: Coordinate): [number, number] {
  const meanLat = ((point[1] + origin[1]) / 2) * (Math.PI / 180);
  return [
    (point[0] - origin[0]) * 111_320 * Math.cos(meanLat),
    (point[1] - origin[1]) * 110_540,
  ];
}

function distanceMeters(a: Coordinate, b: Coordinate) {
  const [x, y] = toMeters(b, a);
  return Math.hypot(x, y);
}

function distanceAlongTransect(point: Coordinate, transect: Transect) {
  const end = toMeters(transect.end, transect.start);
  const target = toMeters(point, transect.start);
  const lengthSquared = end[0] ** 2 + end[1] ** 2;
  if (!lengthSquared) return 0;
  const portion = Math.max(
    0,
    Math.min(1, (target[0] * end[0] + target[1] * end[1]) / lengthSquared),
  );
  return portion * Math.sqrt(lengthSquared);
}

function snapToTransect(point: Coordinate, transect: Transect): Coordinate {
  const end = toMeters(transect.end, transect.start);
  const target = toMeters(point, transect.start);
  const lengthSquared = end[0] ** 2 + end[1] ** 2;
  if (!lengthSquared) return transect.start;
  const portion = Math.max(
    0,
    Math.min(1, (target[0] * end[0] + target[1] * end[1]) / lengthSquared),
  );
  return [
    transect.start[0] + (transect.end[0] - transect.start[0]) * portion,
    transect.start[1] + (transect.end[1] - transect.start[1]) * portion,
  ];
}

function coordinateToPoint(coordinate: Coordinate, bounds: number[]): NormalizedPoint {
  const [minLon, minLat, maxLon, maxLat] = bounds;
  return {
    x: (coordinate[0] - minLon) / (maxLon - minLon),
    y: (maxLat - coordinate[1]) / (maxLat - minLat),
  };
}

function pointToCoordinate(point: NormalizedPoint, bounds: number[]): Coordinate {
  const [minLon, minLat, maxLon, maxLat] = bounds;
  return [
    minLon + point.x * (maxLon - minLon),
    maxLat - point.y * (maxLat - minLat),
  ];
}

function ChangeChart({ observations, baselineWidth }: { observations: Observation[]; baselineWidth: number | null }) {
  if (observations.length < 2 || baselineWidth === null) {
    return <div className="empty-chart">Log two shoreline points to see change.</div>;
  }
  const sorted = [...observations].sort((a, b) => a.date.localeCompare(b.date));
  const values = sorted.map((row) => row.shorelineWidthM - baselineWidth);
  const min = Math.min(...values, 0);
  const max = Math.max(...values, 0);
  const spread = Math.max(max - min, 1);
  const points = values
    .map((value, index) => {
      const x = sorted.length === 1 ? 50 : (index / (sorted.length - 1)) * 100;
      const y = 92 - ((value - min) / spread) * 82;
      return `${x},${y}`;
    })
    .join(" ");
  const zeroY = 92 - ((0 - min) / spread) * 82;
  return (
    <div className="change-chart">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Logged shoreline change">
        <line x1="0" y1={zeroY} x2="100" y2={zeroY} className="zero-line" />
        <polyline points={points} />
      </svg>
      <span>{min.toFixed(1)} m</span>
      <strong>{max.toFixed(1)} m</strong>
    </div>
  );
}

export function ShorelineApp({ catalog }: { catalog: Catalog }) {
  const [mode, setMode] = useState<Mode>("clear");
  const [sceneIndex, setSceneIndex] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [viewportSize, setViewportSize] = useState({ width: 1, height: 1 });
  const [transect, setTransect] = useState<Transect | null>(null);
  const [drawStart, setDrawStart] = useState<Coordinate | null>(null);
  const [gestureStart, setGestureStart] = useState<Coordinate | null>(null);
  const [hoverCoordinate, setHoverCoordinate] = useState<Coordinate | null>(null);
  const [drawing, setDrawing] = useState(false);
  const [baselines, setBaselines] = useState<Partial<Record<Mode, string>>>({});
  const [observations, setObservations] = useState<Observation[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [notice, setNotice] = useState("Choose a date, then start with this baseline.");
  const viewportRef = useRef<HTMLDivElement>(null);
  const initialFocusDoneRef = useRef(false);
  const lastWheelSceneRef = useRef(0);
  const dragRef = useRef<{
    pointerId: number;
    x: number;
    y: number;
    panX: number;
    panY: number;
    moved: boolean;
    startCoordinate: Coordinate | null;
  } | null>(null);

  const scenes = catalog[mode];
  const scene = scenes[sceneIndex] ?? scenes[0];
  const baselineId = baselines[mode];
  const modeObservations = useMemo(
    () => observations.filter((row) => row.mode === mode),
    [mode, observations],
  );
  const baselineObservation = modeObservations.find((row) => row.sceneId === baselineId);
  const baselineWidth = baselineObservation?.shorelineWidthM ?? null;
  const currentObservation = modeObservations.find((row) => row.sceneId === scene?.id);
  const transectLength = transect ? distanceMeters(transect.start, transect.end) : 0;
  const aoiWidthM = distanceMeters(
    [catalog.bounds[0], (catalog.bounds[1] + catalog.bounds[3]) / 2],
    [catalog.bounds[2], (catalog.bounds[1] + catalog.bounds[3]) / 2],
  );
  const sourceWidth = scene?.image_shape?.[0] || 1;
  const sourceHeight = scene?.image_shape?.[1] || 1;
  const imageFrame = useMemo(() => {
    const sourceAspect = sourceWidth / sourceHeight;
    const viewportAspect = viewportSize.width / viewportSize.height;
    if (viewportAspect > sourceAspect) {
      const width = viewportSize.width;
      const height = width / sourceAspect;
      return { width, height, left: 0, top: (viewportSize.height - height) / 2 };
    }
    const height = viewportSize.height;
    const width = height * sourceAspect;
    return { width, height, left: (viewportSize.width - width) / 2, top: 0 };
  }, [sourceHeight, sourceWidth, viewportSize.height, viewportSize.width]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved) {
          const parsed = JSON.parse(saved) as SavedWork;
          setTransect(parsed.transect ?? null);
          setBaselines(parsed.baselines ?? {});
          setObservations(parsed.observations ?? []);
        }
      } catch {
        // A malformed local draft should not block the logger.
      }
      setLoaded(true);
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (!loaded) return;
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ transect, baselines, observations } satisfies SavedWork),
    );
  }, [baselines, loaded, observations, transect]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setViewportSize({ width: rect.width, height: rect.height });
    });
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  const changeScene = useCallback(
    (amount: number) => {
      setSceneIndex((value) => Math.max(0, Math.min(scenes.length - 1, value + amount)));
    },
    [scenes.length],
  );

  const undoLastPoint = useCallback(() => {
    if (drawing && drawStart) {
      setDrawStart(null);
      setGestureStart(null);
      setHoverCoordinate(null);
      setNotice("First transect endpoint undone. Drag a new line or click the landward end.");
      return;
    }
    let lastIndex = -1;
    observations.forEach((row, index) => {
      if (row.mode === mode) lastIndex = index;
    });
    if (lastIndex < 0) {
      setNotice("No shoreline point to undo.");
      return;
    }
    setObservations((current) => current.filter((_, index) => index !== lastIndex));
    setNotice("Last shoreline point undone.");
  }, [drawStart, drawing, mode, observations]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.shiftKey && event.key.toLowerCase() === "z") {
        event.preventDefault();
        undoLastPoint();
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        changeScene(1);
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        changeScene(-1);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [changeScene, undoLastPoint]);

  useEffect(() => {
    for (const neighbor of [scenes[sceneIndex - 1], scenes[sceneIndex + 1]]) {
      if (!neighbor) continue;
      const image = new window.Image();
      image.src = neighbor.image;
    }
  }, [sceneIndex, scenes]);

  const changeMode = (nextMode: Mode) => {
    setMode(nextMode);
    setSceneIndex(0);
    setNotice(nextMode === "low_tide" ? "Low-tide images only. Choose a baseline." : "Two clear images per month. Choose a baseline.");
  };

  const clampPan = useCallback((next: { x: number; y: number }, nextZoom: number) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect || nextZoom <= 1) return { x: 0, y: 0 };
    const minX = rect.width - imageFrame.left - imageFrame.width * nextZoom;
    const maxX = -imageFrame.left;
    const minY = rect.height - imageFrame.top - imageFrame.height * nextZoom;
    const maxY = -imageFrame.top;
    return {
      x: Math.min(maxX, Math.max(minX, next.x)),
      y: Math.min(maxY, Math.max(minY, next.y)),
    };
  }, [imageFrame]);

  const setZoomAt = useCallback((nextZoom: number, clientX?: number, clientY?: number) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return;
    const bounded = Math.max(1, Math.min(50, nextZoom));
    const anchorX = (clientX ?? rect.left + rect.width / 2) - rect.left;
    const anchorY = (clientY ?? rect.top + rect.height / 2) - rect.top;
    const imageX = (anchorX - imageFrame.left - pan.x) / (imageFrame.width * zoom);
    const imageY = (anchorY - imageFrame.top - pan.y) / (imageFrame.height * zoom);
    const nextPan = clampPan(
      {
        x: anchorX - imageFrame.left - imageX * imageFrame.width * bounded,
        y: anchorY - imageFrame.top - imageY * imageFrame.height * bounded,
      },
      bounded,
    );
    setZoom(bounded);
    setPan(nextPan);
  }, [clampPan, imageFrame, pan.x, pan.y, zoom]);

  const focusShoreline = useCallback((nextZoom = DEFAULT_ZOOM) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return;
    const focus = coordinateToPoint(SHORE_FOCUS, catalog.bounds);
    const bounded = Math.max(1, Math.min(50, nextZoom));
    const nextPan = clampPan(
      {
        x: rect.width / 2 - imageFrame.left - focus.x * imageFrame.width * bounded,
        y: rect.height / 2 - imageFrame.top - focus.y * imageFrame.height * bounded,
      },
      bounded,
    );
    setZoom(bounded);
    setPan(nextPan);
  }, [catalog.bounds, clampPan, imageFrame]);

  useEffect(() => {
    if (initialFocusDoneRef.current || viewportSize.width <= 1 || viewportSize.height <= 1) return;
    const frame = window.requestAnimationFrame(() => {
      if (initialFocusDoneRef.current) return;
      initialFocusDoneRef.current = true;
      focusShoreline();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusShoreline, viewportSize.height, viewportSize.width]);

  const eventPoint = useCallback((clientX: number, clientY: number): NormalizedPoint | null => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const x = (clientX - rect.left - imageFrame.left - pan.x) / (imageFrame.width * zoom);
    const y = (clientY - rect.top - imageFrame.top - pan.y) / (imageFrame.height * zoom);
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return { x, y };
  }, [imageFrame, pan.x, pan.y, zoom]);

  const finishTransect = useCallback((start: Coordinate, end: Coordinate) => {
    if (distanceMeters(start, end) < 20) {
      setNotice("Make the transect at least 20 m long.");
      return;
    }
    setTransect({ start, end });
    setDrawStart(null);
    setGestureStart(null);
    setHoverCoordinate(null);
    setDrawing(false);
    setObservations([]);
    setNotice("Transect set · Click the baseline wet/dry line. The marker snaps to the line.");
  }, []);

  const logCoordinate = useCallback((coordinate: Coordinate) => {
    if (!scene) return;
    if (drawing) {
      if (!drawStart) {
        setDrawStart(coordinate);
        setNotice("Click the oceanward end, or drag there from the first point.");
        return;
      }
      finishTransect(drawStart, coordinate);
      return;
    }
    if (!transect) {
      setNotice("Draw a transect first.");
      return;
    }
    if (!baselineId) {
      setNotice("Set this or another image as the baseline first.");
      return;
    }
    const snappedCoordinate = snapToTransect(coordinate, transect);
    const distance = distanceAlongTransect(snappedCoordinate, transect);
    const row: Observation = {
      mode,
      sceneId: scene.id,
      date: scene.datetime,
      month: scene.month,
      latitude: Number(snappedCoordinate[1].toFixed(7)),
      longitude: Number(snappedCoordinate[0].toFixed(7)),
      distanceAlongTransectM: Number(distance.toFixed(2)),
      shorelineWidthM: Number(distance.toFixed(2)),
    };
    setObservations((current) => [
      ...current.filter((item) => !(item.mode === mode && item.sceneId === scene.id)),
      row,
    ]);
    setNotice(scene.id === baselineId ? "Baseline saved. Press → for the next image · ⌘Z undoes." : "Saved. Press → for the next image · ⌘Z undoes.");
  }, [baselineId, drawStart, drawing, finishTransect, mode, scene, transect]);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    const point = drawing ? eventPoint(event.clientX, event.clientY) : null;
    const startCoordinate = point ? pointToCoordinate(point, catalog.bounds) : null;
    if (drawing) setGestureStart(startCoordinate);
    dragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      panX: pan.x,
      panY: pan.y,
      moved: false,
      startCoordinate,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (drawing) {
      const point = eventPoint(event.clientX, event.clientY);
      setHoverCoordinate(point ? pointToCoordinate(point, catalog.bounds) : null);
      if (drag && drag.pointerId === event.pointerId) {
        const dx = event.clientX - drag.x;
        const dy = event.clientY - drag.y;
        if (Math.hypot(dx, dy) > 4) drag.moved = true;
      }
      return;
    }
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.hypot(dx, dy) > 4) drag.moved = true;
    if (drag.moved && zoom > 1) {
      setPan(clampPan({ x: drag.panX + dx, y: drag.panY + dy }, zoom));
    }
  };

  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    dragRef.current = null;
    setGestureStart(null);
    const point = eventPoint(event.clientX, event.clientY);
    if (!drag || !point) return;
    const coordinate = pointToCoordinate(point, catalog.bounds);
    if (drawing && drag.moved && drag.startCoordinate) {
      finishTransect(drag.startCoordinate, coordinate);
      return;
    }
    if (!drag.moved) logCoordinate(coordinate);
  };

  const beginStudy = () => {
    if (!scene) return;
    setBaselines({ [mode]: scene.id });
    setObservations([]);
    setDrawing(true);
    setDrawStart(null);
    setGestureStart(null);
    setHoverCoordinate(null);
    setTransect(null);
    setNotice("Draw the transect · Drag from land to ocean, or click each end.");
    focusShoreline();
  };

  const redrawTransect = () => {
    if (!scene) return;
    setBaselines({ [mode]: baselineId ?? scene.id });
    setObservations([]);
    setDrawing(true);
    setDrawStart(null);
    setGestureStart(null);
    setHoverCoordinate(null);
    setTransect(null);
    setNotice("Draw the transect · Drag from land to ocean, or click each end.");
    focusShoreline();
  };

  const clearWork = () => {
    setTransect(null);
    setDrawStart(null);
    setGestureStart(null);
    setHoverCoordinate(null);
    setDrawing(false);
    setBaselines({});
    setObservations([]);
    setNotice("Choose a date, then start with this baseline.");
  };

  const exportExcel = async () => {
    if (!transect || !observations.length) {
      setNotice("Log at least one point before exporting.");
      return;
    }
    const XLSX = await import("xlsx");
    const rows = observations
      .slice()
      .sort((a, b) => a.date.localeCompare(b.date))
      .map((row) => {
        const reference = observations.find(
          (item) => item.mode === row.mode && item.sceneId === baselines[row.mode],
        );
        return {
          Mode: row.mode === "low_tide" ? "Low tide" : "Twice monthly",
          Date: row.date.slice(0, 10),
          Month: row.month,
          Latitude: row.latitude,
          Longitude: row.longitude,
          "Distance along transect (m)": row.distanceAlongTransectM,
          "Shoreline width (m)": row.shorelineWidthM,
          "Change from baseline (m)": reference
            ? Number((row.shorelineWidthM - reference.shorelineWidthM).toFixed(2))
            : null,
          "Baseline date": reference?.date.slice(0, 10) ?? "",
        };
      });
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(rows), "Shoreline log");
    XLSX.utils.book_append_sheet(
      workbook,
      XLSX.utils.json_to_sheet([
        {
          "Transect start latitude": transect.start[1],
          "Transect start longitude": transect.start[0],
          "Transect end latitude": transect.end[1],
          "Transect end longitude": transect.end[0],
          "Transect length (m)": Number(transectLength.toFixed(2)),
        },
      ]),
      "Transect",
    );
    XLSX.writeFile(workbook, "north-wildwood-shoreline-log.xlsx");
    setNotice("Excel file exported.");
  };

  if (!scene) return <main className="logger-error">No imagery is available.</main>;

  const transectStartPoint = transect ? coordinateToPoint(transect.start, catalog.bounds) : null;
  const transectEndPoint = transect ? coordinateToPoint(transect.end, catalog.bounds) : null;
  const pendingStartPoint = drawStart ? coordinateToPoint(drawStart, catalog.bounds) : null;
  const gestureStartPoint = drawing && gestureStart
    ? coordinateToPoint(gestureStart, catalog.bounds)
    : null;
  const previewStartPoint = pendingStartPoint ?? gestureStartPoint;
  const hoverPoint = hoverCoordinate ? coordinateToPoint(hoverCoordinate, catalog.bounds) : null;
  const currentLoggedPoint = currentObservation
    ? coordinateToPoint([currentObservation.longitude, currentObservation.latitude], catalog.bounds)
    : null;
  const markerHalfX = (6 * 1000) / (Math.max(imageFrame.width, 1) * zoom);
  const markerHalfY = (6 * 1000) / (Math.max(imageFrame.height, 1) * zoom);

  return (
    <main className="logger-app">
      <header className="logger-header">
        <div className="logger-title"><strong>North Wildwood</strong><span>shoreline logger</span></div>
        <div className="mode-switch" aria-label="Imagery set">
          <button className={mode === "clear" ? "active" : ""} onClick={() => changeMode("clear")}>Twice monthly</button>
          <button className={mode === "low_tide" ? "active" : ""} onClick={() => changeMode("low_tide")}>Low tide</button>
        </div>
        <div className="frame-control">
          <button onClick={() => changeScene(-1)} disabled={sceneIndex === 0} aria-label="Previous image" aria-keyshortcuts="ArrowLeft">←</button>
          <input
            type="date"
            value={scene.datetime.slice(0, 10)}
            min={scenes[0].datetime.slice(0, 10)}
            max={scenes.at(-1)!.datetime.slice(0, 10)}
            onChange={(event) => {
              const target = new Date(`${event.target.value}T12:00:00Z`).getTime();
              const nearest = scenes.reduce((best, item, index) =>
                Math.abs(new Date(item.datetime).getTime() - target) <
                Math.abs(new Date(scenes[best].datetime).getTime() - target) ? index : best, 0);
              setSceneIndex(nearest);
            }}
            aria-label="Current image date"
          />
          <button onClick={() => changeScene(1)} disabled={sceneIndex === scenes.length - 1} aria-label="Next image" aria-keyshortcuts="ArrowRight">→</button>
          <span>{sceneIndex + 1}/{scenes.length}</span>
          <input
            className="frame-scrubber"
            type="range"
            min="0"
            max={Math.max(0, scenes.length - 1)}
            step="1"
            value={sceneIndex}
            onChange={(event) => setSceneIndex(Number(event.target.value))}
            aria-label="Fast image scrubber"
          />
        </div>
        <button className="export-button" onClick={exportExcel} disabled={!observations.length}>Export .xlsx</button>
      </header>

      <div className="logger-workspace">
        <section className="image-panel">
          <div className="work-tools">
            <button className="primary-action" onClick={beginStudy}>{transect || baselineId ? "Restart from this baseline" : "Draw transect from this baseline"}</button>
            {transect && <button className="small-button" onClick={redrawTransect}>Redraw transect</button>}
            <output>{notice}</output>
            <label className="zoom-control">
              <span>{Math.max(100, Math.round(aoiWidthM / zoom))} m view</span>
              <button type="button" onClick={() => setZoomAt(zoom / 1.4)} aria-label="Zoom out">−</button>
              <input type="range" min="1" max="50" step="0.25" value={zoom} onChange={(event) => setZoomAt(Number(event.target.value))} aria-label="Map zoom" />
              <button type="button" onClick={() => setZoomAt(zoom * 1.4)} aria-label="Zoom in">+</button>
            </label>
            <button className="small-button" onClick={() => focusShoreline()}>Center shore</button>
          </div>

          <div
            ref={viewportRef}
            className={`image-viewport ${drawing ? "drawing" : ""}`}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={() => { dragRef.current = null; setGestureStart(null); }}
            onPointerLeave={() => setHoverCoordinate(null)}
            title="Scroll to change images · Command/Ctrl-scroll to zoom"
            onWheel={(event) => {
              event.preventDefault();
              if (event.metaKey || event.ctrlKey) {
                setZoomAt(zoom * (event.deltaY < 0 ? 1.18 : 0.85), event.clientX, event.clientY);
                return;
              }
              const now = performance.now();
              if (now - lastWheelSceneRef.current < 55) return;
              lastWheelSceneRef.current = now;
              const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
              if (!delta) return;
              const steps = event.shiftKey
                ? 6
                : Math.max(1, Math.min(6, Math.round(Math.abs(delta) / 80)));
              changeScene(delta > 0 ? steps : -steps);
            }}
          >
            <div
              className="image-transform"
              style={{
                left: imageFrame.left,
                top: imageFrame.top,
                width: imageFrame.width,
                height: imageFrame.height,
                transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
              }}
            >
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={scene.image} alt={`Sentinel-2 North Wildwood on ${cleanDate(scene.datetime)}`} draggable="false" />
              <svg className="measurement-layer" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
                {previewStartPoint && hoverPoint && (
                  <line x1={previewStartPoint.x * 1000} y1={previewStartPoint.y * 1000} x2={hoverPoint.x * 1000} y2={hoverPoint.y * 1000} className="transect-preview" />
                )}
                {previewStartPoint && <circle cx={previewStartPoint.x * 1000} cy={previewStartPoint.y * 1000} r="7" className="transect-end pending" />}
                {transectStartPoint && transectEndPoint && (
                  <>
                    <line x1={transectStartPoint.x * 1000} y1={transectStartPoint.y * 1000} x2={transectEndPoint.x * 1000} y2={transectEndPoint.y * 1000} className="transect-line" />
                    <circle cx={transectStartPoint.x * 1000} cy={transectStartPoint.y * 1000} r="5" className="transect-end" />
                    <circle cx={transectEndPoint.x * 1000} cy={transectEndPoint.y * 1000} r="5" className="transect-end" />
                  </>
                )}
                {currentLoggedPoint && (
                  <g className="logged-crosshair">
                    <line x1={currentLoggedPoint.x * 1000 - markerHalfX} y1={currentLoggedPoint.y * 1000} x2={currentLoggedPoint.x * 1000 + markerHalfX} y2={currentLoggedPoint.y * 1000} />
                    <line x1={currentLoggedPoint.x * 1000} y1={currentLoggedPoint.y * 1000 - markerHalfY} x2={currentLoggedPoint.x * 1000} y2={currentLoggedPoint.y * 1000 + markerHalfY} />
                  </g>
                )}
              </svg>
            </div>
            {drawing && <div className="map-instruction">{notice}</div>}
            <div className="image-label"><strong>{cleanDate(scene.datetime)}</strong><span>Clear study area</span></div>
            <div className="scale-label">Sentinel-2 · 10 m</div>
          </div>
        </section>

        <aside className="log-panel">
          <div className="log-summary">
            <div><span>Baseline</span><strong>{baselineId ? cleanDate(scenes.find((item) => item.id === baselineId)?.datetime ?? scene.datetime) : "—"}</strong></div>
            <div><span>Transect</span><strong>{transect ? `${transectLength.toFixed(0)} m` : "—"}</strong></div>
            <div><span>Logged</span><strong>{modeObservations.length}</strong></div>
          </div>

          <ChangeChart observations={modeObservations} baselineWidth={baselineWidth} />

          <div className="current-reading">
            <span>{currentObservation ? "Logged point" : "Current image"}</span>
            <strong>{currentObservation ? `${currentObservation.shorelineWidthM.toFixed(2)} m` : cleanDate(scene.datetime)}</strong>
            {currentObservation && baselineWidth !== null && <small>{(currentObservation.shorelineWidthM - baselineWidth >= 0 ? "+" : "")}{(currentObservation.shorelineWidthM - baselineWidth).toFixed(2)} m from baseline</small>}
            {mode === "low_tide" && <small>{tideOffset(scene.nearest_low_tide.image_offset_minutes)}</small>}
          </div>

          <div className="log-table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Width</th><th>Δ</th></tr></thead>
              <tbody>
                {[...modeObservations].sort((a, b) => b.date.localeCompare(a.date)).map((row) => (
                  <tr key={row.sceneId} className={row.sceneId === scene.id ? "active" : ""} onClick={() => {
                    const index = scenes.findIndex((item) => item.id === row.sceneId);
                    if (index >= 0) setSceneIndex(index);
                  }}>
                    <td>{row.date.slice(0, 10)}</td>
                    <td>{row.shorelineWidthM.toFixed(1)} m</td>
                    <td>{baselineWidth === null ? "—" : `${row.shorelineWidthM - baselineWidth >= 0 ? "+" : ""}${(row.shorelineWidthM - baselineWidth).toFixed(1)}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="log-footer">
            <a href={scene.stac_url} target="_blank" rel="noreferrer">Scene source ↗</a>
            <button onClick={clearWork}>Clear</button>
          </div>
        </aside>
      </div>
    </main>
  );
}
