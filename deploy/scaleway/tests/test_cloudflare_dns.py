import pytest

from documenso_scw import cloudflare_dns
from documenso_scw.cloudflare_dns import RecordSpec

DKIM = "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkq"


def records(**overrides):
    kwargs = dict(
        domain="mail.sign.cybapi.com",
        mail_project_id="46df593c-2be8-4566-b4ce-4822f3777847",
        spf_include="include:_spf.tem.scaleway.com",
        dkim=DKIM,
        dmarc_name="_dmarc.mail.sign.cybapi.com.",
        dmarc="v=DMARC1; p=none",
        mx_host="blackhole.scw-tem.cloud.",
    )
    kwargs.update(overrides)
    return cloudflare_dns.mail_records(**kwargs)


def test_mail_records_match_what_tem_publishes():
    assert records() == [
        RecordSpec("mail-spf", "TXT", "mail.sign.cybapi.com", "v=spf1 include:_spf.tem.scaleway.com -all"),
        RecordSpec(
            "mail-dkim",
            "TXT",
            "46df593c-2be8-4566-b4ce-4822f3777847._domainkey.mail.sign.cybapi.com",
            DKIM,
        ),
        RecordSpec("mail-dmarc", "TXT", "_dmarc.mail.sign.cybapi.com", "v=DMARC1; p=none"),
        RecordSpec("mail-mx", "MX", "mail.sign.cybapi.com", "blackhole.scw-tem.cloud", priority=10),
    ]


def test_logical_names_cover_every_mail_record():
    assert [spec.logical_name for spec in records()] == cloudflare_dns.MAIL_RECORD_NAMES


def test_trailing_dots_and_case_are_normalised():
    [spf, *_] = records(domain="Mail.Sign.Cybapi.com.")

    assert spf.name == "mail.sign.cybapi.com"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"spf_include": "v=spf1 -all"}, "SPF include"),
        ({"dkim": "not a key"}, "DKIM1"),
        ({"dmarc_name": "_dmarc.other.example."}, "outside"),
    ],
)
def test_unexpected_tem_values_are_refused(override, message):
    with pytest.raises(ValueError, match=message):
        records(**override)


def test_web_record_is_a_cname_to_the_container_host():
    assert cloudflare_dns.web_record(
        hostname="sign.cybapi.com",
        target="documensoabc-documenso.functions.fnc.fr-par.scw.cloud.",
    ) == RecordSpec("web", "CNAME", "sign.cybapi.com", "documensoabc-documenso.functions.fnc.fr-par.scw.cloud")


def test_web_record_refuses_a_self_reference():
    with pytest.raises(ValueError, match="itself"):
        cloudflare_dns.web_record(hostname="sign.cybapi.com", target="sign.cybapi.com.")
