# Course method recipes

Copy these patterns from `/Users/dongwook.kim/UoA/SatelliteDataAI-UOA`. Adapt AOI and dates; keep filters, bands, and operators.

## Lab 1 — GEE / geemap / DEM

```python
import ee
import geemap

ee.Initialize(project='YOUR_GEE_PROJECT')
Map = geemap.Map(center=(-41, 172), zoom=4, basemap='HYBRID')
dem = ee.Image('USGS/SRTMGL1_003')
```

S2 single scene (Lab 1 style): `COPERNICUS/S2_SR_HARMONIZED`, `filterBounds`, `filterDate`, `CLOUDY_PIXEL_PERCENTAGE` filter, `.first()`.

## Lecture 6 / 8 — Sentinel-1 land vs water RF

```python
s1 = (
    ee.ImageCollection('COPERNICUS/S1_GRD')
    .filterBounds(region)
    .filterDate(start, end)
    .filter(ee.Filter.eq('instrumentMode', 'IW'))
    .filter(ee.Filter.eq('orbitProperties_pass', 'DESCENDING'))
    .filter(ee.Filter.eq('resolution_meters', 10))
    .select(['VV', 'VH'])
    .mean()
)

worldcover = ee.Image('ESA/WorldCover/v200/2021').clip(region)
water = worldcover.eq(80)
land = worldcover.eq(10).Or(worldcover.eq(30)).Or(worldcover.eq(50))

classifier = ee.Classifier.smileRandomForest(numberOfTrees=50).train(
    features=training_fc,  # sampleRegions on water/land stratifiedSample, class 1/0
    classProperty='class',
    inputProperties=['VV', 'VH'],
)

s1_speckle = s1.focal_mean(radius=50, units='meters')
vv_vh_ratio = s1.select('VV').subtract(s1.select('VH')).rename('VVVH_ratio')
```

Accuracy: `randomColumn`, train `< 0.7`, `errorMatrix`. Feature space: client-side VV/VH scatter as in the lecture notebook.

Sampling defaults from Lecture 6: 200 water + 200 land points, `scale=10`.

## Lab 2 — Sentinel-2 QA60 composite + pixel RF

```python
def mask_s2_clouds(image):
    qa = image.select('QA60')
    cloudBitMask = 1 << 10
    cirrusBitMask = 1 << 11
    mask = qa.bitwiseAnd(cloudBitMask).eq(0).And(
        qa.bitwiseAnd(cirrusBitMask).eq(0))
    return image.updateMask(mask).divide(10000).select(['B2', 'B3', 'B4', 'B8'])

s2 = (
    ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
    .filterBounds(aoi)
    .filterDate(start, end)
    .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 10))
    .map(mask_s2_clouds)
    .median()
    .clip(aoi)
)

bands = ['B2', 'B3', 'B4', 'B8']
classifier = ee.Classifier.smileRandomForest(numberOfTrees=100).train(
    features=train,
    classProperty='Map_remapped',
    inputProperties=bands,
)
classified = s2.select(bands).classify(classifier)
```

RGB vis: `{'bands': ['B4', 'B3', 'B2'], 'min': 0, 'max': 0.3}` after `/10000`.

Indices from `Lab-Notebooks/helpers/lab2_helpers.py`:

```python
ndwi = img.normalizedDifference(['B3', 'B8']).rename('NDWI')  # McFeeters
ndvi = img.normalizedDifference(['B8', 'B4']).rename('NDVI')
```

`get_sentinel2` in the same helper: QA60 mask, `CLOUDY_PIXEL_PERCENTAGE < 10`, median, clip, select bands.

Lab 2 also trains `ee.Classifier.libsvm(kernelType='RBF', gamma=0.5, cost=10)` as a comparison — RF is the default for this project.

## Lab 3 — s2cloudless composite

Google tutorial basis: Sentinel-2 Cloud Masking with s2cloudless.

Default control variables in the lab (then Auckland summer tune):

| Param | Lab default | Tuned (Auckland Jan–Mar) |
|---|---|---|
| `CLOUD_FILTER` | 60 | 60 |
| `CLD_PRB_THRESH` | 40 | 30 |
| `NIR_DRK_THRESH` | 0.15 | 0.1 |
| `CLD_PRJ_DIST` | 2 | 1.5 |
| `BUFFER` | 50 | 50 |

```python
def get_s2_sr_cld_col(aoi, start_date, end_date):
    s2_sr_col = (
        ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lte('CLOUDY_PIXEL_PERCENTAGE', CLOUD_FILTER))
    )
    s2_cloudless_col = (
        ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY')
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
    )
    return ee.ImageCollection(ee.Join.saveFirst('s2cloudless').apply(**{
        'primary': s2_sr_col,
        'secondary': s2_cloudless_col,
        'condition': ee.Filter.equals(**{
            'leftField': 'system:index',
            'rightField': 'system:index',
        }),
    }))
```

`add_cloud_bands`: `probability` from joined s2cloudless; `clouds = probability.gt(CLD_PRB_THRESH)`.

`add_shadow_bands`: SCL not water (`SCL != 6`); dark NIR `B8 < NIR_DRK_THRESH * 1e4`; project shadows with `MEAN_SOLAR_AZIMUTH_ANGLE` and `directionalDistanceTransform`.

`add_cld_shdw_mask`: clouds OR shadows, `focalMin(2).focalMax(BUFFER*2/20)` at 20 m.

Apply mask then **median** for the cloud-free composite (Lab 3 main path). Use `.mosaic()` only when you need to preserve cloud-mask layers (k-means exercise).

RGB vis on unscaled SR: `{'bands': ['B4', 'B3', 'B2'], 'min': 0, 'max': 2500, 'gamma': 1.1}`.

## Lab 5 — optional U-Net (GeoAI)

Not the default flood path. If used:

- Dataset: 6-band Sentinel-2 (Blue, Green, Red, NIR, SWIR1, SWIR2) + water masks.
- Tiles: `geoai.export_geotiff_tiles_batch(..., tile_size=512, stride=128)`.
- Train: `architecture="unet"`, `encoder_name="resnet34"`, `encoder_weights="imagenet"`, `num_channels=6`, `num_classes=2`, `batch_size=32`, `learning_rate=0.001`. Lab used `num_epochs=3` only to save time.
- Infer: `geoai.semantic_segmentation_batch(..., window_size=512, overlap=256)`.

## Fusion (project)

```text
s1_water      = S1 RF class == 1
s2_water      = NDWI > t  OR  S2 RF/U-Net water
post_water    = s1_water OR s2_water          # or AND if user wants conservative
permanent     = WorldCover == 80
low_flat      = (dem < z_max) AND (slope < s_max)
inundation    = post_water AND NOT pre_water AND NOT permanent AND low_flat
```

`z_max` / `s_max` / NDWI threshold `t` are project choices: state them in the report. Classifier training, speckle, and cloud masking are not.
