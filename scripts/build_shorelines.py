#!/usr/bin/env python3
"""Build tide- and wave-normalized North Wildwood shorelines from Sentinel-2 L2A."""

from __future__ import annotations

import gzip
import io
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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

SCENES = [
    {
        "year": 2016,
        "id": "S2A_MSIL2A_20160720T154912_R054_T18SWJ_20210212T062408",
        "datetime": "2016-07-20T15:49:12.026000Z",
    },
    {
        "year": 2018,
        "id": "S2B_MSIL2A_20180824T154809_R054_T18SWJ_20201011T063219",
        "datetime": "2018-08-24T15:48:09.024000Z",
    },
    {
        "year": 2020,
        "id": "S2B_MSIL2A_20200803T154819_R054_T18SWJ_20200816T013303",
        "datetime": "2020-08-03T15:48:19.024000Z",
    },
    {
        "year": 2022,
        "id": "S2B_MSIL2A_20220803T154819_R054_T18SWJ_20220804T150432",
        "datetime": "2022-08-03T15:48:19.024000Z",
    },
    {
        "year": 2024,
        "id": "S2B_MSIL2A_20240822T154809_R054_T18SWJ_20240822T215036",
        "datetime": "2024-08-22T15:48:09.024000Z",
    },
    {
        "year": 2026,
        "id": "S2C_MSIL2A_20260807T154811_R054_T18SWJ_20260807T210317",
        "datetime": "2026-08-07T15:48:11.025000Z",
    },
]


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NorthWildwoodShoreline/1.0 contact: shoreline-research"})


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
    return fetch_json(SIGN, params={"href": href})["href"]


def item(scene_id: str) -> dict[str, Any]:
    return fetch_json(f"{STAC}/collections/sentinel-2-l2a/items/{scene_id}")


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
        url = f"https://www.ndbc.noaa.gov/data/realtime2/{WAVE_STATION}.txt"
        response = SESSION.get(url, timeout=60)
        if response.ok:
            parsed = parse_ndbc_table(response.text, scene_time)
            if parsed:
                return {**parsed, "source": "NDBC realtime"}

    url = f"https://www.ndbc.noaa.gov/data/historical/stdmet/{WAVE_STATION}h{scene_time.year}.txt.gz"
    response = SESSION.get(url, timeout=60)
    if response.ok:
        try:
            text = gzip.decompress(response.content).decode("utf-8", errors="replace")
        except gzip.BadGzipFile:
            text = response.text
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


def crop_window(dataset: rasterio.DatasetReader):
    bounds = transform_bounds("EPSG:4326", dataset.crs, *AOI, densify_pts=21)
    return from_bounds(*bounds, transform=dataset.transform).round_offsets().round_lengths()


def read_scene(scene: dict[str, Any]) -> dict[str, Any]:
    record = item(scene["id"])
    assets = record["assets"]
    signed = {name: sign_href(assets[name]["href"]) for name in ("B03", "B08", "SCL", "visual")}

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

        rgb = None
        rgb_transform = None
        with rasterio.open(signed["visual"]) as visual_src:
            visual_window = crop_window(visual_src)
            rgb = visual_src.read([1, 2, 3], window=visual_window)
            rgb_transform = visual_src.window_transform(visual_window)

    clear = ~np.isin(scl, [0, 1, 3, 8, 9, 10, 11])
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
                candidate_x.append((px, py, lon[0], candidate_lat[0]))
        if candidate_x:
            px, py, _, _ = max(candidate_x, key=lambda value: value[0])
            xs.append(px)
            ys.append(py)

    if len(xs) < 80:
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
        raise RuntimeError(f"Only {len(xs)} shoreline points extracted for {scene['year']}")

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

    if rgb is not None and scene["year"] in (2016, 2026):
        image = np.moveaxis(rgb, 0, -1)
        if image.dtype != np.uint8:
            image = np.clip(image, 0, 255).astype("uint8")
        Image.fromarray(image).save(PUBLIC_DATA / f"sentinel-{scene['year']}.jpg", quality=90)

    local_cloud_fraction = float(np.mean(~clear))
    return {
        "year": scene["year"],
        "scene_id": scene["id"],
        "datetime": scene["datetime"],
        "cloud_cover_tile_pct": round(float(record["properties"].get("eo:cloud_cover", 0)), 2),
        "cloud_mask_aoi_pct": round(local_cloud_fraction * 100, 2),
        "ndwi_threshold": round(threshold, 3),
        "tide": tide,
        "wave": wave,
        "wave_setup_m": round(setup, 3),
        "horizontal_correction_m": round(correction, 1),
        "uncertainty_m": round(14 + abs(correction) * 0.18, 1),
        "raw_coords": [[round(lon, 7), round(lat, 7)] for lon, lat in zip(raw_lon, raw_lat)],
        "corrected_coords": [
            [round(lon, 7), round(lat, 7)] for lon, lat in zip(corrected_lon, corrected_lat)
        ],
        "image": f"/data/sentinel-{scene['year']}.jpg" if scene["year"] in (2016, 2026) else None,
        "image_transform": list(rgb_transform) if rgb_transform is not None else None,
        "image_shape": [int(rgb.shape[2]), int(rgb.shape[1])] if rgb is not None else None,
        "stac_url": f"{STAC}/collections/sentinel-2-l2a/items/{scene['id']}",
    }


def interpolate_lon(record: dict[str, Any], latitudes: np.ndarray) -> np.ndarray:
    coords = np.asarray(record["corrected_coords"], dtype="float64")
    order = np.argsort(coords[:, 1])
    return np.interp(latitudes, coords[order, 1], coords[order, 0])


def lon_delta_m(delta_lon: np.ndarray, latitude: np.ndarray | float) -> np.ndarray:
    return delta_lon * (111_320.0 * np.cos(np.radians(latitude)))


def build_outputs(records: list[dict[str, Any]]) -> None:
    features = []
    for record in records:
        corrected_coords = [
            coord for coord in record["corrected_coords"] if SHORE_LAT_RANGE[0] <= coord[1] <= SHORE_LAT_RANGE[1]
        ]
        raw_coords = [
            coord for coord in record["raw_coords"] if SHORE_LAT_RANGE[0] <= coord[1] <= SHORE_LAT_RANGE[1]
        ]
        common = {
            key: value
            for key, value in record.items()
            if key not in ("raw_coords", "corrected_coords", "image_transform")
        }
        features.append(
            {
                "type": "Feature",
                "properties": {**common, "geometry_kind": "corrected"},
                "geometry": {"type": "LineString", "coordinates": corrected_coords},
            }
        )
        features.append(
            {
                "type": "Feature",
                "properties": {**common, "geometry_kind": "raw"},
                "geometry": {"type": "LineString", "coordinates": raw_coords},
            }
        )

    (PUBLIC_DATA / "shorelines.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    )

    earliest, latest = records[0], records[-1]
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

    yearly = []
    baseline_lons = interpolate_lon(earliest, latitudes)
    for record in records:
        current_lons = interpolate_lon(record, latitudes)
        values = lon_delta_m(current_lons - baseline_lons, latitudes)
        yearly.append(
            {
                "year": record["year"],
                "median_change_m": round(float(np.median(values)), 1),
                "p10_change_m": round(float(np.percentile(values, 10)), 1),
                "p90_change_m": round(float(np.percentile(values, 90)), 1),
            }
        )

    trend = {
        "baseline_year": earliest["year"],
        "latest_year": latest["year"],
        "net_median_change_m": round(float(np.median(change_m)), 1),
        "retreat_share_pct": round(float(np.mean(change_m < 0) * 100), 1),
        "max_retreat_m": round(float(np.min(change_m)), 1),
        "max_advance_m": round(float(np.max(change_m)), 1),
        "latitudes": [round(float(value), 6) for value in latitudes],
        "change_m": [round(float(value), 1) for value in change_m],
        "zones": zone_stats,
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
        "scenes": [
            {key: value for key, value in record.items() if key not in ("raw_coords", "corrected_coords", "image_transform")}
            for record in records
        ],
        "method": {
            "water_index": "NDWI = (B03 - B08) / (B03 + B08), adaptive Otsu threshold",
            "cloud_mask": "Sentinel-2 Scene Classification Layer; classes 0,1,3,8,9,10,11 excluded",
            "tide": "Nearest NOAA CO-OPS Cape May water level, MSL datum",
            "waves": "Nearest NOAA NDBC 44009 significant wave height and dominant period",
            "wave_setup": "Stockdon setup term: 0.35 * beach slope * sqrt(H0 * L0)",
            "horizontal_normalization": "Observed line shifted along local seaward normal by (tide + setup) / beach slope",
        },
    }
    (PUBLIC_DATA / "metadata.json").write_text(json.dumps(metadata, indent=2))


def main() -> None:
    records = []
    for scene in SCENES:
        print(f"Processing {scene['year']} {scene['id']}", flush=True)
        cache_path = ROOT / "work" / f"shoreline-cache-{scene['year']}.json"
        cache_path.parent.mkdir(exist_ok=True)
        if cache_path.exists():
            record = json.loads(cache_path.read_text())
            print("  using cached extraction", flush=True)
        else:
            record = read_scene(scene)
            cache_path.write_text(json.dumps(record))
        records.append(record)
        print(
            f"  tide={records[-1]['tide']['level_m_msl']:+.2f} m "
            f"wave={records[-1]['wave']['height_m']:.2f} m "
            f"correction={records[-1]['horizontal_correction_m']:+.1f} m",
            flush=True,
        )
    build_outputs(records)
    print(f"Wrote {PUBLIC_DATA}")


if __name__ == "__main__":
    main()
