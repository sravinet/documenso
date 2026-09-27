"""What a session archive must contain, and whether one does. Pure.

The reaper destroys a session only when `verify` returns no problems for the
manifest it wrote, checked against what the retention bucket actually reports.
Everything here is decided from values; the I/O lives in `reaper.py`.

Layout under `sessions/<id>/` in the retention bucket:

    database/<name>.dump                 the exported logical backup
    uploads/<key>                        the current version of every object
    uploads-noncurrent/<version>/<key>   every older version (deleted drafts too)
    manifest.json                        written last; its presence means "complete"
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterable, Mapping

from documenso_scw import naming

MANIFEST_VERSION = 1

# One PUT is at most 5 GB on S3-compatible storage. The reaper refuses larger
# items rather than silently truncating or switching to an unverified path.
MAX_SINGLE_PUT_BYTES = 5 * 1024**3


@dataclasses.dataclass(frozen=True)
class SourceObject:
    key: str
    version_id: str
    is_latest: bool
    size: int


@dataclasses.dataclass(frozen=True)
class ArchivedItem:
    source: str
    destination: str
    size: int
    sha256: str


@dataclasses.dataclass(frozen=True)
class Observed:
    """What the retention bucket reports for one key (HEAD)."""

    size: int
    sha256: str | None
    locked_until: str | None


def manifest_key(session_id: str) -> str:
    return f"{naming.archive_prefix(session_id)}manifest.json"


def database_key(session_id: str, database_name: str) -> str:
    return f"{naming.archive_prefix(session_id)}database/{database_name}.dump"


def object_destination(session_id: str, source: SourceObject) -> str:
    prefix = naming.archive_prefix(session_id)
    if source.is_latest:
        return f"{prefix}uploads/{source.key}"
    return f"{prefix}uploads-noncurrent/{source.version_id}/{source.key}"


def plan_objects(session_id: str, sources: Iterable[SourceObject]) -> list[tuple[SourceObject, str]]:
    """Pair every source version with its destination, refusing collisions and oversize items."""
    planned: list[tuple[SourceObject, str]] = []
    destinations: set[str] = set()

    for source in sources:
        if source.size > MAX_SINGLE_PUT_BYTES:
            raise ValueError(f"{source.key}@{source.version_id} is {source.size} bytes, over the single-PUT limit.")

        destination = object_destination(session_id, source)
        if destination in destinations:
            raise ValueError(f"two source versions map to {destination}; refusing to overwrite in the archive.")

        destinations.add(destination)
        planned.append((source, destination))

    return planned


@dataclasses.dataclass(frozen=True)
class Manifest:
    session_id: str
    starts_at: str
    expires_at: str
    archived_at: str
    database: ArchivedItem
    objects: tuple[ArchivedItem, ...]

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": MANIFEST_VERSION,
                "session_id": self.session_id,
                "starts_at": self.starts_at,
                "expires_at": self.expires_at,
                "archived_at": self.archived_at,
                "database": dataclasses.asdict(self.database),
                "objects": [dataclasses.asdict(item) for item in self.objects],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str) -> Manifest:
        doc = json.loads(raw)
        if doc.get("version") != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest version {doc.get('version')!r}")

        return cls(
            session_id=doc["session_id"],
            starts_at=doc["starts_at"],
            expires_at=doc["expires_at"],
            archived_at=doc["archived_at"],
            database=ArchivedItem(**doc["database"]),
            objects=tuple(ArchivedItem(**item) for item in doc["objects"]),
        )

    def items(self) -> tuple[ArchivedItem, ...]:
        return (self.database, *self.objects)


def verify(manifest: Manifest, observed: Mapping[str, Observed]) -> list[str]:
    """Every way the retention bucket disagrees with the manifest. Empty means safe to destroy."""
    problems: list[str] = []
    prefix = naming.archive_prefix(manifest.session_id)

    for item in manifest.items():
        if not item.destination.startswith(prefix):
            problems.append(f"{item.destination} is outside {prefix}")
            continue

        seen = observed.get(item.destination)
        if seen is None:
            problems.append(f"{item.destination} is missing from the retention bucket")
            continue

        if seen.size != item.size:
            problems.append(f"{item.destination}: size {seen.size} != manifest {item.size}")

        if seen.sha256 != item.sha256:
            problems.append(f"{item.destination}: sha256 {seen.sha256} != manifest {item.sha256}")

        if not seen.locked_until:
            problems.append(f"{item.destination} carries no object-lock retention")

    if manifest.database.size == 0:
        problems.append("database backup is empty")

    return problems


def may_destroy(*, expired: bool, archive_problems: list[str] | None, force_before_expiry: bool) -> tuple[bool, str]:
    """The single gate in front of `pulumi destroy` for a session."""
    if archive_problems is None:
        return False, "no archive has been verified"

    if archive_problems:
        return False, f"archive verification failed: {archive_problems[0]}"

    if not expired and not force_before_expiry:
        return False, "session has not expired"

    return True, "archive verified"
