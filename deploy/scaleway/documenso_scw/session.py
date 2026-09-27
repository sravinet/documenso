"""One 48-hour signing session: its own Project, and everything inside it.

A session moves through three phases, set in stack config by `session_ctl.py`:

* `prepare` — data plane, network, credentials and registry, but no container.
  Lets a custom image be pushed to the session registry before anything runs.
* `live`    — the Documenso container is declared and serving.
* `sealed`  — the container is removed and nothing else changes. The reaper seals
  a session before archiving it, so no signature can land after the backup that
  is supposed to contain it.

Every credential the session creates expires `CREDENTIAL_GRACE` after the
session does. That is the failsafe, not the mechanism: the reaper destroys the
session at `expires_at`, and the expiry only bounds the damage if it does not.
"""

from __future__ import annotations

import dataclasses
import urllib.parse
from typing import Any

import pulumi
import pulumi_cloudflare as cloudflare
import pulumi_random as random
from pulumiverse_scaleway import (
    account,
    containers,
    databases,
    domain,
    iam,
    network,
    object as object_storage,
    registry,
)

from documenso_scw import app_env, cloudflare_dns, grants, naming
from documenso_scw.window import SessionWindow, format_timestamp

PHASES = ("prepare", "live", "sealed")

DATABASE_NAME = "documenso"
DATABASE_USER = "documenso"

GIB = 1024**3


@dataclasses.dataclass(frozen=True)
class Session:
    project: account.Project
    vpc: network.Vpc
    private_network: network.PrivateNetwork
    database_instance: databases.Instance
    database: databases.Database
    app_user: databases.User
    app_privilege: databases.Privilege
    uploads: object_storage.Bucket
    app: iam.Application
    app_policy: iam.Policy
    app_key: iam.ApiKey
    mailer: iam.Application
    mailer_policy: iam.Policy
    mailer_key: iam.ApiKey
    archiver_policy: iam.Policy
    images: registry.Namespace
    namespace: containers.Namespace
    container: containers.Container | None
    container_domain: containers.Domain | None
    dns_record: domain.Record | None
    cloudflare_web_record: cloudflare.DnsRecord | None
    webapp_url: str


def _secret_string(name: str, length: int) -> random.RandomPassword:
    # Generated rather than configured: these exist only in encrypted state and
    # in the container's secret environment, never in the tree or stack config.
    return random.RandomPassword(name, length=length, special=False)


def _field(value: Any, name: str) -> Any:
    """Read a field off a provider output object or a plain mapping (mock monitor)."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _private_endpoint(instance: databases.Instance) -> pulumi.Output[tuple[str, int]]:
    """The Postgres address on the session's private network, never the public one."""

    def pick(endpoints: Any) -> tuple[str, int]:
        endpoint = endpoints if not isinstance(endpoints, list) else endpoints[0]
        host = _field(endpoint, "hostname") or _field(endpoint, "ip")
        port = _field(endpoint, "port")
        if not host or not port:
            raise ValueError("database instance has no private network endpoint")
        return str(host), int(port)

    return instance.private_network.apply(pick)


def declare(
    *,
    session_id: str,
    window: SessionWindow,
    phase: str,
    organization_id: pulumi.Input[str],
    region: str,
    hostname: str,
    dns_zone: str | None,
    image: str,
    cpu_limit_mvcpu: int,
    memory_gib: int,
    max_scale: int,
    db_node_type: str,
    db_volume_gib: int,
    mail_project_id: pulumi.Input[str],
    mail_from_address: str,
    mail_from_name: str,
    archiver_application_id: pulumi.Input[str],
    signing_certificate_base64: pulumi.Input[str],
    signing_passphrase: pulumi.Input[str],
    cloudflare_zone_id: str | None = None,
    cloudflare_api_token: pulumi.Input[str] | None = None,
) -> Session:
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}.")
    if dns_zone and cloudflare_zone_id:
        raise ValueError("set dnsZone (Scaleway DNS) or cloudflareZoneId, not both.")
    if cloudflare_zone_id and cloudflare_api_token is None:
        raise ValueError("cloudflareZoneId is set but cloudflareApiToken is not.")

    project_name = naming.session_project(session_id)
    credentials_expire_at = format_timestamp(window.credentials_expire_at)
    lifetime = f"{format_timestamp(window.starts_at)} -> {format_timestamp(window.expires_at)}"
    tags = ["documenso", f"session:{session_id}", f"expires:{format_timestamp(window.expires_at)}"]

    project = account.Project(
        "session",
        name=project_name,
        organization_id=organization_id,
        description=f"Documenso signing session {session_id}, {lifetime} UTC. Destroyed by the reaper after archiving.",
    )

    # ---- network -------------------------------------------------------------
    vpc = network.Vpc(
        "session",
        name=project_name,
        project_id=project.id,
        region=region,
        tags=tags,
    )

    private_network = network.PrivateNetwork(
        "session",
        name=project_name,
        project_id=project.id,
        region=region,
        vpc_id=vpc.id,
        tags=tags,
    )

    # ---- Postgres ------------------------------------------------------------
    admin_password = random.RandomPassword(
        "db-admin-password", length=40, special=True, override_special="-_.~", min_special=1, min_upper=1, min_numeric=1
    )
    app_password = random.RandomPassword(
        "db-app-password", length=40, special=True, override_special="-_.~", min_special=1, min_upper=1, min_numeric=1
    )

    database_instance = databases.Instance(
        "session",
        name=project_name,
        project_id=project.id,
        region=region,
        engine="PostgreSQL-16",
        node_type=db_node_type,
        is_ha_cluster=True,
        encryption_at_rest=True,
        volume_type="sbs_5k",
        volume_size_in_gb=db_volume_gib,
        # Managed backups stay on; the archive is a separate, exported backup.
        disable_backup=False,
        backup_same_region=False,
        user_name="documenso_admin",
        password=admin_password.result,
        # Private network only. No `load_balancer` block means no public endpoint.
        private_network=databases.InstancePrivateNetworkArgs(pn_id=private_network.id, enable_ipam=True),
        tags=tags,
    )

    database = databases.Database("documenso", instance_id=database_instance.id, name=DATABASE_NAME, region=region)

    app_user = databases.User(
        "documenso",
        instance_id=database_instance.id,
        name=DATABASE_USER,
        password=app_password.result,
        region=region,
        # Not an instance admin: the app owns its database, not the instance.
        is_admin=False,
    )

    # `all` on the one database, because `prisma migrate deploy` runs DDL at every
    # start. This is still scoped to `documenso`, not the instance.
    app_privilege = databases.Privilege(
        "documenso",
        instance_id=database_instance.id,
        database_name=database.name,
        user_name=app_user.name,
        permission="all",
        region=region,
    )

    # ---- uploads -------------------------------------------------------------
    bucket_suffix = random.RandomString("uploads-suffix", length=8, special=False, upper=False)

    uploads = object_storage.Bucket(
        "uploads",
        name=bucket_suffix.result.apply(lambda suffix: f"{project_name}-uploads-{suffix}"),
        project_id=project.id,
        region=region,
        # Versioned so a document the app deletes is still in the archive's reach.
        versioning=object_storage.BucketVersioningArgs(enabled=True),
        cors_rules=[
            object_storage.BucketCorsRuleArgs(
                allowed_methods=["GET", "PUT", "HEAD"],
                allowed_origins=[f"https://{hostname}"],
                allowed_headers=["*"],
                expose_headers=["ETag"],
                max_age_seconds=3000,
            )
        ],
        # The reaper destroys a session only after verifying its archive.
        force_destroy=True,
        tags={"documenso": "uploads", "session": session_id},
    )

    # ---- identities ----------------------------------------------------------
    app = iam.Application(
        "app",
        name=f"{project_name}-app",
        organization_id=organization_id,
        description=f"Documenso container of session {session_id}: its own uploads bucket only.",
        tags=tags,
    )

    app_policy = iam.Policy(
        "app-session",
        name=f"{project_name}-app",
        application_id=app.id,
        description="Uploads in this session only. Nothing in retention or mail.",
        rules=[iam.PolicyRuleArgs(permission_set_names=list(grants.APP_SESSION), project_ids=[project.id])],
    )

    app_key = iam.ApiKey(
        "app-key",
        application_id=app.id,
        default_project_id=project.id,
        expires_at=credentials_expire_at,
        description=f"{project_name} uploads",
    )

    mailer = iam.Application(
        "mailer",
        name=f"{project_name}-mailer",
        organization_id=organization_id,
        description=f"SMTP sender of session {session_id}.",
        tags=tags,
    )

    mailer_policy = iam.Policy(
        "mailer-mail",
        name=f"{project_name}-mailer",
        application_id=mailer.id,
        description="Send mail through the shared TEM domain. Nothing else.",
        rules=[iam.PolicyRuleArgs(permission_set_names=list(grants.MAILER_MAIL), project_ids=[mail_project_id])],
    )

    mailer_key = iam.ApiKey(
        "mailer-key",
        application_id=mailer.id,
        default_project_id=mail_project_id,
        expires_at=credentials_expire_at,
        description=f"{project_name} SMTP",
    )

    # The foundation's archiver, granted read on this session. Declared here so
    # the grant is destroyed with the session instead of accumulating.
    archiver_policy = iam.Policy(
        "archiver-session",
        name=f"{project_name}-archiver",
        application_id=archiver_application_id,
        description=f"Read session {session_id} for archiving. Destroyed with the session.",
        rules=[iam.PolicyRuleArgs(permission_set_names=list(grants.ARCHIVER_SESSION), project_ids=[project.id])],
    )

    images = registry.Namespace(
        "images",
        name=project_name,
        project_id=project.id,
        region=region,
        is_public=False,
        description=f"Custom Documenso images for session {session_id}",
    )

    # ---- application ---------------------------------------------------------
    nextauth_secret = _secret_string("nextauth-secret", 64)
    encryption_key = _secret_string("encryption-key", 64)
    encryption_secondary_key = _secret_string("encryption-secondary-key", 64)

    webapp_url = f"https://{hostname}"

    def environment(values: list[Any]) -> tuple[dict[str, str], dict[str, str]]:
        (
            endpoint,
            bucket_name,
            app_access_key,
            app_secret_key,
            mail_project,
            mailer_secret_key,
            db_password,
            nextauth,
            encryption,
            encryption_secondary,
            certificate,
            passphrase,
        ) = values
        host, port = endpoint
        return app_env.build(
            webapp_url=webapp_url,
            database=app_env.Database(
                host=host, port=port, name=DATABASE_NAME, user=DATABASE_USER, password=db_password
            ),
            uploads=app_env.Uploads(
                region=region, bucket=bucket_name, access_key=app_access_key, secret_key=app_secret_key
            ),
            mail=app_env.Mail(
                project_id=mail_project,
                secret_key=mailer_secret_key,
                from_address=mail_from_address,
                from_name=mail_from_name,
            ),
            secrets=app_env.AppSecrets(
                nextauth_secret=nextauth,
                encryption_key=encryption,
                encryption_secondary_key=encryption_secondary,
                signing_certificate_base64=certificate,
                signing_passphrase=passphrase,
            ),
        )

    env_pair = pulumi.Output.all(
        _private_endpoint(database_instance),
        uploads.name,
        app_key.access_key,
        app_key.secret_key,
        mail_project_id,
        mailer_key.secret_key,
        app_password.result,
        nextauth_secret.result,
        encryption_key.result,
        encryption_secondary_key.result,
        signing_certificate_base64,
        signing_passphrase,
    ).apply(environment)

    namespace = containers.Namespace(
        "session",
        name=project_name,
        project_id=project.id,
        region=region,
        description=f"Documenso session {session_id}",
        tags=tags,
    )

    container: containers.Container | None = None
    container_domain: containers.Domain | None = None
    dns_record: domain.Record | None = None
    cloudflare_web_record: cloudflare.DnsRecord | None = None

    if phase == "live":
        container = containers.Container(
            "documenso",
            name="documenso",
            namespace_id=namespace.id,
            region=region,
            image=image,
            port=app_env.DOCUMENSO_PORT,
            cpu_limit=cpu_limit_mvcpu,
            memory_limit_bytes=memory_gib * GIB,
            # One instance always up: Documenso's cron poller (reminders,
            # expirations) runs in-process and stops when scaled to zero.
            min_scale=1,
            max_scale=max_scale,
            privacy="public",
            https_connections_only=True,
            protocol="http1",
            timeout=300,
            private_network_id=private_network.id,
            # Migrations run before the server listens, so allow five minutes.
            startup_probe=containers.ContainerStartupProbeArgs(
                failure_threshold=30,
                interval="10s",
                timeout="5s",
                http=containers.ContainerStartupProbeHttpArgs(path="/api/health"),
            ),
            liveness_probe=containers.ContainerLivenessProbeArgs(
                failure_threshold=3,
                interval="30s",
                timeout="5s",
                http=containers.ContainerLivenessProbeHttpArgs(path="/api/health"),
            ),
            # The plain half holds no credential (app_env tests assert it), so it is
            # unmarked to stay readable in previews; the secret half stays secret.
            environment_variables=pulumi.Output.unsecret(env_pair.apply(lambda pair: pair[0])),
            secret_environment_variables=env_pair.apply(lambda pair: pair[1]),
            tags=tags,
            opts=pulumi.ResourceOptions(depends_on=[app_privilege, app_policy, mailer_policy]),
        )

        if dns_zone:
            record_name = _record_name(hostname, dns_zone)
            dns_record = domain.Record(
                "documenso",
                dns_zone=dns_zone,
                name=record_name,
                type="CNAME",
                data=container.public_endpoint.apply(lambda endpoint: f"{_host(endpoint)}."),
                ttl=300,
            )

        if cloudflare_zone_id and cloudflare_api_token is not None:
            specs = container.public_endpoint.apply(
                lambda endpoint: [cloudflare_dns.web_record(hostname=hostname, target=_host(endpoint))]
            )
            cloudflare_web_record = cloudflare_dns.declare(
                specs,
                logical_names=["web"],
                zone_id=cloudflare_zone_id,
                cloudflare_provider=cloudflare_dns.provider("cloudflare", cloudflare_api_token),
                comment=f"documenso session {session_id} (deploy/scaleway)",
            )["web"]

        cname: list[pulumi.Resource] = [r for r in (dns_record, cloudflare_web_record) if r is not None]
        container_domain = containers.Domain(
            "documenso",
            container_id=container.id,
            hostname=hostname,
            region=region,
            # The CNAME must exist first: Scaleway verifies it before binding the domain.
            opts=pulumi.ResourceOptions(depends_on=cname),
        )

    return Session(
        project=project,
        vpc=vpc,
        private_network=private_network,
        database_instance=database_instance,
        database=database,
        app_user=app_user,
        app_privilege=app_privilege,
        uploads=uploads,
        app=app,
        app_policy=app_policy,
        app_key=app_key,
        mailer=mailer,
        mailer_policy=mailer_policy,
        mailer_key=mailer_key,
        archiver_policy=archiver_policy,
        images=images,
        namespace=namespace,
        container=container,
        container_domain=container_domain,
        dns_record=dns_record,
        cloudflare_web_record=cloudflare_web_record,
        webapp_url=webapp_url,
    )


def _record_name(hostname: str, dns_zone: str) -> str:
    if hostname == dns_zone:
        raise ValueError("hostname must be a subdomain of dnsZone: a zone apex cannot be a CNAME.")
    suffix = f".{dns_zone}"
    if not hostname.endswith(suffix):
        raise ValueError(f"hostname {hostname!r} is not inside dnsZone {dns_zone!r}.")
    return hostname.removesuffix(suffix)


def _host(endpoint: str) -> str:
    """`public_endpoint` may carry a scheme; a CNAME target must not."""
    parsed = urllib.parse.urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    if not parsed.hostname:
        raise ValueError(f"container public endpoint has no host: {endpoint!r}")
    return parsed.hostname
