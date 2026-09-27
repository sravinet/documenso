import pytest

from documenso_scw import app_env

DATABASE = app_env.Database(host="10.0.0.5", port=5432, name="documenso", user="documenso", password="p@ss/word")
UPLOADS = app_env.Uploads(region="fr-par", bucket="documenso-q4-uploads-abc", access_key="SCWAK", secret_key="sk")
MAIL = app_env.Mail(
    project_id="mail-project-id", secret_key="mail-sk", from_address="sign@example.com", from_name="Board"
)
SECRETS = app_env.AppSecrets(
    nextauth_secret="n" * 64,
    encryption_key="e" * 64,
    encryption_secondary_key="f" * 64,
    signing_certificate_base64="Y2VydA==",
    signing_passphrase="pass",
)


def build(**overrides):
    kwargs = dict(webapp_url="https://sign.example.com", database=DATABASE, uploads=UPLOADS, mail=MAIL, secrets=SECRETS)
    kwargs.update(overrides)
    return app_env.build(**kwargs)


def test_database_url_escapes_credentials_and_requires_tls():
    assert DATABASE.url() == "postgresql://documenso:p%40ss%2Fword@10.0.0.5:5432/documenso?sslmode=require"


def test_plain_variables():
    plain, _ = build()

    assert plain == {
        "NODE_ENV": "production",
        "PORT": "3000",
        "NEXT_PUBLIC_WEBAPP_URL": "https://sign.example.com",
        "NEXT_PRIVATE_INTERNAL_WEBAPP_URL": "http://localhost:3000",
        "NEXT_PRIVATE_JOBS_PROVIDER": "local",
        "NEXT_PUBLIC_UPLOAD_TRANSPORT": "s3",
        "NEXT_PRIVATE_UPLOAD_ENDPOINT": "https://s3.fr-par.scw.cloud",
        "NEXT_PRIVATE_UPLOAD_REGION": "fr-par",
        "NEXT_PRIVATE_UPLOAD_BUCKET": "documenso-q4-uploads-abc",
        "NEXT_PRIVATE_UPLOAD_FORCE_PATH_STYLE": "false",
        "NEXT_PRIVATE_SMTP_TRANSPORT": "smtp-auth",
        "NEXT_PRIVATE_SMTP_HOST": "smtp.tem.scaleway.com",
        "NEXT_PRIVATE_SMTP_PORT": "587",
        "NEXT_PRIVATE_SMTP_SECURE": "false",
        "NEXT_PRIVATE_SMTP_USERNAME": "mail-project-id",
        "NEXT_PRIVATE_SMTP_FROM_ADDRESS": "sign@example.com",
        "NEXT_PRIVATE_SMTP_FROM_NAME": "Board",
        "NEXT_PRIVATE_SIGNING_TRANSPORT": "local",
    }


def test_secret_variables():
    _, secret = build()

    assert secret == {
        "NEXT_PRIVATE_DATABASE_URL": DATABASE.url(),
        "NEXT_PRIVATE_DIRECT_DATABASE_URL": DATABASE.url(),
        "NEXTAUTH_SECRET": "n" * 64,
        "NEXT_PRIVATE_ENCRYPTION_KEY": "e" * 64,
        "NEXT_PRIVATE_ENCRYPTION_SECONDARY_KEY": "f" * 64,
        "NEXT_PRIVATE_UPLOAD_ACCESS_KEY_ID": "SCWAK",
        "NEXT_PRIVATE_UPLOAD_SECRET_ACCESS_KEY": "sk",
        "NEXT_PRIVATE_SMTP_PASSWORD": "mail-sk",
        "NEXT_PRIVATE_SIGNING_LOCAL_FILE_CONTENTS": "Y2VydA==",
        "NEXT_PRIVATE_SIGNING_PASSPHRASE": "pass",
    }


def test_no_credential_leaks_into_plain_variables():
    plain, secret = build()

    for value in secret.values():
        assert value not in plain.values()


@pytest.mark.parametrize("url", ["http://sign.example.com", "https://sign.example.com/", "sign.example.com"])
def test_webapp_url_must_be_https_without_trailing_slash(url):
    with pytest.raises(ValueError, match="webapp_url"):
        build(webapp_url=url)


def test_empty_values_are_refused():
    empty_passphrase = app_env.AppSecrets(
        nextauth_secret="n" * 64,
        encryption_key="e" * 64,
        encryption_secondary_key="f" * 64,
        signing_certificate_base64="Y2VydA==",
        signing_passphrase="",
    )

    with pytest.raises(ValueError, match="NEXT_PRIVATE_SIGNING_PASSPHRASE"):
        build(secrets=empty_passphrase)
