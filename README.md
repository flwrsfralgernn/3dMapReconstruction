# Map Reconstruction MVP

This repository contains a small standalone Python utility that downloads raster map tiles, centers them on a latitude/longitude coordinate, and writes an exact-size PNG image.

This MVP intentionally does not implement 3D reconstruction, Mapillary imagery, satellite imagery, OSM object extraction, CARLA, Isaac Sim, databases, a front end, or an HTTP endpoint. There was no existing API application in the repository, so the reusable Python function and CLI are the supported interfaces.

## Install

Python 3.10 or newer is required. A recent Python 3.14 installation is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

The runtime dependencies are Pillow and requests. The development extra installs pytest.

## Python API

```python
from generate_map import generate_map_image

image = generate_map_image(
    latitude=33.6438,
    longitude=-117.8443,
    width=1024,
    height=1024,
    output_path="map.png",
)
image.close()
```

The function always returns a fully loaded RGB Pillow image. `output_path`, when supplied, must use the `.png` extension. Its parent directory is created when necessary.

The map is generated at a fixed internal zoom of 16. Width and height specify output pixels, not a geographic radius. The requested coordinate is kept at fractional Web Mercator precision and placed at `(width // 2, height // 2)` in the output.

## CLI

```bash
python generate_map.py \
  --lat 33.6438 \
  --lon -117.8443 \
  --width 1024 \
  --height 1024 \
  --output map.png
```

The CLI requires `--output` and prints the saved path on success. Optional configuration flags are `--tile-url`, `--user-agent`, and `--cache-dir`.

## Validation and configuration

Inputs are rejected with user-readable `ValueError`s when:

- latitude or longitude is non-numeric, boolean, NaN, or infinite;
- latitude is outside the Web Mercator range of approximately ±85.05112878°;
- longitude is outside [-180°, 180°]. Longitude 180° is normalized to -180°;
- width or height is non-positive, non-integer, or boolean;
- either dimension exceeds 4096 pixels; or
- total output pixels exceed 16,777,216.

The default tile URL is:

```text
https://tile.openstreetmap.org/{z}/{x}/{y}.png
```

The default User-Agent identifies this application. The API accepts `tile_url`, `user_agent`, and `cache_dir` keyword arguments. Process-level defaults can also be set before import with:

```text
MAP_RECONSTRUCTION_TILE_URL
MAP_RECONSTRUCTION_USER_AGENT
MAP_RECONSTRUCTION_CACHE_DIR
```

The default cache is `~/.cache/map-reconstruction-mvp`. Pass `cache_dir=None` to disable caching for a particular API call.

## Tile provider and attribution

Generated images include visible `© OpenStreetMap contributors` attribution. Applications displaying these images must retain the attribution and comply with the selected tile provider's terms.

The default provider is OpenStreetMap's standard tile service. Read the [OpenStreetMap Tile Usage Policy](https://operations.osmfoundation.org/policies/tiles/) and [copyright and attribution guidance](https://www.openstreetmap.org/copyright) before using it. Changing `tile_url` may require different attribution and compliance with a different provider's terms. Bulk or heavy production use should use a suitable commercial/provider-hosted service or self-hosted tiles rather than relying on the standard public tile server.

## Tests

All automated tests run offline with synthetic tiles and mocked HTTP responses:

```bash
pytest
```

The test suite covers validation, fractional centering, tile boundaries, antimeridian wrapping, cache reuse and repair, download errors, exact output dimensions, PNG output, marker placement, and attribution visibility.
