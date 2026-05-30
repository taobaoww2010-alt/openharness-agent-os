"""Unified authentication management for OpenHarness."""

from daoyi.auth.flows import ApiKeyFlow, BrowserFlow, DeviceCodeFlow
from daoyi.auth.manager import AuthManager
from daoyi.auth.storage import (
    clear_provider_credentials,
    decrypt,
    encrypt,
    load_credential,
    load_external_binding,
    store_credential,
    store_external_binding,
)

__all__ = [
    "AuthManager",
    "ApiKeyFlow",
    "BrowserFlow",
    "DeviceCodeFlow",
    "store_credential",
    "load_credential",
    "store_external_binding",
    "load_external_binding",
    "clear_provider_credentials",
    # Deprecated — use _obfuscate/_deobfuscate directly if needed.
    # Kept for backward compatibility; will be removed in a future version.
    "encrypt",
    "decrypt",
]
