"""Reversible encryption for provider credentials stored at rest.

Provider credentials must be replayed to the upstream API, so they are encrypted
(Fernet/AES-128-CBC + HMAC) rather than hashed. Only masked previews are ever
returned over the API.
"""
import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
from configs import envs, logger

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """Build (once) the Fernet cipher from CREDENTIALS_ENCRYPTION_KEY.

    Returns:
        Cached Fernet instance.
    """
    global _fernet
    if _fernet is None:
        key = envs.CREDENTIALS_ENCRYPTION_KEY
        if not key:
            raise RuntimeError(
                "CREDENTIALS_ENCRYPTION_KEY is not set; cannot store provider credentials."
            )
        _fernet = Fernet(_normalize_key(key))
    return _fernet


def _normalize_key(key: str) -> bytes:
    """Accept either a real Fernet key or an arbitrary passphrase.

    Args:
        key: Configured key material.

    Returns:
        A 32-byte url-safe base64 key usable by Fernet.
    """
    raw = key.encode("utf-8")
    try:
        if len(base64.urlsafe_b64decode(raw)) == 32:
            return raw
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    """Encrypt a credential for storage.

    Args:
        value: Plaintext credential, or None.

    Returns:
        The ciphertext token, or None if value was None.
    """
    if value is None:
        return None
    return _get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: Optional[str]) -> Optional[str]:
    """Decrypt a stored credential.

    Args:
        token: Ciphertext produced by encrypt_secret, or None.

    Returns:
        The plaintext credential, or None if token was None or undecryptable.
    """
    if token is None:
        return None
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.error("Failed to decrypt a stored credential; key rotation mismatch?")
        return None


def mask_secret(token: Optional[str], visible: int = 4) -> Optional[str]:
    """Render a stored credential as a masked preview for API responses.

    Args:
        token: Ciphertext produced by encrypt_secret, or None.
        visible: Number of trailing plaintext characters to reveal.

    Returns:
        A masked string such as "****abcd", or None if there is no value.
    """
    plaintext = decrypt_secret(token)
    if not plaintext:
        return None
    if len(plaintext) <= visible:
        return "*" * len(plaintext)
    return "*" * (len(plaintext) - visible) + plaintext[-visible:]
