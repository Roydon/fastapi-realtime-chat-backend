"""S3-compatible attachment storage (MinIO locally, S3 in AWS).

Uploads use a presigned POST policy rather than a presigned PUT, because only a POST
policy lets the storage service itself enforce the size limit (`content-length-range`)
and the exact Content-Type. The API never proxies file bytes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@dataclass(frozen=True)
class StoredObject:
    size: int
    content_type: str


class AttachmentStorage:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.bucket = settings.s3_bucket
        self._client = self._make_client(settings.s3_endpoint_url)
        # Presigned URLs embed the host the browser will use, which can differ from the
        # in-cluster endpoint (for example nginx on localhost:8080 proxying to minio:9000).
        public = settings.s3_public_endpoint_url or settings.s3_endpoint_url
        self._signer = self._make_client(public)

    def _make_client(self, endpoint_url: str | None) -> S3Client:
        s = self._settings
        return boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def presign_upload(self, key: str, content_type: str) -> dict[str, Any]:
        s = self._settings
        post = self._signer.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, s.attachment_max_bytes],
            ],
            ExpiresIn=s.presign_expiry_seconds,
        )
        return dict(post)

    def presign_download(self, key: str) -> str:
        return self._signer.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self._settings.download_url_expiry_seconds,
        )

    async def head(self, key: str) -> StoredObject | None:
        try:
            meta = await asyncio.to_thread(self._client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return StoredObject(
            size=int(meta.get("ContentLength", 0)),
            content_type=str(meta.get("ContentType", "application/octet-stream")),
        )

    async def ensure_bucket(self) -> None:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
        except ClientError:
            await asyncio.to_thread(self._client.create_bucket, Bucket=self.bucket)
