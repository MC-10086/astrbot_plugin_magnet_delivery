"""Directory-only browsing and narrowly-scoped config changes."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_magnet_delivery.core.path_picker import PAGE_SIZE, PathPicker, local_path


class Config(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


class PathPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(
            qb={"password": "test-private", "enabled": True, "port": 8080},
            aria2_auto={"enabled": False},
            backends_json="[]",
        )
        self.picker = PathPicker(self.config, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_browse_filters_files_and_lists_program_only_when_requested(self):
        (self.root / "folder").mkdir()
        (self.root / "secret.txt").write_text("not returned")
        (self.root / "aria2c.exe").touch()
        result = self.picker.browse(str(self.root))
        self.assertEqual([d["name"] for d in result["directories"]], ["folder"])
        self.assertEqual(result["files"], [])
        self.assertEqual(
            [f["name"] for f in self.picker.browse(str(self.root), True)["files"]], ["aria2c.exe"]
        )

    def test_reject_network_relative_and_invalid_paths(self):
        for value in ("relative", "//server/share", "\\\\server\\share", "\x00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_path(value)

    def test_all_directory_pages_include_dot_folders(self):
        names = [".downloads", *(f"folder-{i:03}" for i in range(PAGE_SIZE + 1))]
        for name in names:
            (self.root / name).mkdir()
        first = self.picker.browse(str(self.root))
        last = self.picker.browse(str(self.root), page=2)
        self.assertEqual((first["pages"], last["page"], last["total"]), (2, 2, len(names)))
        self.assertEqual(
            [item["name"] for item in first["directories"] + last["directories"]], names
        )

    @unittest.skipUnless(os.name == "nt", "Windows directory junctions")
    def test_windows_junction_is_labelled_without_following_it(self):
        import _winapi

        target, junction = self.root / "target", self.root / "junction"
        target.mkdir()
        _winapi.CreateJunction(str(target), str(junction))
        try:
            result = self.picker.browse(f'"{self.root}"')
            self.assertEqual(
                result["directories"][0], dict(name="junction", path=str(junction), linked=True)
            )
            with self.assertRaisesRegex(ValueError, "目录联接"):
                self.picker.browse(str(junction))
            with self.assertRaisesRegex(ValueError, "目录联接"):
                self.picker.save({"qb.download_dir": str(junction)})
        finally:
            junction.rmdir()

    def test_save_changes_paths_only_and_keeps_credentials(self):
        result = self.picker.save(
            {"qb.download_dir": str(self.root), "aria2_auto.download_dir": ""}
        )
        self.assertTrue(result["saved"])
        self.assertEqual(self.config["qb"]["password"], "test-private")
        self.assertTrue(self.config["qb"]["enabled"])
        self.assertEqual(self.config["qb"]["port"], 8080)
        self.assertEqual(self.config.saved, 1)
        serialized = json.dumps(self.picker.settings())
        self.assertNotIn("test-private", serialized)

    def test_invalid_selection_does_not_partially_save(self):
        with self.assertRaises(ValueError):
            self.picker.save(
                {
                    "qb.download_dir": str(self.root),
                    "aria2_auto.executable": str(self.root / "other.exe"),
                }
            )
        self.assertNotIn("download_dir", self.config["qb"])
        self.assertEqual(self.config.saved, 0)
        with self.assertRaises(ValueError):
            self.picker.save({"qb.password": "replacement"})
        self.assertEqual(self.config["qb"]["password"], "test-private")

    def test_persistence_failure_restores_previous_values(self):
        self.config.save_config = lambda: (_ for _ in ()).throw(OSError())
        with self.assertRaises(ValueError):
            self.picker.save({"qb.download_dir": str(self.root)})
        self.assertNotIn("download_dir", self.config["qb"])

    def test_shared_directory_save_and_return_to_shared_preserves_credentials(self):
        self.config["qb"]["download_dir"] = str(self.root)
        self.picker.save(
            {
                "download_paths.directory": str(self.root),
                "qb.download_dir": "",
                "aria2.download_dir": "",
                "aria2_auto.download_dir": "",
            }
        )
        self.assertEqual(self.config["download_paths"]["directory"], str(self.root))
        self.assertEqual(self.config["qb"]["download_dir"], "")
        self.assertEqual(self.config["qb"]["password"], "test-private")
        self.assertTrue(self.config["qb"]["enabled"])
        self.assertEqual(self.picker.settings()["shared_directory"], str(self.root))

    def test_failed_shared_save_rolls_back_all_groups(self):
        self.config["download_paths"] = {"directory": ""}
        self.config["qb"]["download_dir"] = str(self.root)
        self.config.save_config = lambda: (_ for _ in ()).throw(OSError())
        with self.assertRaises(ValueError):
            self.picker.save({"download_paths.directory": str(self.root), "qb.download_dir": ""})
        self.assertEqual(self.config["download_paths"]["directory"], "")
        self.assertEqual(self.config["qb"]["download_dir"], str(self.root))

    def test_linked_directories_are_not_browsed(self):
        target, link = self.root / "target", self.root / "link"
        target.mkdir()
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic link creation unavailable")
        listed = {d["name"]: d for d in self.picker.browse(str(self.root))["directories"]}
        if os.name == "nt":
            self.assertTrue(listed["link"]["linked"])
        else:
            self.assertNotIn("link", listed)
        with self.assertRaises(ValueError):
            self.picker.browse(str(link))


@unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs real AstrBot")
class ViewIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_framework_discovers_and_serves_new_view(self):
        sys.path.insert(0, os.environ["ASTRBOT_APP_PATH"])
        from astrbot.core.star.star import StarMetadata
        from astrbot.dashboard.services.plugin_page_service import PluginPageService

        root = Path(__file__).resolve().parents[1]
        service = PluginPageService(SimpleNamespace(plugin_store_path=str(root.parent)))
        pages = await service.discover_plugin_pages(StarMetadata(root_dir_name=root.name))
        self.assertIn("paths", [page.name for page in pages])
        self.assertIn("tasks", [page.name for page in pages])
        self.assertTrue((root / "views/paths/app.js").is_file())

    async def test_api_requires_dashboard_login_and_save_rejects_unrelated_fields(self):
        from astrbot_plugin_magnet_delivery.main import MagnetDelivery

        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(MagnetDelivery)
            plugin.path_picker = PathPicker(Config(), Path(tmp))
            with patch(
                "astrbot_plugin_magnet_delivery.main.request", SimpleNamespace(username=None)
            ):
                self.assertEqual((await plugin.paths_get()).status_code, 401)
                self.assertEqual((await plugin.paths_browse()).status_code, 401)
                self.assertEqual((await plugin.paths_save()).status_code, 401)
            req = SimpleNamespace(
                username="admin", json=AsyncMock(return_value={"paths": {"qb.password": "bad"}})
            )
            with patch("astrbot_plugin_magnet_delivery.main.request", req):
                response = await plugin.paths_save()
                self.assertEqual(response.status_code, 400)
