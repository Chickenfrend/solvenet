"""Credential setup checks use synthetic input only; no terminal/API access."""

import getpass
import importlib.util
import os
import stat
import tempfile
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    "openai_key_setup",
    Path(__file__).resolve().parents[1] / "worker/setup_openai_key.py",
)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class KeySetupTests(unittest.TestCase):
    def test_hidden_input_exclusive_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-key"
            with patch.object(
                setup.getpass, "getpass", return_value="mock-test-key"
            ) as prompt:
                setup.write_key(path)
                prompt.assert_called_once_with("OpenAI API key (hidden): ")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_text(), "mock-test-key\n")
                with self.assertRaises(FileExistsError):
                    setup.write_key(path)
                self.assertEqual(path.read_text(), "mock-test-key\n")

    def test_interruption_removes_partial_file(self):
        original = os.fdopen

        @contextmanager
        def interrupted_file(fd, mode):
            with original(fd, mode):
                yield Mock(write=Mock(side_effect=KeyboardInterrupt))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-key"
            with (
                patch.object(setup.getpass, "getpass", return_value="mock-test-key"),
                patch.object(setup.os, "fdopen", interrupted_file),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    setup.write_key(path)
            self.assertFalse(path.exists())

    def test_echoing_fallback_is_refused_before_file_creation(self):
        def fallback(prompt):
            warnings.warn("mock fallback", getpass.GetPassWarning, stacklevel=2)
            self.fail("fallback continued")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mock-key"
            with patch.object(setup.getpass, "getpass", fallback):
                with self.assertRaises(getpass.GetPassWarning):
                    setup.write_key(path)
            self.assertFalse(path.exists())
