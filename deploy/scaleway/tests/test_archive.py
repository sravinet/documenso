import pytest

from documenso_scw import archive

SESSION = "board-q4"
PREFIX = "sessions/board-q4/"


def source(key: str, version: str, latest: bool, size: int = 10) -> archive.SourceObject:
    return archive.SourceObject(key=key, version_id=version, is_latest=latest, size=size)


def manifest(objects=None, database_size=1024) -> archive.Manifest:
    database = archive.ArchivedItem(
        source="backup-id",
        destination=archive.database_key(SESSION, "documenso"),
        size=database_size,
        sha256="d" * 64,
    )
    return archive.Manifest(
        session_id=SESSION,
        starts_at="2026-10-01T08:00:00Z",
        expires_at="2026-10-03T08:00:00Z",
        archived_at="2026-10-03T08:05:00Z",
        database=database,
        objects=tuple(objects or ()),
    )


def item(destination: str, size: int = 10, sha: str = "a" * 64) -> archive.ArchivedItem:
    return archive.ArchivedItem(source="src", destination=destination, size=size, sha256=sha)


def observed_for(m: archive.Manifest, locked: str | None = "2036-10-03T08:05:00Z"):
    return {i.destination: archive.Observed(size=i.size, sha256=i.sha256, locked_until=locked) for i in m.items()}


def test_keys_live_under_the_session_prefix():
    assert archive.manifest_key(SESSION) == f"{PREFIX}manifest.json"
    assert archive.database_key(SESSION, "documenso") == f"{PREFIX}database/documenso.dump"


def test_current_and_noncurrent_versions_get_distinct_destinations():
    planned = archive.plan_objects(
        SESSION,
        [source("doc/a.pdf", "v2", True), source("doc/a.pdf", "v1", False), source("draft.pdf", "v9", False)],
    )

    assert [destination for _, destination in planned] == [
        f"{PREFIX}uploads/doc/a.pdf",
        f"{PREFIX}uploads-noncurrent/v1/doc/a.pdf",
        f"{PREFIX}uploads-noncurrent/v9/draft.pdf",
    ]


def test_plan_refuses_collisions():
    with pytest.raises(ValueError, match="refusing to overwrite"):
        archive.plan_objects(SESSION, [source("a.pdf", "v1", True), source("a.pdf", "v2", True)])


def test_plan_refuses_items_over_the_single_put_limit():
    with pytest.raises(ValueError, match="single-PUT limit"):
        archive.plan_objects(SESSION, [source("big.bin", "v1", True, size=archive.MAX_SINGLE_PUT_BYTES + 1)])


def test_manifest_round_trips_through_json():
    original = manifest([item(f"{PREFIX}uploads/a.pdf")])

    assert archive.Manifest.from_json(original.to_json()) == original


def test_manifest_rejects_unknown_versions():
    with pytest.raises(ValueError, match="unsupported manifest version"):
        archive.Manifest.from_json('{"version": 99}')


def test_verify_passes_when_everything_matches_and_is_locked():
    m = manifest([item(f"{PREFIX}uploads/a.pdf"), item(f"{PREFIX}uploads-noncurrent/v1/a.pdf", size=7)])

    assert archive.verify(m, observed_for(m)) == []


def test_verify_reports_missing_mismatched_and_unlocked_items():
    m = manifest([item(f"{PREFIX}uploads/a.pdf"), item(f"{PREFIX}uploads/b.pdf")])
    observed = observed_for(m)
    del observed[f"{PREFIX}uploads/a.pdf"]
    observed[f"{PREFIX}uploads/b.pdf"] = archive.Observed(size=11, sha256="b" * 64, locked_until=None)

    problems = archive.verify(m, observed)

    assert problems == [
        f"{PREFIX}uploads/a.pdf is missing from the retention bucket",
        f"{PREFIX}uploads/b.pdf: size 11 != manifest 10",
        f"{PREFIX}uploads/b.pdf: sha256 {'b' * 64} != manifest {'a' * 64}",
        f"{PREFIX}uploads/b.pdf carries no object-lock retention",
    ]


def test_verify_refuses_items_outside_the_session_prefix():
    m = manifest([item("sessions/other/uploads/a.pdf")])

    assert archive.verify(m, observed_for(m)) == ["sessions/other/uploads/a.pdf is outside sessions/board-q4/"]


def test_verify_refuses_an_empty_database_backup():
    m = manifest(database_size=0)

    assert archive.verify(m, observed_for(m)) == ["database backup is empty"]


def test_a_session_with_no_uploads_still_archives_its_database():
    m = manifest([])

    assert archive.verify(m, observed_for(m)) == []


@pytest.mark.parametrize(
    ("expired", "problems", "force", "allowed", "reason"),
    [
        (True, [], False, True, "archive verified"),
        (True, None, False, False, "no archive has been verified"),
        (True, ["x missing"], False, False, "archive verification failed: x missing"),
        (False, [], False, False, "session has not expired"),
        (False, [], True, True, "archive verified"),
        (False, None, True, False, "no archive has been verified"),
    ],
)
def test_may_destroy_gate(expired, problems, force, allowed, reason):
    assert archive.may_destroy(expired=expired, archive_problems=problems, force_before_expiry=force) == (
        allowed,
        reason,
    )
