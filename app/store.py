from threading import Lock
from uuid import UUID

from .schemas import CompletedRecord


MAX_RECORDS = 20


class StoreFullError(Exception):
    pass


class ResultStore:
    def __init__(self):
        self._records: dict[UUID, CompletedRecord] = {}
        self._lock = Lock()

    def save(self, record: CompletedRecord):
        with self._lock:
            if len(self._records) >= MAX_RECORDS:
                raise StoreFullError("Result store is full; restart the demo service to clear completed records.")
            self._records[record.metadata.id] = record

    def get(self, file_id: UUID) -> CompletedRecord | None:
        with self._lock:
            return self._records.get(file_id)
