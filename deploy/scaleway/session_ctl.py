#!/usr/bin/env python3
"""Open, list and reap 48-hour signing sessions.

    ./session_ctl.py open <id>                 start the 48h clock (stack config only)
    ./session_ctl.py phase <id> <phase>        prepare | live | sealed
    ./session_ctl.py expired                   JSON list of session ids past expiry
    ./session_ctl.py reap <id>                 seal, archive, verify, destroy
    ./session_ctl.py reap <id> --force-before-expiry --confirm <id>

`open` and `phase` only write stack config; run `pulumi up -s session-<id>`
afterwards. `reap` runs Pulumi itself, and only destroys after the archive in
the retention bucket has been verified against its manifest.

Needs `pulumi` logged in to the state backend, SCW_* credentials for Pulumi
(the provisioner profile), and the archiver key in ARCHIVER_ACCESS_KEY /
ARCHIVER_SECRET_KEY for the archive step.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from documenso_scw import archive, naming, reaper  # noqa: E402
from documenso_scw.session import PHASES  # noqa: E402
from documenso_scw.window import SessionWindow, format_timestamp  # noqa: E402


def pulumi(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["pulumi", *args, "--non-interactive"],
        cwd=HERE,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout or ""


def stack_outputs(stack: str) -> dict[str, Any]:
    outputs = json.loads(pulumi("stack", "output", "--json", "--show-secrets", "-s", stack))
    if not isinstance(outputs, dict):
        sys.exit(f"{stack}: `pulumi stack output` did not return an object")
    return outputs


def session_stacks() -> list[str]:
    stacks = json.loads(pulumi("stack", "ls", "--json"))
    return sorted(s["name"].split("/")[-1] for s in stacks if s["name"].split("/")[-1].startswith("session-"))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def cmd_open(args: argparse.Namespace) -> None:
    stack = naming.session_stack(args.session_id)
    pulumi("stack", "select", "--create", stack, capture=False)

    existing = pulumi("config", "get", "startsAt", "-s", stack) if _has_config(stack, "startsAt") else ""
    if existing.strip():
        sys.exit(f"{stack} already opened at {existing.strip()}; a session's window is never extended.")

    window = SessionWindow.opening(now_utc())
    pulumi("config", "set", "startsAt", format_timestamp(window.starts_at), "-s", stack)
    pulumi("config", "set", "expiresAt", format_timestamp(window.expires_at), "-s", stack)
    pulumi("config", "set", "phase", args.phase, "-s", stack)
    print(
        f"{stack}: {format_timestamp(window.starts_at)} -> {format_timestamp(window.expires_at)} (phase {args.phase})"
    )


def _has_config(stack: str, key: str) -> bool:
    config = json.loads(pulumi("config", "--json", "-s", stack))
    return any(name.endswith(f":{key}") for name in config)


def cmd_phase(args: argparse.Namespace) -> None:
    stack = naming.session_stack(args.session_id)
    pulumi("config", "set", "phase", args.phase, "-s", stack)
    print(f"{stack}: phase {args.phase}; run `pulumi up -s {stack}` to apply")


def _window(outputs: dict[str, Any]) -> SessionWindow:
    return SessionWindow.parse(outputs["starts_at"], outputs["expires_at"])


def cmd_expired(_: argparse.Namespace) -> None:
    now = now_utc()
    expired = []
    for stack in session_stacks():
        outputs = stack_outputs(stack)
        if "expires_at" not in outputs:
            continue
        if _window(outputs).is_expired(now):
            expired.append(naming.session_id_from_stack(stack))
    print(json.dumps(expired))


def _credentials() -> reaper.Credentials:
    try:
        return reaper.Credentials(os.environ["ARCHIVER_ACCESS_KEY"], os.environ["ARCHIVER_SECRET_KEY"])
    except KeyError as missing:
        sys.exit(f"{missing.args[0]} is not set; the archive step runs with the foundation archiver key only.")


def cmd_reap(args: argparse.Namespace) -> None:
    session_id = naming.validate_session_id(args.session_id)
    stack = naming.session_stack(session_id)

    if args.force_before_expiry and args.confirm != session_id:
        sys.exit("--force-before-expiry needs --confirm <session id> repeating the id.")

    outputs = stack_outputs(stack)
    foundation = stack_outputs(naming.FOUNDATION_STACK)
    expired = _window(outputs).is_expired(now_utc())

    if not expired and not args.force_before_expiry:
        sys.exit(f"{stack} expires at {outputs['expires_at']}; nothing to do yet.")

    credentials = _credentials()
    facts = reaper.SessionFacts(
        session_id=session_id,
        region=outputs["region"],
        project_id=outputs["project_id"],
        starts_at=outputs["starts_at"],
        expires_at=outputs["expires_at"],
        database_instance_id=outputs["database_instance_id"],
        database_name=outputs["database_name"],
        uploads_bucket=outputs["uploads_bucket"],
    )
    retention_facts = reaper.RetentionFacts(
        region=foundation["region"],
        project_id=foundation["retention_project_id"],
        bucket=foundation["retention_bucket"],
    )
    retention = reaper.s3_client(credentials, retention_facts.region, retention_facts.project_id)

    manifest = reaper.read_manifest(retention, retention_facts.bucket, session_id)
    problems = reaper.verify_archive(retention, retention_facts.bucket, manifest) if manifest else None

    if problems is None or problems:
        # Seal before archiving, so nothing is signed after the backup is taken.
        if outputs.get("phase") != "sealed":
            pulumi("config", "set", "phase", "sealed", "-s", stack)
            pulumi("up", "--yes", "--skip-preview", "-s", stack, capture=False)

        manifest = reaper.archive_session(credentials, facts, retention_facts)
        problems = reaper.verify_archive(retention, retention_facts.bucket, manifest)

    if manifest is None:
        sys.exit(f"{stack}: archiving produced no manifest; not destroying")

    allowed, reason = archive.may_destroy(
        expired=expired,
        archive_problems=problems,
        force_before_expiry=args.force_before_expiry,
    )
    if not allowed:
        sys.exit(f"{stack}: not destroying: {reason}")

    print(f"{stack}: {reason} ({len(manifest.objects)} objects + database); destroying")
    pulumi("destroy", "--yes", "--skip-preview", "-s", stack, capture=False)
    pulumi("stack", "rm", "--yes", stack, capture=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    open_parser = commands.add_parser("open")
    open_parser.add_argument("session_id")
    open_parser.add_argument("--phase", choices=PHASES, default="live")
    open_parser.set_defaults(run=cmd_open)

    phase_parser = commands.add_parser("phase")
    phase_parser.add_argument("session_id")
    phase_parser.add_argument("phase", choices=PHASES)
    phase_parser.set_defaults(run=cmd_phase)

    expired_parser = commands.add_parser("expired")
    expired_parser.set_defaults(run=cmd_expired)

    reap_parser = commands.add_parser("reap")
    reap_parser.add_argument("session_id")
    reap_parser.add_argument("--force-before-expiry", action="store_true")
    reap_parser.add_argument("--confirm")
    reap_parser.set_defaults(run=cmd_reap)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
