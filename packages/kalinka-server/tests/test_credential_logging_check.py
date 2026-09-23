"""The pipeline check that flags code which could log a credential.

It runs on every pull request, so what it flags is what a reviewer sees
annotated on the diff; what it lets through is what reaches production logs.
The repository itself must pass it.
"""

import importlib.util
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_SPEC = importlib.util.spec_from_file_location(
    "check_credential_logging", REPO / "scripts" / "check_credential_logging.py"
)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)


def _flagged(source: str):
    return check.check_source(textwrap.dedent(source), "mod.py")


@pytest.mark.parametrize(
    "source",
    [
        'logger.info("login with %s", password)',
        'logger.debug("key %s", self.api_key)',
        'log.warning("smb %s", config.smb.password)',
        'logging.error("got %s", data["access_token"])',
        'logger.info(f"auth {user_auth_token}")',
        'logger.info("sent %s", request.headers)',
        'logger.info("body %s", {"client_secret": secret_value})',
        'logger.info("retrying", extra={"token": token})',
        'self._logger.exception("failed for %s", creds.password.strip())',
        'print(clientSecret)',
        'logger.info("using %s", self.get_token())',
        'raise ValueError(f"bad password {password}")',
        'logger.info("password=%s", value)',
        'logger.info(f"token: {value}")',
    ],
)
def test_logging_a_credential_is_flagged(source):
    findings = _flagged(source)

    assert len(findings) == 1
    assert findings[0].line == 1


@pytest.mark.parametrize(
    "source",
    [
        'logger.info("Setting %s to %s", key, loggable(value))',
        'logger.info("token length %d", len(token))',
        'logger.info("password set: %s", password is not None)',
        'logger.info("password missing: %s", not password)',
        'logger.info("%d tokens matched", token_count)',
        'logger.info("config %s", config.name)',
        'logger.info("header names %s", headers.keys())',
        'logger.info("Starting on %s:%d", host, port)',
        'logger.info("failed", exc_info=True)',
        'cache.info(password)',
        'raise ValueError("password is required")',
    ],
)
def test_logging_without_the_value_is_not_flagged(source):
    assert _flagged(source) == []


def test_a_reviewed_false_positive_is_kept_with_its_reason():
    assert (
        _flagged(
            """
            logger.warning(
                "status %s",
                headers,  # log-safe: the API's JSON status block
            )
            """
        )
        == []
    )


def test_a_suppression_without_a_reason_does_not_count():
    assert len(_flagged('logger.info("%s", password)  # log-safe:')) == 1


def test_a_finding_is_an_annotation_on_the_line_in_github(monkeypatch, capsys, tmp_path):
    module = tmp_path / "leaky.py"
    module.write_text('import logging\n\nlogging.info("%s", password)\n')
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    status = check.main([str(module)])

    assert status == 1
    annotation = capsys.readouterr().out.strip()
    assert annotation.startswith(f"::error file={module},line=3,col=1,")
    assert "`password`" in annotation


def test_the_repository_logs_no_credential():
    assert [finding.plain() for finding in check.check_paths(check.default_paths())] == []
