"""`__main__.py` runs end to end for both stack kinds, with real stack config.

The boundary tests call `declare` directly; this is the only check that the
config keys, StackReference outputs and exports the entrypoint uses line up.
"""

from __future__ import annotations

import pathlib
import runpy

import pulumi
import pytest

from tests.test_boundary import CONTAINER, Recorder

PROGRAM = pathlib.Path(__file__).resolve().parent.parent / "__main__.py"

FOUNDATION_CONFIG = {
    "documenso:organizationId": "org-id",
    "documenso:retentionBucketName": "documenso-archive-test",
    "documenso:retentionYears": "10",
    "documenso:mailDomain": "example.com",
}

SESSION_CONFIG = {
    "documenso:organizationId": "org-id",
    "documenso:startsAt": "2026-10-01T08:00:00Z",
    "documenso:expiresAt": "2026-10-03T08:00:00Z",
    "documenso:phase": "live",
    "documenso:hostname": "sign.example.com",
    "documenso:dnsZone": "example.com",
    "documenso:image": "docker.io/documenso/documenso:v2.18.0",
    "documenso:mailFromAddress": "sign@example.com",
    "documenso:foundationStack": "org/documenso/foundation",
    "documenso:signingCertificate": "Y2VydA==",
    "documenso:signingPassphrase": "pass",
}


class ProgramRecorder(Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.stack_references: list[str] = []

    def new_resource(self, args):
        if args.typ == "pulumi:pulumi:StackReference":
            self.stack_references.append(args.name)
            outputs = {"mail_project_id": "mail-id", "archiver_application_id": "archiver-id"}
            return args.name, {"name": args.name, "outputs": outputs, "secretOutputNames": []}
        return super().new_resource(args)


def run_program(stack: str, config: dict[str, str]) -> ProgramRecorder:
    recorder = ProgramRecorder()
    pulumi.runtime.set_mocks(recorder, project="documenso", stack=stack, preview=False)
    pulumi.runtime.set_all_config(config)

    @pulumi.runtime.test
    def run():
        runpy.run_path(str(PROGRAM), run_name="__main__")
        return pulumi.Output.from_input(None)

    run()
    return recorder


def test_foundation_program_declares_only_long_lived_resources():
    recorder = run_program("foundation", FOUNDATION_CONFIG)

    projects = sorted(r.inputs["name"] for r in recorder.of("scaleway:account/project:Project"))
    assert projects == ["documenso-mail", "documenso-retention"]
    assert recorder.of(CONTAINER) == []


def test_session_program_declares_the_session():
    recorder = run_program("session-board-q4", SESSION_CONFIG)

    assert recorder.stack_references == ["org/documenso/foundation"]
    [project] = recorder.of("scaleway:account/project:Project")
    assert project.inputs["name"] == "documenso-board-q4"
    assert len(recorder.of(CONTAINER)) == 1


def test_session_program_refuses_a_window_that_is_not_48_hours():
    config = {**SESSION_CONFIG, "documenso:expiresAt": "2026-10-05T08:00:00Z"}

    with pytest.raises(Exception, match="exactly 2 days"):
        run_program("session-board-q4", config)


def test_non_session_non_foundation_stacks_are_refused():
    with pytest.raises(Exception, match="not a session stack"):
        run_program("staging", SESSION_CONFIG)
