import numpy as np
import pytest
from pyproj import CRS, Transformer, network
from shapely.geometry import (GeometryCollection, LineString, MultiLineString,
                              MultiPoint, MultiPolygon, Point, Polygon, box)
from shapely.ops import transform

from app.measurements import crs_label, measure_geometry, parse_source_crs, select_utm
from app import measurements
from pyproj.exceptions import ProjError


UTM = CRS.from_epsg(32632)
SQUARE = box(500_000, 5_500_000, 500_100, 5_500_100)
LINE = LineString([(500_000, 5_500_000), (500_300, 5_500_400)])


@pytest.mark.parametrize("geometry,value,unit", [(SQUARE, 10_000, "m2"), (LINE, 500, "m")])
@pytest.mark.parametrize("epsg", [32632, 4326, 3857])
def test_analytical_shapes_across_crs(geometry, value, unit, epsg):
    source = CRS.from_epsg(epsg)
    converted = transform(Transformer.from_crs(UTM, source, always_xy=True).transform, geometry)
    result = measure_geometry(converted, source)
    assert result.status == "measured", result.reason
    assert result.measurement.value == pytest.approx(value, abs=1e-6, rel=0 if epsg == 32632 else 1e-6)
    assert result.measurement.crs == "EPSG:32632"
    assert result.measurement.unit == unit
    assert result.measurement.method == "utm"
    assert result.reason is None


@pytest.mark.parametrize("geometry,expected", [
    (Polygon(SQUARE.exterior.coords, [box(500_020, 5_500_020, 500_080, 5_500_080).exterior.coords]), 6400),
    (MultiPolygon([SQUARE, box(500_200, 5_500_000, 500_300, 5_500_100)]), 20_000),
    (MultiLineString([LINE, [(500_400, 5_500_000), (500_400, 5_500_100)]]), 600),
])
def test_holes_and_multipart_sum(geometry, expected):
    result = measure_geometry(geometry, UTM)
    assert result.measurement.value == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("geometry,status", [
    (Point(9, 49), "skipped"),
    (MultiPoint([(9, 49), (9.1, 49.1)]), "unsupported"),
    (GeometryCollection([Point(9, 49), LineString([(9, 49), (9.1, 49.1)])]), "unsupported"),
    (None, "error"), (Point(), "error"), (Polygon(), "error"),
    (Polygon([(9, 49), (9.1, 49.1), (9.1, 49), (9, 49.1), (9, 49)]), "error"),
    (Point(np.inf, 49), "error"), (LineString([(9, 49), (np.nan, 49)]), "error"),
    (Point(181, 49), "error"), (Point(9, 91), "error"),
    (box(5.9, 49, 6.1, 49.1), "error"), (box(9, -0.1, 9.1, 0.1), "error"),
    (LineString([(179, 10), (-179, 10)]), "error"),
    (box(9, 84.1, 9.1, 84.2), "error"), (box(9, -81, 9.1, -80.9), "error"),
    (box(7, 20, 8, 27), "error"),
])
def test_explicit_outcomes(geometry, status):
    result = measure_geometry(geometry, CRS.from_epsg(4326))
    assert result.status == status
    assert result.measurement is None
    assert result.reason
    assert result.geometry_type == (geometry.geom_type if geometry is not None else None)


def test_southern_hemisphere_and_altitude():
    source = CRS.from_epsg(32732)
    square = box(500_000, 6_500_000, 500_100, 6_500_100)
    result = measure_geometry(square, source)
    assert result.measurement.crs == "EPSG:32732"
    assert result.measurement.value == pytest.approx(10_000, abs=1e-6)
    line = LineString([(500_000, 5_500_000, 0), (500_300, 5_500_400, 900)])
    assert measure_geometry(line, UTM).measurement.value == pytest.approx(500, abs=1e-6)
    assert network.is_network_enabled() is False


@pytest.mark.parametrize("crs", [None, "nonsense", "EPSG:4978", "EPSG:7405"])
def test_invalid_source_crs(crs):
    with pytest.raises(ValueError):
        parse_source_crs(crs)


def test_valid_wkt_without_authority_and_deterministic_boundary():
    crs = parse_source_crs('+proj=tmerc +lat_0=0 +lon_0=9.123 +k=0.9997 +x_0=500000 +y_0=0 +datum=WGS84 +units=m +type=crs')
    assert crs.to_authority() is None
    assert crs_label(crs).startswith("PROJCRS[")
    geometry = transform(Transformer.from_crs(UTM, crs, always_xy=True).transform, SQUARE)
    assert measure_geometry(geometry, crs).measurement.value == pytest.approx(10_000, rel=1e-6)
    assert select_utm((6, 49, 6, 49)).to_epsg() == 32631


def test_projection_failure_is_explicit_but_programming_errors_surface(monkeypatch):
    def unavailable(*args):
        raise ProjError("missing required grid")
    monkeypatch.setattr(measurements, "reproject", unavailable)
    result = measure_geometry(SQUARE, UTM)
    assert result.status == "error" and "transformation failed" in result.reason
    assert result.measurement is None

    def programming_error(*args):
        raise TypeError("wrong implementation")
    monkeypatch.setattr(measurements, "reproject", programming_error)
    with pytest.raises(TypeError):
        measure_geometry(SQUARE, UTM)


def test_projected_coordinates_are_not_wrapped_into_the_domain():
    # Mercator eastings beyond its world longitude extent must not wrap back into [-180, 180].
    geometry = LineString([(21_000_000, 5_000_000), (21_000_100, 5_000_100)])
    result = measure_geometry(geometry, CRS.from_epsg(3857))
    assert result.status == "error"
    assert result.measurement is None
    assert "longitude/latitude bounds" in result.reason
