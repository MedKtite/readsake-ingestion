"""Storage backends for the cleaned MP3s — local dir or S3/MinIO.

Selected by STORAGE_BACKEND=local|s3 (default local).
  local: STORAGE_DIR (default ./cleaned), optional STORAGE_PUBLIC_BASE
  s3:    S3_BUCKET (required), optional S3_ENDPOINT_URL (MinIO),
         optional S3_PUBLIC_BASE for the returned URL
"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class StorageBackend(ABC):
    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return false only for an absent object; propagate storage outages."""

    @abstractmethod
    def upload(self, local_path: str, key: str, content_type: str = "application/octet-stream") -> str:
        """Store the file under `key`; return the URL/path to record in DB."""

    @abstractmethod
    def download(self, key: str, local_path: str) -> None:
        """Copy an object to a local path for processing."""


class LocalStorage(StorageBackend):
    def exists(self, key: str) -> bool:
        return (self._root / key).is_file()

    def __init__(self, root: str, public_base: str = ""):
        self._root = Path(root)
        self._public_base = public_base.rstrip("/")

    def upload(self, local_path: str, key: str, content_type: str = "application/octet-stream") -> str:
        dest = self._root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local_path, dest)
        if self._public_base:
            return f"{self._public_base}/{key}"
        return str(dest)

    def download(self, key: str, local_path: str) -> None:
        source = self._root / key
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, local_path)


class S3Storage(StorageBackend):
    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def __init__(self, bucket: str, endpoint_url: str = "", public_base: str = ""):
        import boto3  # imported here so local-only runs don't need it
        from botocore.config import Config

        self._bucket = bucket
        self._public_base = public_base.rstrip("/")
        self._endpoint = endpoint_url.rstrip("/")
        # Accept the same explicit names used by Spring Boot's .env while
        # retaining AWS's standard names for cloud deployments.
        access_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
        credentials = {}
        if access_key and secret_key:
            credentials = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key}
        addressing_style = "path" if os.getenv("S3_FORCE_PATH_STYLE", "false").lower() == "true" else "auto"
        self._client = boto3.client(
            "s3", endpoint_url=endpoint_url or None,
            config=Config(s3={"addressing_style": addressing_style}),
            **credentials,
        )

    def upload(self, local_path: str, key: str, content_type: str = "application/octet-stream") -> str:
        self._client.upload_file(
            local_path, self._bucket, key,
            ExtraArgs={"ContentType": content_type},
        )
        if self._public_base:
            return f"{self._public_base}/{key}"
        if self._endpoint:
            return f"{self._endpoint}/{self._bucket}/{key}"
        return f"https://{self._bucket}.s3.amazonaws.com/{key}"

    def download(self, key: str, local_path: str) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, key, local_path)


def make_storage() -> StorageBackend:
    backend = os.getenv("STORAGE_BACKEND", "local").lower()
    if backend == "s3":
        return S3Storage(
            bucket=os.environ["S3_BUCKET"],
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", ""),
            public_base=os.getenv("S3_PUBLIC_BASE", ""),
        )
    return LocalStorage(
        root=os.getenv("STORAGE_DIR", "./cleaned"),
        public_base=os.getenv("STORAGE_PUBLIC_BASE", ""),
    )
