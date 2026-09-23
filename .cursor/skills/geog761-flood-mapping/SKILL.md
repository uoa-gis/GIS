---
name: geog761-flood-mapping
description: Implement GEOG761 flood and inundation extent mapping with GEE, geemap, Sentinel-1 Random Forest, Sentinel-2 indices or U-Net, and DEM fusion, following SatelliteDataAI-UOA course notebooks. Use when writing processing, map UI, fusion, or Gemini report code, or when the user mentions Sentinel-1, Sentinel-2, geemap, WorldCover, s2cloudless, speckle, NDWI, or flood extent.
---

# GEOG761 flood mapping

Source of truth for **logic and method** is the course repo, not generic SAR/optical tutorials:

`/Users/dongwook.kim/UoA/SatelliteDataAI-UOA`

Copy those notebook patterns into this project. Do not invent a dB water cutoff, a different S1 collection, or a different cloud stack unless the user explicitly asks.

## Product

Flood / inundation extent for a user AOI and before/after date range.

**Logical proposition:** flood = Sentinel-1 and/or Sentinel-2 water, minus permanent water, on low / flat ground.

Inputs: user-selected AOI + date range (not a live MetService feed). Outputs: geemap layers, short stats, optional Gemini one-page text (no RAG).

## Always do this first

1. Open the matching notebook under SatelliteDataAI-UOA and reuse its EE filters, bands, classifier, and compositing operator.
2. Keep course honesty: Lecture 6 classifies **land vs water with Random Forest on VV/VH**. A backscatter threshold is a project extra, not the lecture method. Water is dark in calm SAR; wind and speckle are why water is not one dB cutoff.
3. Speckle: `focal_mean(radius=50, units='meters')` as in Lecture 6 / 8. Note that it blurs edges.

## Pipeline (match README architecture)

```
S1 VV/VH + S2 RGB/NIR/SWIR + DEM
  → clip AOI, cloud mask, speckle
  → S1 RF land/water | S2 NDWI or U-Net | DEM keep low/flat
  → flood = water − permanent, on low ground
  → geemap + short report + optional Gemini
```

Map each step to a course source:

| Step | Notebook |
|---|---|
| GEE init, geemap Map, SRTM DEM layer | `Lab-Notebooks/761_Lab1.ipynb` |
| S2 QA60 mask, median composite, pixel RF | `Lab-Notebooks/761_Lab2.ipynb`, `Lab-Notebooks/helpers/lab2_helpers.py` |
| s2cloudless cloud/shadow composite | `Lab-Notebooks/761_Lab3.ipynb` |
| S1 IW GRD, WorldCover labels, smile RF, speckle | `Lecture-Notebooks/761_Lecture6_Exercises.ipynb` (same exercise in `761_Lecture8_Exercises.ipynb`) |
| Optional 6-band U-Net surface water | `Lab-Notebooks/761_Lab5.ipynb` |
| DEM low/flat mask, before/after flood fusion | Project novelty (not a 761 exercise) |

Exact EE snippets: [reference.md](reference.md).

## Implementation rules

- Use `ee` + `geemap`. Initialize Earth Engine with the **user's** GEE Cloud project (`ee.Initialize(project=...)`). Do not copy a classmate's project id from Lab 1.
- Sentinel-1: `COPERNICUS/S1_GRD`, `instrumentMode=IW`, `orbitProperties_pass=DESCENDING`, `resolution_meters=10`, bands `VV`/`VH`, then `.mean()` over the window (Lecture 6).
- S1 labels: `ESA/WorldCover/v200/2021`. Water = class **80**. Land training = **10, 30, 50**. `smileRandomForest(numberOfTrees=50)` on `VV`, `VH`. Optional extra band: `VVVH_ratio = VV - VH` (dB difference).
- Sentinel-2: `COPERNICUS/S2_SR_HARMONIZED`. Fast path = Lab 2 QA60 + `CLOUDY_PIXEL_PERCENTAGE < 10` + median. Storm/cloud path = Lab 3 s2cloudless (default tuned Auckland params in reference).
- S2 water: McFeeters NDWI `normalizedDifference(['B3', 'B8'])` from `lab2_helpers.add_indices`, or Lab 2-style RF on `B2,B3,B4,B8`. U-Net only if the user wants Lab 5 / GeoAI (`architecture="unet"`, `encoder_name="resnet34"`, `num_channels=6`).
- Permanent water for fusion: WorldCover 80 (same definition as S1 training water), not a second ad-hoc mask unless specified.
- DEM: Lab 1 uses `USGS/SRTMGL1_003`. Constrain flood to low elevation and low slope; document the thresholds as a project choice.
- Before vs after: run the same water classifier on pre-event and post-event composites; inundation = post water and not pre water (and not WorldCover 80), then apply DEM.

## UI / report

- Team name **Geographically Informed Speculators**; logo from `assets/logo/`.
- Same page: AOI on map → processing → flood polygon/layers + short report.
- Gemini: map stats → one page of explainable text. No RAG.

## Do not

- Treat Lecture 6 as “threshold VV < −X dB”.
- Swap collections (`S1_GRD_FLOAT`, `S2_HARMONIZED` L1C, etc.) without a stated reason.
- Retrain Lab 5 U-Net from scratch as the default; RF + NDWI is the course-scale path.
- Add live disaster APIs unless asked.
---
