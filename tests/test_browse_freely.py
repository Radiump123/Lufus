import unittest
from unittest.mock import patch, MagicMock
import os
from lufus.browse_freely import open_url_non_root


class TestBrowseFreely(unittest.TestCase):
    def setUp(self):
        self.url = "https://github.com/Hogjects/Lufus"

    @patch("os.geteuid")
    @patch("webbrowser.open")
    def test_open_url_non_root_as_regular_user(self, mock_webbrowser, mock_geteuid):
        # Simulate regular user (UID 1000)
        mock_geteuid.return_value = 1000

        open_url_non_root(self.url)

        # Should fallback to standard webbrowser.open
        mock_webbrowser.assert_called_once_with(self.url)

    @patch("os.geteuid")
    @patch("subprocess.Popen")
    @patch("pwd.getpwuid")
    def test_open_url_as_root_via_pkexec(self, mock_getpwuid, mock_popen, mock_geteuid):
        # Simulate root (UID 0)
        mock_geteuid.return_value = 0

        # Mock environment variables
        env_overrides = {
            "PKEXEC_UID": "1000",
            "DISPLAY": ":0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }

        # Mock pwd info
        mock_user = MagicMock()
        mock_user.pw_name = "raphael"
        mock_user.pw_dir = "/home/raphael"
        mock_getpwuid.return_value = mock_user

        with patch.dict(os.environ, env_overrides, clear=False):
            open_url_non_root(self.url)

        # Verify subprocess.Popen was called with runuser
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "runuser")
        self.assertEqual(cmd[2], "raphael")
        self.assertIn("xdg-open", cmd)
        self.assertIn(self.url, cmd)

        # Verify env passing
        env = kwargs.get("env", {})
        self.assertEqual(env.get("DISPLAY"), ":0")
        self.assertEqual(env.get("XDG_RUNTIME_DIR"), "/run/user/1000")
        self.assertEqual(env.get("HOME"), "/home/raphael")

    @patch("os.geteuid")
    @patch("subprocess.Popen")
    @patch("pwd.getpwnam")
    def test_open_url_as_root_via_sudo(self, mock_getpwnam, mock_popen, mock_geteuid):
        # Simulate root (UID 0)
        mock_geteuid.return_value = 0

        # Mock environment variables (no PKEXEC_UID, but SUDO_USER)
        env_overrides = {
            "SUDO_USER": "raphael",
            "DISPLAY": ":0",
        }
        # Ensure PKEXEC_UID is not in env if it was there
        with patch.dict(os.environ, env_overrides, clear=False):
            if "PKEXEC_UID" in os.environ:
                del os.environ["PKEXEC_UID"]

            # Mock pwd info
            mock_user = MagicMock()
            mock_user.pw_uid = 1000
            mock_user.pw_dir = "/home/raphael"
            mock_getpwnam.return_value = mock_user

            open_url_non_root(self.url)

        # Verify subprocess.Popen was called with runuser
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], "runuser")
        self.assertEqual(cmd[2], "raphael")
        self.assertIn(self.url, cmd)

        # Verify env passing (XDG_RUNTIME_DIR should be derived from UID 1000)
        env = kwargs.get("env", {})
        self.assertEqual(env.get("XDG_RUNTIME_DIR"), "/run/user/1000")
        self.assertEqual(env.get("HOME"), "/home/raphael")


if __name__ == "__main__":
    unittest.main()
