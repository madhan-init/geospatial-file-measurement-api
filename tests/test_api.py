from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
from datetime import date, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
from pyproj import CRS
import pytest
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box

from app import processing, readers, store
from app.main import create_app
from app.readers import Dataset, Feature


def upload(client, fixtures, filename="sample.kml"):
    return client.post("/api/files/", files={"file": (filename, (fixtures / filename).read_bytes())})


@pytest.mark.parametrize("filename,count,crs", [("sample.kml", 3, "EPSG:4326"), ("sample.zip", 1, "EPSG:32632")])
def test_full_real_file_flow(client, fixtures, filename, count, crs):
    response = upload(client, fixtures, filename)
    assert response.status_code == 201, response.text
    metadata = response.json()
    assert set(metadata) == {"id", "filename", "status", "feature_count", "crs", "counts"}
    assert metadata["filename"] == filename
    assert metadata["feature_count"] == count
    assert metadata["crs"] == crs
    assert metadata["status"] == "COMPLETED"
    assert sum(metadata["counts"].values()) == count
    assert client.get(f'/api/files/{metadata["id"]}/').json() == metadata
    response = client.get(f'/api/files/{metadata["id"]}/measurements/')
    assert response.status_code == 200
    results = response.json()
    assert results["file_id"] == metadata["id"]
    assert results["source_crs"] == crs
    assert len(results["features"]) == count
    assert [f["feature_id"] for f in results["features"]] == list(range(count))
    if filename.endswith(".zip"):
        assert metadata["counts"] == {"measured": 1, "skipped": 0, "unsupported": 0, "error": 0}
        assert results["features"][0]["measurement"]["value"] == pytest.approx(10_000, abs=1e-6)
        assert results["features"][0]["properties"]["source_id"] == 42
    else:
        assert metadata["counts"] == {"measured": 2, "skipped": 1, "unsupported": 0, "error": 0}
        assert {f["layer"] for f in results["features"]} == {"plots", "routes"}


def test_http_errors_and_app_isolation(client, fixtures):
    for suffix in ("/", "/measurements/"):
        assert client.get(f"/api/files/{uuid4()}{suffix}").status_code == 404
        assert client.get(f"/api/files/not-a-uuid{suffix}").status_code == 422
    assert client.post("/api/files/").status_code == 422
    for filename, content, code in [("x.txt", b"bad", 400), ("x.kml", b"bad", 422),
                                    ("x.zip", b"bad", 422), ("x.kml", b"", 422)]:
        response = client.post("/api/files/", files={"file": (filename, content)})
        assert response.status_code == code
        assert isinstance(response.json()["detail"], str)
        assert "Traceback" not in response.text
    metadata = upload(client, fixtures).json()
    with TestClient(create_app()) as other:
        assert other.get(f'/api/files/{metadata["id"]}/').status_code == 404


@pytest.mark.parametrize("encoding", ["nonexistent", "UTF-32", "utf-7"])
def test_malformed_kml_encoding_does_not_consume_store_slot(fixtures, monkeypatch, encoding):
    monkeypatch.setattr(store, "MAX_RECORDS", 1)
    content = (fixtures / "sample.kml").read_bytes().replace(
        b'encoding="UTF-8"', f'encoding="{encoding}"'.encode("ascii")
    )
    with TestClient(create_app(), raise_server_exceptions=False) as client:
        response = client.post("/api/files/", files={"file": ("bad.kml", content)})
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str) and detail.strip()
        assert "KML" in detail
        assert upload(client, fixtures).status_code == 201


def test_store_capacity_is_atomic_and_does_not_evict(client, fixtures, monkeypatch):
    monkeypatch.setattr(store, "MAX_RECORDS", 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: upload(client, fixtures), range(4)))
    assert sorted(r.status_code for r in responses) == [201, 201, 503, 503]
    for response in responses:
        if response.status_code == 201:
            assert client.get(f'/api/files/{response.json()["id"]}/').status_code == 200
        else:
            assert "restart" in response.json()["detail"]


def test_mixed_normalized_features_continue(client, monkeypatch):
    shapes = [box(500_000, 5_500_000, 500_100, 5_500_100),
              LineString([(500_000, 5_500_000), (500_300, 5_500_400)]),
              Point(500_000, 5_500_000), GeometryCollection([Point(500_000, 5_500_000)]),
              Polygon([(500_000, 5_500_000), (500_100, 5_500_100), (500_100, 5_500_000), (500_000, 5_500_100)]), None]
    dataset = Dataset(CRS.from_epsg(32632), [Feature(i, "mixed", shape, {"n": np.int64(42), "nan": np.nan,
        "date": pd.Timestamp("2026-10-08"), "missing": pd.NA, "items": np.array([np.inf, 1.0])}) for i, shape in enumerate(shapes)])
    monkeypatch.setattr(processing, "read_dataset", lambda *args: dataset)
    response = client.post("/api/files/", files={"file": ("mixed.kml", b"placeholder")})
    assert response.status_code == 201
    metadata = response.json()
    assert metadata["counts"] == {"measured": 2, "skipped": 1, "unsupported": 1, "error": 2}
    response = client.get(f'/api/files/{metadata["id"]}/measurements/')
    assert [f["status"] for f in response.json()["features"]] == ["measured", "measured", "skipped", "unsupported", "error", "error"]
    properties = response.json()["features"][0]["properties"]
    assert properties == {"n": 42, "nan": None, "date": "2026-10-08T00:00:00", "missing": None, "items": [None, 1.0]}
    json.dumps(response.json(), allow_nan=False)


@pytest.mark.parametrize("limit", ["upload", "result"])
def test_limits_leave_no_record(client, fixtures, monkeypatch, limit):
    if limit == "upload":
        monkeypatch.setattr(readers, "MAX_UPLOAD_BYTES", 1)
    else:
        monkeypatch.setattr(processing, "MAX_RESULT_BYTES", 1)
    response = upload(client, fixtures)
    assert response.status_code == 413
    # Failure must not occupy the only slot.
    monkeypatch.setattr(readers, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
    monkeypatch.setattr(processing, "MAX_RESULT_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(store, "MAX_RECORDS", 1)
    assert upload(client, fixtures).status_code == 201


@pytest.mark.parametrize("fail", [False, True])
def test_temporary_workspace_and_upload_cleanup(fixtures, monkeypatch, fail):
    workspaces = []
    original = processing.read_dataset

    def tracked(path, file_type, workspace):
        workspaces.append(workspace)
        assert path.exists()
        if fail:
            raise RuntimeError("unexpected programming error")
        return original(path, file_type, workspace)

    monkeypatch.setattr(processing, "read_dataset", tracked)
    source = BytesIO((fixtures / "sample.kml").read_bytes())
    if fail:
        with pytest.raises(RuntimeError, match="programming"):
            processing.process_upload(source, "sample.kml")
    else:
        record = processing.process_upload(source, "C:\\uploads\\sample.kml")
        assert record.metadata.filename == "sample.kml"
        processing.serialize_record(record)
    assert source.closed
    assert workspaces and all(not path.exists() for path in workspaces)


def test_openapi_contract(client):
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {"/api/files/", "/api/files/{id}/", "/api/files/{id}/measurements/"}
    assert "multipart/form-data" in schema["paths"]["/api/files/"]["post"]["requestBody"]["content"]


@pytest.mark.parametrize("limit,value,filename", [
    ("MAX_FEATURES", 2, "sample.kml"), ("MAX_COORDINATES", 5, "sample.kml"),
    ("MAX_KML_LAYERS", 1, "sample.kml"), ("MAX_ARCHIVE_ENTRIES", 1, "sample.zip"),
    ("MAX_ARCHIVE_BYTES", 1, "sample.zip"),
])
def test_reader_limit_http_mapping(client, fixtures, monkeypatch, limit, value, filename):
    with monkeypatch.context() as limited:
        limited.setattr(readers, limit, value)
        response = upload(client, fixtures, filename)
        assert response.status_code == 413
        assert isinstance(response.json()["detail"], str)
    monkeypatch.setattr(store, "MAX_RECORDS", 1)
    assert upload(client, fixtures, filename).status_code == 201


def test_real_mixed_kml_outcomes(client):
    content = '''<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>mixed</name>
      <Placemark><name>line</name><LineString><coordinates>9,49 9.001,49.001</coordinates></LineString></Placemark>
      <Placemark><name>point</name><Point><coordinates>9,49</coordinates></Point></Placemark>
      <Placemark><name>multipoint</name><MultiGeometry><Point><coordinates>9,49</coordinates></Point>
        <Point><coordinates>9.001,49.001</coordinates></Point></MultiGeometry></Placemark>
      <Placemark><name>invalid</name><Polygon><outerBoundaryIs><LinearRing>
        <coordinates>9,49 9.001,49.001 9.001,49 9,49.001 9,49</coordinates>
      </LinearRing></outerBoundaryIs></Polygon></Placemark>
      <Placemark><name>last line</name><LineString><coordinates>9,49 9.002,49.002</coordinates></LineString></Placemark>
    </Folder></Document></kml>'''
    response = client.post("/api/files/", files={"file": ("mixed.kml", content.encode())})
    assert response.status_code == 201, response.text
    metadata = response.json()
    assert metadata["counts"] == {"measured": 2, "skipped": 1, "unsupported": 1, "error": 1}
    features = client.get(f'/api/files/{metadata["id"]}/measurements/').json()["features"]
    assert [f["status"] for f in features] == ["measured", "skipped", "unsupported", "error", "measured"]


def test_unsafe_zip_cleanup_and_missing_crs_http(client, fixtures, monkeypatch):
    from zipfile import ZipFile
    original = processing.read_dataset
    workspaces = []

    def tracked(path, file_type, workspace):
        workspaces.append(workspace)
        return original(path, file_type, workspace)

    monkeypatch.setattr(processing, "read_dataset", tracked)
    for unsafe in (False, True):
        payload = BytesIO()
        with ZipFile(payload, "w") as output, ZipFile(fixtures / "sample.zip") as sample:
            for info in sample.infolist():
                if not info.filename.endswith(".prj") or unsafe:
                    output.writestr(info.filename, sample.read(info))
            if unsafe:
                output.writestr("../escape", b"bad")
        response = client.post("/api/files/", files={"file": ("bad.zip", payload.getvalue())})
        assert response.status_code == 422
        assert "geospatial-" not in response.text
    assert len(workspaces) == 2 and all(not path.exists() for path in workspaces)
    monkeypatch.setattr(store, "MAX_RECORDS", 1)
    assert upload(client, fixtures).status_code == 201


def test_json_normalization_missing_dates_and_scalars():
    values = {"missing": [None, pd.NaT, np.datetime64("NaT", "ns"), float("inf"), float("-inf"), np.float64("nan")],
              "scalars": [np.bool_(True), np.int32(7), np.float32(1.5)],
              "dates": [date(2026, 10, 8), datetime(2026, 10, 8, 12, 30), np.datetime64("2026-10-08"),
                        np.datetime64("2026-10-08T12:30:00.123456789", "ns")],
              "nested": {"x": (np.float64("inf"), b"name"), "scalar_array": np.array(7)}}
    normalized = processing.normalize_property(values)
    assert normalized["missing"] == [None] * 6
    assert normalized["scalars"] == [True, 7, 1.5]
    assert normalized["dates"] == ["2026-10-08", "2026-10-08T12:30:00", "2026-10-08", "2026-10-08T12:30:00.123456789"]
    assert normalized["nested"] == {"x": [None, "name"], "scalar_array": 7}
    json.dumps(normalized, allow_nan=False)
