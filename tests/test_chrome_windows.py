"""Windows / CDP Chrome 路径与桌面目录解析测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from bijia import jd_chrome
from bijia.runner import _desktop_dirs


class DesktopPathTests(unittest.TestCase):
    def test_windows_desktop_includes_userprofile_and_onedrive(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(
                os.environ,
                {
                    "USERPROFILE": r"C:\Users\demo",
                    "OneDrive": r"C:\Users\demo\OneDrive",
                },
                clear=False,
            ):
                with mock.patch("bijia.runner._windows_known_desktop", return_value=None):
                    dirs = _desktop_dirs()
        texts = [str(path) for path in dirs]
        self.assertTrue(any(text.endswith("Desktop") or text.endswith("桌面") for text in texts))
        self.assertTrue(any("OneDrive" in text for text in texts))
        self.assertTrue(any(r"Users\demo" in text or "Users/demo" in text.replace("\\", "/") for text in texts))


class ChromeBackendTests(unittest.TestCase):
    def test_windows_defaults_to_cdp(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            backend, port = jd_chrome.resolve_chrome_backend(debug_port=None)
        self.assertEqual(backend, "cdp")
        self.assertEqual(port, 9222)

    def test_darwin_defaults_to_applescript(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            backend, port = jd_chrome.resolve_chrome_backend(debug_port=None)
        self.assertEqual(backend, "applescript")
        self.assertIsNone(port)

    def test_explicit_debug_port_forces_cdp_on_mac(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            backend, port = jd_chrome.resolve_chrome_backend(debug_port=9333)
        self.assertEqual(backend, "cdp")
        self.assertEqual(port, 9333)

    def test_env_debug_port_forces_cdp(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"):
            with mock.patch.dict(os.environ, {"BIJIA_CHROME_DEBUG_PORT": "9222"}):
                backend, port = jd_chrome.resolve_chrome_backend(debug_port=None)
        self.assertEqual(backend, "cdp")
        self.assertEqual(port, 9222)

    def test_cdp_endpoint_and_ready_check(self) -> None:
        self.assertEqual(jd_chrome.cdp_endpoint(9222), "http://127.0.0.1:9222")
        with mock.patch("urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = b"[]"
            self.assertTrue(jd_chrome.cdp_is_ready(9222))
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(jd_chrome.cdp_is_ready(9222))

    def test_find_chrome_windows_candidates(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(
                os.environ,
                {
                    "PROGRAMFILES": r"C:\Program Files",
                    "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
                    "LOCALAPPDATA": r"C:\Users\demo\AppData\Local",
                },
                clear=False,
            ):
                paths = jd_chrome.chrome_executable_candidates()
        joined = " | ".join(str(path) for path in paths)
        self.assertIn("chrome.exe", joined.lower())

    def test_default_user_data_dir_windows(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            with mock.patch.dict(
                os.environ,
                {"LOCALAPPDATA": r"C:\Users\demo\AppData\Local"},
                clear=False,
            ):
                path = jd_chrome.default_chrome_user_data_dir()
        self.assertIsNotNone(path)
        self.assertTrue(str(path).replace("\\", "/").endswith("Google/Chrome/User Data"))


if __name__ == "__main__":
    unittest.main()
