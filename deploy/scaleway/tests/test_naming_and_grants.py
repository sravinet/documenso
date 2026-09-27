import pytest

from documenso_scw import grants, naming


@pytest.mark.parametrize("session_id", ["abc", "board-2026-10", "q4-signing", "a1b"])
def test_valid_session_ids(session_id):
    assert naming.validate_session_id(session_id) == session_id


@pytest.mark.parametrize(
    "session_id",
    ["ab", "1abc", "Abc", "abc-", "a--b", "abc_def", "a" * 21, "", "abc.def"],
)
def test_invalid_session_ids(session_id):
    with pytest.raises(naming.NamingError):
        naming.validate_session_id(session_id)


def test_names_derive_from_the_session_id():
    assert naming.session_project("board-q4") == "documenso-board-q4"
    assert naming.session_stack("board-q4") == "session-board-q4"
    assert naming.archive_prefix("board-q4") == "sessions/board-q4/"


def test_stack_name_round_trip_and_rejection():
    assert naming.session_id_from_stack("session-board-q4") == "board-q4"

    with pytest.raises(naming.NamingError):
        naming.session_id_from_stack("foundation")

    with pytest.raises(naming.NamingError):
        naming.session_id_from_stack("session-BAD")


def test_every_grant_is_a_real_project_scoped_permission_set():
    assert grants.grant_problems() == []


def test_snapshot_knows_organization_scoped_sets():
    scopes = grants.permission_set_scopes()

    assert scopes["ProjectManager"] == "Organization"
    assert scopes["ObjectStorageObjectsWrite"] == "Project"


def test_archiver_cannot_delete_in_retention():
    assert "ObjectStorageObjectsDelete" not in grants.ARCHIVER_RETENTION
    assert "ObjectStorageBucketsDelete" not in grants.ARCHIVER_RETENTION
    assert "ObjectStorageFullAccess" not in grants.ARCHIVER_RETENTION


def test_app_and_mailer_hold_nothing_beyond_their_product():
    assert all(name.startswith("ObjectStorage") for name in grants.APP_SESSION)
    assert grants.MAILER_MAIL == ("TransactionalEmailEmailSmtpCreate",)
