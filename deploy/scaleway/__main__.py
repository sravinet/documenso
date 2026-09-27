"""Pulumi entrypoint. The stack name decides which half is declared.

* `foundation`     — long-lived retention and mail Projects (`foundation.py`).
* `session-<id>`   — one 48-hour signing session (`session.py`), reading the
  foundation's outputs through a StackReference.

No account identifier is committed: `organizationId` comes from stack config.
"""

import pulumi

from documenso_scw import foundation, naming, session
from documenso_scw.window import SessionWindow, format_timestamp

config = pulumi.Config()
stack = pulumi.get_stack()

organization_id = config.require("organizationId")
region = config.get("region") or "fr-par"

if stack == naming.FOUNDATION_STACK:
    base_stack = foundation.declare(
        organization_id=organization_id,
        region=region,
        retention_bucket_name=config.require("retentionBucketName"),
        # No default: how long signed agreements must be kept is a legal decision,
        # and a lock cannot be shortened once applied.
        retention_years=config.require_int("retentionYears"),
        mail_domain=config.require("mailDomain"),
        mail_domain_autoconfig=config.get_bool("mailDomainAutoconfig") or False,
        cloudflare_zone_id=config.get("cloudflareZoneId"),
        cloudflare_api_token=config.get_secret("cloudflareApiToken"),
    )

    pulumi.export("region", region)
    pulumi.export("retention_project_id", base_stack.retention.id)
    pulumi.export("mail_project_id", base_stack.mail.id)
    pulumi.export("retention_bucket", base_stack.archive.name)
    pulumi.export("archiver_application_id", base_stack.archiver.id)
    pulumi.export("archiver_access_key", base_stack.archiver_key.access_key)
    pulumi.export("archiver_secret_key", base_stack.archiver_key.secret_key)
    pulumi.export("mail_domain_id", base_stack.mail_domain.id)
    pulumi.export("mail_domain_status", base_stack.mail_domain.status)

else:
    session_id = naming.session_id_from_stack(stack)
    window = SessionWindow.parse(config.require("startsAt"), config.require("expiresAt"))
    base = pulumi.StackReference(config.require("foundationStack"))

    declared_session = session.declare(
        session_id=session_id,
        window=window,
        phase=config.require("phase"),
        organization_id=organization_id,
        region=region,
        hostname=config.require("hostname"),
        dns_zone=config.get("dnsZone"),
        image=config.require("image"),
        cpu_limit_mvcpu=config.get_int("cpuLimit") or 2000,
        memory_gib=config.get_int("memoryGib") or 4,
        max_scale=config.get_int("maxScale") or 3,
        db_node_type=config.get("dbNodeType") or "DB-PRO2-XXS",
        db_volume_gib=config.get_int("dbVolumeGib") or 10,
        mail_project_id=base.require_output("mail_project_id"),
        mail_from_address=config.require("mailFromAddress"),
        mail_from_name=config.get("mailFromName") or "Documenso",
        archiver_application_id=base.require_output("archiver_application_id"),
        signing_certificate_base64=config.require_secret("signingCertificate"),
        signing_passphrase=config.require_secret("signingPassphrase"),
        cloudflare_zone_id=config.get("cloudflareZoneId"),
        cloudflare_api_token=config.get_secret("cloudflareApiToken"),
    )

    pulumi.export("session_id", session_id)
    pulumi.export("starts_at", format_timestamp(window.starts_at))
    pulumi.export("expires_at", format_timestamp(window.expires_at))
    pulumi.export("phase", config.require("phase"))
    pulumi.export("project_id", declared_session.project.id)
    pulumi.export("region", region)
    pulumi.export("webapp_url", declared_session.webapp_url)
    pulumi.export("database_instance_id", declared_session.database_instance.id)
    pulumi.export("database_name", declared_session.database.name)
    pulumi.export("uploads_bucket", declared_session.uploads.name)
    pulumi.export("registry_endpoint", declared_session.images.endpoint)
    if declared_session.container is not None:
        pulumi.export("container_endpoint", declared_session.container.public_endpoint)
