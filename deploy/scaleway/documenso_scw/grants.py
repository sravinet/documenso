"""Who may do what, and in which Project.

Every permission set named here must exist in `permission_sets.json`, a parsed
snapshot of Scaleway's reference (copied from CaptainEmpower/captain-assets,
which records its source and hash). `check_boundary.py` enforces that, and that
none of them is Organization-scoped.

The boundaries these tuples draw:

* The **app** (the Documenso container) reaches its own session's uploads and
  nothing else. It never touches the retention Project.
* The **archiver** writes into the retention bucket but holds no delete, and the
  bucket's COMPLIANCE object lock refuses deletion regardless. The same
  Application gets read-only reach into each session, granted by that session's
  stack so the grant is destroyed with it.
* The **mailer** may only mint SMTP credentials in the mail Project.
"""

from __future__ import annotations

import json
import pathlib

_SNAPSHOT = pathlib.Path(__file__).with_name("permission_sets.json")

APP_SESSION = (
    "ObjectStorageBucketsRead",
    "ObjectStorageObjectsRead",
    "ObjectStorageObjectsWrite",
    # Documenso deletes the files of deleted drafts. The bucket is versioned, so
    # a deletion still leaves the prior version for the archiver.
    "ObjectStorageObjectsDelete",
)

ARCHIVER_RETENTION = (
    "ObjectStorageBucketsRead",
    "ObjectStorageObjectsRead",
    "ObjectStorageObjectsWrite",
)

# Creating a logical backup and exporting it has no read-only permission set;
# `RelationalDatabasesReadOnly` cannot start a backup.
ARCHIVER_SESSION = (
    "ObjectStorageBucketsRead",
    "ObjectStorageObjectsRead",
    "RelationalDatabasesFullAccess",
)

MAILER_MAIL = ("TransactionalEmailEmailSmtpCreate",)

ALL_GRANTS = {
    "APP_SESSION": APP_SESSION,
    "ARCHIVER_RETENTION": ARCHIVER_RETENTION,
    "ARCHIVER_SESSION": ARCHIVER_SESSION,
    "MAILER_MAIL": MAILER_MAIL,
}

# What must never appear in a grant, whatever the Project.
FORBIDDEN = frozenset(
    {
        "ObjectStorageFullAccess",
        "ObjectStorageBucketsDelete",
        "ObjectStorageBucketPolicyFullAccess",
    }
)


def permission_set_scopes() -> dict[str, str]:
    """name -> "Project" | "Organization", from the committed snapshot."""
    doc = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return {name: entry["scope"] for name, entry in doc["permission_sets"].items()}


def grant_problems() -> list[str]:
    """Every way the tuples above disagree with the snapshot or the rules."""
    scopes = permission_set_scopes()
    problems: list[str] = []

    for grant, names in ALL_GRANTS.items():
        for name in names:
            if name not in scopes:
                problems.append(f"{grant}: {name} is not a Scaleway permission set")
            elif scopes[name] != "Project":
                problems.append(f"{grant}: {name} is {scopes[name]}-scoped, not Project-scoped")

            if name in FORBIDDEN:
                problems.append(f"{grant}: {name} is forbidden")

        if len(set(names)) != len(names):
            problems.append(f"{grant}: duplicate permission set")

    if "ObjectStorageObjectsDelete" in ARCHIVER_RETENTION:
        problems.append("ARCHIVER_RETENTION: the archiver must not delete archives")

    return problems
