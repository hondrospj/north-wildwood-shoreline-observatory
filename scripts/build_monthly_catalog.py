#!/usr/bin/env python3
"""Build cloud-free and low-tide Sentinel-2 image catalogs and publish them to Bunny."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import mimetypes
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import numpy as np
import rasterio
import requests
from PIL import Image
from rasterio.features import geometry_mask

import build_shorelines as shoreline


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "public" / "data" / "monthly-catalog.json"
WORK_CATALOG_PATH = ROOT / "work" / "monthly-catalog-build.json"
TIDE_CACHE = ROOT / "work" / "low-tide-cache"
SCENE_CACHE = ROOT / "work" / "scene-images"
STUDY_CLOUD_CACHE = ROOT / "work" / "study-cloud-cache"

START_MONTH = "2015-08"
END_YEAR = 2026
LOW_TIDE_WINDOW_MINUTES = 105.0

# The exposed North Wildwood oceanfront. Every included image must contain zero
# Sentinel-2 SCL cloud, shadow, cirrus, or snow/ice pixels in this box. A tiny
# invalid-pixel allowance accommodates product-edge fill without admitting cloud.
STUDY_BOUNDS = (-74.793, 38.984, -74.773, 39.007)
# A roughly 430 m-wide corridor centered on the exposed wet/dry shoreline.
STUDY_CENTERLINE = (
    (-74.7890, 38.9840),
    (-74.7840, 38.9900),
    (-74.7790, 38.9970),
    (-74.7770, 39.0030),
    (-74.7795, 39.0070),
)
STUDY_CORRIDOR_HALF_WIDTH_LON = 0.0025
OBSCURED_SCL_CLASSES = (0, 1, 3, 8, 9, 10, 11)
CLOUD_SCL_CLASSES = (3, 8, 9, 10)
INVALID_SCL_CLASSES = (0, 1)
SNOW_SCL_CLASSES = (11,)
MAX_STUDY_INVALID_PCT = 0.5
CLOUD_MASK_VERSION = 3

BUNNY_ZONE = "floodmapperv1"
BUNNY_CDN = "https://floodmapperv1.b-cdn.net"
BUNNY_STORAGE = f"https://storage.bunnycdn.com/{BUNNY_ZONE}"
BUNNY_PREFIX = "NorthWildwoodShoreline/scenes"
BUNNY_KEYCHAIN_SERVICE = f"shorelysafe.bunny.storage.{BUNNY_ZONE}"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "NorthWildwoodMonthlyShoreline/1.0"})


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def month_key(value: str) -> str:
    return value[:7]


def image_name(value: str) -> str:
    return f"sentinel-{parse_dt(value).strftime('%Y%m%dT%H%M%S')}.jpg"


def monthly_range() -> list[str]:
    current = datetime(2015, 8, 1, tzinfo=timezone.utc)
    today = datetime.now(timezone.utc)
    end = datetime(min(today.year, END_YEAR), today.month if today.year <= END_YEAR else 12, 1, tzinfo=timezone.utc)
    values = []
    while current <= end:
        values.append(current.strftime("%Y-%m"))
        if current.month == 12:
            current = datetime(current.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            current = datetime(current.year, current.month + 1, 1, tzinfo=timezone.utc)
    return values


def discover_year(year: int) -> list[dict[str, Any]]:
    end_year = year + 1
    payload = shoreline.fetch_json(
        f"{shoreline.STAC}/search",
        params={
            "collections": "sentinel-2-l2a",
            "bbox": ",".join(str(value) for value in shoreline.AOI),
            "datetime": f"{year}-01-01T00:00:00Z/{end_year}-01-01T00:00:00Z",
            "limit": 1000,
            "query": json.dumps(
                {"s2:mgrs_tile": {"eq": shoreline.MGRS_TILE}},
                separators=(",", ":"),
            ),
        },
    )
    return [
        {
            "id": feature["id"],
            "datetime": feature["properties"]["datetime"],
            "catalog_cloud_pct": round(float(feature["properties"].get("eo:cloud_cover", 100)), 2),
        }
        for feature in payload.get("features", [])
    ]


def discover_all_scenes() -> list[dict[str, Any]]:
    years = range(2015, END_YEAR + 1)
    collected: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(discover_year, year): year for year in years}
        for future in concurrent.futures.as_completed(futures):
            rows = future.result()
            collected.extend(rows)
            print(f"Catalog {futures[future]}: {len(rows)} products", flush=True)

    by_datetime: dict[str, dict[str, Any]] = {}
    for scene in collected:
        existing = by_datetime.get(scene["datetime"])
        if existing is None or scene["id"].rsplit("_", 1)[-1] > existing["id"].rsplit("_", 1)[-1]:
            by_datetime[scene["datetime"]] = scene
    return sorted(by_datetime.values(), key=lambda scene: scene["datetime"])


def study_cloud_quality(scene: dict[str, Any]) -> dict[str, Any]:
    STUDY_CLOUD_CACHE.mkdir(parents=True, exist_ok=True)
    path = STUDY_CLOUD_CACHE / f"{scene['id']}.json"
    if path.exists():
        cached = json.loads(path.read_text())
        if cached.get("cloud_mask_version") == CLOUD_MASK_VERSION:
            return cached

    record = shoreline.item(scene["id"])
    href = shoreline.sign_href(record["assets"]["SCL"]["href"])
    env_options = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_MULTIRANGE": "YES",
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_CACHEMAX": 64,
    }
    with rasterio.Env(**env_options), rasterio.open(href) as source:
        bounds = shoreline.transform_bounds(
            "EPSG:4326", source.crs, *STUDY_BOUNDS, densify_pts=21
        )
        window = shoreline.from_bounds(*bounds, transform=source.transform).round_offsets().round_lengths()
        scl = source.read(1, window=window)

        west = [(lon - STUDY_CORRIDOR_HALF_WIDTH_LON, lat) for lon, lat in STUDY_CENTERLINE]
        east = [(lon + STUDY_CORRIDOR_HALF_WIDTH_LON, lat) for lon, lat in reversed(STUDY_CENTERLINE)]
        ring = west + east + [west[0]]
        xs, ys = shoreline.warp_transform(
            "EPSG:4326",
            source.crs,
            [point[0] for point in ring],
            [point[1] for point in ring],
        )
        corridor = {
            "type": "Polygon",
            "coordinates": [list(zip(xs, ys, strict=True))],
        }
        inside = geometry_mask(
            [corridor],
            out_shape=scl.shape,
            transform=source.window_transform(window),
            invert=True,
        )
        scl = scl[inside]

    total = int(scl.size)
    cloud_pixels = int(np.count_nonzero(np.isin(scl, CLOUD_SCL_CLASSES)))
    invalid_pixels = int(np.count_nonzero(np.isin(scl, INVALID_SCL_CLASSES)))
    snow_pixels = int(np.count_nonzero(np.isin(scl, SNOW_SCL_CLASSES)))
    obscured_pixels = int(np.count_nonzero(np.isin(scl, OBSCURED_SCL_CLASSES)))
    result = {
        "cloud_mask_version": CLOUD_MASK_VERSION,
        "study_pixel_count": total,
        "study_cloud_pixels": cloud_pixels,
        "study_invalid_pixels": invalid_pixels,
        "study_snow_pixels": snow_pixels,
        "study_obscured_pixels": obscured_pixels,
        "study_cloud_pct": round(cloud_pixels * 100 / total, 4),
        "study_invalid_pct": round(invalid_pixels * 100 / total, 4),
        "study_obscured_pct": round(obscured_pixels * 100 / total, 4),
    }
    path.write_text(json.dumps(result))
    return result


def attach_study_quality(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(study_cloud_quality, scene): scene for scene in scenes}
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            scene = futures[future]
            quality = future.result()
            output.append(
                {
                    **scene,
                    **quality,
                    "estimated_aoi_cloud_pct": quality["study_obscured_pct"],
                    "quality_source": "Sentinel-2 SCL oceanfront mask",
                }
            )
            if index % 50 == 0 or index == len(scenes):
                print(f"Study cloud screen {index:04d}/{len(scenes):04d}", flush=True)
    return sorted(output, key=lambda scene: scene["datetime"])


def low_tides_for_year(year: int) -> list[dict[str, Any]]:
    TIDE_CACHE.mkdir(parents=True, exist_ok=True)
    path = TIDE_CACHE / f"{shoreline.TIDE_STATION}-{year}.json"
    if path.exists():
        return json.loads(path.read_text())

    begin = datetime(year, 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) + timedelta(days=1)
    payload = shoreline.fetch_json(
        shoreline.NOAA_COOPS,
        params={
            "application": "NorthWildwoodMonthlyShoreline",
            "begin_date": begin.strftime("%Y%m%d"),
            "end_date": end.strftime("%Y%m%d"),
            "datum": "MSL",
            "station": shoreline.TIDE_STATION,
            "time_zone": "gmt",
            "units": "metric",
            "format": "json",
            "product": "predictions",
            "interval": "hilo",
        },
    )
    rows = []
    for row in payload.get("predictions", []):
        if row.get("type") != "L":
            continue
        rows.append(
            {
                "time": datetime.strptime(row["t"], "%Y-%m-%d %H:%M")
                .replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "level_m_msl": round(float(row["v"]), 3),
            }
        )
    if not rows:
        raise RuntimeError(f"No NOAA low-tide predictions returned for {year}")
    path.write_text(json.dumps(rows))
    return rows


def attach_quality_and_tide(
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tide_by_year: dict[int, list[dict[str, Any]]] = {}
    for year in sorted({parse_dt(scene["datetime"]).year for scene in scenes}):
        tide_by_year[year] = low_tides_for_year(year)

    output = []
    for scene in scenes:
        scene_time = parse_dt(scene["datetime"])
        events = tide_by_year[scene_time.year]
        nearest = min(
            events,
            key=lambda event: abs((parse_dt(event["time"]) - scene_time).total_seconds()),
        )
        offset = (scene_time - parse_dt(nearest["time"])).total_seconds() / 60
        output.append(
            {
                **scene,
                "nearest_low_tide": {
                    **nearest,
                    "image_offset_minutes": round(offset, 1),
                },
            }
        )
    return output


def select_catalogs(scenes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = [
        scene
        for scene in scenes
        if scene["study_cloud_pixels"] == 0
        and scene["study_snow_pixels"] == 0
        and scene["study_invalid_pct"] <= MAX_STUDY_INVALID_PCT
    ]
    clear = select_twice_monthly(eligible)
    low_tide = [
        scene
        for scene in eligible
        if abs(scene["nearest_low_tide"]["image_offset_minutes"])
        <= LOW_TIDE_WINDOW_MINUTES
    ]
    return clear, low_tide


def select_twice_monthly(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the best scene from each half-month, with a two-scene monthly cap."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for scene in scenes:
        grouped.setdefault(month_key(scene["datetime"]), []).append(scene)

    selected: list[dict[str, Any]] = []
    for month in sorted(grouped):
        candidates = grouped[month]
        chosen: list[dict[str, Any]] = []
        for start_day, end_day, midpoint in ((1, 15, 8), (16, 31, 23)):
            half = [
                scene
                for scene in candidates
                if start_day <= parse_dt(scene["datetime"]).day <= end_day
            ]
            if half:
                chosen.append(
                    min(
                        half,
                        key=lambda scene: (
                            scene["study_invalid_pct"],
                            scene["catalog_cloud_pct"],
                            abs(parse_dt(scene["datetime"]).day - midpoint),
                            scene["datetime"],
                        ),
                    )
                )

        if len(chosen) < 2:
            remaining = [scene for scene in candidates if scene not in chosen]
            remaining.sort(
                key=lambda scene: (
                    scene["study_invalid_pct"],
                    scene["catalog_cloud_pct"],
                    scene["datetime"],
                )
            )
            chosen.extend(remaining[: 2 - len(chosen)])

        selected.extend(sorted(chosen, key=lambda scene: scene["datetime"]))
    return selected


def download_visual(scene: dict[str, Any]) -> tuple[str, list[int]]:
    name = image_name(scene["datetime"])
    path = SCENE_CACHE / name
    if not path.exists():
        record = shoreline.item(scene["id"])
        href = shoreline.sign_href(record["assets"]["visual"]["href"])
        env_options = {
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
            "GDAL_HTTP_MULTIRANGE": "YES",
            "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
            "GDAL_CACHEMAX": 128,
        }
        with rasterio.Env(**env_options), rasterio.open(href) as source:
            window = shoreline.crop_window(source)
            rgb = source.read([1, 2, 3], window=window)
        image = np.moveaxis(rgb, 0, -1)
        if image.dtype != np.uint8:
            image = np.clip(image, 0, 255).astype("uint8")
        Image.fromarray(image).save(path, quality=90, optimize=True)
    with Image.open(path) as image:
        return name, [image.width, image.height]


def prepare_images(scenes: list[dict[str, Any]]) -> dict[str, list[int]]:
    unique = {scene["id"]: scene for scene in scenes}
    dimensions: dict[str, list[int]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(download_visual, scene): scene for scene in unique.values()
        }
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            scene = futures[future]
            name, shape = future.result()
            dimensions[scene["id"]] = shape
            print(f"Image {index:03d}/{len(unique):03d}: {name}", flush=True)
    return dimensions


def public_scene(scene: dict[str, Any], dimensions: dict[str, list[int]]) -> dict[str, Any]:
    name = image_name(scene["datetime"])
    return {
        "id": scene["id"],
        "month": month_key(scene["datetime"]),
        "datetime": scene["datetime"],
        "image": f"{BUNNY_CDN}/{BUNNY_PREFIX}/{name}",
        "image_shape": dimensions[scene["id"]],
        "catalog_cloud_pct": scene["catalog_cloud_pct"],
        "estimated_aoi_cloud_pct": scene["estimated_aoi_cloud_pct"],
        "quality_source": scene["quality_source"],
        "study_pixel_count": scene["study_pixel_count"],
        "study_cloud_pixels": scene["study_cloud_pixels"],
        "study_invalid_pixels": scene["study_invalid_pixels"],
        "study_snow_pixels": scene["study_snow_pixels"],
        "study_obscured_pixels": scene["study_obscured_pixels"],
        "nearest_low_tide": scene["nearest_low_tide"],
        "stac_url": f"{shoreline.STAC}/collections/sentinel-2-l2a/items/{scene['id']}",
    }


def keychain_password() -> str:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-w",
            "-s",
            BUNNY_KEYCHAIN_SERVICE,
            "-a",
            BUNNY_ZONE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    password = result.stdout.strip()
    if result.returncode != 0 or not password:
        raise RuntimeError("Bunny credential was not found in macOS Keychain")
    return password


def upload_one(password: str, path: Path, remote_path: str) -> dict[str, Any]:
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    url = f"{BUNNY_STORAGE}/{quote(remote_path, safe='/')}"
    response = SESSION.put(
        url,
        data=payload,
        headers={
            "AccessKey": password,
            "Content-Type": mimetypes.guess_type(path.name)[0] or "image/jpeg",
        },
        timeout=180,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Bunny upload failed for {remote_path}: HTTP {response.status_code}")
    return {"path": remote_path, "bytes": len(payload), "sha256": digest}


def upload_images(scenes: list[dict[str, Any]], catalog: dict[str, Any]) -> None:
    password = keychain_password()
    try:
        unique_names = sorted({image_name(scene["datetime"]) for scene in scenes})
        records = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(
                    upload_one,
                    password,
                    SCENE_CACHE / name,
                    f"{BUNNY_PREFIX}/{name}",
                ): name
                for name in unique_names
            }
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                records.append(future.result())
                if index % 20 == 0 or index == len(unique_names):
                    print(f"Bunny {index:03d}/{len(unique_names):03d}", flush=True)

        manifest = {
            "generated": catalog["generated"],
            "catalog_sha256": hashlib.sha256(
                json.dumps(catalog, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "image_count": len(records),
            "images": records,
        }
        upload_one(
            password,
            write_temporary_manifest(manifest),
            "NorthWildwoodShoreline/manifest.json",
        )
    finally:
        password = ""


def write_temporary_manifest(manifest: dict[str, Any]) -> Path:
    path = ROOT / "work" / "bunny-image-manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


def verify_cdn(scenes: list[dict[str, Any]]) -> None:
    names = sorted({image_name(scene["datetime"]) for scene in scenes})
    failures = []
    for name in names:
        url = f"{BUNNY_CDN}/{BUNNY_PREFIX}/{name}?v={int(time.time())}"
        response = SESSION.get(url, headers={"Range": "bytes=0-31"}, timeout=60)
        if response.status_code not in (200, 206) or not response.content:
            failures.append(f"{name}: HTTP {response.status_code}")
    if failures:
        raise RuntimeError("Bunny CDN verification failed: " + "; ".join(failures[:10]))
    print(f"Verified {len(names)} Bunny CDN images", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload-bunny", action="store_true")
    args = parser.parse_args()

    scenes = discover_all_scenes()
    scenes = attach_study_quality(scenes)
    scenes = attach_quality_and_tide(scenes)
    clear, low_tide = select_catalogs(scenes)
    chosen = clear + low_tide
    dimensions = prepare_images(chosen)

    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    catalog = {
        "generated": generated,
        "bounds": list(shoreline.AOI),
        "study_bounds": list(STUDY_BOUNDS),
        "resolution_m": 10,
        "tide_station": shoreline.TIDE_STATION,
        "range": [START_MONTH, monthly_range()[-1]],
        "selection": {
            "clear": "Up to two Sentinel-2 L2A acquisitions per month: the best qualifying image from each half-month, with zero cloud, shadow, cirrus, or snow/ice SCL pixels over the oceanfront study area and no more than 0.5% invalid pixels",
            "low_tide": f"Every qualifying cloud-free acquisition within +/- {LOW_TIDE_WINDOW_MINUTES:.0f} minutes of a NOAA-predicted low tide",
            "low_tide_window_minutes": LOW_TIDE_WINDOW_MINUTES,
            "maximum_clear_images_per_month": 2,
            "maximum_study_cloud_pixels": 0,
            "maximum_study_snow_pixels": 0,
            "maximum_study_invalid_pct": MAX_STUDY_INVALID_PCT,
        },
        "clear": [public_scene(scene, dimensions) for scene in clear],
        "low_tide": [public_scene(scene, dimensions) for scene in low_tide],
    }
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2))
    WORK_CATALOG_PATH.write_text(json.dumps(catalog, indent=2))
    print(
        f"Selected {len(clear)} twice-monthly clear scenes and {len(low_tide)} low-tide scenes",
        flush=True,
    )
    if args.upload_bunny:
        upload_images(chosen, catalog)
        verify_cdn(chosen)


if __name__ == "__main__":
    main()
