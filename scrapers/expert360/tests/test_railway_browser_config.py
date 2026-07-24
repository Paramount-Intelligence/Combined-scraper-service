"""Tests for Expert360 Railway-safe browser configuration."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scrapers.expert360 import expert360_monitor as e360
from run_all import summarize_scraper_failure


class TestRailwayDetection(unittest.TestCase):
    def test_railway_forces_headless_and_temp_profile(self):
        env = {"RAILWAY_ENVIRONMENT": "production"}
        self.assertTrue(e360.detect_is_railway(env))
        self.assertTrue(e360.resolve_expert360_headless(True, env))
        self.assertFalse(e360.resolve_use_persistent_profile(True, env))

    def test_local_visible_persistent_defaults(self):
        env = {
            "EXPERT360_HEADLESS": "false",
            "EXPERT360_USE_PERSISTENT_PROFILE": "true",
        }
        self.assertFalse(e360.detect_is_railway(env))
        self.assertFalse(e360.resolve_expert360_headless(False, env))
        self.assertTrue(e360.resolve_use_persistent_profile(False, env))


class TestChromeOptions(unittest.TestCase):
    def test_railway_options_contain_container_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = e360.build_expert360_chrome_options(
                headless=True,
                user_data_dir=tmp,
            )
            args = list(options.arguments)
            self.assertIn("--headless=new", args)
            self.assertIn("--no-sandbox", args)
            self.assertIn("--disable-dev-shm-usage", args)
            self.assertIn("--window-size=1920,1080", args)
            self.assertIn(f"--user-data-dir={tmp}", args)
            # Exactly one user-data-dir
            self.assertEqual(
                sum(1 for a in args if a.startswith("--user-data-dir=")),
                1,
            )

    def test_no_windows_paths_in_options(self):
        # Use a POSIX-style path so the assertion checks option construction,
        # not the host OS temp directory.
        options = e360.build_expert360_chrome_options(
            headless=True,
            user_data_dir="/tmp/expert360-profile-test",
        )
        joined = " ".join(options.arguments)
        self.assertNotIn("C:\\", joined)
        self.assertNotIn("D:\\", joined)
        self.assertIn("--user-data-dir=/tmp/expert360-profile-test", joined)

    def test_source_has_no_hardcoded_windows_paths(self):
        source = Path(e360.__file__).read_text(encoding="utf-8")
        self.assertNotIn("C:\\Program Files", source)
        self.assertNotIn("D:\\", source)
        self.assertNotIn("winreg", source)
        self.assertNotIn("version_main=150", source)


class TestTemporaryProfile(unittest.TestCase):
    def test_railway_temp_profile_under_tmp_and_cleaned(self):
        with patch.object(e360, "USE_PERSISTENT_PROFILE", False), \
             patch.object(e360, "IS_RAILWAY", True):
            profile = e360.create_runtime_profile()
            self.assertTrue(os.path.isdir(profile))
            self.assertTrue(
                profile.startswith("/tmp")
                or "expert360-profile-" in Path(profile).name
                or "expert360-profile-" in profile
            )
            e360._cleanup_temp_profile(profile)
            self.assertFalse(os.path.isdir(profile))

    def test_local_persistent_profile_path(self):
        with patch.object(e360, "USE_PERSISTENT_PROFILE", True), \
             patch.object(e360, "IS_RAILWAY", False):
            profile = e360.create_runtime_profile()
            self.assertEqual(Path(profile), e360.LOCAL_PROFILE_DIR)
            self.assertTrue(Path(profile).exists())


class TestCleanupOnException(unittest.TestCase):
    def test_main_quits_driver_and_removes_temp_profile_on_error(self):
        driver = MagicMock()
        profile = tempfile.mkdtemp(prefix="expert360-profile-test-")
        try:
            with patch.object(e360, "USE_PERSISTENT_PROFILE", False), \
                 patch.object(e360, "IS_RAILWAY", True), \
                 patch.object(
                     e360,
                     "initialize_driver_with_retry",
                     return_value=(driver, profile),
                 ), \
                 patch.object(
                     e360,
                     "ensure_authenticated",
                     side_effect=RuntimeError("boom"),
                 ):
                with self.assertRaises(RuntimeError):
                    e360.main()
            driver.quit.assert_called()
            self.assertFalse(os.path.isdir(profile))
        finally:
            if os.path.isdir(profile):
                import shutil
                shutil.rmtree(profile, ignore_errors=True)


class TestStartupRetry(unittest.TestCase):
    def test_retry_uses_two_profiles_then_succeeds(self):
        profiles = []
        driver = MagicMock()

        def fake_create_profile():
            path = tempfile.mkdtemp(prefix="expert360-profile-retry-")
            profiles.append(path)
            return path

        create_calls = {"n": 0}

        def fake_create_driver(profile_dir):
            create_calls["n"] += 1
            if create_calls["n"] == 1:
                raise RuntimeError("Chrome failed to start")
            return driver

        with patch.object(e360, "MAX_BROWSER_START_ATTEMPTS", 2), \
             patch.object(e360, "USE_PERSISTENT_PROFILE", False), \
             patch.object(e360, "create_runtime_profile", side_effect=fake_create_profile), \
             patch.object(e360, "create_expert360_driver", side_effect=fake_create_driver), \
             patch.object(e360, "time") as mock_time:
            mock_time.sleep = MagicMock()
            result_driver, result_profile = e360.initialize_driver_with_retry()

        self.assertIs(result_driver, driver)
        self.assertEqual(len(profiles), 2)
        self.assertNotEqual(profiles[0], profiles[1])
        self.assertFalse(os.path.isdir(profiles[0]))  # first cleaned after failure
        self.assertEqual(result_profile, profiles[1])
        # Clean successful profile leftover from test
        e360._cleanup_temp_profile(result_profile)

    def test_both_attempts_fail_raises(self):
        with patch.object(e360, "MAX_BROWSER_START_ATTEMPTS", 2), \
             patch.object(e360, "USE_PERSISTENT_PROFILE", False), \
             patch.object(
                 e360,
                 "create_runtime_profile",
                 side_effect=lambda: tempfile.mkdtemp(prefix="expert360-fail-"),
             ), \
             patch.object(
                 e360,
                 "create_expert360_driver",
                 side_effect=RuntimeError("session not created"),
             ), \
             patch.object(e360, "time") as mock_time:
            mock_time.sleep = MagicMock()
            with self.assertRaises(RuntimeError):
                e360.initialize_driver_with_retry()


class TestCombinedRunnerSummary(unittest.TestCase):
    def test_summary_prefers_root_exception_over_native_frames(self):
        stdout = "\n".join([
            "68 <unknown>",
            "#3 0x55aabbccddee",
            "#4 0x55aabbccdfff",
            "❌ Expert360 WebDriver failure: session not created: Chrome failed to start",
            "DevToolsActivePort file doesn't exist",
        ])
        summary = summarize_scraper_failure("Expert360", stdout, "", 1)
        self.assertIn("Root error:", summary)
        self.assertIn("Expert360 WebDriver failure", summary)
        self.assertNotEqual(summary.strip().splitlines()[0], "68 <unknown>")


if __name__ == "__main__":
    unittest.main()
