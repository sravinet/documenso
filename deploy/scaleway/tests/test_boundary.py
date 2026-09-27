"""The declared resource graph keeps its boundaries, checked by running it.

Like `captain-assets/scripts/check_trail_boundary.py`, this does not read the
program: it executes `foundation.declare` and `session.declare` under Pulumi's
mock monitor, which stands in for the engine (there is no other way to resolve
`Output`s without a cloud), and asserts on what actually gets registered.

What this cannot catch is Scaleway enforcing a rule differently from its
documentation. That needs a live session: see README "Verifying a live session".
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pulumi
import pytest

from documenso_scw import grants
from documenso_scw.window import SessionWindow, format_timestamp

import datetime as dt

PROJECT = "scaleway:account/project:Project"
POLICY = "scaleway:iam/policy:Policy"
API_KEY = "scaleway:iam/apiKey:ApiKey"
BUCKET = "scaleway:object/bucket:Bucket"
LOCK = "scaleway:object/bucketLockConfiguration:BucketLockConfiguration"
PG_INSTANCE = "scaleway:databases/instance:Instance"
PG_USER = "scaleway:databases/user:User"
PG_PRIVILEGE = "scaleway:databases/privilege:Privilege"
CONTAINER = "scaleway:containers/container:Container"
NAMESPACE = "scaleway:containers/namespace:Namespace"
RECORD = "scaleway:domain/record:Record"
RANDOM_PASSWORD = "random:index/randomPassword:RandomPassword"
RANDOM_STRING = "random:index/randomString:RandomString"

WINDOW = SessionWindow.opening(dt.datetime(2026, 10, 1, 8, 0, 0, tzinfo=dt.timezone.utc))


@dataclasses.dataclass
class Declared:
    typ: str
    name: str
    inputs: dict[str, Any]
    id: str


class Recorder(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.seen: list[Declared] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        rid = f"{args.name}-id"
        inputs = dict(args.inputs)
        self.seen.append(Declared(args.typ, args.name, inputs, rid))
        outputs = dict(inputs)
        if args.typ == RANDOM_PASSWORD:
            outputs["result"] = f"pw-{args.name}"
        elif args.typ == RANDOM_STRING:
            outputs["result"] = "abcd1234"
        elif args.typ == API_KEY:
            outputs["accessKey"] = f"SCW{args.name}"
            outputs["secretKey"] = f"secret-{args.name}"
        elif args.typ == PG_INSTANCE:
            network = dict((inputs.get("privateNetwork") or {}))
            network.update({"hostname": f"{args.name}.internal", "ip": "10.0.0.5", "port": 5432})
            outputs["privateNetwork"] = network
        elif args.typ == CONTAINER:
            outputs["publicEndpoint"] = "https://documensosession-documenso.functions.fnc.fr-par.scw.cloud"
        elif args.typ == "scaleway:registry/namespace:Namespace":
            outputs["endpoint"] = f"rg.fr-par.scw.cloud/{inputs.get('name')}"
        return rid, outputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}

    def of(self, typ: str) -> list[Declared]:
        return [resource for resource in self.seen if resource.typ == typ]

    def one(self, typ: str, name: str) -> Declared:
        matches = [resource for resource in self.of(typ) if resource.name == name]
        assert len(matches) == 1, f"expected one {typ} named {name}, found {len(matches)}"
        return matches[0]


def _declare_session(recorder: Recorder, phase: str, dns_zone: str | None = "example.com"):
    pulumi.runtime.set_mocks(recorder, project="documenso", stack="session-board-q4", preview=False)

    from documenso_scw import session

    @pulumi.runtime.test
    def run():
        declared = session.declare(
            session_id="board-q4",
            window=WINDOW,
            phase=phase,
            organization_id="org-id",
            region="fr-par",
            hostname="sign.example.com",
            dns_zone=dns_zone,
            image="rg.fr-par.scw.cloud/documenso/documenso:v2.18.0",
            cpu_limit_mvcpu=2000,
            memory_gib=4,
            max_scale=3,
            db_node_type="DB-PRO2-XXS",
            db_volume_gib=10,
            mail_project_id="mail-id",
            mail_from_address="sign@example.com",
            mail_from_name="Board",
            archiver_application_id="archiver-id",
            signing_certificate_base64=pulumi.Output.secret("Y2VydA=="),
            signing_passphrase=pulumi.Output.secret("pass"),
        )
        outputs = [
            declared.project.id,
            declared.database_instance.id,
            declared.app_privilege.id,
            declared.uploads.id,
            declared.app_key.id,
            declared.mailer_key.id,
            declared.archiver_policy.id,
            declared.images.id,
            declared.namespace.id,
        ]
        if declared.container is not None:
            outputs += [declared.container.id, declared.container_domain.id]
        if declared.dns_record is not None:
            outputs.append(declared.dns_record.id)
        return pulumi.Output.all(*outputs)

    run()
    return recorder


def _declare_foundation(recorder: Recorder):
    pulumi.runtime.set_mocks(recorder, project="documenso", stack="foundation", preview=False)

    from documenso_scw import foundation

    @pulumi.runtime.test
    def run():
        declared = foundation.declare(
            organization_id="org-id",
            region="fr-par",
            retention_bucket_name="documenso-archive-test",
            retention_years=10,
            mail_domain="example.com",
            mail_domain_autoconfig=True,
        )
        return pulumi.Output.all(
            declared.retention.id,
            declared.archive_lock.id,
            declared.archiver_policy.id,
            declared.archiver_key.id,
            declared.mail_domain.id,
        )

    run()
    return recorder


@pytest.fixture
def live() -> Recorder:
    return _declare_session(Recorder(), "live")


@pytest.fixture
def base() -> Recorder:
    return _declare_foundation(Recorder())


# Pulumi's wire marker for a secret value.
_SECRET_SIG = "4dabf18193072939515e22adb298388d"


def _reveal(value: Any) -> tuple[Any, bool]:
    """(value, was_secret) for an input that may carry Pulumi's secret envelope."""
    if isinstance(value, dict) and value.get(_SECRET_SIG) == "1b47061264138c4ac30d75fd1eb44270":
        return value["value"], True
    return value, False


def _rules(policy: Declared) -> list[dict[str, Any]]:
    return policy.inputs["rules"]


# ---- session placement ---------------------------------------------------------


def test_every_placed_session_resource_is_in_the_session_project(live: Recorder):
    project = live.one(PROJECT, "session")
    assert project.inputs["name"] == "documenso-board-q4"

    placed = [r for r in live.seen if "projectId" in r.inputs]
    assert {r.typ for r in placed} >= {BUCKET, PG_INSTANCE, NAMESPACE, "scaleway:network/vpc:Vpc"}
    for resource in placed:
        assert resource.inputs["projectId"] == project.id, (
            f"{resource.typ} {resource.name} is outside the session project"
        )


def test_database_is_private_ha_encrypted_and_app_user_is_not_admin(live: Recorder):
    instance = live.one(PG_INSTANCE, "session")

    assert "loadBalancer" not in instance.inputs
    assert instance.inputs["privateNetwork"]["pnId"] == "session-id"
    assert instance.inputs["isHaCluster"] is True
    assert instance.inputs["encryptionAtRest"] is True
    assert instance.inputs["disableBackup"] is False

    user = live.one(PG_USER, "documenso")
    assert user.inputs["isAdmin"] is False
    assert live.one(PG_PRIVILEGE, "documenso").inputs["databaseName"] == "documenso"


def test_uploads_bucket_is_versioned_and_cors_is_limited_to_the_host(live: Recorder):
    bucket = live.one(BUCKET, "uploads")

    assert bucket.inputs["versioning"] == {"enabled": True}
    assert bucket.inputs["corsRules"][0]["allowedOrigins"] == ["https://sign.example.com"]
    assert bucket.inputs["name"] == "documenso-board-q4-uploads-abcd1234"


# ---- session identities --------------------------------------------------------


def test_app_policy_reaches_only_the_session_project(live: Recorder):
    [rule] = _rules(live.one(POLICY, "app-session"))

    assert rule["permissionSetNames"] == list(grants.APP_SESSION)
    assert rule["projectIds"] == ["session-id"]
    assert "organizationId" not in rule


def test_mailer_policy_reaches_only_the_mail_project(live: Recorder):
    [rule] = _rules(live.one(POLICY, "mailer-mail"))

    assert rule["permissionSetNames"] == ["TransactionalEmailEmailSmtpCreate"]
    assert rule["projectIds"] == ["mail-id"]


def test_archiver_is_granted_read_on_this_session_only(live: Recorder):
    policy = live.one(POLICY, "archiver-session")
    [rule] = _rules(policy)

    assert policy.inputs["applicationId"] == "archiver-id"
    assert rule["projectIds"] == ["session-id"]
    assert "ObjectStorageObjectsWrite" not in rule["permissionSetNames"]
    assert "ObjectStorageObjectsDelete" not in rule["permissionSetNames"]


def test_no_session_policy_is_organization_wide(live: Recorder):
    for policy in live.of(POLICY):
        for rule in _rules(policy):
            assert "organizationId" not in rule, f"{policy.name} has an organization-wide rule"
            assert rule["projectIds"], f"{policy.name} has a rule with no project"


def test_every_session_key_expires_after_the_grace_period(live: Recorder):
    keys = live.of(API_KEY)

    assert {key.name for key in keys} == {"app-key", "mailer-key"}
    for key in keys:
        assert key.inputs["expiresAt"] == format_timestamp(WINDOW.credentials_expire_at)


# ---- the container ------------------------------------------------------------


def test_live_container_is_always_on_private_networked_and_https_only(live: Recorder):
    container = live.one(CONTAINER, "documenso")

    assert container.inputs["minScale"] == 1
    assert container.inputs["httpsConnectionsOnly"] is True
    assert container.inputs["privateNetworkId"] == "session-id"
    assert container.inputs["port"] == 3000
    assert container.inputs["startupProbe"]["http"]["path"] == "/api/health"


def test_live_container_keeps_credentials_out_of_plain_env(live: Recorder):
    container = live.one(CONTAINER, "documenso")
    plain, plain_is_secret = _reveal(container.inputs["environmentVariables"])
    secret, secret_is_secret = _reveal(container.inputs["secretEnvironmentVariables"])

    assert secret_is_secret is True
    assert plain_is_secret is False

    assert "NEXT_PRIVATE_DATABASE_URL" in secret
    assert "NEXT_PRIVATE_UPLOAD_SECRET_ACCESS_KEY" in secret
    assert secret["NEXT_PRIVATE_UPLOAD_SECRET_ACCESS_KEY"] == "secret-app-key"
    assert secret["NEXT_PRIVATE_SMTP_PASSWORD"] == "secret-mailer-key"
    assert "@session.internal:5432/documenso" in secret["NEXT_PRIVATE_DATABASE_URL"]
    assert not any("secret" in value or "pw-" in value for value in plain.values())


def test_dns_record_points_at_the_container_endpoint(live: Recorder):
    record = live.one(RECORD, "documenso")

    assert record.inputs["name"] == "sign"
    assert record.inputs["type"] == "CNAME"
    assert record.inputs["data"] == "documensosession-documenso.functions.fnc.fr-par.scw.cloud."


@pytest.mark.parametrize("phase", ["prepare", "sealed"])
def test_container_exists_only_when_live(phase: str):
    recorder = _declare_session(Recorder(), phase)

    assert recorder.of(CONTAINER) == []
    assert recorder.of(RECORD) == []
    # Sealing removes compute only: data stays for the archiver.
    assert len(recorder.of(PG_INSTANCE)) == 1
    assert len(recorder.of(BUCKET)) == 1


def test_unknown_phase_is_refused():
    with pytest.raises(Exception, match="phase must be one of"):
        _declare_session(Recorder(), "paused")


# ---- foundation ----------------------------------------------------------------


def test_archive_bucket_is_locked_in_compliance_mode_and_protected(base: Recorder):
    bucket = base.one(BUCKET, "archive")
    lock = base.one(LOCK, "archive")

    assert bucket.inputs["objectLockEnabled"] is True
    assert bucket.inputs["versioning"] == {"enabled": True}
    assert bucket.inputs["forceDestroy"] is False
    assert bucket.inputs["projectId"] == "retention-id"
    assert lock.inputs["rule"]["defaultRetention"] == {"mode": "COMPLIANCE", "years": 10}


def test_archiver_writes_retention_without_delete(base: Recorder):
    [rule] = _rules(base.one(POLICY, "archiver-retention"))

    assert rule["projectIds"] == ["retention-id"]
    assert rule["permissionSetNames"] == list(grants.ARCHIVER_RETENTION)
    assert not any("Delete" in name or "FullAccess" in name for name in rule["permissionSetNames"])


def test_mail_domain_lives_in_the_mail_project(base: Recorder):
    domain = base.one("scaleway:tem/domain:Domain", "mail")

    assert domain.inputs["projectId"] == "mail-id"
    assert domain.inputs["acceptTos"] is True


def test_retention_must_be_at_least_one_year():
    with pytest.raises(Exception, match="retention_years"):
        from documenso_scw import foundation

        foundation.declare(
            organization_id="org-id",
            region="fr-par",
            retention_bucket_name="x",
            retention_years=0,
            mail_domain="example.com",
            mail_domain_autoconfig=False,
        )
