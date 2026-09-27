"""DNS records in a Cloudflare zone, for zones not hosted on Scaleway.

The values come from the resources they point at (the TEM domain's DKIM key,
the container's endpoint), never from a copy, so a rotated key or a new
session endpoint updates DNS in the same `pulumi up`.

Every record is DNS-only (`proxied=False`): Scaleway must see its own endpoint
to verify the container's custom domain and issue its certificate, and a
proxied MX or TXT record is not a thing.

The provider is explicit and takes its token from stack config, so the only
Cloudflare credential involved is the one scoped for this: Zone.DNS Edit on the
one zone. The zone id is config for the same reason: looking it up by name
would need Zone Read as well.
"""

from __future__ import annotations

import dataclasses

import pulumi
import pulumi_cloudflare as cloudflare

TTL_SECONDS = 300


@dataclasses.dataclass(frozen=True)
class RecordSpec:
    logical_name: str
    type: str
    name: str
    content: str
    priority: int | None = None


def _fqdn(name: str) -> str:
    return name.rstrip(".").lower()


def mail_records(
    *,
    domain: str,
    mail_project_id: str,
    spf_include: str,
    dkim: str,
    dmarc_name: str,
    dmarc: str,
    mx_host: str,
) -> list[RecordSpec]:
    """The four records TEM needs to verify and sign for `domain`."""
    domain = _fqdn(domain)

    if not spf_include.startswith("include:"):
        raise ValueError(f"unexpected SPF include from TEM: {spf_include!r}")
    if not dkim.startswith("v=DKIM1;"):
        raise ValueError("TEM's DKIM value is not a DKIM1 record")
    if not _fqdn(dmarc_name).endswith(f".{domain}"):
        raise ValueError(f"DMARC name {dmarc_name!r} is outside {domain!r}")

    return [
        RecordSpec("mail-spf", "TXT", domain, f"v=spf1 {spf_include} -all"),
        RecordSpec("mail-dkim", "TXT", f"{mail_project_id}._domainkey.{domain}", dkim),
        RecordSpec("mail-dmarc", "TXT", _fqdn(dmarc_name), dmarc),
        RecordSpec("mail-mx", "MX", domain, _fqdn(mx_host), priority=10),
    ]


def web_record(*, hostname: str, target: str) -> RecordSpec:
    hostname = _fqdn(hostname)
    target = _fqdn(target)
    if hostname == target:
        raise ValueError("a CNAME cannot point at itself")
    return RecordSpec("web", "CNAME", hostname, target)


def provider(name: str, api_token: pulumi.Input[str]) -> cloudflare.Provider:
    return cloudflare.Provider(name, api_token=api_token)


def declare(
    specs: pulumi.Output[list[RecordSpec]],
    *,
    logical_names: list[str],
    zone_id: str,
    cloudflare_provider: cloudflare.Provider,
    comment: str,
) -> dict[str, cloudflare.DnsRecord]:
    """One DnsRecord per spec. Logical names are passed up front, because a
    resource's name cannot come from an Output."""

    def pick(logical_name: str) -> pulumi.Output[RecordSpec]:
        def find(all_specs: list[RecordSpec]) -> RecordSpec:
            matches = [spec for spec in all_specs if spec.logical_name == logical_name]
            if len(matches) != 1:
                raise ValueError(f"expected one DNS record named {logical_name}, found {len(matches)}")
            return matches[0]

        return specs.apply(find)

    records: dict[str, cloudflare.DnsRecord] = {}
    for logical_name in logical_names:
        spec = pick(logical_name)
        records[logical_name] = cloudflare.DnsRecord(
            logical_name,
            zone_id=zone_id,
            type=spec.apply(lambda s: s.type),
            name=spec.apply(lambda s: s.name),
            content=spec.apply(lambda s: s.content),
            priority=spec.apply(lambda s: s.priority),
            ttl=TTL_SECONDS,
            proxied=False,
            comment=comment,
            opts=pulumi.ResourceOptions(provider=cloudflare_provider),
        )
    return records


MAIL_RECORD_NAMES = ["mail-spf", "mail-dkim", "mail-dmarc", "mail-mx"]
