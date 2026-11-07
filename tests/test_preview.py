"""Preview API behavior, quota protection and screenshot fallback."""

import unittest
from unittest.mock import patch

import httpx

from astrbot_plugin_magnet_delivery.core.preview import (
    PreviewService,
    public_image_url,
    render,
    resolve_proxy,
)

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
PAYLOAD = {
    "type": "magnet",
    "file_type": "video",
    "name": "Example video",
    "size": 1048576,
    "count": 2,
    "screenshots": [{"time": 5, "screenshot": "https://whatslink.info/example.jpg"}],
}


class PreviewTests(unittest.IsolatedAsyncioTestCase):
    def test_system_proxy_explicit_proxy_and_direct(self):
        with patch(
            "astrbot_plugin_magnet_delivery.core.preview.getproxies",
            return_value={"https": "http://127.0.0.1:7890"},
        ):
            self.assertEqual(resolve_proxy("system"), "http://127.0.0.1:7890")
        with (
            patch("astrbot_plugin_magnet_delivery.core.preview.getproxies", return_value={}),
            patch("astrbot_plugin_magnet_delivery.core.preview.registry_proxies", return_value={}),
        ):
            self.assertIsNone(resolve_proxy("system"))
        self.assertIsNone(resolve_proxy(""))
        self.assertEqual(resolve_proxy("http://127.0.0.1:8888"), "http://127.0.0.1:8888")
        with self.assertRaises(ValueError):
            resolve_proxy("file:///private")

    def test_astrbot_no_proxy_does_not_hide_windows_system_proxy(self):
        with (
            patch(
                "astrbot_plugin_magnet_delivery.core.preview.getproxies",
                return_value={"no": "localhost,127.0.0.1,::1"},
            ),
            patch(
                "astrbot_plugin_magnet_delivery.core.preview.registry_proxies",
                return_value={"http": "http://127.0.0.1:7890"},
            ),
        ):
            self.assertEqual(resolve_proxy("system"), "http://127.0.0.1:7890")

    def test_explicit_environment_proxy_keeps_priority_over_registry(self):
        with (
            patch(
                "astrbot_plugin_magnet_delivery.core.preview.getproxies",
                return_value={"https": "http://127.0.0.1:8888", "no": "localhost"},
            ),
            patch(
                "astrbot_plugin_magnet_delivery.core.preview.registry_proxies",
                return_value={"http": "http://127.0.0.1:7890"},
            ),
        ):
            self.assertEqual(resolve_proxy("system"), "http://127.0.0.1:8888")

    async def asyncSetUp(self):
        self.service = PreviewService()
        await self.service.http.aclose()

    async def asyncTearDown(self):
        await self.service.close()

    def mock(self, handler):
        self.service.http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        )

    async def test_cache_hash_only_and_source_attribution(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json=PAYLOAD)

        self.mock(respond)
        result = await self.service.lookup(
            MAGNET + "&tr=https://tracker.test/?passkey=secret&dn=private", "chatA"
        )
        cached = await self.service.lookup(MAGNET, "chatB")
        self.assertEqual(result, cached)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["url"], MAGNET)
        text = render(result)
        self.assertIn("信息来源：https://whatslink.info/", text)
        self.assertIn("1.0 MiB", text)
        self.assertIn("Example video", text)
        with self.assertRaisesRegex(ValueError, "频繁"):
            await self.service.lookup(MAGNET, "chatA")

    async def test_rate_limit_stops_further_api_calls(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(429)

        self.mock(respond)
        with self.assertRaisesRegex(ValueError, "额度"):
            await self.service.lookup(MAGNET, "chatA")
        with self.assertRaisesRegex(ValueError, "间隔"):
            await self.service.lookup(MAGNET, "chatB")
        self.assertEqual(len(requests), 1)

    async def test_error_empty_invalid_and_oversized_responses(self):
        for response in [
            httpx.Response(200, json={}),
            httpx.Response(200, content=b"not JSON"),
            httpx.Response(503),
            httpx.Response(200, content=b"x" * 262145),
        ]:
            with self.subTest(response=response):
                self.service.next_request = 0
                self.service.chats.clear()
                await self.service.http.aclose()
                self.mock(lambda request: response)
                with self.assertRaises(ValueError):
                    await self.service.lookup(MAGNET, "chatA")

    async def test_unsafe_and_missing_screenshots_fall_back_to_text(self):
        self.mock(
            lambda request: httpx.Response(
                200,
                json=dict(
                    PAYLOAD,
                    screenshots=[
                        {"screenshot": "file:///private"},
                        {"screenshot": "https://127.0.0.1/private"},
                        {"screenshot": "https://localhost/private"},
                        {"screenshot": "javascript:bad"},
                    ],
                ),
            )
        )
        result = await self.service.lookup(MAGNET, "chatA")
        self.assertEqual(result["screenshots"], [])
        self.assertIn("暂无可用截图", render(result))

    async def test_inline_only_allowlisted_small_image_without_redirect(self):
        calls = []

        def respond(request):
            calls.append(str(request.url))
            if request.url.path == "/redirect.jpg":
                return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xffexample"
            )

        self.mock(respond)
        self.assertIsNone(await self.service.image("https://other.example/image.jpg"))
        self.assertEqual(calls, [])
        self.assertIsNotNone(await self.service.image("https://whatslink.info/image.jpg"))
        self.assertIsNone(await self.service.image("https://whatslink.info/redirect.jpg"))
        self.assertEqual(len(calls), 2)

    async def test_negative_numbers_and_nonlist_screenshots_are_not_trusted(self):
        self.mock(
            lambda request: httpx.Response(
                200, json=dict(PAYLOAD, size=-1, count=True, screenshots="bad")
            )
        )
        result = await self.service.lookup(MAGNET, "chatA")
        self.assertIsNone(result["size"])
        self.assertIsNone(result["count"])
        self.assertIsNone(public_image_url("https://user:pass@example.com/a.jpg"))


if __name__ == "__main__":
    unittest.main()
