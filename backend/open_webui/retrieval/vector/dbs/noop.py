import math
from typing import Optional

from open_webui.retrieval.vector.main import (
    GetResult,
    SearchResult,
    VectorDBBase,
    VectorItem,
)


class NoopVectorClient(VectorDBBase):
    """In-memory vector store for local development and tests.

    This keeps the backend bootable when a persistent vector DB is unavailable.
    Data is process-local and non-persistent by design.
    """

    def __init__(self):
        self._collections: dict[str, dict[str, VectorItem]] = {}

    def has_collection(self, collection_name: str) -> bool:
        return collection_name in self._collections

    def delete_collection(self, collection_name: str) -> None:
        self._collections.pop(collection_name, None)

    def insert(self, collection_name: str, items: list[VectorItem]) -> None:
        collection = self._collections.setdefault(collection_name, {})
        for item in items:
            collection[item.id] = item

    def upsert(self, collection_name: str, items: list[VectorItem]) -> None:
        self.insert(collection_name, items)

    def search(
        self,
        collection_name: str,
        vectors: list[list[float | int]],
        filter: Optional[dict] = None,
        limit: int = 10,
    ) -> Optional[SearchResult]:
        collection = self._collections.get(collection_name)
        if not collection or not vectors:
            return None

        query_vector = vectors[0]
        matches = [
            item
            for item in collection.values()
            if self._metadata_matches(item.metadata, filter)
        ]
        if not matches:
            return None

        ranked = sorted(
            matches,
            key=lambda item: self._cosine_similarity(query_vector, item.vector),
            reverse=True,
        )[:limit]

        return SearchResult(
            ids=[[item.id for item in ranked]],
            documents=[[item.text for item in ranked]],
            metadatas=[[item.metadata for item in ranked]],
            distances=[
                [self._cosine_similarity(query_vector, item.vector) for item in ranked]
            ],
        )

    def query(
        self, collection_name: str, filter: dict, limit: Optional[int] = None
    ) -> Optional[GetResult]:
        collection = self._collections.get(collection_name)
        if not collection:
            return None

        matches = [
            item
            for item in collection.values()
            if self._metadata_matches(item.metadata, filter)
        ]
        if limit is not None:
            matches = matches[:limit]
        return self._items_to_get_result(matches)

    def get(self, collection_name: str) -> Optional[GetResult]:
        collection = self._collections.get(collection_name)
        if not collection:
            return None
        return self._items_to_get_result(list(collection.values()))

    def delete(
        self,
        collection_name: str,
        ids: Optional[list[str]] = None,
        filter: Optional[dict] = None,
    ) -> None:
        collection = self._collections.get(collection_name)
        if not collection:
            return

        if ids:
            for item_id in ids:
                collection.pop(item_id, None)

        if filter:
            to_delete = [
                item_id
                for item_id, item in collection.items()
                if self._metadata_matches(item.metadata, filter)
            ]
            for item_id in to_delete:
                collection.pop(item_id, None)

        if not collection:
            self._collections.pop(collection_name, None)

    def reset(self) -> None:
        self._collections.clear()

    def _items_to_get_result(self, items: list[VectorItem]) -> Optional[GetResult]:
        if not items:
            return None
        return GetResult(
            ids=[[item.id for item in items]],
            documents=[[item.text for item in items]],
            metadatas=[[item.metadata for item in items]],
        )

    @staticmethod
    def _metadata_matches(metadata, filter: Optional[dict]) -> bool:
        if not filter:
            return True
        if not isinstance(metadata, dict):
            return False
        return all(metadata.get(key) == value for key, value in filter.items())

    @staticmethod
    def _cosine_similarity(
        left: list[float | int], right: list[float | int]
    ) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
        right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
        if left_norm == 0.0 or right_norm == 0.0:
            return 0.0
        return dot / (left_norm * right_norm)
