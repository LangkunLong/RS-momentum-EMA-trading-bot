"""Gmail API OAuth authorization and credential storage.

The default dependency boundary is loaded lazily so importing the trading stack never opens a
browser, contacts Google, or reads Windows Credential Manager.  OAuth refresh credentials are
stored by the OS keyring backend rather than in ``.env`` or a repository file.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Protocol

_GMAIL_KEYRING_SERVICE = "canslim-trading-bot.gmail-oauth"
_GMAIL_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
)


class GmailOAuthError(RuntimeError):
    """A safe operator-facing Gmail authorization failure."""


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


@dataclass(frozen=True)
class GmailOAuthDependencies:
    """Injectable external boundaries used by the OAuth workflow."""

    keyring: KeyringBackend
    flow_factory: Callable[[str, tuple[str, ...]], Any]
    credentials_loader: Callable[[dict[str, object], tuple[str, ...]], Any]
    request_factory: Callable[[], Any]
    authorized_session_factory: Callable[[Any], Any]
    id_token_verifier: Callable[[str, Any, str], dict[str, object]]
    revoke_token: Callable[[str], None]


@dataclass(frozen=True)
class GmailAuthorizationResult:
    email: str
    refresh_token_stored: bool


@dataclass(frozen=True)
class GmailRevocationResult:
    email: str
    remote_revoked: bool


def gmail_keyring_service() -> str:
    """Return the stable OS credential-vault service name."""
    return _GMAIL_KEYRING_SERVICE


def _normalize_email(value: str) -> str:
    if not isinstance(value, str):
        raise GmailOAuthError("Gmail address is required")
    normalized = value.strip().casefold()
    if (
        not normalized
        or len(normalized) > 254
        or "\r" in normalized
        or "\n" in normalized
        or normalized.count("@") != 1
        or normalized.startswith("@")
        or normalized.endswith("@")
    ):
        raise GmailOAuthError("Gmail address is invalid")
    return normalized


def _default_dependencies() -> GmailOAuthDependencies:
    try:
        import keyring
        from google.auth.transport.requests import AuthorizedSession, Request
        from google.oauth2.credentials import Credentials
        from google.oauth2.id_token import verify_oauth2_token
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - exercised after dependency installation
        raise GmailOAuthError("Gmail OAuth dependencies are not installed") from exc

    def flow_factory(path: str, scopes: tuple[str, ...]) -> Any:
        return InstalledAppFlow.from_client_secrets_file(path, scopes=list(scopes))

    def credentials_loader(value: dict[str, object], scopes: tuple[str, ...]) -> Any:
        return Credentials.from_authorized_user_info(value, scopes=list(scopes))

    def revoke_token(token: str) -> None:
        import requests

        response = requests.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token},
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        response.raise_for_status()

    return GmailOAuthDependencies(
        keyring=keyring,
        flow_factory=flow_factory,
        credentials_loader=credentials_loader,
        request_factory=Request,
        authorized_session_factory=AuthorizedSession,
        id_token_verifier=verify_oauth2_token,
        revoke_token=revoke_token,
    )


def _read_stored_credential(
    email: str,
    dependencies: GmailOAuthDependencies,
) -> tuple[str, dict[str, object]] | None:
    try:
        raw = dependencies.keyring.get_password(_GMAIL_KEYRING_SERVICE, email)
    except Exception as exc:
        raise GmailOAuthError("Windows Credential Manager is unavailable") from exc
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GmailOAuthError("Stored Gmail authorization is malformed") from exc
    if not isinstance(value, dict):
        raise GmailOAuthError("Stored Gmail authorization is malformed")
    required_text = ("refresh_token", "token_uri", "client_id", "client_secret")
    if any(not isinstance(value.get(name), str) or not value[name] for name in required_text):
        raise GmailOAuthError("Stored Gmail authorization is incomplete")
    scopes = value.get("scopes")
    if not isinstance(scopes, list) or not set(_GMAIL_SCOPES).issubset(
        {item for item in scopes if isinstance(item, str)}
    ):
        raise GmailOAuthError("Stored Gmail authorization has the wrong scopes")
    return raw, value


def is_gmail_authorized(
    email: str,
    *,
    dependencies: GmailOAuthDependencies | None = None,
) -> bool:
    """Return whether a complete Gmail refresh credential exists in the OS vault."""
    try:
        normalized_email = _normalize_email(email)
        return _read_stored_credential(
            normalized_email,
            dependencies or _default_dependencies(),
        ) is not None
    except GmailOAuthError:
        return False


def _load_credentials(
    email: str,
    dependencies: GmailOAuthDependencies,
) -> Any:
    stored = _read_stored_credential(email, dependencies)
    if stored is None:
        raise GmailOAuthError("Gmail has not been authorized")
    _raw, value = stored
    try:
        credentials = dependencies.credentials_loader(value, _GMAIL_SCOPES)
        if bool(getattr(credentials, "expired", False)):
            credentials.refresh(dependencies.request_factory())
            serialized = credentials.to_json()
            refreshed = json.loads(serialized)
            if not isinstance(refreshed, dict) or not refreshed.get("refresh_token"):
                raise GmailOAuthError("Refreshed Gmail authorization is incomplete")
            dependencies.keyring.set_password(
                _GMAIL_KEYRING_SERVICE,
                email,
                serialized,
            )
        if not bool(getattr(credentials, "valid", False)):
            raise GmailOAuthError("Gmail authorization is not valid")
        return credentials
    except GmailOAuthError:
        raise
    except Exception as exc:
        raise GmailOAuthError("Gmail authorization refresh failed") from exc


def send_gmail_email(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    dependencies: GmailOAuthDependencies | None = None,
) -> str:
    """Send one plain-text message through the Gmail API and return its message ID."""
    sender = _normalize_email(from_email)
    recipient = _normalize_email(to_email)
    if not isinstance(subject, str) or not subject.strip() or "\r" in subject or "\n" in subject:
        raise GmailOAuthError("Email subject is invalid")
    if not isinstance(body, str) or not body:
        raise GmailOAuthError("Email body is required")
    deps = dependencies or _default_dependencies()
    credentials = _load_credentials(sender, deps)

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    session = None
    try:
        session = deps.authorized_session_factory(credentials)
        response = session.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            json={"raw": encoded},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        message_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise GmailOAuthError("Gmail accepted no message identifier")
        return message_id
    except GmailOAuthError:
        raise
    except Exception as exc:
        raise GmailOAuthError("Gmail API send failed") from exc
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if callable(close):
                close()


def revoke_gmail_authorization(
    email: str,
    *,
    dependencies: GmailOAuthDependencies | None = None,
) -> GmailRevocationResult:
    """Revoke Google's refresh grant, then remove the local vault credential."""
    normalized_email = _normalize_email(email)
    deps = dependencies or _default_dependencies()
    stored = _read_stored_credential(normalized_email, deps)
    if stored is None:
        raise GmailOAuthError("Gmail has not been authorized")
    _raw, value = stored
    refresh_token = str(value["refresh_token"])
    try:
        deps.revoke_token(refresh_token)
    except Exception as exc:
        raise GmailOAuthError("Gmail authorization revocation failed") from exc
    try:
        deps.keyring.delete_password(_GMAIL_KEYRING_SERVICE, normalized_email)
    except Exception as exc:
        raise GmailOAuthError("Gmail was revoked but the local credential could not be removed") from exc
    return GmailRevocationResult(email=normalized_email, remote_revoked=True)


def authorize_gmail(
    email: str,
    client_secrets_path: Path,
    *,
    dependencies: GmailOAuthDependencies | None = None,
) -> GmailAuthorizationResult:
    """Open Google's local desktop consent flow and vault the verified refresh credential."""
    normalized_email = _normalize_email(email)
    unresolved_path = Path(client_secrets_path)
    if unresolved_path.is_symlink():
        raise GmailOAuthError("OAuth client-secrets path must be a regular file")
    try:
        path = unresolved_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise GmailOAuthError("OAuth client-secrets file is unavailable") from exc
    if not path.is_file() or path.is_symlink():
        raise GmailOAuthError("OAuth client-secrets path must be a regular file")

    deps = dependencies or _default_dependencies()
    try:
        flow = deps.flow_factory(str(path), _GMAIL_SCOPES)
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=True,
            access_type="offline",
            prompt="consent",
            authorization_prompt_message="Opening Google authorization in your browser...",
            success_message="Gmail authorization complete. You may close this window.",
        )
        refresh_token = str(getattr(credentials, "refresh_token", "") or "")
        client_id = str(getattr(credentials, "client_id", "") or "")
        id_token = str(getattr(credentials, "id_token", "") or "")
        if not refresh_token or not client_id or not id_token:
            raise GmailOAuthError("Google did not return a reusable desktop authorization")
        claims = deps.id_token_verifier(
            id_token,
            deps.request_factory(),
            client_id,
        )
        authorized_email = _normalize_email(str(claims.get("email", "")))
        if authorized_email != normalized_email or claims.get("email_verified") is not True:
            raise GmailOAuthError("Authorized Google account does not match the requested Gmail address")
        serialized = credentials.to_json()
        parsed = json.loads(serialized)
        if not isinstance(parsed, dict) or parsed.get("refresh_token") != refresh_token:
            raise GmailOAuthError("Google credential serialization is incomplete")
        deps.keyring.set_password(_GMAIL_KEYRING_SERVICE, normalized_email, serialized)
    except GmailOAuthError:
        raise
    except Exception as exc:
        raise GmailOAuthError("Gmail authorization failed") from exc

    return GmailAuthorizationResult(
        email=normalized_email,
        refresh_token_stored=True,
    )
