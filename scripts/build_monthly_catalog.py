#!/usr/bin/env python3
"""Build monthly and low-tide Sentinel-2 image catalogs and publish them to Bunny."""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import hashlib
import json
import mimetypes
import re
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

import build_shorelines as shoreline


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "public" / "data" / "monthly-catalog.json"
WORK_CATALOG_PATH = ROOT / "work" / "monthly-catalog-build.json"
TIDE_CACHE = ROOT / "work" / "low-tide-cache"
SCENE_CACHE = ROOT / "work" / "scene-images"

START_MONTH = "2015-08"
END_YEAR = 2026
LOW_TIDE_WINDOW_MINUTES = 90.0
MAX_CATALOG_CLOUD_FOR_LOW_TIDE = 50.0

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


def cache_quality() -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    cache_directory = ROOT / "work" / "shoreline-cache"
    cloud_pattern = re.compile(r"AOI cloud/invalid mask ([0-9.]+)%")
    for path in cache_directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        scene_id = payload.get("record", {}).get("scene_id") or payload.get("scene", {}).get("id")
        if not scene_id:
            continue
        if payload.get("status") == "accepted":
            record = payload["record"]
            values[scene_id] = {
                "aoi_cloud_pct": float(record.get("cloud_mask_aoi_pct", 100)),
                "quality_source": "Sentinel-2 AOI mask",
            }
            continue
        match = cloud_pattern.search(str(payload.get("reason", "")))
        if match:
            values[scene_id] = {
                "aoi_cloud_pct": float(match.group(1)),
                "quality_source": "Sentinel-2 AOI mask",
            }
    return values


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
    cached_quality: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    tide_by_year: dict[int, list[dict[str, Any]]] = {}
    for year in sorted({parse_dt(scene["datetime"]).year for scene in scenes}):
        tide_by_year[year] = low_tides_for_year(year)

    output = []
    for scene in scenes:
        scene_time = parse_dt(scene["datetime"])
        quality = cached_quality.get(scene["id"])
        estimated_cloud = (
            quality["aoi_cloud_pct"] if quality else scene["catalog_cloud_pct"]
        )
        events = tide_by_year[scene_time.year]
        nearest = min(
            events,
            key=lambda event: abs((parse_dt(event["time"]) - scene_time).total_seconds()),
        )
        offset = (scene_time - parse_dt(nearest["time"])).total_seconds() / 60
        output.append(
            {
                **scene,
                "estimated_aoi_cloud_pct": round(float(estimated_cloud), 2),
                "quality_source": quality["quality_source"] if quality else "Sentinel-2 tile cloud",
                "nearest_low_tide": {
                    **nearest,
                    "image_offset_minutes": round(offset, 1),
                },
            }
        )
    return output


def select_catalogs(scenes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    expected = monthly_range()
    by_month: dict[str, list[dict[str, Any]]] = {month: [] for month in expected}
    for scene in scenes:
        key = month_key(scene["datetime"])
        if key in by_month:
            by_month[key].append(scene)

    monthly = []
    low_tide = []
    for month in expected:
        candidates = by_month[month]
        if not candidates:
            continue
        best = min(
            candidates,
            key=lambda scene: (
                scene["estimated_aoi_cloud_pct"],
                scene["catalog_cloud_pct"],
                scene["datetime"],
            ),
        )
        monthly.append(best)

        low_candidates = [
            scene
            for scene in candidates
            if abs(scene["nearest_low_tide"]["image_offset_minutes"])
            <= LOW_TIDE_WINDOW_MINUTES
            and scene["catalog_cloud_pct"] <= MAX_CATALOG_CLOUD_FOR_LOW_TIDE
        ]
        if low_candidates:
            low_tide.append(
                min(
                    low_candidates,
                    key=lambda scene: (
                        scene["estimated_aoi_cloud_pct"],
                        scene["catalog_cloud_pct"],
                        abs(scene["nearest_low_tide"]["image_offset_minutes"]),
                    ),
                )
            )
    return monthly, low_tide


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
    cached = cache_quality()
    scenes = attach_quality_and_tide(scenes, cached)
    monthly, low_tide = select_catalogs(scenes)
    chosen = monthly + low_tide
    dimensions = prepare_images(chosen)

    generated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    catalog = {
        "generated": generated,
        "bounds": list(shoreline.AOI),
        "resolution_m": 10,
        "tide_station": shoreline.TIDE_STATION,
        "range": [START_MONTH, monthly_range()[-1]],
        "selection": {
            "monthly": "Lowest available Sentinel-2 cloud estimate in each calendar month",
            "low_tide": f"Lowest-cloud scene within +/- {LOW_TIDE_WINDOW_MINUTES:.0f} minutes of a NOAA-predicted low tide",
            "low_tide_window_minutes": LOW_TIDE_WINDOW_MINUTES,
        },
        "monthly": [public_scene(scene, dimensions) for scene in monthly],
        "low_tide": [public_scene(scene, dimensions) for scene in low_tide],
    }
    CATALOG_PATH.write_text(json.dumps(catalog, indent=2))
    WORK_CATALOG_PATH.write_text(json.dumps(catalog, indent=2))
    print(
        f"Selected {len(monthly)} monthly scenes and {len(low_tide)} low-tide scenes",
        flush=True,
    )
    if args.upload_bunny:
        upload_images(chosen, catalog)
        verify_cdn(chosen)


if __name__ == "__main__":
    main()
