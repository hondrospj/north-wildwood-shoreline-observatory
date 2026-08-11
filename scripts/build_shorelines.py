#!/usr/bin/env python3
"""Build tide- and wave-normalized North Wildwood shorelines from Sentinel-2 L2A."""

from __future__ import annotations

import gzip
import json
import math
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform as warp_transform, transform_bounds
from rasterio.windows import from_bounds


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DATA = ROOT / "public" / "data"
PUBLIC_DATA.mkdir(parents=True, exist_ok=True)
SCENE_IMAGE_CACHE = ROOT / "work" / "scene-images"
SCENE_IMAGE_CACHE.mkdir(parents=True, exist_ok=True)

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SIGN = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
NOAA_COOPS = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

AOI = (-74.825, 38.98, -74.77, 39.03)
SHORE_LON_RANGE = (-74.807, -74.773)
# Exposed oceanfront from the Wildwood boundary to the start of Hereford Inlet.
# The inlet shoreline is intentionally excluded because its shore-normal geometry
# requires a separate curvilinear transect model.
SHORE_LAT_RANGE = (38.985, 39.006)
TIDE_STATION = "8536110"  # Cape May, NJ
WAVE_STATION = "44009"  # Delaware Bay 26 NM SE of Cape May
BEACH_SLOPE = 0.045
G = 9.80665

COLLECTION_START = "2015-06-23T00:00:00Z"
CATALOG_CLOUD_MAX_PCT = 20.0
AOI_CLOUD_MAX_PCT = 12.5
MIN_SHORELINE_POINTS = 80
HIGH_TIDE_WINDOW_MINUTES = 90.0
WET_DRY_SEARCH_PIXELS = 4
WET_DRY_SIDE_PIXELS = 3
MIN_WET_DRY_NDWI_CONTRAST = 0.04
GEOMETRY_P90_MAX_DEVIATION_M = 200.0
GEOMETRY_MAX_DEVIATION_M = 300.0
MGRS_TILE = "18SWJ"
MAX_WORKERS = 3


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NorthWildwoodShoreline/1.0 contact: shoreline-research"})
NDBC_TEXT_CACHE: dict[str, str | None] = {}
NDBC_CACHE_LOCK = threading.Lock()
SAS_QUERY_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
SAS_CACHE_LOCK = threading.Lock()


class UnsuitableScene(RuntimeError):
    """Raised when an acquisition fails the declared shoreline quality screen."""


def fetch_json(url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    for attempt in range(7):
        response = SESSION.get(url, params=params, timeout=60)
        if response.status_code != 429:
            response.raise_for_status()
            return response.json()
        delay = float(response.headers.get("Retry-After", 2 + attempt * 2))
        print(f"  catalog rate limit; retrying in {delay:.0f}s", flush=True)
        time.sleep(delay)
    response.raise_for_status()
    raise RuntimeError("Unreachable")


def sign_href(href: str) -> str:
    parsed = urlparse(href)
    container = parsed.path.strip("/").split("/", 1)[0]
    cache_key = (parsed.netloc, container)
    with SAS_CACHE_LOCK:
        cached = SAS_QUERY_CACHE.get(cache_key)
        if cached and cached[1] > time.time() + 300:
            return urlunparse(parsed._replace(query=cached[0]))

        signed_href = fetch_json(SIGN, params={"href": href})["href"]
        signed = urlparse(signed_href)
        # Planetary Computer issues a read-only container SAS for Sentinel-2.
        # Reusing its query is equivalent to signing every blob individually
        # and prevents unnecessary catalog throttling.
        if "sr=c" in signed.query:
            SAS_QUERY_CACHE[cache_key] = (signed.query, time.time() + 45 * 60)
        return signed_href


def item(scene_id: str) -> dict[str, Any]:
    return fetch_json(f"{STAC}/collections/sentinel-2-l2a/items/{scene_id}")


def discover_scenes() -> tuple[list[dict[str, Any]], int]:
    end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = fetch_json(
        f"{STAC}/search",
        params={
            "collections": "sentinel-2-l2a",
            "bbox": ",".join(str(value) for value in AOI),
            "datetime": f"{COLLECTION_START}/{end}",
            "limit": 1000,
            "query": json.dumps(
                {
                    "eo:cloud_cover": {"lt": CATALOG_CLOUD_MAX_PCT},
                    "s2:mgrs_tile": {"eq": MGRS_TILE},
                },
                separators=(",", ":"),
            ),
        },
    )
    catalog_items = [
        {
            "year": int(feature["properties"]["datetime"][:4]),
            "id": feature["id"],
            "datetime": feature["properties"]["datetime"],
            "catalog_cloud_pct": round(float(feature["properties"].get("eo:cloud_cover", 0)), 2),
        }
        for feature in payload.get("features", [])
    ]
    # The catalog can contain both the original and a reprocessed product for
    # one physical acquisition. Keep the newest processing timestamp so the UI
    # contains every image capture once, rather than duplicate observations.
    by_datetime: dict[str, dict[str, Any]] = {}
    for scene in catalog_items:
        existing = by_datetime.get(scene["datetime"])
        if existing is None or scene["id"].rsplit("_", 1)[-1] > existing["id"].rsplit("_", 1)[-1]:
            by_datetime[scene["datetime"]] = scene
    scenes = sorted(by_datetime.values(), key=lambda scene: scene["datetime"])
    return scenes, len(catalog_items)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def tide_at(scene_time: datetime) -> dict[str, Any]:
    day = scene_time.strftime("%Y%m%d")
    common = {
        "application": "NorthWildwoodShoreline",
        "begin_date": day,
        "end_date": day,
        "datum": "MSL",
        "station": TIDE_STATION,
        "time_zone": "gmt",
        "units": "metric",
        "format": "json",
    }
    for product, verified in (("water_level", True), ("predictions", False)):
        payload = fetch_json(NOAA_COOPS, params={**common, "product": product, "interval": "6"})
        rows = payload.get("data") or payload.get("predictions") or []
        candidates = []
        for row in rows:
            try:
                dt = datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                candidates.append((abs((dt - scene_time).total_seconds()), dt, float(row["v"])))
            except (KeyError, TypeError, ValueError):
                continue
        if candidates:
            _, dt, value = min(candidates, key=lambda record: record[0])
            return {
                "level_m_msl": round(value, 3),
                "time": dt.isoformat().replace("+00:00", "Z"),
                "source": "verified" if verified else "predicted",
            }
    raise RuntimeError(f"No tide data for {scene_time.isoformat()}")


def high_tides_for_month(year: int, month: int) -> list[dict[str, Any]]:
    """Return NOAA-predicted high tides, with adjacent days for boundary safety."""
    cache_dir = ROOT / "work" / "high-tide-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{TIDE_STATION}-{year:04d}-{month:02d}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    payload = fetch_json(
        NOAA_COOPS,
        params={
            "application": "NorthWildwoodShoreline",
            "begin_date": (month_start - timedelta(days=1)).strftime("%Y%m%d"),
            "end_date": next_month.strftime("%Y%m%d"),
            "datum": "MSL",
            "station": TIDE_STATION,
            "time_zone": "gmt",
            "units": "metric",
            "format": "json",
            "product": "predictions",
            "interval": "hilo",
        },
    )
    events = []
    for row in payload.get("predictions", []):
        if row.get("type") != "H":
            continue
        try:
            event_time = datetime.strptime(row["t"], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
            events.append(
                {
                    "time": event_time.isoformat().replace("+00:00", "Z"),
                    "level_m_msl": round(float(row["v"]), 3),
                    "source": "NOAA predicted high tide",
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not events:
        raise RuntimeError(f"No NOAA high-tide predictions for {year:04d}-{month:02d}")
    cache_path.write_text(json.dumps(events))
    return events


def parse_ndbc_table(text: str, scene_time: datetime) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    header = None
    candidates = []
    for line in lines:
        parts = line.split()
        if line.startswith("#") and "WVHT" in parts:
            header = [value.lstrip("#") for value in parts]
            continue
        if header is None or line.startswith("#"):
            continue
        if len(parts) < len(header):
            continue
        row = dict(zip(header, parts))
        try:
            year_key = "YYYY" if "YYYY" in row else "YY"
            year = int(row[year_key])
            if year < 100:
                year += 2000 if year < 70 else 1900
            minute = int(row.get("mm", "0"))
            dt = datetime(
                year,
                int(row["MM"]),
                int(row["DD"]),
                int(row["hh"]),
                minute,
                tzinfo=timezone.utc,
            )
            hs = float(row["WVHT"])
            period = float(row.get("DPD", row.get("APD", "99")))
            if hs >= 90 or period >= 90:
                continue
            candidates.append((abs((dt - scene_time).total_seconds()), dt, hs, period))
        except (KeyError, TypeError, ValueError):
            continue
    if not candidates:
        return None
    delta, dt, hs, period = min(candidates, key=lambda record: record[0])
    if delta > 18 * 3600:
        return None
    return {
        "height_m": round(hs, 2),
        "dominant_period_s": round(period, 1),
        "time": dt.isoformat().replace("+00:00", "Z"),
    }


def wave_at(scene_time: datetime) -> dict[str, Any]:
    if scene_time.year == datetime.now(timezone.utc).year:
        cache_key = "realtime"
        with NDBC_CACHE_LOCK:
            if cache_key not in NDBC_TEXT_CACHE:
                url = f"https://www.ndbc.noaa.gov/data/realtime2/{WAVE_STATION}.txt"
                response = SESSION.get(url, timeout=60)
                NDBC_TEXT_CACHE[cache_key] = response.text if response.ok else None
            realtime_text = NDBC_TEXT_CACHE[cache_key]
        if realtime_text:
            parsed = parse_ndbc_table(realtime_text, scene_time)
            if parsed:
                return {**parsed, "source": "NDBC realtime"}

    cache_key = f"historical-{scene_time.year}"
    with NDBC_CACHE_LOCK:
        if cache_key not in NDBC_TEXT_CACHE:
            url = f"https://www.ndbc.noaa.gov/data/historical/stdmet/{WAVE_STATION}h{scene_time.year}.txt.gz"
            response = SESSION.get(url, timeout=60)
            if response.ok:
                try:
                    NDBC_TEXT_CACHE[cache_key] = gzip.decompress(response.content).decode(
                        "utf-8", errors="replace"
                    )
                except gzip.BadGzipFile:
                    NDBC_TEXT_CACHE[cache_key] = response.text
            else:
                NDBC_TEXT_CACHE[cache_key] = None
        text = NDBC_TEXT_CACHE[cache_key]
    if text:
        parsed = parse_ndbc_table(text, scene_time)
        if parsed:
            return {**parsed, "source": "NDBC historical"}

    # Conservative fallback used only if the buoy record has a short outage.
    return {
        "height_m": 0.8,
        "dominant_period_s": 7.0,
        "time": scene_time.isoformat().replace("+00:00", "Z"),
        "source": "regional climatology fallback",
    }


def otsu_threshold(values: np.ndarray) -> float:
    values = values[np.isfinite(values)]
    values = values[(values > -0.45) & (values < 0.75)]
    hist, edges = np.histogram(values, bins=240, range=(-0.45, 0.75))
    centers = (edges[:-1] + edges[1:]) / 2
    weight1 = np.cumsum(hist)
    weight2 = np.cumsum(hist[::-1])[::-1]
    mean1 = np.cumsum(hist * centers) / np.maximum(weight1, 1)
    mean2 = (np.cumsum((hist * centers)[::-1]) / np.maximum(weight2[::-1], 1))[::-1]
    score = weight1[:-1] * weight2[1:] * (mean1[:-1] - mean2[1:]) ** 2
    threshold = centers[int(np.argmax(score))]
    return float(np.clip(threshold, -0.02, 0.22))


def smooth(values: np.ndarray, window: int = 9) -> np.ndarray:
    radius = window // 2
    output = values.copy()
    for index in range(len(values)):
        lo = max(0, index - radius)
        hi = min(len(values), index + radius + 1)
        output[index] = np.nanmedian(values[lo:hi])
    return output


def wet_dry_transition(
    ndwi_row: np.ndarray,
    clear_row: np.ndarray,
    water_col: int,
    threshold: float,
) -> tuple[float, float, float, float] | None:
    """Locate and validate the dry-to-wet spectral step beside the ocean edge.

    The ocean mask anchors the search so dark roofs, roads, and back-bay water
    cannot be selected. Within that small search window, the line is placed at
    the strongest seaward NDWI increase whose landward sample is dry and whose
    seaward sample is wet. Returned column coordinates sit between the two
    Sentinel pixels rather than at either pixel center.
    """
    # Side medians suppress single-pixel noise without shifting the boundary.
    values = ndwi_row.astype("float64")
    start = max(WET_DRY_SIDE_PIXELS, water_col - WET_DRY_SEARCH_PIXELS)
    stop = min(
        len(values) - WET_DRY_SIDE_PIXELS - 1,
        water_col + WET_DRY_SEARCH_PIXELS,
    )
    candidates: list[tuple[float, float, float, float, float]] = []
    for dry_col in range(start, stop + 1):
        wet_col = dry_col + 1
        dry_slice = slice(dry_col - WET_DRY_SIDE_PIXELS + 1, dry_col + 1)
        wet_slice = slice(wet_col, wet_col + WET_DRY_SIDE_PIXELS)
        if not np.all(clear_row[dry_slice]) or not np.all(clear_row[wet_slice]):
            continue
        dry_value = float(np.nanmedian(values[dry_slice]))
        wet_value = float(np.nanmedian(values[wet_slice]))
        contrast = wet_value - dry_value
        immediate_step = float(values[wet_col] - values[dry_col])
        if (
            not np.isfinite(contrast)
            or contrast < MIN_WET_DRY_NDWI_CONTRAST
            or dry_value >= threshold
            or wet_value <= threshold
        ):
            continue
        candidates.append(
            (contrast, immediate_step, dry_col + 1.0, dry_value, wet_value)
        )
    if not candidates:
        return None
    contrast, _, column, dry_value, wet_value = max(
        candidates,
        key=lambda value: (
            value[0] + max(value[1], 0.0),
            -abs(value[2] - water_col),
        ),
    )
    return column, contrast, dry_value, wet_value


def crop_window(dataset: rasterio.DatasetReader):
    bounds = transform_bounds("EPSG:4326", dataset.crs, *AOI, densify_pts=21)
    return from_bounds(*bounds, transform=dataset.transform).round_offsets().round_lengths()


def read_scene(scene: dict[str, Any]) -> dict[str, Any]:
    record = item(scene["id"])
    assets = record["assets"]
    signed = {name: sign_href(assets[name]["href"]) for name in ("B03", "B08", "SCL")}

    env_options = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_CACHEMAX": 128,
    }
    with rasterio.Env(**env_options):
        with rasterio.open(signed["B03"]) as green_src:
            window = crop_window(green_src)
            green = green_src.read(1, window=window).astype("float32")
            transform = green_src.window_transform(window)
            crs = green_src.crs
            print(
                f"  source_crs={crs} source_bounds={tuple(round(v, 1) for v in green_src.bounds)} "
                f"window={window} green=({np.nanmin(green):.1f},{np.nanmax(green):.1f})",
                flush=True,
            )

        with rasterio.open(signed["B08"]) as nir_src:
            nir = nir_src.read(1, window=window).astype("float32")

        with rasterio.open(signed["SCL"]) as scl_src:
            scl_window = crop_window(scl_src)
            scl_source = scl_src.read(1, window=scl_window)
            scl = np.zeros(green.shape, dtype="uint8")
            reproject(
                source=scl_source,
                destination=scl,
                src_transform=scl_src.window_transform(scl_window),
                src_crs=scl_src.crs,
                dst_transform=transform,
                dst_crs=crs,
                resampling=Resampling.nearest,
            )

    clear = ~np.isin(scl, [0, 1, 3, 8, 9, 10, 11])
    local_cloud_fraction = float(np.mean(~clear))
    if local_cloud_fraction * 100 > AOI_CLOUD_MAX_PCT:
        raise UnsuitableScene(
            f"AOI cloud/invalid mask {local_cloud_fraction * 100:.2f}% exceeds {AOI_CLOUD_MAX_PCT:.1f}%"
        )

    denominator = green + nir
    ndwi = np.where(denominator > 0, (green - nir) / denominator, np.nan)
    threshold = otsu_threshold(ndwi[clear])
    water = (ndwi > threshold) & clear

    print(
        f"  grid={water.shape} clear={np.mean(clear):.3f} water={np.mean(water):.3f} "
        f"ndwi=({np.nanmin(ndwi):.3f},{np.nanmax(ndwi):.3f}) threshold={threshold:.3f}",
        flush=True,
    )

    rows = np.arange(0, water.shape[0], 2)
    xs, ys = [], []
    wet_dry_contrasts: list[float] = []
    dry_side_values: list[float] = []
    wet_side_values: list[float] = []
    for row in rows:
        center_x, center_y = transform * (0, row + 0.5)
        _, lat_values = warp_transform(crs, "EPSG:4326", [center_x], [center_y])
        lat = lat_values[0]
        if not (SHORE_LAT_RANGE[0] <= lat <= SHORE_LAT_RANGE[1]):
            continue

        signal = water[row].astype("float32")
        valid = clear[row].astype("float32")
        kernel = np.ones(9, dtype="float32")
        smoothed = np.convolve(signal, kernel, mode="same") / np.maximum(
            np.convolve(valid, kernel, mode="same"), 1
        )
        ocean = smoothed >= 0.58
        transitions = np.where((~ocean[:-1]) & ocean[1:])[0] + 1
        if not len(transitions):
            continue
        candidate_x = []
        for col in transitions:
            px, py = transform * (col + 0.5, row + 0.5)
            lon, candidate_lat = warp_transform(crs, "EPSG:4326", [px], [py])
            if SHORE_LON_RANGE[0] <= lon[0] <= SHORE_LON_RANGE[1]:
                transition = wet_dry_transition(ndwi[row], clear[row], int(col), threshold)
                if transition is None:
                    continue
                wet_dry_col, contrast, dry_value, wet_value = transition
                wet_dry_px, wet_dry_py = transform * (wet_dry_col, row + 0.5)
                candidate_x.append(
                    (
                        wet_dry_px,
                        wet_dry_py,
                        lon[0],
                        candidate_lat[0],
                        contrast,
                        dry_value,
                        wet_value,
                    )
                )
        if candidate_x:
            px, py, _, _, contrast, dry_value, wet_value = max(
                candidate_x, key=lambda value: value[0]
            )
            xs.append(px)
            ys.append(py)
            wet_dry_contrasts.append(contrast)
            dry_side_values.append(dry_value)
            wet_side_values.append(wet_value)

    if len(xs) < MIN_SHORELINE_POINTS:
        sample_row = water.shape[0] // 2
        sample_signal = water[sample_row].astype("float32")
        sample_ocean = np.convolve(sample_signal, np.ones(9), mode="same") / 9 >= 0.58
        sample_transitions = np.where((~sample_ocean[:-1]) & sample_ocean[1:])[0] + 1
        transition_lons = []
        for col in sample_transitions:
            px, py = transform * (col + 0.5, sample_row + 0.5)
            lon, lat = warp_transform(crs, "EPSG:4326", [px], [py])
            transition_lons.append((round(lon[0], 5), round(lat[0], 5)))
        print(f"  sample transitions={transition_lons[:20]}", flush=True)
        raise UnsuitableScene(
            f"Only {len(xs)} validated wet/dry-line points extracted; "
            f"minimum is {MIN_SHORELINE_POINTS}"
        )

    xs_array = smooth(np.asarray(xs, dtype="float64"), 11)
    ys_array = np.asarray(ys, dtype="float64")
    raw_lon, raw_lat = warp_transform(crs, "EPSG:4326", xs_array.tolist(), ys_array.tolist())

    scene_time = parse_dt(scene["datetime"])
    tide = tide_at(scene_time)
    wave = wave_at(scene_time)
    wavelength = G * wave["dominant_period_s"] ** 2 / (2 * math.pi)
    setup = 0.35 * BEACH_SLOPE * math.sqrt(max(wave["height_m"], 0.05) * wavelength)
    correction = (tide["level_m_msl"] + setup) / BEACH_SLOPE

    tangent_x = np.gradient(xs_array)
    tangent_y = np.gradient(ys_array)
    norm = np.hypot(tangent_x, tangent_y)
    normal_x = -tangent_y / np.maximum(norm, 1e-6)
    normal_y = tangent_x / np.maximum(norm, 1e-6)
    flip = normal_x < 0
    normal_x[flip] *= -1
    normal_y[flip] *= -1
    corrected_x = xs_array + correction * normal_x
    corrected_y = ys_array + correction * normal_y
    corrected_lon, corrected_lat = warp_transform(
        crs, "EPSG:4326", corrected_x.tolist(), corrected_y.tolist()
    )

    image_name = f"sentinel-{scene_time.strftime('%Y%m%dT%H%M%S')}.jpg"
    image_path = SCENE_IMAGE_CACHE / image_name
    rgb_shape = None
    if not image_path.exists():
        visual_href = sign_href(assets["visual"]["href"])
        with rasterio.Env(**env_options), rasterio.open(visual_href) as visual_src:
            visual_window = crop_window(visual_src)
            rgb = visual_src.read([1, 2, 3], window=visual_window)
        image = np.moveaxis(rgb, 0, -1)
        if image.dtype != np.uint8:
            image = np.clip(image, 0, 255).astype("uint8")
        Image.fromarray(image).save(image_path, quality=82, optimize=True)
        rgb_shape = [int(rgb.shape[2]), int(rgb.shape[1])]
    else:
        with Image.open(image_path) as existing_image:
            rgb_shape = [int(existing_image.width), int(existing_image.height)]

    return {
        "year": scene["year"],
        "scene_id": scene["id"],
        "datetime": scene["datetime"],
        "cloud_cover_tile_pct": round(float(record["properties"].get("eo:cloud_cover", 0)), 2),
        "cloud_mask_aoi_pct": round(local_cloud_fraction * 100, 2),
        "ndwi_threshold": round(threshold, 3),
        "wet_dry_point_count": len(xs),
        "wet_dry_median_ndwi_contrast": round(float(np.median(wet_dry_contrasts)), 3),
        "wet_dry_median_dry_side_ndwi": round(float(np.median(dry_side_values)), 3),
        "wet_dry_median_wet_side_ndwi": round(float(np.median(wet_side_values)), 3),
        "tide": tide,
        "high_tide": scene["high_tide"],
        "wave": wave,
        "wave_setup_m": round(setup, 3),
        "horizontal_correction_m": round(correction, 1),
        "uncertainty_m": round(14 + abs(correction) * 0.18, 1),
        "raw_coords": [[round(lon, 7), round(lat, 7)] for lon, lat in zip(raw_lon, raw_lat)],
        "corrected_coords": [
            [round(lon, 7), round(lat, 7)] for lon, lat in zip(corrected_lon, corrected_lat)
        ],
        "image": f"/data/scenes/{image_name}",
        "image_shape": rgb_shape,
        "stac_url": f"{STAC}/collections/sentinel-2-l2a/items/{scene['id']}",
    }


def interpolate_lon(record: dict[str, Any], latitudes: np.ndarray) -> np.ndarray:
    coords = np.asarray(record["corrected_coords"], dtype="float64")
    order = np.argsort(coords[:, 1])
    return np.interp(latitudes, coords[order, 1], coords[order, 0])


def lon_delta_m(delta_lon: np.ndarray, latitude: np.ndarray | float) -> np.ndarray:
    return delta_lon * (111_320.0 * np.cos(np.radians(latitude)))


def screen_high_tide(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep catalog captures made no more than 90 minutes from a NOAA high tide."""
    month_keys = sorted({(record["year"], parse_dt(record["datetime"]).month) for record in records})
    monthly_events: dict[tuple[int, int], list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(high_tides_for_month, year, month): (year, month)
            for year, month in month_keys
        }
        for future in as_completed(futures):
            monthly_events[futures[future]] = future.result()

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record in records:
        scene_time = parse_dt(record["datetime"])
        events = monthly_events[(record["year"], scene_time.month)]
        nearest = min(events, key=lambda event: abs((parse_dt(event["time"]) - scene_time).total_seconds()))
        offset_minutes = (scene_time - parse_dt(nearest["time"])).total_seconds() / 60
        if abs(offset_minutes) > HIGH_TIDE_WINDOW_MINUTES:
            rejected.append(
                {
                    "scene": {"id": record["id"], "datetime": record["datetime"]},
                    "reason": (
                        f"Capture is {abs(offset_minutes):.1f} minutes from nearest high tide; "
                        f"maximum is {HIGH_TIDE_WINDOW_MINUTES:.0f} minutes"
                    ),
                }
            )
            continue
        accepted.append(
            {
                **record,
                "high_tide": {
                    **nearest,
                    "image_offset_minutes": round(offset_minutes, 1),
                },
            }
        )
    return accepted, rejected


def screen_geometry(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject broken traces by comparison with the robust temporal shoreline."""
    if len(records) < 3:
        return records, []
    latitudes = np.linspace(SHORE_LAT_RANGE[0] + 0.0005, SHORE_LAT_RANGE[1] - 0.0005, 90)
    interpolated = np.asarray([interpolate_lon(record, latitudes) for record in records])
    temporal_median = np.median(interpolated, axis=0)
    deviations_m = lon_delta_m(interpolated - temporal_median, latitudes)
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for record, deviations in zip(records, deviations_m):
        p90 = float(np.percentile(np.abs(deviations), 90))
        maximum = float(np.max(np.abs(deviations)))
        if p90 > GEOMETRY_P90_MAX_DEVIATION_M or maximum > GEOMETRY_MAX_DEVIATION_M:
            rejected.append(
                {
                    "scene": {"id": record["scene_id"], "datetime": record["datetime"]},
                    "reason": (
                        f"Shoreline geometry failed temporal-coherence screen "
                        f"(p90={p90:.1f}m, max={maximum:.1f}m)"
                    ),
                }
            )
            continue
        record["geometry_p90_deviation_m"] = round(p90, 1)
        record["geometry_max_deviation_m"] = round(maximum, 1)
        accepted.append(record)
    return accepted, rejected


def build_outputs(records: list[dict[str, Any]], diagnostics: dict[str, Any]) -> None:
    if len(records) < 2:
        raise RuntimeError("At least two suitable acquisitions are required")

    records.sort(key=lambda record: record["datetime"])
    public_scene_images = PUBLIC_DATA / "scenes"
    public_scene_images.mkdir(parents=True, exist_ok=True)
    accepted_image_names = {Path(record["image"]).name for record in records}
    for image_name in accepted_image_names:
        source = SCENE_IMAGE_CACHE / image_name
        destination = public_scene_images / image_name
        if not source.exists() and not destination.exists():
            raise RuntimeError(f"Missing cached scene image: {image_name}")
        if source.exists():
            shutil.copyfile(source, destination)
    for image_path in public_scene_images.glob("sentinel-*.jpg"):
        if image_path.name not in accepted_image_names:
            image_path.unlink()

    features = []
    for record in records:
        corrected_coords = [
            coord for coord in record["corrected_coords"] if SHORE_LAT_RANGE[0] <= coord[1] <= SHORE_LAT_RANGE[1]
        ]
        common = {
            key: value
            for key, value in record.items()
            if key not in ("raw_coords", "corrected_coords")
        }
        features.append(
            {
                "type": "Feature",
                "properties": {
                    **common,
                    "geometry_kind": "corrected",
                    "shoreline_proxy": "wet/dry line",
                },
                "geometry": {"type": "LineString", "coordinates": corrected_coords},
            }
        )
    shoreline_payload = json.dumps(
        {"type": "FeatureCollection", "features": features}, separators=(",", ":")
    )
    (PUBLIC_DATA / "shorelines.geojson").write_text(shoreline_payload)
    (PUBLIC_DATA / "shorelines.json").write_text(shoreline_payload)

    earliest, latest = records[0], records[-1]
    shutil.copyfile(ROOT / "public" / earliest["image"].lstrip("/"), PUBLIC_DATA / "sentinel-baseline.jpg")
    shutil.copyfile(ROOT / "public" / latest["image"].lstrip("/"), PUBLIC_DATA / "sentinel-latest.jpg")
    latitudes = np.linspace(SHORE_LAT_RANGE[0] + 0.0005, SHORE_LAT_RANGE[1] - 0.0005, 90)
    earliest_lon = interpolate_lon(earliest, latitudes)
    latest_lon = interpolate_lon(latest, latitudes)
    change_m = lon_delta_m(latest_lon - earliest_lon, latitudes)

    zones = [
        ("South beach", 38.985, 38.992),
        ("Central beach", 38.992, 38.999),
        ("North beach", 38.999, 39.006),
    ]
    zone_stats = []
    for name, lo, hi in zones:
        mask = (latitudes >= lo) & (latitudes < hi)
        values = change_m[mask]
        zone_stats.append(
            {
                "name": name,
                "median_change_m": round(float(np.median(values)), 1),
                "min_change_m": round(float(np.min(values)), 1),
                "max_change_m": round(float(np.max(values)), 1),
            }
        )

    observations = []
    baseline_lons = interpolate_lon(earliest, latitudes)
    for record in records:
        current_lons = interpolate_lon(record, latitudes)
        values = lon_delta_m(current_lons - baseline_lons, latitudes)
        observations.append(
            {
                "datetime": record["datetime"],
                "year": record["year"],
                "median_change_m": round(float(np.median(values)), 1),
                "p10_change_m": round(float(np.percentile(values, 10)), 1),
                "p90_change_m": round(float(np.percentile(values, 90)), 1),
            }
        )

    yearly = []
    for year in sorted({observation["year"] for observation in observations}):
        values = np.asarray(
            [
                observation["median_change_m"]
                for observation in observations
                if observation["year"] == year
            ],
            dtype="float64",
        )
        yearly.append(
            {
                "year": year,
                "observation_count": int(len(values)),
                "median_change_m": round(float(np.median(values)), 1),
                "p10_change_m": round(float(np.percentile(values, 10)), 1),
                "p90_change_m": round(float(np.percentile(values, 90)), 1),
            }
        )

    trend = {
        "baseline_year": earliest["year"],
        "latest_year": latest["year"],
        "baseline_datetime": earliest["datetime"],
        "latest_datetime": latest["datetime"],
        "observation_count": len(records),
        "net_median_change_m": round(float(np.median(change_m)), 1),
        "retreat_share_pct": round(float(np.mean(change_m < 0) * 100), 1),
        "max_retreat_m": round(float(np.min(change_m)), 1),
        "max_advance_m": round(float(np.max(change_m)), 1),
        "latitudes": [round(float(value), 6) for value in latitudes],
        "change_m": [round(float(value), 1) for value in change_m],
        "zones": zone_stats,
        "observations": observations,
        "yearly": yearly,
    }
    (PUBLIC_DATA / "trend.json").write_text(json.dumps(trend, indent=2))

    metadata = {
        "title": "North Wildwood Shoreline Observatory",
        "generated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "aoi": list(AOI),
        "display_bounds": [-74.808, 38.984, -74.7725, 39.007],
        "reference": "Mean Sea Level (MSL)",
        "beach_slope": BEACH_SLOPE,
        "sentinel_resolution_m": 10,
        "tide_station": TIDE_STATION,
        "wave_station": WAVE_STATION,
        "suitability": {
            "catalog_cloud_max_pct": CATALOG_CLOUD_MAX_PCT,
            "aoi_cloud_mask_max_pct": AOI_CLOUD_MAX_PCT,
            "minimum_shoreline_points": MIN_SHORELINE_POINTS,
            "high_tide_window_minutes": HIGH_TIDE_WINDOW_MINUTES,
            "wet_dry_search_pixels": WET_DRY_SEARCH_PIXELS,
            "wet_dry_side_pixels": WET_DRY_SIDE_PIXELS,
            "minimum_wet_dry_ndwi_contrast": MIN_WET_DRY_NDWI_CONTRAST,
            "geometry_p90_max_deviation_m": GEOMETRY_P90_MAX_DEVIATION_M,
            "geometry_max_deviation_m": GEOMETRY_MAX_DEVIATION_M,
            **diagnostics,
        },
        "scenes": [
            {key: value for key, value in record.items() if key not in ("raw_coords", "corrected_coords", "image_transform")}
            for record in records
        ],
        "method": {
            "water_index": "NDWI = (B03 - B08) / (B03 + B08), adaptive Otsu threshold",
            "shoreline_proxy": "Ocean-facing wet/dry line: strongest validated seaward NDWI step next to the ocean-connected water mask",
            "wet_dry_validation": "Median of three clear pixels sampled on each side; dry-side NDWI must be below the scene threshold, wet-side NDWI above it, and contrast at least 0.04",
            "cloud_mask": "Sentinel-2 Scene Classification Layer; classes 0,1,3,8,9,10,11 excluded",
            "tide": "Nearest NOAA CO-OPS Cape May water level, MSL datum",
            "high_tide_screen": "Capture must fall within +/- 90 minutes of a NOAA-predicted Cape May high tide",
            "waves": "Nearest NOAA NDBC 44009 significant wave height and dominant period",
            "wave_setup": "Stockdon setup term: 0.35 * beach slope * sqrt(H0 * L0)",
            "horizontal_normalization": "Observed wet/dry line shifted along local seaward normal by (tide + setup) / beach slope",
        },
    }
    (PUBLIC_DATA / "metadata.json").write_text(json.dumps(metadata, indent=2))


PIPELINE_VERSION = 3


def process_scene(scene: dict[str, Any]) -> dict[str, Any]:
    cache_dir = ROOT / "work" / "shoreline-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{scene['id']}.json"
    if cache_path.exists():
        payload = json.loads(cache_path.read_text())
        if payload.get("pipeline_version") == PIPELINE_VERSION:
            if payload.get("status") == "accepted":
                record = payload["record"]
                image_path = SCENE_IMAGE_CACHE / Path(record["image"]).name
                if image_path.exists():
                    return payload
            elif payload.get("status") == "rejected":
                return payload

    try:
        record = read_scene(scene)
        payload = {
            "pipeline_version": PIPELINE_VERSION,
            "status": "accepted",
            "record": record,
        }
        cache_path.write_text(json.dumps(payload))
        return payload
    except UnsuitableScene as error:
        payload = {
            "pipeline_version": PIPELINE_VERSION,
            "status": "rejected",
            "scene": scene,
            "reason": str(error),
        }
        cache_path.write_text(json.dumps(payload))
        return payload
    except Exception as error:
        return {
            "pipeline_version": PIPELINE_VERSION,
            "status": "error",
            "scene": scene,
            "reason": f"{type(error).__name__}: {error}",
        }


def main() -> None:
    scenes, raw_catalog_count = discover_scenes()
    print(
        f"Catalog candidates: {len(scenes)} unique acquisitions "
        f"({raw_catalog_count - len(scenes)} duplicate products removed) with tile cloud < "
        f"{CATALOG_CLOUD_MAX_PCT:.0f}%",
        flush=True,
    )
    high_tide_scenes, high_tide_rejected = screen_high_tide(scenes)
    print(
        f"High-tide screen: {len(high_tide_scenes)} inside +/- "
        f"{HIGH_TIDE_WINDOW_MINUTES:.0f} minutes; {len(high_tide_rejected)} rejected",
        flush=True,
    )
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_scene, scene): scene for scene in high_tide_scenes}
        for completed, future in enumerate(as_completed(futures), start=1):
            scene = futures[future]
            result = future.result()
            results.append(result)
            status = result["status"]
            detail = ""
            if status == "accepted":
                record = result["record"]
                detail = (
                    f" AOI mask={record['cloud_mask_aoi_pct']:.2f}% "
                    f"correction={record['horizontal_correction_m']:+.1f}m"
                )
            elif status != "accepted":
                detail = f" {result.get('reason', '')}"
            print(
                f"[{completed:03d}/{len(high_tide_scenes):03d}] {status:8s} "
                f"{scene['datetime'][:10]}{detail}",
                flush=True,
            )

    errors = [result for result in results if result["status"] == "error"]
    if errors:
        print("Retrying processing errors sequentially...", flush=True)
        retried = []
        for result in errors:
            retried.append(process_scene(result["scene"]))
        results = [result for result in results if result["status"] != "error"] + retried
        errors = [result for result in results if result["status"] == "error"]
    if errors:
        sample = "; ".join(
            f"{result['scene']['datetime'][:10]} {result['reason']}" for result in errors[:8]
        )
        raise RuntimeError(f"{len(errors)} catalog acquisitions could not be evaluated: {sample}")

    extracted = [result["record"] for result in results if result["status"] == "accepted"]
    rejected = [result for result in results if result["status"] == "rejected"]
    records, geometry_rejected = screen_geometry(extracted)
    diagnostics = {
        "catalog_item_count": raw_catalog_count,
        "catalog_candidate_count": len(scenes),
        "duplicate_product_count": raw_catalog_count - len(scenes),
        "accepted_count": len(records),
        "rejected_count": len(rejected) + len(high_tide_rejected) + len(geometry_rejected),
        "cloud_or_extraction_rejected_count": len(rejected),
        "high_tide_rejected_count": len(high_tide_rejected),
        "geometry_rejected_count": len(geometry_rejected),
        "date_start": scenes[0]["datetime"] if scenes else None,
        "date_end": scenes[-1]["datetime"] if scenes else None,
    }
    build_outputs(records, diagnostics)
    print(
        f"Accepted {len(records)} of {len(scenes)} unique acquisitions "
        f"({len(high_tide_rejected)} high-tide and {len(geometry_rejected)} geometry rejections); "
        f"wrote {PUBLIC_DATA}",
        flush=True,
    )


if __name__ == "__main__":
    main()
