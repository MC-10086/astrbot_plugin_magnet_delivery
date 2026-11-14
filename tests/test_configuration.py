"""Form defaults, legacy JSON migration and non-destructive merge rules."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_magnet_delivery.core.configuration import (
    configured_profiles,
    download_directory,
    load_profiles,
    migrate_preview_config,
)
from astrbot_plugin_magnet_delivery.core.manager import identity


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_do_not_enable_unconfigured_services_or_create_directories(self):
        self.assertEqual(configured_profiles({}, self.root), {})
        self.assertFalse(self.root.exists())

    def test_forms_build_both_services_and_share_one_default_directory(self):
        profiles = configured_profiles(
            {
                "qb": {"enabled": True, "password": "test-only"},
                "aria2": {"enabled": True, "secret": "test-only"},
            },
            self.root,
        )
        self.assertEqual(profiles["qb"]["url"], "http://127.0.0.1:8080")
        self.assertEqual(profiles["aria2"]["url"], "http://127.0.0.1:6800/jsonrpc")
        self.assertEqual(profiles["qb"]["username"], "admin")
        for profile in profiles.values():
            self.assertEqual(Path(profile["local_root"]), self.root / "downloads")
            self.assertTrue(Path(profile["local_root"]).is_dir())
        self.assertEqual(download_directory({}, self.root, "aria2_auto"), self.root / "downloads")

    def test_shared_custom_directory_and_one_optional_override(self):
        common, independent = Path(self.tmp.name) / "shared", Path(self.tmp.name) / "independent"
        config = {
            "download_paths": {"directory": str(common)},
            "qb": {"enabled": True},
            "aria2": {"enabled": True, "download_dir": str(independent)},
        }
        profiles = configured_profiles(config, self.root)
        self.assertEqual(Path(profiles["qb"]["local_root"]), common)
        self.assertEqual(Path(profiles["aria2"]["local_root"]), independent)
        self.assertEqual(download_directory(config, self.root, "aria2_auto"), common)
        config["aria2_auto"] = {"download_dir": str(independent)}
        self.assertEqual(download_directory(config, self.root, "aria2_auto"), independent)

    def test_advanced_json_uses_shared_default_but_retains_explicit_mapping(self):
        entries = [
            dict(name="remote", kind="qb", url="http://remote:8080", remote_root="/downloads"),
            dict(
                name="custom",
                kind="aria2",
                url="http://remote:6800/jsonrpc",
                local_root=str(self.root / "custom"),
            ),
        ]
        profiles = configured_profiles({"backends_json": json.dumps(entries)}, self.root)
        self.assertEqual(Path(profiles["remote"]["local_root"]), self.root / "downloads")
        self.assertEqual(profiles["remote"]["remote_root"], "/downloads")
        self.assertEqual(Path(profiles["custom"]["local_root"]), self.root / "custom")

    def test_invalid_shared_directory_does_not_create_partial_directories(self):
        for paths in ({"directory": "relative"}, "wrong-type"):
            with self.assertRaises(ValueError):
                configured_profiles({"download_paths": paths, "qb": {"enabled": True}}, self.root)
        self.assertFalse(self.root.exists())

    def test_port_and_directory_overrides(self):
        custom = Path(self.tmp.name) / "on-another-disk"
        profiles = configured_profiles(
            {"aria2": {"enabled": True, "port": 16800, "download_dir": str(custom)}}, self.root
        )
        self.assertEqual(profiles["aria2"]["url"], "http://127.0.0.1:16800/jsonrpc")
        self.assertEqual(profiles["aria2"]["local_root"], str(custom))

    def test_legacy_json_retains_name_credentials_paths_and_task_identity(self):
        legacy = json.dumps(
            [
                {
                    "name": "qb",
                    "kind": "qb",
                    "url": "http://remote:9090",
                    "local_root": str(self.root / "old"),
                    "password": "test-only",
                }
            ]
        )
        before = load_profiles(legacy)
        after = configured_profiles({"backends_json": legacy, "qb": {"enabled": False}}, self.root)
        self.assertEqual(before, after)
        self.assertEqual(identity(before["qb"]), identity(after["qb"]))

    def test_custom_json_and_builtins_can_coexist_but_never_silently_override(self):
        custom = {
            "name": "nas",
            "kind": "qb",
            "url": "http://nas:8080",
            "local_root": str(self.root / "nas"),
        }
        config = {"qb": {"enabled": True}, "backends_json": json.dumps([custom])}
        self.assertEqual(set(configured_profiles(config, self.root)), {"qb", "nas"})
        config["backends_json"] = json.dumps([dict(custom, name="qb")])
        with self.assertRaisesRegex(ValueError, "重复"):
            configured_profiles(config, self.root)

    def test_invalid_ports_and_paths_fail_before_creating_directories(self):
        for port in (-1, 0, 65536, True, "http://secret", 12.5):
            with self.subTest(port=port), self.assertRaises(ValueError):
                configured_profiles({"qb": {"enabled": True, "port": port}}, self.root)
        with self.assertRaises(ValueError):
            configured_profiles({"qb": {"enabled": True, "download_dir": "relative"}}, self.root)
        self.assertFalse(self.root.exists())

    def test_bad_json_does_not_echo_password_and_empty_input_is_optional(self):
        with self.assertRaises(ValueError) as raised:
            load_profiles('[{"password":"PRIVATE_TEST_VALUE",')
        self.assertNotIn("PRIVATE_TEST_VALUE", str(raised.exception))
        self.assertEqual(configured_profiles({"backends_json": "  "}, self.root), {})

    def test_invalid_advanced_urls_produce_a_safe_configuration_error(self):
        for url in (None, 8080, "http://:PRIVATE_VALUE@localhost", "http://localhost:PRIVATE_VALUE",
                    "http://localhost:0", "http://localhost:65536", "http://[broken"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "有效 HTTP") as error:
                load_profiles(json.dumps([dict(name="nas", kind="qb", url=url)]), self.root)
            self.assertNotIn("PRIVATE_VALUE", str(error.exception))

    @unittest.skipUnless(os.environ.get("ASTRBOT_APP_PATH"), "Needs installed AstrBot")
    def test_real_astrbot_schema_upgrades_legacy_without_changing_existing_fields(self):
        sys.path.insert(0, os.environ["ASTRBOT_APP_PATH"])
        from astrbot.api import AstrBotConfig
        from astrbot.dashboard.services.config_service import validate_config

        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        path = Path(self.tmp.name) / "plugin_config.json"
        original = {"backends_json": "[]", "default_backend": "custom", "auto_download": True}
        legacy_preview = {
            "preview_enabled": False,
            "auto_preview": True,
            "preview_inline_images": False,
            "preview_merge_forward": True,
            "preview_proxy": "",
            "preview_image_hosts": [],
            "preview_cooldown": 42,
            "preview_cache_seconds": 600,
        }
        path.write_text(json.dumps({**original, **legacy_preview}), encoding="utf-8")
        config = AstrBotConfig(str(path), schema=schema)
        self.assertTrue(migrate_preview_config(config))
        config.save_config()
        config = AstrBotConfig(str(path), schema=schema)
        self.assertEqual(config["preview"], {**legacy_preview, "legacy_migrated": True})
        self.assertEqual(config["preview_proxy"], "system")
        self.assertFalse(migrate_preview_config(config))
        config["preview"]["preview_enabled"] = True
        errors, payload = validate_config(dict(config), schema, is_core=False)
        self.assertEqual(errors, [])
        config.save_config(payload)
        config = AstrBotConfig(str(path), schema=schema)
        self.assertFalse(migrate_preview_config(config))
        self.assertTrue(config["preview"]["preview_enabled"])
        fields = list(schema)
        self.assertEqual(
            fields[fields.index("preview"):fields.index("scan_forwarded_magnets") + 1],
            ["preview", "auto_download", "scan_forwarded_magnets"],
        )
        visible = [key for key, field in schema["preview"]["items"].items() if not field.get("invisible")]
        self.assertEqual(visible, list(legacy_preview))
        self.assertFalse(config["qb"]["enabled"])
        self.assertEqual(config["qb"]["port"], 8080)
        self.assertEqual(config["aria2"]["port"], 6800)
        for key, value in original.items():
            self.assertEqual(config[key], value)
        self.assertEqual(configured_profiles(config, self.root), {})
        self.assertTrue(schema["qb"]["items"]["password"]["secret"])
        self.assertTrue(schema["aria2"]["items"]["secret"]["secret"])
        self.assertEqual(config["download_paths"]["directory"], "")
        self.assertTrue(config["delivery"]["delete_after_send"])
        self.assertEqual(
            schema["delivery"]["items"]["delete_after_send"]["condition"], {"auto_send": True}
        )


if __name__ == "__main__":
    unittest.main()
