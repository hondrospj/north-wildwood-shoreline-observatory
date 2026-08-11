# North Wildwood Shoreline Logger

A minimal Sentinel-2 shoreline measurement app. It includes up to two acquisitions per month from August 2015 onward: the best qualifying scene from each half-month with zero cloud, shadow, cirrus, or snow/ice Scene Classification Layer pixels over the North Wildwood oceanfront and no more than 0.5% invalid pixels. A separate view applies the same two-per-month cap to clear acquisitions captured within 90 minutes of NOAA-predicted low tide.

Choose a baseline image, draw a transect with two clicks, then click the wet/dry line once per image. `←` and `→`, the timeline scrubber, or the mouse wheel move through the catalog; hold Command/Ctrl while scrolling to zoom. Logged coordinates, distance along the transect, shoreline width, and baseline change are stored locally in the browser and export as an Excel workbook.

Selected imagery is served from Bunny CDN. The upload key is read from macOS Keychain and is never written to the repository.

The public app is at `https://hondrospj.github.io/north-wildwood-shoreline-observatory/`.

## Run locally

```bash
npm install
npm run dev
```

## Rebuild the imagery catalog

```bash
python scripts/build_monthly_catalog.py --upload-bunny
```

## Validate

```bash
npm test
npm run lint
```
