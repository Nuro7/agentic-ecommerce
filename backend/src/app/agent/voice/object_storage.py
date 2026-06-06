"""Object Storage client — Phase 11.

Thin async wrapper around boto3 (S3-compatible API).
Supports AWS S3, Cloudflare R2, and GCS (via S3-compatible XML API).

Configuration (.env)
---------------------
  OBJECT_STORAGE_PROVIDER   s3 | r2 | gcs | disabled  (default: disabled)
  OBJECT_STORAGE_BUCKET     bucket name
  OBJECT_STORAGE_REGION     aws region (e.g. ap-south-1) — ignored for R2
  OBJECT_STORAGE_ENDPOINT   custom endpoint URL (required for R2/GCS)
  AWS_ACCESS_KEY_ID         access key (or R2 account ID / GCS HMAC key)
  AWS_SECRET_ACCESS_KEY     secret key

When OBJECT_STORAGE_PROVIDER is "disabled" or any env var is missing, all
operations degrade gracefully: uploads are skipped, downloads return None.
No exceptions are raised to callers.

Usage
-----
    storage = ObjectStorageClient.from_settings(settings)
    await storage.upload(key="tts-cache/en/abc123.mp3", data=b"...", content_type="audio/mpeg")
    data = await storage.download("tts-cache/en/abc123.mp3")   # bytes or None
    exists = await storage.exists("tts-cache/en/abc123.mp3")   # bool
"""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Optional

logger = logging.getLogger(__name__)


class ObjectStorageClient:
    """Async S3-compatible object storage client backed by boto3 in a thread pool."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: Optional[str] = None,
        access_key: str,
        secret_key: str,
    ) -> None:
        self._bucket = bucket
        self._endpoint = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = None   # lazy-initialised boto3 client

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_settings(cls, settings) -> "ObjectStorageClient":
        """Build from app settings. Returns a no-op client when storage is disabled."""
        provider = getattr(settings, "object_storage_provider", "disabled").lower()
        bucket   = getattr(settings, "object_storage_bucket", "")
        region   = getattr(settings, "object_storage_region", "us-east-1")
        endpoint = getattr(settings, "object_storage_endpoint", "") or None
        ak       = getattr(settings, "aws_access_key_id", "")
        sk       = getattr(settings, "aws_secret_access_key", "")

        if provider == "disabled" or not bucket or not ak or not sk:
            logger.info(
                "Object storage disabled (provider=%s bucket=%s key_set=%s)",
                provider, bool(bucket), bool(ak),
            )
            return _NoOpStorageClient()

        logger.info(
            "Object storage enabled: provider=%s bucket=%s region=%s",
            provider, bucket, region,
        )
        return cls(
            bucket=bucket,
            region=region,
            endpoint_url=endpoint,
            access_key=ak,
            secret_key=sk,
        )

    # ── Public async API ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return True

    async def upload(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Upload `data` bytes to `key`. Returns True on success."""
        try:
            client = self._boto_client()
            fn = partial(
                client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
            await asyncio.get_running_loop().run_in_executor(None, fn)
            return True
        except Exception as exc:
            logger.warning("ObjectStorage upload failed key=%s: %s", key, exc)
            return False

    async def download(self, key: str) -> Optional[bytes]:
        """Download and return raw bytes, or None if the key does not exist."""
        try:
            client = self._boto_client()
            fn = partial(client.get_object, Bucket=self._bucket, Key=key)
            response = await asyncio.get_running_loop().run_in_executor(None, fn)
            body = response["Body"]
            read_fn = partial(body.read)
            return await asyncio.get_running_loop().run_in_executor(None, read_fn)
        except Exception as exc:
            err = str(exc)
            if "NoSuchKey" in err or "404" in err:
                return None
            logger.warning("ObjectStorage download failed key=%s: %s", key, exc)
            return None

    async def exists(self, key: str) -> bool:
        """Return True if the key exists in the bucket."""
        try:
            client = self._boto_client()
            fn = partial(client.head_object, Bucket=self._bucket, Key=key)
            await asyncio.get_running_loop().run_in_executor(None, fn)
            return True
        except Exception:
            return False

    async def delete(self, key: str) -> None:
        """Delete a key — silently ignores missing keys."""
        try:
            client = self._boto_client()
            fn = partial(client.delete_object, Bucket=self._bucket, Key=key)
            await asyncio.get_running_loop().run_in_executor(None, fn)
        except Exception as exc:
            logger.debug("ObjectStorage delete failed key=%s: %s", key, exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _boto_client(self):
        """Return (or lazily create) a boto3 S3 client."""
        if self._client is None:
            try:
                import boto3  # type: ignore
            except ImportError:
                raise RuntimeError(
                    "boto3 is required for object storage. "
                    "Add boto3 to pyproject.toml dependencies."
                )
            kwargs = dict(
                region_name=self._region,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
            )
            if self._endpoint:
                kwargs["endpoint_url"] = self._endpoint
            self._client = boto3.client("s3", **kwargs)
        return self._client


class _NoOpStorageClient(ObjectStorageClient):
    """Returned when object storage is not configured. All ops are silent no-ops."""

    def __init__(self) -> None:
        pass  # skip parent __init__

    @property
    def enabled(self) -> bool:
        return False

    async def upload(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> bool:
        return False

    async def download(self, key: str) -> Optional[bytes]:
        return None

    async def exists(self, key: str) -> bool:
        return False

    async def delete(self, key: str) -> None:
        pass
