from io import BytesIO
from pathlib import Path
import stat
import struct
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_LZMA, ZipFile, ZipInfo

import pytest

from app import readers
from app.readers import DatasetError, copy_upload, extract_shapefile, read_dataset, validate_kml


def make_zip(path, entries):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in entries:
            if isinstance(name, str):
                info = ZipInfo(name)
                # Windows ZipInfo normalizes backslashes on creation; preserve hostile bytes.
                info.filename = info.orig_filename = name
                name = info
            archive.writestr(name, content)
    return path


def sample_entries(fixtures):
    with ZipFile(fixtures / "sample.zip") as archive:
        return [(info.filename, archive.read(info)) for info in archive.infolist()]


def test_real_readers(fixtures, tmp_path):
    kml = read_dataset(fixtures / "sample.kml", ".kml", tmp_path)
    assert kml.source_crs.to_epsg() == 4326
    assert [f.feature_id for f in kml.features] == [0, 1, 2]
    assert [f.layer for f in kml.features] == ["plots", "routes", "routes"]
    assert kml.features[0].properties["Name"] == "Plot A"
    assert all(not f.geometry.has_z for f in kml.features)
    shape = read_dataset(fixtures / "sample.zip", ".zip", tmp_path)
    assert shape.source_crs.to_epsg() == 32632
    assert len(shape.features) == 1
    assert shape.features[0].properties == {"name": "UTM square", "source_id": 42}
    assert shape.features[0].geometry.area == 10_000


def test_nested_case_insensitive_sidecars(fixtures, tmp_path):
    entries = [(name.upper(), data) for name, data in sample_entries(fixtures)] + [("README.txt", b"ignored")]
    path = make_zip(tmp_path / "upload.zip", entries)
    dataset = read_dataset(path, ".zip", tmp_path)
    assert len(dataset.features) == 1
    assert not (tmp_path / "README.txt").exists()
    assert not (tmp_path / "SURVEY").exists()


@pytest.mark.parametrize("missing", [".prj", ".shx", ".dbf"])
def test_missing_sidecar(fixtures, tmp_path, missing):
    path = make_zip(tmp_path / "upload.zip", [(name, data) for name, data in sample_entries(fixtures) if not name.endswith(missing)])
    with pytest.raises(DatasetError, match=missing):
        extract_shapefile(path, tmp_path)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "\\\\server\\file", "folder\\ambiguous",
                                 "a/../file", "a//file", "a/./file", "file:stream", "NUL.txt", "file. ", "bad\x00name"])
def test_unsafe_paths_even_when_unrelated(fixtures, tmp_path, name):
    path = make_zip(tmp_path / "upload.zip", sample_entries(fixtures) + [(name, b"bad")])
    # ZipInfo truncates NUL on construction, so inject the untrusted original name.
    if "\x00" in name:
        info = ZipInfo("safe")
        info.orig_filename = name
        with pytest.raises(DatasetError, match="unsafe"):
            readers.archive_destination(info)
        return
    with pytest.raises(DatasetError, match="unsafe"):
        extract_shapefile(path, tmp_path)
    assert not (tmp_path / "dataset").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_duplicates_and_multiple_datasets(fixtures, tmp_path):
    entries = sample_entries(fixtures)
    for extra, reason in [((entries[0][0].upper(), b"copy"), "duplicate"), (("second.shp", b"shape"), "exactly one")]:
        path = make_zip(tmp_path / "upload.zip", entries + [extra])
        with pytest.raises(DatasetError, match=reason):
            extract_shapefile(path, tmp_path)


def test_symlink_and_encrypted_entry(tmp_path):
    link = ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = make_zip(tmp_path / "upload.zip", [(link, b"target")])
    with pytest.raises(DatasetError, match="symlinks"):
        extract_shapefile(path, tmp_path)
    encrypted = ZipInfo("entry")
    encrypted.flag_bits |= 1
    with pytest.raises(DatasetError, match="encrypted"):
        readers.archive_destination(encrypted)


@pytest.mark.parametrize("content", [b"not zip", b"PK\x03\x04truncated"])
def test_corrupt_zip(tmp_path, content):
    path = tmp_path / "upload.zip"
    path.write_bytes(content)
    with pytest.raises(DatasetError, match="corrupt"):
        extract_shapefile(path, tmp_path)


@pytest.mark.parametrize("content", [
    '<wrong/>', '<kml>', '<!DOCTYPE kml><kml/>',
    '<!DOCTYPE kml [<!ENTITY x "value">]><kml>&x;</kml>',
    '<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///secret">]><kml>&x;</kml>',
    '<kml xmlns="http://www.opengis.net/kml/2.2"><NetworkLink><Link><href>https://example.com</href></Link></NetworkLink></kml>',
])
def test_unsafe_or_malformed_kml(tmp_path, content):
    path = tmp_path / "upload.kml"
    path.write_text(content)
    with pytest.raises(DatasetError):
        validate_kml(path)


def test_empty_datasets_and_invalid_prj(fixtures, tmp_path):
    path = tmp_path / "empty.kml"
    path.write_text('<kml xmlns="http://www.opengis.net/kml/2.2"><Document/></kml>')
    with pytest.raises(DatasetError):
        read_dataset(path, ".kml", tmp_path)
    path = make_zip(tmp_path / "empty.zip", [])
    with pytest.raises(DatasetError, match="exactly one"):
        extract_shapefile(path, tmp_path)
    path = make_zip(tmp_path / "bad.zip", [(name, b"invalid CRS" if name.endswith(".prj") else data) for name, data in sample_entries(fixtures)])
    with pytest.raises(DatasetError, match="CRS"):
        read_dataset(path, ".zip", tmp_path)


@pytest.mark.parametrize("limit,value,file_type", [("MAX_FEATURES", 2, ".kml"), ("MAX_COORDINATES", 5, ".kml"),
    ("MAX_KML_LAYERS", 1, ".kml"), ("MAX_ARCHIVE_ENTRIES", 1, ".zip"), ("MAX_ARCHIVE_BYTES", 1, ".zip")])
def test_reader_limits(fixtures, tmp_path, monkeypatch, limit, value, file_type):
    monkeypatch.setattr(readers, limit, value)
    with pytest.raises(DatasetError) as error:
        read_dataset(fixtures / ("sample" + file_type), file_type, tmp_path)
    assert error.value.status_code == 413


def test_upload_counts_actual_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(readers, "MAX_UPLOAD_BYTES", 3)
    with pytest.raises(DatasetError) as error:
        copy_upload(BytesIO(b"1234"), tmp_path / "upload")
    assert error.value.status_code == 413
    with pytest.raises(DatasetError, match="empty"):
        copy_upload(BytesIO(), tmp_path / "empty")
    copy_upload(BytesIO(b"123"), tmp_path / "valid")
    assert (tmp_path / "valid").read_bytes() == b"123"


def test_native_kml_id_is_retained(fixtures, tmp_path):
    path = tmp_path / "identified.kml"
    path.write_text((fixtures / "sample.kml").read_text().replace('<Placemark><name>Plot A', '<Placemark id="plot-42"><name>Plot A'))
    dataset = read_dataset(path, ".kml", tmp_path)
    assert dataset.features[0].properties["id"] == "plot-42"


def test_actual_streamed_extraction_limit(fixtures, tmp_path, monkeypatch):
    path = make_zip(tmp_path / "upload.zip", sample_entries(fixtures))
    with ZipFile(path) as archive:
        declared = sum(info.file_size for info in archive.infolist())
    monkeypatch.setattr(readers, "MAX_ARCHIVE_BYTES", declared)
    monkeypatch.setattr(ZipFile, "open", lambda *args, **kwargs: BytesIO(b"x" * (declared + 1)))
    with pytest.raises(DatasetError, match="actual extraction") as error:
        extract_shapefile(path, tmp_path)
    assert error.value.status_code == 413


def test_real_crc_failure(fixtures, tmp_path):
    path = make_zip(tmp_path / "upload.zip", sample_entries(fixtures))
    with ZipFile(path) as archive:
        info = next(info for info in archive.infolist() if info.filename.endswith(".dbf"))
        offset = info.header_offset
    data = bytearray(path.read_bytes())
    filename_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
    data[offset + 30 + filename_length + extra_length] ^= 1
    path.write_bytes(data)
    with pytest.raises(DatasetError, match="corrupt"):
        extract_shapefile(path, tmp_path)


@pytest.mark.parametrize("compression", [ZIP_DEFLATED, ZIP_BZIP2, ZIP_LZMA])
def test_corrupt_compressed_member(fixtures, tmp_path, compression):
    path = tmp_path / "upload.zip"
    with ZipFile(path, "w", compression=compression) as archive:
        for name, content in sample_entries(fixtures):
            archive.writestr(name, content)
    with ZipFile(path) as archive:
        info = next(info for info in archive.infolist() if info.filename.endswith(".shp"))
        offset = info.header_offset
    data = bytearray(path.read_bytes())
    filename_length, extra_length = struct.unpack_from("<HH", data, offset + 26)
    start = offset + 30 + filename_length + extra_length
    data[start:start + min(info.compress_size, 20)] = b"\xff" * min(info.compress_size, 20)
    path.write_bytes(data)
    with pytest.raises(DatasetError, match="corrupt"):
        extract_shapefile(path, tmp_path)


@pytest.mark.parametrize("epsg", [4978, 7405])
def test_unsupported_crs_in_real_shapefile(fixtures, tmp_path, epsg):
    from pyproj import CRS
    path = make_zip(tmp_path / "upload.zip", [(name, CRS.from_epsg(epsg).to_wkt().encode() if name.endswith(".prj") else data)
                                           for name, data in sample_entries(fixtures)])
    with pytest.raises(DatasetError, match="CRS"):
        read_dataset(path, ".zip", tmp_path)


def test_null_geometry_row_in_real_shapefile(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box
    folder = tmp_path / "source"
    folder.mkdir()
    frame = gpd.GeoDataFrame({"name": ["valid", "missing"]},
                            geometry=[box(500_000, 5_500_000, 500_100, 5_500_100), None], crs="EPSG:32632")
    frame.to_file(folder / "survey.shp", engine="pyogrio")
    path = tmp_path / "upload.zip"
    with ZipFile(path, "w") as archive:
        for member in folder.iterdir():
            archive.write(member, member.name)
    dataset = read_dataset(path, ".zip", tmp_path)
    assert len(dataset.features) == 2
    assert dataset.features[1].geometry is None
    assert dataset.features[1].properties["name"] == "missing"
