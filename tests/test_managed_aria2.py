"""Lifecycle and optional real local aria2 integration tests (no download jobs)."""

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_magnet_delivery.core.managed_aria2 import ManagedAria2
from astrbot_plugin_magnet_delivery.core.manager import identity

BINARY = os.environ.get("ARIA2_TEST_EXE", "")


class ManagedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.close()
        self.tmp.cleanup()

    def service(self, name="managed", binary=BINARY):
        service = ManagedAria2(self.root / name, binary, self.root / "downloads" / name)
        self.services.append(service)
        return service

    async def test_missing_binary_reports_error_without_launching(self):
        service = self.service(binary=str(self.root / "not-found.exe"))
        with self.assertRaisesRegex(ValueError, "未找到"):
            await service.start()
        self.assertIsNone(service.process)
        self.assertIn("未找到", await service.health())

    @unittest.skipUnless(BINARY, "Set ARIA2_TEST_EXE for real process tests")
    async def test_persistent_port_and_secret_are_not_in_process_arguments(self):
        service = self.service()
        version = await service.start()
        self.assertTrue(version)
        self.assertIn("运行正常", await service.health())
        first = dict(service.state)
        fingerprint = identity(service.profile)
        self.assertNotIn(first["secret"], " ".join(service.process.args))
        # The temporary instance has no active tasks and requires our token.
        self.assertEqual(await service.rpc("tellActive"), [])
        await service.close()
        self.assertIsNotNone(service.process.poll())
        second = self.service()
        await second.start()
        self.assertEqual(first, second.state)
        self.assertEqual(fingerprint, identity(second.profile))

    @unittest.skipUnless(BINARY, "Set ARIA2_TEST_EXE for real process tests")
    async def test_stopping_one_owned_instance_never_stops_another(self):
        first, other = self.service("first"), self.service("other")
        await first.start()
        await other.start()
        await first.close()
        self.assertIsNotNone(first.process.poll())
        self.assertIsNone(other.process.poll())
        self.assertIn("运行正常", await other.health())

    @unittest.skipUnless(BINARY, "Set ARIA2_TEST_EXE for real process tests")
    async def test_occupied_port_is_not_adopted_or_terminated(self):
        service = self.service()
        service._prepare()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", service.state["port"]))
            listener.listen()
            with self.assertRaisesRegex(ValueError, "被占用"):
                await service.start()
            self.assertIsNone(service.process)
            self.assertEqual(listener.getsockname()[1], service.state["port"])

    @unittest.skipUnless(BINARY, "Set ARIA2_TEST_EXE for real process tests")
    async def test_invalid_stored_state_is_not_overwritten(self):
        service = self.service()
        service.root.mkdir()
        original = {"port": 0, "secret": "bad"}
        (service.root / "connection.json").write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "损坏"):
            await service.start()
        self.assertEqual(json.loads((service.root / "connection.json").read_text()), original)


if __name__ == "__main__":
    unittest.main()
