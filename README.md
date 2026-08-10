# North Wildwood Shoreline Observatory

An interactive coastal-change dashboard built from six Sentinel-2 L2A summer acquisitions between 2016 and 2026. Extracted ocean-facing waterlines are normalized to mean sea level with NOAA Cape May tide observations and NDBC wave conditions using a Stockdon setup term.

The site includes a swipe comparison, corrected shoreline timeline, scene-level correction details, alongshore change summaries, uncertainty guidance, and direct source links.

## Run locally

```bash
npm install
npm run dev
```

## Validate

```bash
npm test
npm run lint
```

The reproducible data workflow is in `scripts/build_shorelines.py`; generated site data is under `public/data/`.
