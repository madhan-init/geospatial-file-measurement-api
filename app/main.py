from uuid import UUID

from fastapi import FastAPI, HTTPException, UploadFile

from .processing import process_upload
from .readers import DatasetError
from .schemas import FileMetadata, Measurements
from .store import ResultStore, StoreFullError


def create_app() -> FastAPI:
    application = FastAPI(title="Geospatial File Measurement API", version="1.0.0",
                          description="Bounded KML/Shapefile uploads and regional 2D UTM measurements.")
    store = ResultStore()
    application.state.store = store

    @application.post("/api/files/", response_model=FileMetadata, status_code=201)
    def upload_file(file: UploadFile):
        try:
            record = process_upload(file.file, file.filename)
            store.save(record)
        except DatasetError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except StoreFullError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return record.metadata

    def find_record(file_id: UUID):
        record = store.get(file_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Completed file was not found.")
        return record

    @application.get("/api/files/{id}/", response_model=FileMetadata)
    def file_metadata(id: UUID):
        return find_record(id).metadata

    @application.get("/api/files/{id}/measurements/", response_model=Measurements)
    def file_measurements(id: UUID):
        return find_record(id).measurements

    return application


app = create_app()
