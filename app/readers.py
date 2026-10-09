from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import lzma
import stat
from typing import BinaryIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile
import zlib

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
import pyogrio
from pyogrio.errors import (CRSError, DataLayerError, DataSourceError, FeatureError,
                            FieldError, GeometryError)
from pyproj import CRS
import shapely
from shapely.geometry.base import BaseGeometry

from .measurements import parse_source_crs


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100
MAX_KML_LAYERS = 100
MAX_FEATURES = 1000
MAX_COORDINATES = 100_000
CHUNK_BYTES = 64 * 1024


class DatasetError(Exception):
    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass
class Feature:
    feature_id: int
    layer: str
    geometry: BaseGeometry | None
    properties: dict


@dataclass
class Dataset:
    source_crs: CRS
    features: list[Feature]


def safe_filename(filename: str | None) -> str:
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if char.isprintable() and char not in '<>:"|?*')
    return name.strip(" .") or "upload"


def copy_upload(source: BinaryIO, destination: Path):
    total = 0
    with destination.open("wb") as output:
        while chunk := source.read(CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise DatasetError("Upload exceeds the 10 MiB processing limit.", 413)
            output.write(chunk)
    if total == 0:
        raise DatasetError("Uploaded file is empty.")


def validate_kml(path: Path):
    try:
        tree = ElementTree.parse(path, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, LookupError, ValueError) as exc:
        raise DatasetError("KML must be valid XML without DTDs, entities or external references.") from exc
    root = tree.getroot()
    if root.tag not in {"kml", "{http://www.opengis.net/kml/2.2}kml",
                        "{http://earth.google.com/kml/2.0}kml", "{http://earth.google.com/kml/2.1}kml"}:
        raise DatasetError("XML root must be KML.")
    if any(element.tag.rsplit("}", 1)[-1] == "NetworkLink" for element in root.iter()):
        raise DatasetError("KML NetworkLink is unsupported; upload embedded geometry.")


def archive_destination(info) -> str:
    name = info.orig_filename
    parts = name.rstrip("/").split("/")
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
    if (not name or name.startswith("/") or "\\" in name
            or any(not part or part in {".", ".."} or part.endswith((".", " "))
                   or any(char in part for char in '\x00<>:"|?*')
                   or any(ord(char) < 32 for char in part)
                   or part.split(".", 1)[0].casefold() in reserved for part in parts)):
        raise DatasetError("ZIP contains an unsafe or ambiguous path.")
    mode = stat.S_IFMT(info.external_attr >> 16)
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or info.flag_bits & 1:
        raise DatasetError("ZIP symlinks, special files and encrypted entries are unsupported.")
    return "/".join(parts).casefold()


def extract_shapefile(path: Path, workspace: Path) -> Path:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise DatasetError("ZIP entry limit exceeded (100).", 413)
            destinations = {}
            for info in entries:
                key = archive_destination(info)
                if key in destinations:
                    raise DatasetError("ZIP contains duplicate destinations (case-insensitive).")
                destinations[key] = info
            if sum(info.file_size for info in entries) > MAX_ARCHIVE_BYTES:
                raise DatasetError("ZIP declared expansion exceeds 50 MiB.", 413)
            shapes = [key for key, info in destinations.items() if not info.is_dir() and key.endswith(".shp")]
            if len(shapes) != 1:
                raise DatasetError("ZIP must contain exactly one Shapefile dataset (.shp).")
            stem = str(PurePosixPath(shapes[0]).with_suffix(""))
            for extension in (".shx", ".dbf", ".prj"):
                if stem + extension not in destinations or destinations[stem + extension].is_dir():
                    raise DatasetError(f"Shapefile is missing matching {extension} sidecar in the same folder.")
            total = 0
            # Fixed names avoid platform-specific archive paths reaching GDAL.
            folder = workspace / "dataset"
            folder.mkdir()
            for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
                info = destinations.get(stem + extension)
                if info is None or info.is_dir():
                    continue
                destination = folder / ("dataset" + extension)
                if not destination.resolve().is_relative_to(workspace.resolve()):
                    raise DatasetError("ZIP destination escapes temporary workspace.")
                with archive.open(info) as source, destination.open("wb") as output:
                    while True:
                        try:
                            chunk = source.read(CHUNK_BYTES)
                        except (OSError, lzma.LZMAError) as exc:
                            raise DatasetError("ZIP compressed member is corrupt.") from exc
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise DatasetError("ZIP actual extraction exceeds 50 MiB.", 413)
                        output.write(chunk)
            return folder / "dataset.shp"
    except (BadZipFile, EOFError, zlib.error, NotImplementedError, UnicodeDecodeError) as exc:
        raise DatasetError("ZIP is corrupt or uses an unsupported compression method.") from exc


def read_dataset(path: Path, file_type: str, workspace: Path) -> Dataset:
    if file_type == ".kml":
        validate_kml(path)
        source_path = path
    else:
        source_path = extract_shapefile(path, workspace)
    try:
        layers = pyogrio.list_layers(source_path)
        if file_type == ".kml" and len(layers) > MAX_KML_LAYERS:
            raise DatasetError("KML layer limit exceeded (100).", 413)
        features = []
        coordinate_count = 0
        source_crs = None
        for layer_index, (layer_name, geometry_type) in enumerate(layers):
            if geometry_type is None:
                continue
            frame = pyogrio.read_dataframe(source_path, layer=layer_index,
                                           max_features=MAX_FEATURES - len(features) + 1,
                                           force_2d=True, use_arrow=False, on_invalid="warn")
            if len(frame) + len(features) > MAX_FEATURES:
                raise DatasetError("Feature limit exceeded (1,000 across all layers).", 413)
            try:
                layer_crs = parse_source_crs("EPSG:4326" if file_type == ".kml" else frame.crs)
            except ValueError as exc:
                raise DatasetError(str(exc)) from exc
            if source_crs is not None and source_crs != layer_crs:
                raise DatasetError("Dataset spatial layers use inconsistent source CRSs.")
            source_crs = layer_crs
            # iterrows can coerce missing geometries into NaN in mixed-dtype rows.
            properties_rows = frame.drop(columns=[frame.geometry.name]).to_dict("records")
            for geometry, properties in zip(frame.geometry, properties_rows):
                coordinate_count += int(shapely.get_num_coordinates(geometry))
                if coordinate_count > MAX_COORDINATES:
                    raise DatasetError("Coordinate limit exceeded (100,000 across all features).", 413)
                features.append(Feature(len(features), str(layer_name), geometry, properties))
        if not features:
            raise DatasetError("Dataset has no readable spatial features.")
        return Dataset(source_crs, features)
    except (CRSError, DataLayerError, DataSourceError, FeatureError, FieldError, GeometryError,
            UnicodeDecodeError) as exc:
        raise DatasetError("GIS reader could not read the dataset or its source CRS.") from exc
