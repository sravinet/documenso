"""The Documenso process environment for one session, split plain / secret.

Pure: takes resolved strings, returns two dicts. The Pulumi program feeds it
`Output`s through `apply`, and the tests feed it literals, so the mapping from
infrastructure to Documenso's variable names is tested without a cloud.
"""

from __future__ import annotations

import dataclasses
import urllib.parse

DOCUMENSO_PORT = 3000

TEM_SMTP_HOST = "smtp.tem.scaleway.com"
TEM_SMTP_PORT = 587


@dataclasses.dataclass(frozen=True)
class Database:
    host: str
    port: int
    name: str
    user: str
    password: str

    def url(self) -> str:
        return "postgresql://{user}:{password}@{host}:{port}/{name}?sslmode=require".format(
            user=urllib.parse.quote(self.user, safe=""),
            password=urllib.parse.quote(self.password, safe=""),
            host=self.host,
            port=self.port,
            name=urllib.parse.quote(self.name, safe=""),
        )


@dataclasses.dataclass(frozen=True)
class Uploads:
    region: str
    bucket: str
    access_key: str
    secret_key: str

    @property
    def endpoint(self) -> str:
        return f"https://s3.{self.region}.scw.cloud"


@dataclasses.dataclass(frozen=True)
class Mail:
    """TEM SMTP: the username is the mail Project id, the password an API secret key."""

    project_id: str
    secret_key: str
    from_address: str
    from_name: str


@dataclasses.dataclass(frozen=True)
class AppSecrets:
    nextauth_secret: str
    encryption_key: str
    encryption_secondary_key: str
    signing_certificate_base64: str
    signing_passphrase: str


def build(
    *,
    webapp_url: str,
    database: Database,
    uploads: Uploads,
    mail: Mail,
    secrets: AppSecrets,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (environment_variables, secret_environment_variables)."""
    if not webapp_url.startswith("https://") or webapp_url.endswith("/"):
        raise ValueError(f"webapp_url must be https:// with no trailing slash, got {webapp_url!r}.")

    database_url = database.url()

    plain = {
        "NODE_ENV": "production",
        "PORT": str(DOCUMENSO_PORT),
        "NEXT_PUBLIC_WEBAPP_URL": webapp_url,
        # Background jobs call back into the app; keep that loopback in the instance.
        "NEXT_PRIVATE_INTERNAL_WEBAPP_URL": f"http://localhost:{DOCUMENSO_PORT}",
        "NEXT_PRIVATE_JOBS_PROVIDER": "local",
        # Files go to the session bucket, never into Postgres, so the archive of a
        # session is its bucket plus one database backup.
        "NEXT_PUBLIC_UPLOAD_TRANSPORT": "s3",
        "NEXT_PRIVATE_UPLOAD_ENDPOINT": uploads.endpoint,
        "NEXT_PRIVATE_UPLOAD_REGION": uploads.region,
        "NEXT_PRIVATE_UPLOAD_BUCKET": uploads.bucket,
        "NEXT_PRIVATE_UPLOAD_FORCE_PATH_STYLE": "false",
        "NEXT_PRIVATE_SMTP_TRANSPORT": "smtp-auth",
        "NEXT_PRIVATE_SMTP_HOST": TEM_SMTP_HOST,
        "NEXT_PRIVATE_SMTP_PORT": str(TEM_SMTP_PORT),
        "NEXT_PRIVATE_SMTP_SECURE": "false",
        "NEXT_PRIVATE_SMTP_USERNAME": mail.project_id,
        "NEXT_PRIVATE_SMTP_FROM_ADDRESS": mail.from_address,
        "NEXT_PRIVATE_SMTP_FROM_NAME": mail.from_name,
        "NEXT_PRIVATE_SIGNING_TRANSPORT": "local",
    }

    secret = {
        "NEXT_PRIVATE_DATABASE_URL": database_url,
        "NEXT_PRIVATE_DIRECT_DATABASE_URL": database_url,
        "NEXTAUTH_SECRET": secrets.nextauth_secret,
        "NEXT_PRIVATE_ENCRYPTION_KEY": secrets.encryption_key,
        "NEXT_PRIVATE_ENCRYPTION_SECONDARY_KEY": secrets.encryption_secondary_key,
        "NEXT_PRIVATE_UPLOAD_ACCESS_KEY_ID": uploads.access_key,
        "NEXT_PRIVATE_UPLOAD_SECRET_ACCESS_KEY": uploads.secret_key,
        "NEXT_PRIVATE_SMTP_PASSWORD": mail.secret_key,
        "NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS": secrets.signing_certificate_base64,
        "NEXT_PRIVATE_SIGNING_PASSPHRASE": secrets.signing_passphrase,
    }

    overlap = plain.keys() & secret.keys()
    if overlap:
        raise ValueError(f"variables declared both plain and secret: {sorted(overlap)}")

    empty = sorted(key for key, value in {**plain, **secret}.items() if value == "")
    if empty:
        raise ValueError(f"empty values would silently disable features: {empty}")

    return plain, secret
