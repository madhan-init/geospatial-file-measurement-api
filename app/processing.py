from datetime import date, datetime
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from uuid import uuid4

import numpy as np
import pandas as pd

from .measurements import crs_label, measure_geometry
from .readers import DatasetError, copy_upload, read_dataset, safe_filename
from .schemas import CompletedRecord, Counts, FeatureResult, FileMetadata, Measurements


MAX_RESULT_BYTES = 2 * 1024 * 1024


def normalize_property(value):
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(key): normalize_property(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return normalize_property(value.tolist())
    if isinstance(value, (list, tuple)):
        return [normalize_property(item) for item in value]
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else str(np.datetime_as_string(value))
    if isinstance(value, np.generic):
        return normalize_property(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def serialize_record(record: CompletedRecord) -> bytes:
    payload = json.dumps(record.model_dump(mode="json"), allow_nan=False,
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        raise DatasetError("Serialized completed result exceeds 2 MiB.", 413)
    return payload


def process_upload(source: BinaryIO, filename: str | None) -> CompletedRecord:
    try:
        display_name = safe_filename(filename)
        file_type = Path(display_name).suffix.lower()
        if file_type not in {".kml", ".zip"}:
            raise DatasetError("Only .kml and Shapefile .zip uploads are supported.", 400)
        with TemporaryDirectory(prefix="geospatial-") as workspace_name:
            workspace = Path(workspace_name)
            path = workspace / ("upload" + file_type)
            copy_upload(source, path)
            dataset = read_dataset(path, file_type, workspace)
            features = []
            totals = {status: 0 for status in ("measured", "skipped", "unsupported", "error")}
            for feature in dataset.features:
                outcome = measure_geometry(feature.geometry, dataset.source_crs)
                features.append(FeatureResult(**outcome.model_dump(), feature_id=feature.feature_id,
                                              layer=feature.layer,
                                              properties=normalize_property(feature.properties)))
                totals[outcome.status] += 1
            file_id = uuid4()
            crs = crs_label(dataset.source_crs)
            record = CompletedRecord(
                metadata=FileMetadata(id=file_id, filename=display_name, feature_count=len(features),
                                      crs=crs, counts=Counts(**totals)),
                measurements=Measurements(file_id=file_id, source_crs=crs, features=features),
            )
            serialize_record(record)
        # The temporary workspace has been removed before the caller can save a record.
        return record
    finally:
        source.close()
