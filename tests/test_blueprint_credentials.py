import contextlib
import io
import os
import sys
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import blueprint_credentials as credentials

FAKE_KEY = "sk-synthetic-test-key-never-valid"


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.backend = MagicMock()
        self.backend.get_password.return_value = None
        self.store = self.stack.enter_context(patch.object(credentials, "_credential_store", return_value=self.backend))
        self.stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": ""}))
        self.prompt = self.stack.enter_context(patch.object(credentials.getpass, "getpass", return_value=FAKE_KEY))
        self.stack.enter_context(patch.object(sys, "stdin", MagicMock(isatty=lambda: True)))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))

    def assert_secret_hidden(self):
        self.assertNotIn(FAKE_KEY, self.output.getvalue())
        self.assertNotIn(FAKE_KEY, os.environ.values())

    def test_first_run_saves_and_later_run_reuses_without_prompt(self):
        self.backend.set_password.side_effect = lambda service, account, key: setattr(
            self.backend.get_password, "return_value", key)
        self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.prompt.assert_called_once()
        self.backend.set_password.assert_called_once_with(credentials.SERVICE, credentials.ACCOUNT, FAKE_KEY)
        self.assert_secret_hidden()

    def test_environment_overrides_store_without_persisting_or_loading_backend(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "  " + FAKE_KEY + "\n"}):
            self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.store.assert_not_called()
        self.prompt.assert_not_called()
        self.backend.set_password.assert_not_called()
        self.assert_secret_hidden()

    def test_whitespace_environment_is_missing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": " \n\t"}):
            self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.prompt.assert_called_once()

    def test_invalid_environment_fails_without_prompting_or_exposing_value(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY + "\nextra"}):
            with self.assertRaises(credentials.CredentialError) as error:
                credentials.get_api_key()
        self.assertNotIn(FAKE_KEY, str(error.exception))
        self.store.assert_not_called()
        self.prompt.assert_not_called()

    def test_blank_control_character_and_whitespace_input_retry_without_saving(self):
        self.prompt.side_effect = ["", "   ", FAKE_KEY + "\x00", "two words", "  " + FAKE_KEY + "\n"]
        self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.assertEqual(self.prompt.call_count, 5)
        self.backend.set_password.assert_called_once_with(credentials.SERVICE, credentials.ACCOUNT, FAKE_KEY)
        self.assert_secret_hidden()

    def test_eof_and_keyboard_cancel_preserve_saved_key_during_replacement(self):
        self.backend.get_password.return_value = "previous-synthetic-key"
        for raised, expected in ((EOFError(), credentials.CredentialError),
                                 (KeyboardInterrupt(), KeyboardInterrupt)):
            with self.subTest(error=type(raised).__name__):
                self.prompt.side_effect = raised
                with self.assertRaises(expected):
                    credentials.get_api_key(replace=True)
        self.backend.set_password.assert_not_called()
        self.backend.delete_password.assert_not_called()

    def test_missing_key_never_reads_piped_input(self):
        with patch.object(sys, "stdin", MagicMock(isatty=lambda: False)):
            with self.assertRaisesRegex(credentials.CredentialError, "interactive terminal"):
                credentials.get_api_key()
        self.prompt.assert_not_called()
        self.backend.set_password.assert_not_called()

    def test_saved_key_can_be_reused_without_terminal(self):
        self.backend.get_password.return_value = FAKE_KEY
        with patch.object(sys, "stdin", None):
            self.assertEqual(credentials.get_api_key(), FAKE_KEY)
        self.prompt.assert_not_called()

    def test_echo_fallback_stops_before_reading_secret(self):
        def fallback(*args, **kwargs):
            warnings.warn("Cannot disable echo", credentials.getpass.GetPassWarning)
            self.fail("Echoed input must never be reached")

        self.prompt.side_effect = fallback
        with self.assertRaisesRegex(credentials.CredentialError, "Hidden input is unavailable"):
            credentials.get_api_key()
        self.backend.set_password.assert_not_called()

    def test_backend_read_failure_is_redacted_and_does_not_prompt_or_overwrite(self):
        self.backend.get_password.side_effect = RuntimeError(FAKE_KEY)
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.get_api_key()
        self.assertNotIn(FAKE_KEY, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.prompt.assert_not_called()
        self.backend.set_password.assert_not_called()
        self.assert_secret_hidden()

    def test_failed_save_does_not_report_success_or_leak_backend_error(self):
        self.backend.set_password.side_effect = RuntimeError(FAKE_KEY)
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.get_api_key()
        self.assertNotIn(FAKE_KEY, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)
        self.assertNotIn("API key saved", self.output.getvalue())
        self.assert_secret_hidden()

    def test_explicit_replacement_prompts_even_with_environment_and_saved_key(self):
        self.backend.get_password.return_value = "previous-synthetic-key"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-synthetic-key"}):
            self.assertEqual(credentials.get_api_key(replace=True), FAKE_KEY)
        self.prompt.assert_called_once()
        self.backend.set_password.assert_called_once_with(credentials.SERVICE, credentials.ACCOUNT, FAKE_KEY)
        self.assertIn("will still override", self.output.getvalue())
        self.assert_secret_hidden()

    def test_forget_deletes_only_owned_entry_and_leaves_environment_unchanged(self):
        self.backend.get_password.return_value = FAKE_KEY
        with patch.dict(os.environ, {"OPENAI_API_KEY": "environment-synthetic-key"}):
            self.assertTrue(credentials.forget_api_key())
            self.assertEqual(os.environ["OPENAI_API_KEY"], "environment-synthetic-key")
        self.backend.delete_password.assert_called_once_with(credentials.SERVICE, credentials.ACCOUNT)
        self.prompt.assert_not_called()
        self.assert_secret_hidden()

    def test_forget_missing_key_is_idempotent(self):
        self.assertFalse(credentials.forget_api_key())
        self.backend.delete_password.assert_not_called()

    def test_delete_failure_is_redacted(self):
        self.backend.get_password.return_value = FAKE_KEY
        self.backend.delete_password.side_effect = RuntimeError(FAKE_KEY)
        with self.assertRaises(credentials.CredentialError) as error:
            credentials.forget_api_key()
        self.assertNotIn(FAKE_KEY, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)


class BackendSelectionTests(unittest.TestCase):
    def test_only_explicit_native_backends_are_loaded(self):
        for platform, module, name in (
            ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
            ("darwin", "keyring.backends.macOS", "Keyring"),
            ("linux", "keyring.backends.SecretService", "Keyring"),
        ):
            backend = MagicMock(priority=5, persist="enterprise")
            constructor = MagicMock(return_value=backend)
            with self.subTest(platform=platform), patch.object(sys, "platform", platform), \
                    patch.object(credentials.importlib, "import_module",
                                 return_value=SimpleNamespace(**{name: constructor})) as load:
                self.assertIs(credentials._credential_store(), backend)
                load.assert_called_once_with(module)
                if platform == "win32":
                    self.assertEqual(backend.persist, "local machine")

    def test_no_fallback_when_native_backend_missing_or_broken(self):
        for error in (ImportError(FAKE_KEY), RuntimeError(FAKE_KEY)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(credentials.importlib, "import_module", side_effect=error) as load:
                with self.assertRaises(credentials.CredentialError) as caught:
                    credentials._credential_store()
                self.assertNotIn(FAKE_KEY, str(caught.exception))
                load.assert_called_once()

    def test_unsupported_platform_does_not_load_backend(self):
        with patch.object(sys, "platform", "unsupported"), \
                patch.object(credentials.importlib, "import_module") as load:
            with self.assertRaisesRegex(credentials.CredentialError, "unsupported"):
                credentials._credential_store()
            load.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows credential adapter")
    def test_real_windows_adapter_writes_with_local_persistence(self):
        try:
            from keyring.backends import Windows
        except ImportError:
            self.skipTest("Install requirements-browser.txt for the native adapter test")
        with patch.dict(os.environ, {"KEYRING_PROPERTY_PERSIST": "enterprise",
                                     "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"}):
            backend = credentials._credential_store()
        self.assertIsInstance(backend, Windows.WinVaultKeyring)
        # Exercise the installed adapter without reading/writing the user's real store.
        with patch.object(backend, "_read_credential", return_value=None), \
                patch.object(Windows.win32cred, "CredWrite") as write:
            backend.set_password(credentials.SERVICE, credentials.ACCOUNT, FAKE_KEY)
        record, flags = write.call_args.args
        self.assertEqual(record["Persist"], Windows.win32cred.CRED_PERSIST_LOCAL_MACHINE)
        self.assertEqual(record["TargetName"], credentials.SERVICE)
        self.assertEqual(record["CredentialBlob"], FAKE_KEY)


if __name__ == "__main__":
    unittest.main()
