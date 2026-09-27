"""Archive one session into the retention bucket. The I/O half of `archive.py`.

Runs with the foundation's archiver key only. That key writes the retention
bucket without delete and reads the session through the grant the session stack
gave it, so a bug here can fail to archive but cannot destroy an archive.

Object Storage does not copy between buckets in different Projects in one call
(an API key has one preferred Project), so each object is downloaded through a
client scoped to the session Project and re-uploaded through one scoped to the
retention Project, hashed on the way.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as dt
import hashlib
import json
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import IO, Any

import boto3
from botocore.config import Config

from documenso_scw import archive

API = "https://api.scaleway.com"
CHUNK = 1024 * 1024
BACKUP_POLL_SECONDS = 10
BACKUP_TIMEOUT_SECONDS = 60 * 60


@dataclasses.dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str


@dataclasses.dataclass(frozen=True)
class SessionFacts:
    """The session stack's outputs the archive needs."""

    session_id: str
    region: str
    project_id: str
    starts_at: str
    expires_at: str
    database_instance_id: str
    database_name: str
    uploads_bucket: str


@dataclasses.dataclass(frozen=True)
class RetentionFacts:
    region: str
    project_id: str
    bucket: str


def s3_client(credentials: Credentials, region: str, project_id: str) -> Any:
    # `ACCESS_KEY@PROJECT_ID` selects the Project an Object Storage call acts in.
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.scw.cloud",
        aws_access_key_id=f"{credentials.access_key}@{project_id}",
        aws_secret_access_key=credentials.secret_key,
        config=Config(
            signature_version="s3v4",
            # Scaleway does not implement the CRC checksums newer botocore sends by
            # default; integrity is enforced with Content-MD5 and a sha256 below.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 8, "mode": "standard"},
        ),
    )


# ---- Scaleway REST -------------------------------------------------------------


def _api(credentials: Credentials, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Auth-Token": credentials.secret_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path} returned {type(payload).__name__}, expected an object")
    return payload


def _wait_for_backup(
    credentials: Credentials,
    region: str,
    backup_id: str,
    ready: Callable[[dict[str, Any]], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS
    while True:
        backup = _api(credentials, "GET", f"/rdb/v1/regions/{region}/backups/{backup_id}")
        if backup["status"] == "error":
            raise RuntimeError(f"database backup {backup_id} failed")
        if ready(backup):
            return backup
        if time.monotonic() > deadline:
            raise TimeoutError(f"database backup {backup_id} still {backup['status']} after {BACKUP_TIMEOUT_SECONDS}s")
        sleep(BACKUP_POLL_SECONDS)


def export_database(credentials: Credentials, facts: SessionFacts) -> tuple[str, str]:
    """Create a logical backup, export it, return (backup_id, download_url)."""
    region = facts.region
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    created = _api(
        credentials,
        "POST",
        f"/rdb/v1/regions/{region}/backups",
        {
            "instance_id": facts.database_instance_id,
            "database_name": facts.database_name,
            "name": f"archive-{facts.session_id}",
            "expires_at": expires,
        },
    )
    backup_id = created["id"]

    _wait_for_backup(credentials, region, backup_id, lambda backup: backup["status"] == "ready")
    _api(credentials, "POST", f"/rdb/v1/regions/{region}/backups/{backup_id}/export", {})
    exported = _wait_for_backup(
        credentials,
        region,
        backup_id,
        lambda backup: backup["status"] == "ready" and bool(backup.get("download_url")),
    )
    return backup_id, exported["download_url"]


# ---- copying -------------------------------------------------------------------


@dataclasses.dataclass
class Spooled:
    file: IO[bytes]
    size: int
    sha256: str
    md5_base64: str


def spool(chunks: Iterator[bytes]) -> Spooled:
    """Write a stream to a temp file, hashing it; the PUT needs Content-MD5 up front."""
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()  # noqa: S324 — Content-MD5 is an integrity header, not a security control
    file = tempfile.TemporaryFile()
    size = 0

    for chunk in chunks:
        sha256.update(chunk)
        md5.update(chunk)
        file.write(chunk)
        size += len(chunk)

    if size > archive.MAX_SINGLE_PUT_BYTES:
        file.close()
        raise ValueError(f"{size} bytes is over the single-PUT limit")

    file.seek(0)
    return Spooled(file=file, size=size, sha256=sha256.hexdigest(), md5_base64=base64.b64encode(md5.digest()).decode())


def _read_chunks(stream: Any) -> Iterator[bytes]:
    while chunk := stream.read(CHUNK):
        yield chunk


def put_archived(retention: Any, bucket: str, key: str, spooled: Spooled, content_type: str) -> None:
    with spooled.file:
        retention.put_object(
            Bucket=bucket,
            Key=key,
            Body=spooled.file,
            ContentLength=spooled.size,
            ContentMD5=spooled.md5_base64,
            ContentType=content_type,
            Metadata={"sha256": spooled.sha256},
        )


def list_versions(source: Any, bucket: str) -> list[archive.SourceObject]:
    found: list[archive.SourceObject] = []
    paginator = source.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        for version in page.get("Versions", []):
            found.append(
                archive.SourceObject(
                    key=version["Key"],
                    version_id=version["VersionId"],
                    is_latest=bool(version["IsLatest"]),
                    size=int(version["Size"]),
                )
            )
    return found


def observe(retention: Any, bucket: str, keys: list[str]) -> dict[str, archive.Observed]:
    observed: dict[str, archive.Observed] = {}
    for key in keys:
        try:
            head = retention.head_object(Bucket=bucket, Key=key)
        except retention.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                continue
            raise
        locked = head.get("ObjectLockRetainUntilDate")
        observed[key] = archive.Observed(
            size=int(head["ContentLength"]),
            sha256=head.get("Metadata", {}).get("sha256"),
            locked_until=locked.isoformat() if locked else None,
        )
    return observed


def read_manifest(retention: Any, bucket: str, session_id: str) -> archive.Manifest | None:
    try:
        body = retention.get_object(Bucket=bucket, Key=archive.manifest_key(session_id))["Body"].read()
    except retention.exceptions.NoSuchKey:
        return None
    return archive.Manifest.from_json(body.decode())


def verify_archive(retention: Any, bucket: str, manifest: archive.Manifest) -> list[str]:
    return archive.verify(manifest, observe(retention, bucket, [item.destination for item in manifest.items()]))


def archive_session(credentials: Credentials, facts: SessionFacts, retention_facts: RetentionFacts) -> archive.Manifest:
    """Copy the database backup and every upload version, then write the manifest last."""
    source = s3_client(credentials, facts.region, facts.project_id)
    retention = s3_client(credentials, retention_facts.region, retention_facts.project_id)

    backup_id, download_url = export_database(credentials, facts)
    with urllib.request.urlopen(download_url, timeout=600) as response:
        database_spool = spool(_read_chunks(response))
    database_key = archive.database_key(facts.session_id, facts.database_name)
    database_item = archive.ArchivedItem(
        source=f"rdb-backup:{backup_id}",
        destination=database_key,
        size=database_spool.size,
        sha256=database_spool.sha256,
    )
    put_archived(retention, retention_facts.bucket, database_key, database_spool, "application/octet-stream")

    items: list[archive.ArchivedItem] = []
    for version, destination in archive.plan_objects(facts.session_id, list_versions(source, facts.uploads_bucket)):
        body = source.get_object(Bucket=facts.uploads_bucket, Key=version.key, VersionId=version.version_id)["Body"]
        spooled = spool(_read_chunks(body))
        if spooled.size != version.size:
            raise RuntimeError(f"{version.key}@{version.version_id}: read {spooled.size} bytes, listed {version.size}")
        put_archived(retention, retention_facts.bucket, destination, spooled, "application/octet-stream")
        items.append(
            archive.ArchivedItem(
                source=f"{facts.uploads_bucket}/{version.key}@{version.version_id}",
                destination=destination,
                size=spooled.size,
                sha256=spooled.sha256,
            )
        )

    manifest = archive.Manifest(
        session_id=facts.session_id,
        starts_at=facts.starts_at,
        expires_at=facts.expires_at,
        archived_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        database=database_item,
        objects=tuple(items),
    )

    manifest_bytes = manifest.to_json().encode()
    put_archived(
        retention,
        retention_facts.bucket,
        archive.manifest_key(facts.session_id),
        spool(iter([manifest_bytes])),
        "application/json",
    )
    return manifest
