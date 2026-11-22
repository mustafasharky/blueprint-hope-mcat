"""Local OpenAI API-key setup and retrieval. Never writes secrets to project files."""
from __future__ import annotations

import getpass
import importlib
import os
import sys
import warnings

SERVICE = "blueprint-hope-openai"
ACCOUNT = "api-key"
ENVIRONMENT_KEY = "OPENAI_API_KEY"


class CredentialError(ValueError):
    """A credential failure whose message is safe to show in the terminal."""


def _credential_store():
    """Select an OS store directly; never use configurable/third-party backends."""
    if sys.platform == "win32":
        module, name = "keyring.backends.Windows", "WinVaultKeyring"
    elif sys.platform == "darwin":
        module, name = "keyring.backends.macOS", "Keyring"
    elif sys.platform.startswith("linux"):
        module, name = "keyring.backends.SecretService", "Keyring"
    else:
        raise CredentialError(
            "Secure credential storage is unsupported on this platform. "
            "Supply OPENAI_API_KEY through your environment, or use --local-only."
        )
    try:
        backend = getattr(importlib.import_module(module), name)()
        if sys.platform == "win32":
            # Override keyring's roaming default and any KEYRING_PROPERTY_PERSIST.
            backend.persist = "local machine"
        if backend.priority < 1:
            raise RuntimeError("OS credential store unavailable")
        return backend
    except ImportError:
        raise CredentialError(
            "Install requirements-browser.txt with the same Python interpreter, then retry. "
            "Alternatively, supply OPENAI_API_KEY through your environment or use --local-only."
        ) from None
    except Exception:
        raise CredentialError(
            "The OS credential store is unavailable. Unlock your local keychain and retry, "
            "supply OPENAI_API_KEY through your environment, or use --local-only. "
            "No plaintext fallback is used."
        ) from None


def _validate_key(value):
    # Check only paste/header safety, not a prefix or length tied to an API-key version.
    key = value.strip()
    if not key or any(not 33 <= ord(char) <= 126 for char in key):
        raise CredentialError("Enter a nonempty OpenAI API key without spaces or control characters.")
    return key


def _read_saved_key(backend):
    try:
        return backend.get_password(SERVICE, ACCOUNT)
    except Exception:
        # Backend exception text may contain credentials. Do not print or chain it.
        raise CredentialError(
            "Could not read the local API key. Unlock your OS credential store and retry, "
            "or supply OPENAI_API_KEY through your environment."
        ) from None


def _prompt_for_key():
    if sys.stdin is None or not sys.stdin.isatty():
        raise CredentialError(
            "OpenAI API-key setup requires an interactive terminal. Run blueprint_run.py "
            "--configure-openai in a terminal first, supply OPENAI_API_KEY through your "
            "environment, or use --local-only."
        )
    print("Enter your OpenAI API key for ChatGPT connection setup.", file=sys.stderr)
    print("Create a key at https://platform.openai.com/api-keys", file=sys.stderr)
    print("Input is hidden. The key will be saved only in this computer's OS credential store.",
          file=sys.stderr)
    while True:
        try:
            with warnings.catch_warnings():
                # getpass normally falls back to echoed input when echo cannot be disabled.
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("OpenAI API key (hidden; Ctrl+C to cancel): ")
        except (getpass.GetPassWarning, OSError):
            raise CredentialError(
                "Hidden input is unavailable. Use a normal terminal to enter the API key. "
                "No key was saved."
            ) from None
        except EOFError:
            raise CredentialError("API-key setup cancelled. No key was saved.") from None
        try:
            return _validate_key(value)
        except CredentialError as exc:
            print(str(exc), file=sys.stderr)


def get_api_key(*, replace=False):
    """Return the environment key, saved OS key, or a newly prompted/saved key.

    Environment credentials override the saved key and are never persisted here.
    replace=True explicitly prompts for a new saved key, ignoring either source.
    Callers must keep the returned secret out of logs, exports, and subprocesses.
    This performs no network requests and does not validate access with OpenAI.
    """
    environment_key = os.environ.get(ENVIRONMENT_KEY, "").strip()
    if not replace and environment_key:
        return _validate_key(environment_key)
    backend = _credential_store()
    if not replace:
        saved = _read_saved_key(backend)
        if saved and saved.strip():
            return _validate_key(saved)
    elif environment_key:
        print("OPENAI_API_KEY is set and will still override this saved key on normal runs.",
              file=sys.stderr)
    key = _prompt_for_key()
    try:
        backend.set_password(SERVICE, ACCOUNT, key)
    except Exception:
        raise CredentialError(
            "Could not save the API key in the OS credential store. Unlock it and retry. "
            "No plaintext fallback is used."
        ) from None
    print("API key saved in the local OS credential store for future runs.", file=sys.stderr)
    return key


def forget_api_key():
    """Remove only this tool's saved key; never change the environment or other secrets."""
    backend = _credential_store()
    if _read_saved_key(backend) is None:
        return False
    try:
        backend.delete_password(SERVICE, ACCOUNT)
    except Exception:
        raise CredentialError(
            "Could not remove the saved API key. Unlock your OS credential store and retry."
        ) from None
    return True
