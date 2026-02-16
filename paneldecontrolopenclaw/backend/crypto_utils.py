"""Utility functions for encrypting and decrypting API keys.

This module centralises handling of sensitive configuration values.  It
implements symmetric encryption of API keys and other secrets using
the `cryptography` library's Fernet symmetric cipher.  An
``encryption_key`` is stored in the application's configuration file
(`config.json`).  When absent, a new key is generated and persisted.

Values encrypted via :func:`encrypt_value` are prefixed with
``"ENC:"`` to indicate that decryption should be performed when
reading them back via :func:`decrypt_value`.  Unencrypted values are
returned untouched.

The encryption key is automatically created when first needed.  It is
generated using :func:`cryptography.fernet.Fernet.generate_key`,
which yields a URL-safe base64-encoded 32-byte key.  This module
handles reading and writing the key to the configuration file so that
encryption is transparent for callers.

Functions defined here are intentionally decoupled from the GUI and
backend components to avoid cyclic dependencies.  The `config_path`
must be provided for each call so that the key can be looked up and
saved consistently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from cryptography.fernet import Fernet
except Exception:
    Fernet = None  # type: ignore

def _load_config(config_path: str) -> dict[str, Any]:
    """Load the JSON configuration file, returning an empty dict on error."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(config_path: str, cfg: dict[str, Any]) -> None:
    """Persist the given configuration back to disk."""
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _ensure_encryption_key(cfg: dict[str, Any], config_path: str) -> str:
    """Ensure that the configuration contains an ``encryption_key``.

    If the key is missing or empty, a new one is generated and saved
    back to the configuration file.  The returned key is a URL-safe
    base64 encoded string suitable for passing directly to
    :class:`cryptography.fernet.Fernet`.

    Parameters
    ----------
    cfg : dict
        The configuration dictionary loaded from JSON.
    config_path : str
        Path to the configuration file.  Used to persist a newly
        generated key.

    Returns
    -------
    str
        The encryption key, as a base64-encoded string.
    """
    key = cfg.get("encryption_key")
    if not key:
        if Fernet is None:
            # If cryptography isn't available, fall back to a dummy
            # marker.  Consumers should check for this and avoid
            # encrypting/decrypting.
            key = ""
        else:
            # Generate a new key and persist it
            key_bytes = Fernet.generate_key()
            key = key_bytes.decode("utf-8")
            cfg["encryption_key"] = key
            _save_config(config_path, cfg)
    return key


def encrypt_value(value: str, config_path: str) -> str:
    """Encrypt a sensitive value using the configured encryption key.

    If no encryption key is configured or the `cryptography` library
    isn't available, the value is returned unchanged.  When
    encryption succeeds, the returned string will be prefixed with
    ``"ENC:"`` to indicate that decryption is required when reading
    the value.

    Parameters
    ----------
    value : str
        The plain-text value to encrypt.  If falsy, an empty string
        is returned.
    config_path : str
        Path to the JSON configuration file containing the
        ``encryption_key``.

    Returns
    -------
    str
        The encrypted value prefaced with ``"ENC:"`` or the original
        value if encryption cannot be performed.
    """
    if not value:
        return ""
    if Fernet is None:
        # cryptography not available
        return value
    cfg = _load_config(config_path)
    key = _ensure_encryption_key(cfg, config_path)
    if not key:
        return value
    try:
        f = Fernet(key.encode("utf-8"))
        token = f.encrypt(value.encode("utf-8")).decode("utf-8")
        return "ENC:" + token
    except Exception:
        return value


def decrypt_value(value: str, config_path: str) -> str:
    """Decrypt a value previously encrypted with :func:`encrypt_value`.

    If the value does not start with ``"ENC:"``, it is returned
    unchanged.  If decryption fails, an empty string is returned.

    Parameters
    ----------
    value : str
        The encrypted value (prefixed with ``"ENC:"``).
    config_path : str
        Path to the JSON configuration file containing the
        ``encryption_key``.

    Returns
    -------
    str
        The decrypted plain-text value, or the original value if no
        decryption is needed or possible.
    """
    if not value or not isinstance(value, str):
        return value
    if not value.startswith("ENC:"):
        return value
    if Fernet is None:
        return ""
    token = value[4:]
    cfg = _load_config(config_path)
    key = _ensure_encryption_key(cfg, config_path)
    if not key:
        return ""
    try:
        f = Fernet(key.encode("utf-8"))
        decrypted = f.decrypt(token.encode("utf-8")).decode("utf-8")
        return decrypted
    except Exception:
        return ""