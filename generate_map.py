"""Low-level map configuration, validation, coordinate, and tile helpers."""

from __future__ import annotations

import argparse
import io
import math
import os
from numbers import Number
from pathlib import Path
import sys
import tempfile

import requests
from PIL import Image, ImageDraw, ImageFont

# The MVP uses a fixed zoom level; output dimensions determine the requested
# pixel extent rather than a geographic radius.
DEFAULT_ZOOM = 16
TILE_SIZE = 256


_DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_DEFAULT_USER_AGENT = "map-reconstruction-mvp/0.1 (map image generator)"
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "map-reconstruction-mvp"

DEFAULT_TILE_URL = os.environ.get("MAP_RECONSTRUCTION_TILE_URL", _DEFAULT_TILE_URL)
DEFAULT_USER_AGENT = os.environ.get(
    "MAP_RECONSTRUCTION_USER_AGENT", _DEFAULT_USER_AGENT
)
DEFAULT_CACHE_DIR = Path(
    os.environ.get("MAP_RECONSTRUCTION_CACHE_DIR", str(_DEFAULT_CACHE_DIR))
)
DEFAULT_TILE_TIMEOUT = 10.0
ATTRIBUTION_TEXT = "© OpenStreetMap contributors"


# Safety limits for generated images.
MAX_IMAGE_DIMENSION = 4096
MAX_TOTAL_PIXELS = 16_777_216


# Geographic limits supported by raster Web Mercator and the tile provider's
# longitude range. The poles themselves cannot be represented in Web Mercator.
WEB_MERCATOR_MAX_LATITUDE = 85.0511287798066
MIN_LONGITUDE = -180.0
MAX_LONGITUDE = 180.0


class TileDownloadError(RuntimeError):
    """Raised when a requested tile cannot be read, downloaded, or decoded."""

    def __init__(self, zoom: int, tile_x: int, tile_y: int, reason: object):
        self.zoom = zoom
        self.tile_x = tile_x
        self.tile_y = tile_y
        self.x = tile_x
        self.y = tile_y
        self.reason = str(reason)
        super().__init__(
            f"failed to load tile z={zoom}, x={tile_x}, y={tile_y}: "
            f"{self.reason}"
        )


def _finite_real(value: object, name: str) -> float:
    """Return *value* as a finite real number or raise ``ValueError``."""

    if isinstance(value, bool) or not isinstance(value, Number):
        raise ValueError(f"{name} must be a finite real number")

    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite real number") from error

    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite real number")
    return number


def validate_coordinates(latitude: object, longitude: object) -> tuple[float, float]:
    """Validate coordinates and normalize ``180`` longitude to ``-180``.

    Raster Web Mercator cannot represent either pole, so latitude is limited
    to the standard Web Mercator range. The two endpoints of the longitude
    range describe the same antimeridian position; returning ``-180`` for
    ``180`` gives later tile calculations one canonical representation.
    """

    latitude_number = _finite_real(latitude, "latitude")
    if not -WEB_MERCATOR_MAX_LATITUDE <= latitude_number <= (
        WEB_MERCATOR_MAX_LATITUDE
    ):
        raise ValueError(
            "latitude must be between "
            f"-{WEB_MERCATOR_MAX_LATITUDE} and {WEB_MERCATOR_MAX_LATITUDE} "
            "degrees; raster Web Mercator cannot represent the poles"
        )

    longitude_number = _finite_real(longitude, "longitude")
    if not MIN_LONGITUDE <= longitude_number <= MAX_LONGITUDE:
        raise ValueError(
            f"longitude must be between {MIN_LONGITUDE:g} and "
            f"{MAX_LONGITUDE:g} degrees"
        )
    if longitude_number == MAX_LONGITUDE:
        longitude_number = MIN_LONGITUDE

    return latitude_number, longitude_number


def validate_dimensions(width: object, height: object) -> tuple[int, int]:
    """Validate output dimensions against type, size, and pixel limits."""

    dimensions: list[int] = []
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
        if value > MAX_IMAGE_DIMENSION:
            raise ValueError(
                f"{name} must not exceed {MAX_IMAGE_DIMENSION} pixels"
            )
        dimensions.append(value)

    width_number, height_number = dimensions
    total_pixels = width_number * height_number
    if total_pixels > MAX_TOTAL_PIXELS:
        raise ValueError(
            "width * height must not exceed "
            f"{MAX_TOTAL_PIXELS} pixels (got {total_pixels})"
        )

    return width_number, height_number


def validate_map_inputs(
    latitude: object,
    longitude: object,
    width: object,
    height: object,
) -> tuple[float, float, int, int]:
    """Validate all map inputs and return normalized values.

    This helper is intentionally independent of networking and rendering so it
    can be shared by the Python API and command-line interface.
    """

    latitude_number, longitude_number = validate_coordinates(latitude, longitude)
    width_number, height_number = validate_dimensions(width, height)
    return latitude_number, longitude_number, width_number, height_number


def lat_lon_to_global_pixel(
    latitude: object,
    longitude: object,
    zoom: int = DEFAULT_ZOOM,
) -> tuple[float, float]:
    """Convert latitude/longitude to fractional global Web Mercator pixels.

    The returned coordinates are deliberately not rounded to a tile or source
    pixel. ``zoom`` defaults to the MVP's fixed zoom level but is accepted as
    an argument so this pure coordinate helper can be tested independently.
    """

    latitude_number, longitude_number = validate_coordinates(latitude, longitude)
    world_size = TILE_SIZE * (2**zoom)
    latitude_radians = math.radians(latitude_number)

    global_x = (longitude_number + 180.0) / 360.0 * world_size
    global_y = (
        1.0 - math.asinh(math.tan(latitude_radians)) / math.pi
    ) / 2.0 * world_size
    return float(global_x), float(global_y)


def source_crop_origin(
    global_x: object,
    global_y: object,
    width: object,
    height: object,
) -> tuple[float, float]:
    """Return the floating-point global-pixel origin for a centered crop.

    The requested global coordinate maps to exactly ``(width // 2,
    height // 2)`` in the output coordinate system, including when either
    dimension is even.
    """

    global_x_number = _finite_real(global_x, "global_x")
    global_y_number = _finite_real(global_y, "global_y")
    width_number, height_number = validate_dimensions(width, height)
    return (
        global_x_number - width_number // 2,
        global_y_number - height_number // 2,
    )


def centered_crop_bounds(
    latitude: object,
    longitude: object,
    width: object,
    height: object,
    zoom: int = DEFAULT_ZOOM,
) -> tuple[float, float, float, float]:
    """Return ``(left, top, right, bottom)`` floating-point crop bounds."""

    global_x, global_y = lat_lon_to_global_pixel(latitude, longitude, zoom)
    left, top = source_crop_origin(global_x, global_y, width, height)
    width_number, height_number = validate_dimensions(width, height)
    return left, top, left + width_number, top + height_number


def _validate_zoom(zoom: object) -> int:
    if isinstance(zoom, bool) or not isinstance(zoom, int) or zoom < 0:
        raise ValueError("zoom must be a non-negative integer")
    return zoom


def _canonical_tile_coordinates(
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> tuple[int, int]:
    """Wrap X and clamp Y to valid tile coordinates for a zoom level."""

    tile_count = 2**zoom
    return tile_x % tile_count, min(max(tile_y, 0), tile_count - 1)


def tile_ranges_for_bounds(
    left: object,
    top: object,
    right: object,
    bottom: object,
    zoom: int = DEFAULT_ZOOM,
) -> tuple[range, range]:
    """Return unwrapped tile-index ranges intersecting crop bounds.

    X ranges intentionally remain unwrapped so a crop crossing the
    antimeridian can be placed continuously in a temporary mosaic. X is
    canonicalized when tiles are requested. Y is also kept in its unwrapped
    position for mosaic placement, while its requested tile coordinate is
    clamped to the valid Web Mercator edge by :func:`load_tile`.
    """

    zoom_number = _validate_zoom(zoom)
    left_number = _finite_real(left, "left")
    top_number = _finite_real(top, "top")
    right_number = _finite_real(right, "right")
    bottom_number = _finite_real(bottom, "bottom")
    if right_number <= left_number:
        raise ValueError("right crop bound must be greater than left crop bound")
    if bottom_number <= top_number:
        raise ValueError("bottom crop bound must be greater than top crop bound")

    first_x = math.floor(left_number / TILE_SIZE)
    last_x = math.ceil(right_number / TILE_SIZE) - 1
    first_y = math.floor(top_number / TILE_SIZE)
    last_y = math.ceil(bottom_number / TILE_SIZE) - 1

    # A valid latitude-centered crop intersects the world vertically. Keep
    # this guard explicit so an impossible crop does not produce an empty
    # tile request after edge clamping.
    tile_count = 2**zoom_number
    if last_y < 0 or first_y >= tile_count:
        raise ValueError("crop does not intersect the Web Mercator tile range")

    return range(first_x, last_x + 1), range(first_y, last_y + 1)


def _cache_path(
    cache_dir: str | Path,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> Path:
    return Path(cache_dir) / str(zoom) / str(tile_x) / f"{tile_y}.png"


def _read_cached_tile(
    cache_dir: str | Path | None,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> Image.Image | None:
    if cache_dir is None:
        return None

    path = _cache_path(cache_dir, zoom, tile_x, tile_y)
    if not path.is_file():
        return None

    try:
        with Image.open(path) as cached:
            cached.load()
            if cached.size != (TILE_SIZE, TILE_SIZE):
                return None
            return cached.convert("RGB")
    except (OSError, ValueError, SyntaxError):
        # Corrupt or incompatible cached data is replaced by a fresh fetch.
        return None


def _write_cached_tile(
    tile: Image.Image,
    cache_dir: str | Path | None,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> None:
    if cache_dir is None:
        return

    path = _cache_path(cache_dir, zoom, tile_x, tile_y)
    temporary_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{tile_y}.",
            suffix=".tmp",
            dir=path.parent,
        )
        os.close(file_descriptor)
        tile.save(temporary_path, format="PNG")
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, ValueError) as error:
        raise TileDownloadError(
            zoom,
            tile_x,
            tile_y,
            f"could not write cache: {error}",
        ) from error
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def _decode_tile_bytes(
    payload: bytes,
    zoom: int,
    tile_x: int,
    tile_y: int,
) -> Image.Image:
    try:
        with Image.open(io.BytesIO(payload)) as downloaded:
            downloaded.load()
            if downloaded.size != (TILE_SIZE, TILE_SIZE):
                raise ValueError(
                    f"expected {TILE_SIZE}x{TILE_SIZE} pixels, "
                    f"got {downloaded.width}x{downloaded.height}"
                )
            return downloaded.convert("RGB")
    except (OSError, TypeError, ValueError, SyntaxError) as error:
        raise TileDownloadError(
            zoom,
            tile_x,
            tile_y,
            f"could not decode image response: {error}",
        ) from error


def _download_tile(
    zoom: int,
    tile_x: int,
    tile_y: int,
    *,
    tile_url: str,
    user_agent: str,
    timeout: float,
    session: object | None = None,
) -> Image.Image:
    try:
        url = tile_url.format(z=zoom, x=tile_x, y=tile_y)
    except (KeyError, IndexError, ValueError) as error:
        raise TileDownloadError(
            zoom,
            tile_x,
            tile_y,
            f"invalid tile URL template: {error}",
        ) from error

    client = session if session is not None else requests
    response = None
    try:
        response = client.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.content
    except Exception as error:
        raise TileDownloadError(zoom, tile_x, tile_y, error) from error
    finally:
        if response is not None:
            response.close()

    return _decode_tile_bytes(payload, zoom, tile_x, tile_y)


def load_tile(
    zoom: int,
    tile_x: int,
    tile_y: int,
    *,
    tile_url: str = DEFAULT_TILE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
    timeout: float = DEFAULT_TILE_TIMEOUT,
    session: object | None = None,
) -> Image.Image:
    """Load one RGB tile from cache or the configured HTTP tile service."""

    zoom_number = _validate_zoom(zoom)
    canonical_x, canonical_y = _canonical_tile_coordinates(
        zoom_number,
        int(tile_x),
        int(tile_y),
    )
    cached = _read_cached_tile(cache_dir, zoom_number, canonical_x, canonical_y)
    if cached is not None:
        return cached

    tile = _download_tile(
        zoom_number,
        canonical_x,
        canonical_y,
        tile_url=tile_url,
        user_agent=user_agent,
        timeout=timeout,
        session=session,
    )
    try:
        _write_cached_tile(
            tile,
            cache_dir,
            zoom_number,
            canonical_x,
            canonical_y,
        )
    except Exception:
        tile.close()
        raise
    return tile


def render_tile_crop(
    crop_bounds: tuple[float, float, float, float],
    width: int,
    height: int,
    *,
    zoom: int = DEFAULT_ZOOM,
    tile_url: str = DEFAULT_TILE_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_dir: str | Path | None = DEFAULT_CACHE_DIR,
    timeout: float = DEFAULT_TILE_TIMEOUT,
    session: object | None = None,
) -> Image.Image:
    """Stitch intersecting tiles and return an exact-size RGB crop.

    The crop bounds remain fractional throughout. Pillow performs one extent
    transform against the temporary mosaic, avoiding integer-rounded source
    crops and preserving the one-global-pixel-per-output-pixel map scale.
    """

    width_number, height_number = validate_dimensions(width, height)
    left, top, right, bottom = crop_bounds
    x_range, y_range = tile_ranges_for_bounds(left, top, right, bottom, zoom)
    x_start = x_range.start
    y_start = y_range.start
    mosaic_width = len(x_range) * TILE_SIZE
    mosaic_height = len(y_range) * TILE_SIZE
    mosaic = Image.new("RGB", (mosaic_width, mosaic_height))
    loaded_tiles: dict[tuple[int, int], Image.Image] = {}

    try:
        for unwrapped_x in x_range:
            for unwrapped_y in y_range:
                canonical_x, canonical_y = _canonical_tile_coordinates(
                    _validate_zoom(zoom),
                    unwrapped_x,
                    unwrapped_y,
                )
                key = (canonical_x, canonical_y)
                tile = loaded_tiles.get(key)
                if tile is None:
                    tile = load_tile(
                        zoom,
                        canonical_x,
                        canonical_y,
                        tile_url=tile_url,
                        user_agent=user_agent,
                        cache_dir=cache_dir,
                        timeout=timeout,
                        session=session,
                    )
                    loaded_tiles[key] = tile

                mosaic.paste(
                    tile,
                    (
                        (unwrapped_x - x_start) * TILE_SIZE,
                        (unwrapped_y - y_start) * TILE_SIZE,
                    ),
                )

        source_bounds = (
            float(left) - x_start * TILE_SIZE,
            float(top) - y_start * TILE_SIZE,
            float(right) - x_start * TILE_SIZE,
            float(bottom) - y_start * TILE_SIZE,
        )
        result = mosaic.transform(
            (width_number, height_number),
            Image.Transform.EXTENT,
            source_bounds,
            resample=Image.Resampling.BILINEAR,
        )
        result.load()
        return result
    finally:
        for tile in loaded_tiles.values():
            tile.close()
        mosaic.close()


def draw_marker(image: Image.Image) -> None:
    """Draw a high-contrast circular marker at the documented image center."""

    draw = ImageDraw.Draw(image)
    center_x = image.width // 2
    center_y = image.height // 2
    radius = max(2, min(8, max(1, min(image.width, image.height) // 16)))

    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=(255, 255, 255),
        outline=(0, 0, 0),
        width=1,
    )
    inner_radius = max(1, radius - 2)
    draw.ellipse(
        (
            center_x - inner_radius,
            center_y - inner_radius,
            center_x + inner_radius,
            center_y + inner_radius,
        ),
        fill=(220, 30, 38),
        outline=(120, 0, 0),
        width=1,
    )


def draw_attribution(image: Image.Image) -> None:
    """Draw a readable attribution label in the lower-left corner."""

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    text_box = draw.textbbox((0, 0), ATTRIBUTION_TEXT, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    padding = 3
    margin = 4
    box_width = text_width + 2 * padding
    box_height = text_height + 2 * padding
    left = min(margin, max(0, image.width - 1))
    top = max(0, image.height - margin - box_height)
    right = min(image.width - 1, left + box_width)
    bottom = min(image.height - 1, top + box_height)

    if right < left or bottom < top:
        return

    draw.rectangle((left, top, right, bottom), fill=(255, 255, 255))
    draw.text(
        (left + padding - text_box[0], top + padding - text_box[1]),
        ATTRIBUTION_TEXT,
        fill=(0, 0, 0),
        font=font,
    )


def _save_png(image: Image.Image, output_path: str | Path) -> Path:
    path = Path(output_path)
    if path.suffix.lower() != ".png":
        raise ValueError("output_path must use the .png extension")
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path


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
    """Generate an exact-size, centered RGB map image.

    The fixed MVP zoom is used internally. If ``output_path`` is provided,
    the same final image is also written as a PNG.
    """

    latitude_number, longitude_number, width_number, height_number = (
        validate_map_inputs(latitude, longitude, width, height)
    )
    bounds = centered_crop_bounds(
        latitude_number,
        longitude_number,
        width_number,
        height_number,
    )
    image = render_tile_crop(
        bounds,
        width_number,
        height_number,
        tile_url=tile_url,
        user_agent=user_agent,
        cache_dir=cache_dir,
    )
    try:
        draw_marker(image)
        draw_attribution(image)
        image.load()
        if output_path is not None:
            _save_png(image, output_path)
        return image
    except Exception:
        image.close()
        raise


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a centered OpenStreetMap tile image."
    )
    parser.add_argument("--lat", required=True, type=float, help="latitude in degrees")
    parser.add_argument(
        "--lon", required=True, type=float, help="longitude in degrees"
    )
    parser.add_argument("--width", required=True, type=int, help="output width in pixels")
    parser.add_argument(
        "--height", required=True, type=int, help="output height in pixels"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="PNG output path",
    )
    parser.add_argument(
        "--tile-url",
        default=DEFAULT_TILE_URL,
        help="tile URL template (default: %(default)s)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP User-Agent (default: %(default)s)",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="tile cache directory (default: %(default)s)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface and return a process exit code."""

    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    try:
        image = generate_map_image(
            args.lat,
            args.lon,
            args.width,
            args.height,
            args.output,
            tile_url=args.tile_url,
            user_agent=args.user_agent,
            cache_dir=args.cache_dir,
        )
        image.close()
    except (OSError, TileDownloadError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print(args.output)
    return 0


__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_TILE_URL",
    "DEFAULT_TILE_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "DEFAULT_ZOOM",
    "ATTRIBUTION_TEXT",
    "MAX_IMAGE_DIMENSION",
    "MAX_TOTAL_PIXELS",
    "MAX_LONGITUDE",
    "MIN_LONGITUDE",
    "TILE_SIZE",
    "WEB_MERCATOR_MAX_LATITUDE",
    "centered_crop_bounds",
    "draw_attribution",
    "draw_marker",
    "generate_map_image",
    "lat_lon_to_global_pixel",
    "load_tile",
    "render_tile_crop",
    "source_crop_origin",
    "tile_ranges_for_bounds",
    "TileDownloadError",
    "validate_coordinates",
    "validate_dimensions",
    "validate_map_inputs",
]


if __name__ == "__main__":
    raise SystemExit(main())
