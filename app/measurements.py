from functools import partial
from math import isfinite

import numpy as np
import shapely
from pyproj import CRS, Transformer, network
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from pyproj.exceptions import CRSError, ProjError
from shapely.errors import GEOSException
from shapely.ops import transform

from .schemas import Measurement, Outcome


# Processing must never download transformation grids or silently use a ballpark datum.
network.set_network_enabled(False)


class MeasurementDomainError(ValueError):
    pass


def parse_source_crs(value) -> CRS:
    if value is None:
        raise ValueError("A source CRS is required.")
    try:
        crs = CRS.from_user_input(value)
    except CRSError as exc:
        raise ValueError("Source CRS could not be parsed.") from exc
    if crs.is_compound or not (crs.is_geographic or crs.is_projected):
        raise ValueError("Source CRS must be geographic or projected; compound/geocentric CRS is unsupported.")
    return crs


def crs_label(crs: CRS) -> str:
    authority = crs.to_authority()
    return ":".join(authority) if authority else crs.to_wkt()


def reproject(geometry, source_crs: CRS, target_crs: CRS):
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True,
                                      allow_ballpark=False, only_best=True, force_over=True)
    return transform(partial(transformer.transform, errcheck=True), geometry)


def geometry_problem(geometry) -> str | None:
    if geometry is None:
        return "Geometry is missing or could not be decoded."
    if geometry.is_empty:
        return "Geometry is empty."
    if not np.isfinite(shapely.get_coordinates(geometry)).all():
        return "Geometry has nonfinite XY coordinates."
    if not geometry.is_valid:
        return f"Invalid geometry: {shapely.is_valid_reason(geometry)}"
    return None


def select_utm(bounds: tuple[float, float, float, float]) -> CRS:
    west, south, east, north = bounds
    if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise MeasurementDomainError("Geographic coordinates are outside longitude/latitude bounds.")
    if east - west > 6 or north - south > 6:
        raise MeasurementDomainError("Feature extent exceeds the regional six-degree limit or crosses the dateline.")
    candidates = query_utm_crs_info(datum_name="WGS 84",
                                   area_of_interest=AreaOfInterest(*bounds), contains=True)
    if not candidates:
        raise MeasurementDomainError("Feature does not fit a supported UTM area of use (zone, equator or polar boundary).")
    candidate = min(candidates, key=lambda item: int(item.code))
    crs = CRS.from_authority(candidate.auth_name, candidate.code)
    if len(crs.axis_info) != 2 or any(axis.unit_name != "metre" for axis in crs.axis_info):
        raise MeasurementDomainError("Selected measurement CRS does not use metre axes.")
    return crs


def measure_geometry(geometry, source_crs: CRS) -> Outcome:
    kind = geometry.geom_type if geometry is not None else None

    def error(reason):
        return Outcome(geometry_type=kind, status="error", reason=reason)

    try:
        problem = geometry_problem(geometry)
        if problem:
            return error(problem)
        geometry = shapely.force_2d(geometry)
        geographic = reproject(geometry, source_crs, CRS.from_epsg(4326))
        problem = geometry_problem(geographic)
        if problem:
            return error(f"After geographic transformation: {problem}")
        target = select_utm(geographic.bounds)
        if kind == "Point":
            return Outcome(geometry_type=kind, status="skipped",
                           reason="Points have no area or length measurement.")
        if kind not in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
            return Outcome(geometry_type=kind, status="unsupported",
                           reason=f"Geometry type {kind} is not supported for measurement.")
        projected = reproject(geometry, source_crs, target)
        problem = geometry_problem(projected)
        if problem:
            return error(f"After UTM transformation: {problem}")
        area = kind in {"Polygon", "MultiPolygon"}
        value = projected.area if area else projected.length
        if not isfinite(value) or value < 0:
            return error("Measurement is not a finite nonnegative value.")
        return Outcome(geometry_type=kind, status="measured", measurement=Measurement(
            kind="area" if area else "length", value=value, unit="m2" if area else "m",
            crs=crs_label(target),
        ))
    except (ProjError, GEOSException):
        return error("Geometry transformation failed; check coordinates and available datum transformations.")
    except MeasurementDomainError as exc:
        return error(str(exc))
