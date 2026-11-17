"""Document storage: local disk for development, S3 (ap-south-1) for production."""

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import settings


class Storage(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> None: ...


class LocalStorage(Storage):
    def __init__(self, root: str | None = None):
        self.root = Path(root or settings.LOCAL_STORAGE_DIR).resolve()

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    async def put(self, key, data, content_type):
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def get(self, key):
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def delete(self, key):
        p = self._path(key)
        if p.exists():
            await asyncio.to_thread(p.unlink)


class S3Storage(Storage):
    def __init__(self):
        import aioboto3

        self.session = aioboto3.Session()

    async def put(self, key, data, content_type):
        async with self.session.client("s3", region_name=settings.S3_REGION) as s3:
            await s3.put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256")

    async def get(self, key):
        async with self.session.client("s3", region_name=settings.S3_REGION) as s3:
            obj = await s3.get_object(Bucket=settings.S3_BUCKET, Key=key)
            return await obj["Body"].read()

    async def delete(self, key):
        async with self.session.client("s3", region_name=settings.S3_REGION) as s3:
            await s3.delete_object(Bucket=settings.S3_BUCKET, Key=key)


def get_storage() -> Storage:
    return S3Storage() if settings.STORAGE_BACKEND == "s3" else LocalStorage()
