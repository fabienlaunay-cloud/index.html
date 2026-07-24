"""Transparent encryption-at-rest for OAuth tokens (Amazon SP-API, Drive).

Amazon's Data Protection Policy requires credentials/tokens to be encrypted at
rest. Tokens are stored with an ``enc:`` prefix so encrypted and legacy
plaintext values can coexist during migration:

- ``decrypt_token`` returns legacy plaintext unchanged and only decrypts
  values carrying the prefix — so existing rows keep working.
- ``encrypt_token`` is a no-op when no key is configured, so the app never
  breaks if ``TOKEN_ENCRYPTION_KEY`` is unset (it just stays plaintext until
  a key is provided).

Set ``TOKEN_ENCRYPTION_KEY`` (env or admin config) to a Fernet key, e.g.
``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
"""
import os
from typing import Optional

from app.db import get_config
from app.logger import log

_PREFIX = "enc:"
_fernet = None  # cached once successfully built


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip() or get_config("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        log.error(f"TOKEN_ENCRYPTION_KEY invalide: {type(e).__name__}: {e}")
        return None
    return _fernet


def encrypt_token(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a token for storage. No-op if already encrypted or no key set."""
    if not plaintext or plaintext.startswith(_PREFIX):
        return plaintext
    f = _get_fernet()
    if f is None:
        log.warning("TOKEN_ENCRYPTION_KEY absente — token stocké en clair")
        return plaintext
    return _PREFIX + f.encrypt(plaintext.encode()).decode()


def decrypt_token(stored: Optional[str]) -> Optional[str]:
    """Return the usable token. Legacy plaintext passes through untouched."""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    f = _get_fernet()
    if f is None:
        log.error("Token chiffré mais TOKEN_ENCRYPTION_KEY absente — déchiffrement impossible")
        return stored
    try:
        return f.decrypt(stored[len(_PREFIX):].encode()).decode()
    except Exception as e:
        log.error(f"Échec du déchiffrement du token: {type(e).__name__}")
        return stored
