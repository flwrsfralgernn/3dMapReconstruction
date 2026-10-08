import io
import re
from pathlib import Path

import pytest
from PIL import Image

import generate_map as map_module


def tile_png(x: int, y: int, color: tuple[int, int, int] | None = None) -> bytes:
    if color is None:
        color = (x % 256, y % 256, 40)
    image = Image.new("RGB", (map_module.TILE_SIZE, map_module.TILE_SIZE), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} test failure")

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, color: tuple[int, int, int] | None = None):
        self.calls: list[tuple[str, dict[str, str], float]] = []
        self.color = color
        self.status_code: int | None = None

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> FakeResponse:
        self.calls.append((url, headers, timeout))
        match = re.search(r"/(\d+)/(\d+)/(\d+)\.png$", url)
        assert match is not None
        _, x, y = (int(value) for value in match.groups())
        if self.status_code is not None:
            return FakeResponse(b"not an image", status_code=self.status_code)
        return FakeResponse(tile_png(x, y, self.color))


def install_fake_requests(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    monkeypatch.setattr(map_module.requests, "get", session.get)


@pytest.mark.parametrize(
    ("width", "height"),
    [(1, 1), (2, 3), (255, 257), (256, 256), (1024, 768)],
)
def test_generate_map_returns_exact_dimensions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    width: int,
    height: int,
) -> None:
    session = FakeSession(color=(25, 25, 25))
    install_fake_requests(monkeypatch, session)

    image = map_module.generate_map_image(
        33.6438,
        -117.8443,
        width,
        height,
        cache_dir=tmp_path,
    )
    try:
        assert image.size == (width, height)
        assert image.mode == "RGB"
    finally:
        image.close()


@pytest.mark.parametrize("width,height", [(1, 1), (2, 4), (255, 256), (1024, 768)])
def test_fractional_coordinate_is_at_documented_center(width: int, height: int) -> None:
    global_x, global_y = map_module.lat_lon_to_global_pixel(33.6438, -117.8443)
    left, top = map_module.source_crop_origin(global_x, global_y, width, height)

    assert global_x - left == pytest.approx(width // 2, abs=1e-12)
    assert global_y - top == pytest.approx(height // 2, abs=1e-12)


@pytest.mark.parametrize("latitude", [85.05112878 + 0.01, -85.05112878 - 0.01, float("nan"), float("inf")])
def test_invalid_latitude_is_rejected(latitude: float) -> None:
    with pytest.raises(ValueError, match="latitude"):
        map_module.validate_map_inputs(latitude, 0, 100, 100)


@pytest.mark.parametrize("longitude", [-180.01, 180.01, float("nan"), float("inf")])
def test_invalid_longitude_is_rejected(longitude: float) -> None:
    with pytest.raises(ValueError, match="longitude"):
        map_module.validate_map_inputs(0, longitude, 100, 100)


def test_antimeridian_endpoints_are_equivalent() -> None:
    assert map_module.lat_lon_to_global_pixel(0, 180) == map_module.lat_lon_to_global_pixel(
        0, -180
    )


@pytest.mark.parametrize(
    ("width", "height"),
    [(0, 10), (-1, 10), (10, 0), (10, -1), (True, 10), (10, 2.5), (4097, 1)],
)
def test_invalid_dimensions_are_rejected(width: object, height: object) -> None:
    with pytest.raises(ValueError):
        map_module.validate_dimensions(width, height)


def test_total_pixel_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(map_module, "MAX_TOTAL_PIXELS", 100)
    with pytest.raises(ValueError, match=r"width \* height"):
        map_module.validate_dimensions(11, 10)


def test_tile_boundary_selection_and_size() -> None:
    tile_ranges = map_module.tile_ranges_for_bounds(256.0, 256.0, 512.0, 512.0)
    assert list(tile_ranges[0]) == [1]
    assert list(tile_ranges[1]) == [1]

    session = FakeSession()
    image = map_module.render_tile_crop(
        (255.5, 255.5, 512.5, 512.5),
        257,
        257,
        cache_dir=None,
        session=session,
        tile_url="https://tiles.test/{z}/{x}/{y}.png",
    )
    try:
        assert image.size == (257, 257)
        requested = {call[0] for call in session.calls}
        assert any("/0/" in url for url in requested)
        assert any("/1/" in url for url in requested)
        assert len(requested) == len(session.calls)
    finally:
        image.close()


def test_antimeridian_x_wrapping_deduplicates_requests() -> None:
    world_size = map_module.TILE_SIZE * 2**map_module.DEFAULT_ZOOM
    session = FakeSession()
    image = map_module.render_tile_crop(
        (world_size - 64.0, 255.5, world_size + 64.0, 511.5),
        128,
        256,
        cache_dir=None,
        session=session,
        tile_url="https://tiles.test/{z}/{x}/{y}.png",
    )
    try:
        requested = [call[0] for call in session.calls]
        assert any(f"/{2**map_module.DEFAULT_ZOOM - 1}/" in url for url in requested)
        assert any("/0/" in url for url in requested)
        assert len(requested) == len(set(requested))
    finally:
        image.close()


def test_tile_download_error_contains_coordinates_and_cause() -> None:
    session = FakeSession()
    session.status_code = 503
    with pytest.raises(map_module.TileDownloadError) as raised:
        map_module.load_tile(
            map_module.DEFAULT_ZOOM,
            -1,
            2,
            cache_dir=None,
            session=session,
            tile_url="https://tiles.test/{z}/{x}/{y}.png",
        )

    message = str(raised.value)
    assert "z=16" in message
    assert "x=65535" in message
    assert "y=2" in message
    assert "503" in message


def test_cache_reuse_and_corrupt_cache_replacement(tmp_path: Path) -> None:
    session = FakeSession()
    first = map_module.load_tile(
        map_module.DEFAULT_ZOOM,
        3,
        4,
        cache_dir=tmp_path,
        session=session,
        tile_url="https://tiles.test/{z}/{x}/{y}.png",
    )
    first.close()
    assert len(session.calls) == 1

    second = map_module.load_tile(
        map_module.DEFAULT_ZOOM,
        3,
        4,
        cache_dir=tmp_path,
        session=session,
        tile_url="https://tiles.test/{z}/{x}/{y}.png",
    )
    second.close()
    assert len(session.calls) == 1

    cache_path = tmp_path / str(map_module.DEFAULT_ZOOM) / "3" / "4.png"
    cache_path.write_bytes(b"corrupt cached image")
    replacement = map_module.load_tile(
        map_module.DEFAULT_ZOOM,
        3,
        4,
        cache_dir=tmp_path,
        session=session,
        tile_url="https://tiles.test/{z}/{x}/{y}.png",
    )
    replacement.close()
    assert len(session.calls) == 2
    with Image.open(cache_path) as cached:
        assert cached.size == (map_module.TILE_SIZE, map_module.TILE_SIZE)


def test_output_path_writes_same_size_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = FakeSession(color=(25, 25, 25))
    install_fake_requests(monkeypatch, session)
    output_path = tmp_path / "nested" / "map.png"

    image = map_module.generate_map_image(
        0,
        0,
        320,
        240,
        output_path=str(output_path),
        cache_dir=tmp_path / "cache",
    )
    try:
        assert output_path.is_file()
        with Image.open(output_path) as saved:
            assert saved.format == "PNG"
            assert saved.size == image.size == (320, 240)
            saved.load()
            assert saved.tobytes() == image.tobytes()
    finally:
        image.close()


def test_marker_and_attribution_are_visible(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = FakeSession(color=(20, 20, 20))
    install_fake_requests(monkeypatch, session)

    image = map_module.generate_map_image(
        0,
        0,
        128,
        96,
        cache_dir=tmp_path,
    )
    try:
        assert image.getpixel((128 // 2, 96 // 2)) == (220, 30, 38)
        assert any(
            image.getpixel((x, y)) == (255, 255, 255)
            for y in range(96 - 24, 96)
            for x in range(0, 80)
        )
    finally:
        image.close()


def test_cli_requires_output() -> None:
    parser = map_module._build_cli_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--lat", "0", "--lon", "0", "--width", "1", "--height", "1"])
