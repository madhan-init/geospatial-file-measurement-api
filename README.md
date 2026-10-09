# Geospatial File Measurement API

A small FastAPI backend for uploaded KML and zipped Shapefiles. It returns polygon area in square metres and line length in metres after projecting each feature into an appropriate WGS84 UTM CRS. Points have no measurement. Every readable feature has an outcome, including invalid and unsupported geometry.

## Setup

Requires Python 3.12+ with compatible GIS wheels; pinned NumPy and pyproj require this minimum. Verified on **Windows x64, Python 3.12.14** (and Python 3.14), using the exact package versions in `requirements.txt` and `requirements-dev.txt`. The pyogrio wheel supplies GDAL **3.12.4**, including functioning LIBKML/KML and ESRI Shapefile drivers; no separate GDAL build is needed on the tested platform.

From the repository root, in Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

If `py` is available on Windows, you can also run `py -3.12 -m venv .venv`. Production-only dependencies can be installed with `-r requirements.txt`.

On macOS/Linux, from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

If using a specific Python version on macOS/Linux, replace `python3` with `python3.12`. Driver availability varies by wheel/platform. The real-format environment test checks drivers **and reads both formats**; a KML failure needs a compatible wheel/driver, not a substituted format. Non-Windows setup has not been exercised here.

Open [Swagger UI](http://127.0.0.1:8000/docs) or [OpenAPI JSON](http://127.0.0.1:8000/openapi.json). Keep this demo local and run **one worker**: ordinary Python memory is process-local. Restarting the service clears all results.

## API and sample walkthrough

| Method | Path | Successful response |
| --- | --- | --- |
| POST | `/api/files/` | 201, completed metadata |
| GET | `/api/files/{id}/` | 200, identical metadata |
| GET | `/api/files/{id}/measurements/` | 200, complete feature results |

Use the exact trailing slashes and multipart field **`file`**. A UUID is returned after processing completes; there is no polling or pending state. `COMPLETED` means every readable feature was considered, even if some feature outcomes are errors.

With the server running, in a second PowerShell terminal from the project root:

```powershell
# Three features across two KML folders: polygon, line, point.
$kmlResult = curl.exe --fail-with-body -sS -F "file=@tests/fixtures/sample.kml" http://127.0.0.1:8000/api/files/ | ConvertFrom-Json
$kmlResult | ConvertTo-Json -Depth 10
curl.exe --fail-with-body -sS "http://127.0.0.1:8000/api/files/$($kmlResult.id)/"
curl.exe --fail-with-body -sS "http://127.0.0.1:8000/api/files/$($kmlResult.id)/measurements/"

# One analytical 100 m by 100 m square, originally in EPSG:32632.
$zipResult = curl.exe --fail-with-body -sS -F "file=@tests/fixtures/sample.zip" http://127.0.0.1:8000/api/files/ | ConvertFrom-Json
$zipResult | ConvertTo-Json -Depth 10
curl.exe --fail-with-body -sS "http://127.0.0.1:8000/api/files/$($zipResult.id)/"
curl.exe --fail-with-body -sS "http://127.0.0.1:8000/api/files/$($zipResult.id)/measurements/"
```

On macOS/Linux, use `curl` to upload, then paste the returned UUID into both GET URLs:

```bash
curl --fail-with-body -sS -F 'file=@tests/fixtures/sample.zip' http://127.0.0.1:8000/api/files/
curl --fail-with-body -sS http://127.0.0.1:8000/api/files/RETURNED_UUID/
curl --fail-with-body -sS http://127.0.0.1:8000/api/files/RETURNED_UUID/measurements/
```

Actual response from uploading `sample.zip` in the live walkthrough (your UUID will differ):

```json
{
  "id": "c35c3891-3f3d-4867-a4ca-1a69546eef4c",
  "filename": "sample.zip",
  "status": "COMPLETED",
  "feature_count": 1,
  "crs": "EPSG:32632",
  "counts": {"measured": 1, "skipped": 0, "unsupported": 0, "error": 0}
}
```

The metadata GET returns the same object. The measurements GET returns:

```json
{
  "file_id": "c35c3891-3f3d-4867-a4ca-1a69546eef4c",
  "source_crs": "EPSG:32632",
  "features": [
    {
      "geometry_type": "Polygon",
      "status": "measured",
      "measurement": {
        "kind": "area", "value": 10000.0, "unit": "m2",
        "crs": "EPSG:32632", "method": "utm"
      },
      "reason": null,
      "feature_id": 0,
      "layer": "dataset",
      "properties": {"name": "UTM square", "source_id": 42}
    }
  ]
}
```

The KML sample returns `feature_count: 3`, `crs: EPSG:4326`, and counts `measured: 2`, `skipped: 1`, `unsupported: 0`, `error: 0`. Its polygon measures **8025.402021445091 m2**, its line **132.5564929709273 m**, both in **EPSG:32632**. Its Point returns `measurement: null` and `reason: "Points have no area or length measurement."`. KML properties include driver-provided fields such as `Name`, `id` and `description`; missing values become JSON null. Native IDs remain in properties, while `feature_id` is a zero-based index unique across the uploaded file.

## Accepted inputs and feature outcomes

- `.kml`: validated embedded KML XML, WGS84/EPSG:4326, all spatial layers read. DTDs, entities, external references and `NetworkLink` are rejected.
- `.zip`: exactly one `.shp`, with matching `.shx`, `.dbf` and parseable `.prj` in the same folder; matching `.cpg` is optional. Matching is case-insensitive. Nested folders are allowed. Unrelated safe files are ignored. Sidecars are extracted to fixed internal names, so the returned Shapefile layer is `dataset`.
- Polygon/MultiPolygon: `measured`, area in `m2`; holes subtract and parts sum.
- LineString/MultiLineString: `measured`, length in `m`; parts sum.
- Valid Point in the supported domain: `skipped`, no measurement.
- MultiPoint, GeometryCollection and other readable types: `unsupported`, no measurement and a reason naming the type.
- Null, empty, nonfinite, topologically invalid or out-of-domain shapes: `error`, no measurement and a readable reason. Missing geometry has `geometry_type: null`.

Each feature retains its layer, geometry type and JSON-safe properties. Geometry coordinates are used internally and excluded from results. Measurements keep full floating-point precision and have a finite nonnegative value, measurement CRS and `method: "utm"`. Counts sum to `feature_count`. Arbitrary binary corruption that prevents native feature enumeration is a file-level error; it cannot be recovered row by row.

## CRS algorithm and limitations

1. Require a parseable geographic or projected source CRS. Shapefiles require `.prj`; never guess their CRS. Compound and geocentric CRSs are rejected. Preserve an authority string when available, otherwise WKT.
2. Validate nonempty, finite XY geometry and topology; ignore altitude. Transform to WGS84 with `always_xy=True`, checked transforms, longitude wrapping disabled, no ballpark datum operation and network grid downloads disabled. An unavailable required transform produces an explicit feature error.
3. Validate longitude/latitude ranges and the full geographic bounds. Both longitude and latitude spans must be at most **6 degrees**.
4. Query WGS84 UTM candidates with `contains=True`, requiring the **entire bounds** to fit the CRS area of use. Select the lowest EPSG code deterministically when several match and verify metre axes.
5. Transform from the original CRS to that UTM CRS, check finite valid output, then measure Shapely polygon area or line length. EPSG:3857 follows this same path rather than being measured directly.

This conservative regional scope intentionally rejects even small features crossing a UTM zone or the equator, as well as polar and dateline-crossing extents. The latitude span cap is an application policy. Projection transforms vertices with straight segments between them; it does not reconstruct geodesic edges. Results are 2D approximations, with no terrain correction, geometry repair, densification, runtime geodesic fallback or worldwide/survey-grade accuracy promise. Numerical tests establish behavior for the declared fixtures.

## Resource limits and errors

| Resource | Limit |
| --- | --- |
| Actual uploaded bytes | 10 MiB |
| ZIP declared / actually extracted bytes | 50 MiB each |
| ZIP entries / KML layers | 100 each |
| Features across all spatial layers | 1,000 |
| Coordinates across all features | 100,000 |
| Serialized completed record | 2 MiB |
| Retained completed records | 20 |

Limits are named constants in `readers.py`, `processing.py` and `store.py`. Upload bytes are counted while copying; extension, filename and Content-Length are not evidence of content or size. FastAPI may already spool multipart data before the route runs: the upload cap bounds application processing, not the entire HTTP transport. Deployment would require transport/concurrency limits.

ZIP validation covers every entry before extraction: unsafe paths, drive/UNC/backslash ambiguity, Windows reserved names, symlinks, special files, encrypted entries and duplicate case-insensitive destinations are rejected. Extraction streams only matching sidecars, checks actual byte totals, keeps paths inside the temporary workspace and rejects CRC failures. A file is saved only after successful processing, strict JSON serialization and temporary workspace cleanup. Original uploads and geometry objects are not retained. Capacity is guarded by a lock; previous records are never silently evicted.

| Error | HTTP status |
| --- | --- |
| Unsupported extension | 400 |
| Upload, ZIP, layer, feature, coordinate or result limit | 413 |
| Corrupt/empty dataset, unsafe/ambiguous ZIP, missing sidecars/CRS, unsafe KML | 422 |
| Missing `file` or malformed UUID | 422, FastAPI validation |
| Valid but unknown UUID | 404 |
| Result store full | 503, restart instruction |

Domain errors use `{"detail": "readable explanation"}`. FastAPI validation errors use its normal detail list. Local paths and tracebacks are not exposed in domain error bodies. Unexpected programming errors surface as server failures and logs, rather than masquerading as invalid input.

## Architecture and decisions

```text
POST multipart file
  -> bounded copy in TemporaryDirectory
  -> XML checks or ZIP inspection/streamed sidecar extraction
  -> pyogrio -> GeoDataFrame -> normalized feature records + source CRS
  -> per-feature validation -> UTM selection -> area/length/outcome
  -> normalize properties -> counts -> strict bounded JSON
  -> remove temporary files -> lock capacity check + completed record insert
  -> HTTP 201 metadata

GET metadata / measurements -> UUID lookup in this app's in-memory store
```

| Module | Responsibility |
| --- | --- |
| `app/main.py` | App factory, three sync routes, HTTP error mapping |
| `app/schemas.py` | Explicit API models, statuses, measurement kinds and units |
| `app/readers.py` | Upload/XML/archive validation, GIS reading and input budgets |
| `app/measurements.py` | Geometry outcomes, source CRS, projection and numerical values |
| `app/processing.py` | Temporary-file lifecycle, property normalization, results and JSON size |
| `app/store.py` | App-specific bounded completed-result dictionary and lock |

FastAPI keeps a three-route API and OpenAPI documentation small. GeoPandas/pyogrio use actual GIS drivers rather than a custom Shapefile parser. Shapely and pyproj handle topology and projections. Plain synchronous routes suit blocking GIS processing of bounded files. A queue would add polling and lifecycle complexity unnecessary for the demo.

Memory storage follows the agreed restart-clears-results lifecycle; a database would provide durability at additional operational cost. A short lock covers capacity-check-plus-insert, while parsing runs outside it. UTM fulfills the brief's projection requirement; global projection policies need a broader accuracy design. Invalid geometry is reported rather than automatically repaired, since repair can change topology or geometry type. Functions and small models keep measurement independent of HTTP and storage.

## Tests, learning and future scope

The suite covers analytical UTM area/length, geographic and Web Mercator round trips, holes, multiparts, southern UTM, altitude, invalid/missing/nonfinite shapes, boundaries and longitude wrapping, custom WKT without an authority, real multilayer KML, projected Shapefiles, native attributes/IDs, actual null Shapefile rows, unsafe XML/ZIP, CRC and compressed-member failures, declared/streamed resource limits, JSON normalization, endpoint contracts, cleanup, app isolation and concurrent capacity. The fresh-environment run passed **96 tests**; `pip check` reported no broken requirements. See [verification evidence](VERIFICATION.md).

`tests/fixtures/sample.zip` is generated from known Shapely coordinates in EPSG:32632. Rebuild it with:

```powershell
.\.venv\Scripts\python.exe tests/generate_fixtures.py
```

The verified dependency set emits deprecation warnings for `shapely.ops.transform` (the API selected by the plan) and Starlette's `httpx` TestClient compatibility. These do not prevent the checks from passing. Malformed-coordinate fixtures intentionally trigger a Shapely warning. Future dependency upgrades should be checked with both real formats and the numerical suite.

Learning: metre units alone do not guarantee a suitable projection; full feature extent matters. GIS drivers and default layer selection must be exercised with real files. Row conversion can alter missing geometry values, so geometry is read separately from attribute rows. Archive declarations require both path validation and streamed byte accounting. A single worker still runs sync requests concurrently, so capacity insertion needs a lock. Strict JSON normalization keeps GIS missing values out of API numeric fields.

Future scope, requiring a separate request: durable storage, authentication, transport/concurrency limits, background jobs for larger inputs, more formats, globally appropriate projections, geodesic reference QA, geometry repair with reported changes, and a map/export UI. This repository implements the bounded backend only.
