"""The long-lived half: what outlives every 48-hour session.

Two Projects, each its own boundary:

* `documenso-retention` holds the archive bucket and nothing else. No session
  credential is ever granted anything here. The archiver can write objects but
  holds no delete, and the bucket's COMPLIANCE object lock refuses deletion
  before the retention period ends to everyone, including the Organization owner.
* `documenso-mail` holds the TEM sending domain. Verifying a domain means DNS
  records and propagation, which a 48-hour session cannot afford to redo, so it
  is verified once here and each session only mints an expiring SMTP key.

This stack is never destroyed by the reaper. Destroying it by hand does not
delete locked archives: the bucket cannot be emptied until every lock expires.
"""

from __future__ import annotations

import dataclasses

import pulumi
from pulumiverse_scaleway import account, iam, object as object_storage, tem

from documenso_scw import grants, naming


@dataclasses.dataclass(frozen=True)
class Foundation:
    retention: account.Project
    mail: account.Project
    archive: object_storage.Bucket
    archive_lock: object_storage.BucketLockConfiguration
    archiver: iam.Application
    archiver_policy: iam.Policy
    archiver_key: iam.ApiKey
    mail_domain: tem.Domain


def declare(
    *,
    organization_id: pulumi.Input[str],
    region: str,
    retention_bucket_name: str,
    retention_years: int,
    mail_domain: str,
    mail_domain_autoconfig: bool,
) -> Foundation:
    if retention_years < 1:
        raise ValueError("retention_years must be at least 1: a lock of zero is no lock.")

    retention = account.Project(
        "retention",
        name=naming.RETENTION_PROJECT,
        organization_id=organization_id,
        description=(
            "Documenso signing-session archives under COMPLIANCE object lock. "
            "No session credential holds any permission here."
        ),
    )

    mail = account.Project(
        "mail",
        name=naming.MAIL_PROJECT,
        organization_id=organization_id,
        description="Documenso transactional email domain, verified once for all sessions.",
    )

    archive = object_storage.Bucket(
        "archive",
        name=retention_bucket_name,
        project_id=retention.id,
        region=region,
        # Object lock can only be switched on at creation, and requires versioning.
        object_lock_enabled=True,
        versioning=object_storage.BucketVersioningArgs(enabled=True),
        # Never True: an archive bucket that `pulumi destroy` may empty is not one.
        force_destroy=False,
        tags={"documenso": "retention"},
        opts=pulumi.ResourceOptions(protect=True),
    )

    archive_lock = object_storage.BucketLockConfiguration(
        "archive",
        bucket=archive.name,
        project_id=retention.id,
        region=region,
        rule=object_storage.BucketLockConfigurationRuleArgs(
            default_retention=object_storage.BucketLockConfigurationRuleDefaultRetentionArgs(
                mode="COMPLIANCE",
                years=retention_years,
            ),
        ),
        opts=pulumi.ResourceOptions(protect=True),
    )

    archiver = iam.Application(
        "archiver",
        name="documenso-archiver",
        organization_id=organization_id,
        description=(
            "Copies each session into the retention bucket before teardown. Writes archives, never deletes them."
        ),
        tags=["documenso", "archiver"],
    )

    archiver_policy = iam.Policy(
        "archiver-retention",
        name="documenso-archiver-retention",
        application_id=archiver.id,
        description="Write-without-delete on the retention Project only.",
        rules=[
            iam.PolicyRuleArgs(
                permission_set_names=list(grants.ARCHIVER_RETENTION),
                project_ids=[retention.id],
            )
        ],
    )

    archiver_key = iam.ApiKey(
        "archiver-key",
        application_id=archiver.id,
        default_project_id=retention.id,
        description="documenso archiver (used by the session reaper)",
        # Scaleway stamps a default expiry (one year) on keys created without one.
        # Without this, every `pulumi up` sees that as drift and replaces the key,
        # silently invalidating the copy the reaper holds.
        opts=pulumi.ResourceOptions(ignore_changes=["expiresAt"]),
    )

    mail_domain_resource = tem.Domain(
        "mail",
        name=mail_domain,
        project_id=mail.id,
        region=region,
        accept_tos=True,
        autoconfig=mail_domain_autoconfig,
    )

    return Foundation(
        retention=retention,
        mail=mail,
        archive=archive,
        archive_lock=archive_lock,
        archiver=archiver,
        archiver_policy=archiver_policy,
        archiver_key=archiver_key,
        mail_domain=mail_domain_resource,
    )
