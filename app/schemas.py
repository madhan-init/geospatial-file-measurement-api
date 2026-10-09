from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Measurement(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    kind: Literal["area", "length"]
    value: float = Field(ge=0)
    unit: Literal["m2", "m"]
    crs: str
    method: Literal["utm"] = "utm"

    @model_validator(mode="after")
    def matching_unit(self):
        if self.unit != ("m2" if self.kind == "area" else "m"):
            raise ValueError("Measurement unit must match its kind.")
        return self


class Outcome(BaseModel):
    geometry_type: str | None
    status: Literal["measured", "skipped", "unsupported", "error"]
    measurement: Measurement | None = None
    reason: str | None = None


class FeatureResult(Outcome):
    feature_id: int = Field(ge=0)
    layer: str
    properties: dict[str, JsonValue]


class Counts(BaseModel):
    measured: int = 0
    skipped: int = 0
    unsupported: int = 0
    error: int = 0


class FileMetadata(BaseModel):
    id: UUID
    filename: str
    status: Literal["COMPLETED"] = "COMPLETED"
    feature_count: int
    crs: str
    counts: Counts


class Measurements(BaseModel):
    file_id: UUID
    source_crs: str
    features: list[FeatureResult]


class CompletedRecord(BaseModel):
    metadata: FileMetadata
    measurements: Measurements
