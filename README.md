# North Wildwood Shoreline Observatory

An interactive coastal-change dashboard built from every suitable Sentinel-2 L2A acquisition over North Wildwood since 2015. Each catalog image is screened locally for clouds and shoreline coherence; accepted ocean-facing waterlines are normalized to mean sea level with NOAA Cape May tide observations and NDBC wave conditions using a Stockdon setup term.

The site includes an acquisition-by-acquisition explorer, swipe comparison, corrected shoreline overlays, scene-level correction details, alongshore change summaries, uncertainty guidance, and direct source links.

The public dashboard is deployed through GitHub Pages at `https://hondrospj.github.io/north-wildwood-shoreline-observatory/`.

## Run locally

```bash
npm install
npm run dev
```

## Validate

```bash
npm test
npm run lint
npm run build:github
```

The reproducible data workflow is in `scripts/build_shorelines.py`; generated site data is under `public/data/`.
