"""Gmail OAuth notification tests; no browser, keyring, or network is used."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest


class MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


@dataclass
class FakeCredentials:
    refresh_token: str = "refresh-token"
    client_id: str = "desktop-client"
    client_secret: str = "desktop-secret"
    token_uri: str = "https://oauth2.googleapis.com/token"
    id_token: str = "signed-id-token"
    token: str = "access-token"
    expired: bool = False
    valid: bool = True
    refresh_count: int = 0

    def refresh(self, _request: object) -> None:
        self.refresh_count += 1
        self.expired = False
        self.valid = True
        self.token = "refreshed-access-token"

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "refresh_token": self.refresh_token,
                "token_uri": self.token_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scopes": [
                    "openid",
                    "https://www.googleapis.com/auth/userinfo.email",
                    "https://www.googleapis.com/auth/gmail.send",
                ],
            }
        )


class FakeFlow:
    def __init__(self, credentials: FakeCredentials) -> None:
        self.credentials = credentials
        self.run_kwargs: dict[str, object] = {}

    def run_local_server(self, **kwargs: object) -> FakeCredentials:
        self.run_kwargs = kwargs
        return self.credentials


class FakeResponse:
    def __init__(self, payload: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.payload = payload or {"id": "gmail-message-1"}
        self.error = error

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> dict[str, object]:
        return self.payload


class FakeAuthorizedSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.posts: list[tuple[str, dict[str, object], int]] = []
        self.closed = False

    def post(self, url: str, *, json: dict[str, object], timeout: int) -> FakeResponse:
        self.posts.append((url, json, timeout))
        return self.response

    def close(self) -> None:
        self.closed = True


def _dependencies(
    keyring: MemoryKeyring,
    *,
    credentials: FakeCredentials | None = None,
    flow: FakeFlow | None = None,
    session: FakeAuthorizedSession | None = None,
    authorized_email: str = "langkunlong@gmail.com",
    revoke_token=lambda _token: None,
):
    from core.gmail_oauth import GmailOAuthDependencies

    credentials = credentials or FakeCredentials()
    flow = flow or FakeFlow(credentials)
    session = session or FakeAuthorizedSession()
    return GmailOAuthDependencies(
        keyring=keyring,
        flow_factory=lambda _path, _scopes: flow,
        credentials_loader=lambda _value, _scopes: credentials,
        request_factory=object,
        authorized_session_factory=lambda _credentials: session,
        id_token_verifier=lambda _token, _request, _client_id: {
            "email": authorized_email,
            "email_verified": True,
        },
        revoke_token=revoke_token,
    )


def test_authorize_gmail_uses_browser_consent_and_stores_refresh_credential(tmp_path: Path) -> None:
    """Dropping the credential write would force reauthorization on every notification."""
    from core.gmail_oauth import (
        GmailOAuthDependencies,
        authorize_gmail,
        gmail_keyring_service,
    )

    client_secrets = tmp_path / "gmail-oauth-client.json"
    client_secrets.write_text('{"installed": {}}', encoding="utf-8")
    keyring = MemoryKeyring()
    flow = FakeFlow(FakeCredentials())
    captured: dict[str, object] = {}

    def flow_factory(path: str, scopes: tuple[str, ...]) -> FakeFlow:
        captured["path"] = path
        captured["scopes"] = scopes
        return flow

    dependencies = GmailOAuthDependencies(
        keyring=keyring,
        flow_factory=flow_factory,
        credentials_loader=lambda _value, _scopes: FakeCredentials(),
        request_factory=object,
        authorized_session_factory=lambda _credentials: object(),
        id_token_verifier=lambda _token, _request, _client_id: {
            "email": "langkunlong@gmail.com",
            "email_verified": True,
        },
        revoke_token=lambda _token: None,
    )

    result = authorize_gmail(
        "langkunlong@gmail.com",
        client_secrets,
        dependencies=dependencies,
    )

    assert result.email == "langkunlong@gmail.com"
    assert result.refresh_token_stored is True
    assert captured == {
        "path": str(client_secrets.resolve()),
        "scopes": (
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/gmail.send",
        ),
    }
    assert flow.run_kwargs["host"] == "127.0.0.1"
    assert flow.run_kwargs["port"] == 0
    assert flow.run_kwargs["access_type"] == "offline"
    assert flow.run_kwargs["prompt"] == "consent"
    stored = keyring.get_password(gmail_keyring_service(), "langkunlong@gmail.com")
    assert stored is not None
    assert json.loads(stored)["refresh_token"] == "refresh-token"


def test_authorize_gmail_rejects_a_different_google_account_without_storing(tmp_path: Path) -> None:
    """Selecting the wrong account must not silently authorize a different mailbox."""
    from core.gmail_oauth import GmailOAuthError, authorize_gmail, gmail_keyring_service

    client_secrets = tmp_path / "gmail-oauth-client.json"
    client_secrets.write_text('{"installed": {}}', encoding="utf-8")
    keyring = MemoryKeyring()

    with pytest.raises(GmailOAuthError, match="does not match"):
        authorize_gmail(
            "langkunlong@gmail.com",
            client_secrets,
            dependencies=_dependencies(keyring, authorized_email="someone-else@gmail.com"),
        )

    assert keyring.get_password(gmail_keyring_service(), "langkunlong@gmail.com") is None


def test_authorize_gmail_rejects_symlinked_client_secrets(tmp_path: Path) -> None:
    """A linked credential file could redirect authorization to attacker-controlled content."""
    from core.gmail_oauth import GmailOAuthError, authorize_gmail

    client_secrets = tmp_path / "gmail-oauth-client.json"
    client_secrets.write_text('{"installed": {}}', encoding="utf-8")
    linked_secrets = tmp_path / "linked-client.json"
    try:
        linked_secrets.symlink_to(client_secrets)
    except OSError:
        pytest.skip("Creating file symlinks is not permitted on this host")

    with pytest.raises(GmailOAuthError, match="regular file"):
        authorize_gmail(
            "langkunlong@gmail.com",
            linked_secrets,
            dependencies=_dependencies(MemoryKeyring()),
        )


def test_is_gmail_authorized_rejects_malformed_vault_content() -> None:
    """A corrupt credential must report unconfigured instead of crashing the trading workflow."""
    from core.gmail_oauth import gmail_keyring_service, is_gmail_authorized

    keyring = MemoryKeyring()
    keyring.set_password(gmail_keyring_service(), "langkunlong@gmail.com", "not-json")

    assert is_gmail_authorized(
        "langkunlong@gmail.com",
        dependencies=_dependencies(keyring),
    ) is False


def test_send_gmail_email_refreshes_and_posts_an_exact_message() -> None:
    """Dropping refresh or altering MIME headers would break unattended notifications."""
    from core.gmail_oauth import gmail_keyring_service, send_gmail_email

    keyring = MemoryKeyring()
    keyring.set_password(
        gmail_keyring_service(),
        "langkunlong@gmail.com",
        FakeCredentials().to_json(),
    )
    credentials = FakeCredentials(expired=True, valid=False)
    session = FakeAuthorizedSession()

    message_id = send_gmail_email(
        from_email="langkunlong@gmail.com",
        to_email="langkunlong@gmail.com",
        subject="CANSLIM OAuth test",
        body="OAuth delivery body",
        dependencies=_dependencies(keyring, credentials=credentials, session=session),
    )

    assert message_id == "gmail-message-1"
    assert credentials.refresh_count == 1
    assert session.closed is True
    assert len(session.posts) == 1
    url, payload, timeout = session.posts[0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert timeout == 20
    encoded = payload["raw"]
    assert isinstance(encoded, str)
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(encoded))
    assert message["From"] == "langkunlong@gmail.com"
    assert message["To"] == "langkunlong@gmail.com"
    assert message["Subject"] == "CANSLIM OAuth test"
    assert message.get_content().strip() == "OAuth delivery body"
    stored = keyring.get_password(gmail_keyring_service(), "langkunlong@gmail.com")
    assert stored is not None
    assert json.loads(stored)["token"] == "refreshed-access-token"


def test_revoke_gmail_authorization_deletes_only_after_google_accepts() -> None:
    """Local deletion before remote revocation would leave an unrecoverable active grant."""
    from core.gmail_oauth import gmail_keyring_service, revoke_gmail_authorization

    keyring = MemoryKeyring()
    keyring.set_password(
        gmail_keyring_service(),
        "langkunlong@gmail.com",
        FakeCredentials().to_json(),
    )
    revoked: list[str] = []

    result = revoke_gmail_authorization(
        "langkunlong@gmail.com",
        dependencies=_dependencies(keyring, revoke_token=revoked.append),
    )

    assert result.email == "langkunlong@gmail.com"
    assert result.remote_revoked is True
    assert revoked == ["refresh-token"]
    assert keyring.get_password(gmail_keyring_service(), "langkunlong@gmail.com") is None


def test_revoke_gmail_authorization_retains_vault_entry_on_remote_failure() -> None:
    """A transient revoke failure must remain retryable."""
    from core.gmail_oauth import (
        GmailOAuthError,
        gmail_keyring_service,
        revoke_gmail_authorization,
    )

    keyring = MemoryKeyring()
    serialized = FakeCredentials().to_json()
    keyring.set_password(gmail_keyring_service(), "langkunlong@gmail.com", serialized)

    def fail_revoke(_token: str) -> None:
        raise OSError("network unavailable")

    with pytest.raises(GmailOAuthError, match="revocation failed"):
        revoke_gmail_authorization(
            "langkunlong@gmail.com",
            dependencies=_dependencies(keyring, revoke_token=fail_revoke),
        )

    assert keyring.get_password(gmail_keyring_service(), "langkunlong@gmail.com") == serialized
