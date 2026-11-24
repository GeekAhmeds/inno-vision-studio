"""Camera passwords live in the OS keyring, never in project files (prompt §5.2).

Linux: Secret Service (GNOME Keyring / KWallet). Windows: Credential Manager.
"""

from __future__ import annotations

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "inno-vision-studio"


class SecretError(Exception):
    """The system keyring is missing or locked."""


def _key(project: str, source_id: str) -> str:
    return f"{project}:{source_id}"


def set_password(project: str, source_id: str, password: str) -> None:
    try:
        keyring.set_password(SERVICE, _key(project, source_id), password)
    except KeyringError as exc:
        raise SecretError(
            "No system keyring is available to store the camera password. "
            "On Linux install and unlock GNOME Keyring or KWallet."
        ) from exc


def get_password(project: str, source_id: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, _key(project, source_id))
    except KeyringError:
        return None


def delete_password(project: str, source_id: str) -> None:
    try:
        keyring.delete_password(SERVICE, _key(project, source_id))
    except (PasswordDeleteError, KeyringError):
        pass
