# First MVP Implementation Plan

## Scope and repository findings

The repository is currently empty and is not initialized as a Git repository. Build a small standalone Python implementation using Pillow and requests. There is no existing API structure to extend, so this MVP will provide a reusable Python function and CLI only; do not add a web framework or HTTP endpoint.

Keep all 3D reconstruction, Mapillary, satellite imagery, OSM object extraction, CARLA, Isaac Sim, databases, and front-end work out of scope.

## Proposed file layout

```text
.
├── generate_map.py
├── pyproject.toml
├── README.md
└── tests/
    └── test_generate_map.py
```

`generate_map.py` will contain the reusable map-generation logic and an `argparse` CLI entry point. Keeping the implementation in one module is intentional for this MVP.

## Implementation steps

### 1. Project setup

- Add a minimal `pyproject.toml` declaring supported Python version and runtime dependencies:
  - `Pillow`
  - `requests`
- Add `pytest` as a development/test dependency.
- Avoid packaging machinery beyond what is needed to install dependencies and run tests.

### 2. Constants and configuration

Define clear module-level defaults:

- `DEFAULT_ZOOM = 16`
- `TILE_SIZE = 256`
- `DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"`
- A descriptive default User-Agent that identifies this application and can be overridden.
- A simple cache directory default, preferably configurable through a function argument and/or environment variable.
- A maximum dimension and maximum total pixel count, for example 4096 pixels per side and 16,777,216 total pixels.
- Web Mercator's supported latitude limit (`85.05112878...`) and longitude limits (`-180` through `180`).

Allow the tile URL template and User-Agent to be configured with optional keyword-only function arguments and CLI flags (or environment variables) while preserving the requested primary function signature.

### 3. Validation

Create a small validation helper used by both the function and CLI:

- Reject booleans, non-numeric values, NaN, and infinity for latitude/longitude.
- Reject latitudes outside the Web Mercator range. Explain that raster Web Mercator cannot represent the poles.
- Reject longitudes outside `[-180, 180]`; treat `180` as wrapping to the same map position as `-180`.
- Reject booleans/non-integers for dimensions.
- Reject zero or negative dimensions.
- Reject dimensions or total pixel counts above the configured safety limits.
- Raise `ValueError` with specific, user-readable messages. The CLI should surface these as concise argument errors and exit non-zero.

### 4. Coordinate conversion and exact centering

Implement Web Mercator conversion from latitude/longitude to fractional global pixel coordinates at `DEFAULT_ZOOM`:

```text
world_size = TILE_SIZE * 2**zoom
x = (longitude + 180) / 360 * world_size
y = (1 - asinh(tan(latitude_radians)) / pi) / 2 * world_size
```

Keep coordinates fractional; do not round the requested location to a tile or source pixel.

Use `(width // 2, height // 2)` as the documented output center pixel. Define the source crop origin as:

```text
left = global_x - width // 2
top  = global_y - height // 2
```

This makes the input coordinate transform back to exactly `(width // 2, height // 2)`, including for even image dimensions. Use a Pillow affine/extent transform with floating-point source bounds when extracting the final image, rather than integer-rounded `crop()`, so fractional source positions do not introduce a centering error.

Expose the coordinate conversion helper as an internal/testable function so tests can verify centering independently of marker appearance.

### 5. Determine, download, and cache only required tiles

- Derive inclusive tile ranges from the fractional crop bounds using floor/ceiling math.
- Fetch only tiles intersecting the requested output extent.
- Wrap tile X modulo `2**zoom` so requests crossing the antimeridian work.
- Clamp/reject impossible tile Y values at the Web Mercator edge; do not wrap vertically.
- Stitch downloaded 256x256 RGB tiles into a temporary mosaic positioned in global pixel space.
- Crop/resample once to the exact requested width and height without stretching, preserving the native map aspect ratio.
- Deduplicate wrapped tile downloads within one request.

Implement a basic filesystem cache keyed by zoom/X/Y (for example `<cache>/<z>/<x>/<y>.png`):

- Read a cached tile when present and decodable.
- Otherwise request it with a timeout and configured User-Agent, validate the HTTP response, decode it as an image, and store it atomically.
- If a cached tile is corrupt, ignore it and download a replacement.
- Report the failing tile coordinates and underlying HTTP/network/decode reason in a custom `TileDownloadError`.
- Do not add eviction, expiry, concurrency control, retries, or a cache database in this MVP.

### 6. Marker and attribution

- Draw a clearly visible marker after the final crop, centered at `(width // 2, height // 2)`. Use a high-contrast pin/circle with an outline so it remains visible on varied map backgrounds.
- Draw a compact `© OpenStreetMap contributors` attribution label in a readable corner of the generated image, with a contrasting background.
- Document that applications displaying the image must retain visible attribution and comply with the selected tile provider's usage policy.
- Make clear in the README that changing `tile_url` may require changing attribution and following a different provider's terms.

### 7. Public Python API and CLI

Provide:

```python
def generate_map_image(
    latitude: float,
    longitude: float,
    width: int,
    height: int,
    output_path: str | None = None,
    *,
    tile_url: str = DEFAULT_TILE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
) -> Image.Image:
    ...
```

Behavior:

- Always return a fully loaded Pillow `Image.Image` in RGB or RGBA mode.
- If `output_path` is supplied, create only its parent directory when appropriate and save the same final image as PNG (reject or clearly document non-PNG output behavior).
- Avoid hidden network work at import time.
- Close HTTP responses and intermediate image objects appropriately.

Add an `argparse` CLI supporting:

```bash
python generate_map.py \
  --lat 33.6438 \
  --lon -117.8443 \
  --width 1024 \
  --height 1024 \
  --output map.png
```

Include optional `--tile-url`, `--user-agent`, and `--cache-dir` flags. Require `--output` for CLI use so the command has an unambiguous result. Print the saved path on success and a concise error on failure.

Do not implement an HTTP API because the repository has no existing API structure. The reusable function makes adding one later straightforward.

## Test plan

All normal tests must run offline. Mock the HTTP session or tile-fetch helper and generate synthetic 256x256 tiles whose colors/patterns encode tile coordinates. Use a temporary directory for cache tests.

Add tests for:

1. Exact output dimensions for several odd, even, rectangular, and sub-tile image sizes.
2. Coordinate transformation: after applying the computed crop origin, the requested fractional global pixel position equals `(width // 2, height // 2)` within tight floating-point tolerance.
3. Invalid latitude values, including values beyond the Web Mercator limit, NaN, and infinity.
4. Invalid longitude values, including values below -180, above 180, NaN, and infinity.
5. Invalid dimensions: zero, negative, non-integer/bool, over per-side maximum, and over total-pixel maximum.
6. Coordinates exactly on tile boundaries, verifying the correct adjacent tiles are selected and the result remains centered and correctly sized.
7. Crops crossing tile boundaries in both axes, including a crop near the antimeridian to verify X wrapping.
8. Tile download failures produce a clear `TileDownloadError` containing zoom/X/Y and the original cause/status.
9. Cache reuse avoids a second network request; corrupt cached data is replaced.
10. Supplying `output_path` writes a valid PNG identical in dimensions to the returned image.
11. The marker is centered and attribution is present. Prefer testing small deterministic regions or helper calls rather than brittle whole-image snapshots.

Do not contact the real OpenStreetMap tile service from the automated test suite.

## Documentation plan

Create `README.md` with:

- MVP scope and non-goals.
- Python version and installation steps, ideally using a virtual environment and `pip install -e ".[dev]"` (matching the final `pyproject.toml`).
- The reusable Python example and CLI example.
- Validation limits and error behavior.
- Explanation that zoom is fixed internally at 16 and that requested width/height control output pixels, not a geographic radius.
- How tile URL, User-Agent, and cache location are configured.
- Default tile provider, visible `© OpenStreetMap contributors` attribution, and a link to the provider's current tile usage policy/copyright guidance.
- A note that bulk/heavy production use needs a suitable tile provider or self-hosted tiles.
- Test command (`pytest`).
- Explicit note that there is no HTTP endpoint in this MVP because no API application existed in the repository.

## Verification before handoff

Run:

```bash
python -m pytest
python generate_map.py --help
```

Also perform one opt-in manual CLI smoke test against the configured tile server (not as part of pytest), confirm the PNG dimensions with Pillow, visually inspect marker/attribution placement, and confirm a second run uses the cache. If network access is unavailable, report that the offline automated coverage passed and leave the live smoke test clearly identified as unrun.

## Definition of done

- The reusable function and CLI meet the requested inputs and produce an exact-size PNG.
- Fractional Web Mercator math places the requested coordinate at the documented center pixel without integer tile rounding.
- Only intersecting tiles are fetched, with basic cache reuse and actionable failures.
- Marker and OpenStreetMap attribution are visible.
- Offline tests cover all requested validation and boundary cases.
- README instructions are sufficient for a fresh checkout.
- No out-of-scope systems or frameworks are introduced.
