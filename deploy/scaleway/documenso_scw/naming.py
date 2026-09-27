"""Names derived from a session id, so no resource name is typed twice."""

from __future__ import annotations

import re

_SESSION_ID = re.compile(r"^[a-z][a-z0-9-]{1,18}[a-z0-9]$")

SESSION_STACK_PREFIX = "session-"
FOUNDATION_STACK = "foundation"

RETENTION_PROJECT = "documenso-retention"
MAIL_PROJECT = "documenso-mail"


class NamingError(ValueError):
    """A session id that would produce an invalid or ambiguous resource name."""


def validate_session_id(session_id: str) -> str:
    """3-20 chars, lowercase, starts with a letter, no trailing or doubled dash.

    The id ends up in a Project name, a bucket name and a DNS-safe container
    name, so the strictest of the three decides.
    """
    if not _SESSION_ID.match(session_id) or "--" in session_id:
        raise NamingError(
            "session id must be 3-20 characters of [a-z0-9-], start with a letter, "
            f"end with a letter or digit and contain no '--'; got {session_id!r}."
        )

    return session_id


def session_project(session_id: str) -> str:
    return f"documenso-{validate_session_id(session_id)}"


def session_stack(session_id: str) -> str:
    return f"{SESSION_STACK_PREFIX}{validate_session_id(session_id)}"


def session_id_from_stack(stack: str) -> str:
    if not stack.startswith(SESSION_STACK_PREFIX):
        raise NamingError(f"{stack!r} is not a session stack (expected '{SESSION_STACK_PREFIX}<id>').")

    return validate_session_id(stack.removeprefix(SESSION_STACK_PREFIX))


def archive_prefix(session_id: str) -> str:
    """Where one session's archive lives inside the retention bucket."""
    return f"sessions/{validate_session_id(session_id)}/"
