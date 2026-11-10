"""Preview API behavior, quota protection and screenshot fallback."""

import unittest
from unittest.mock import patch

import httpx

from astrbot_plugin_magnet_delivery.core.preview import (
    ENDPOINT,
    PreviewService,
    _windows_route,
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
    def test_explicit_proxy_and_direct(self):
        self.assertIsNone(resolve_proxy(""))
        self.assertEqual(resolve_proxy("http://127.0.0.1:8888"), "http://127.0.0.1:8888")
        with self.assertRaises(ValueError):
            resolve_proxy("file:///private")

    def test_windows_route_uses_https_endpoint_and_honors_bypass(self):
        with patch(
            "astrbot_plugin_magnet_delivery.core.preview.windows_proxy_rules",
            return_value=("http=127.0.0.1:8888 https=127.0.0.1:7897", "*.example.test;<local>"),
        ):
            self.assertEqual(_windows_route(ENDPOINT), "http://127.0.0.1:7897")
            self.assertEqual(_windows_route("https://cdn.example.test/image/1"), "")

    async def test_hash_only_placeholder_is_not_a_preview(self):
        placeholder = {"name": "a" * 40, "file_type": "", "size": 0, "count": 1, "screenshots": None}
        self.mock(lambda request: httpx.Response(200, json=placeholder))
        with self.assertRaisesRegex(ValueError, "暂未收录"):
            await self.service.lookup(MAGNET, "group")

    async def test_system_route_is_resolved_for_each_request_and_never_falls_back(self):
        service = PreviewService(proxy="system")
        screenshot = PAYLOAD["screenshots"][0]["screenshot"]
        routes = []

        def client(proxy):
            routes.append(proxy)

            def respond(request):
                if proxy == "http://127.0.0.1:9090":
                    raise httpx.ConnectError("unreachable", request=request)
                if str(request.url) == screenshot:
                    return httpx.Response(
                        200, headers={"content-type": "image/jpeg"},
                        content=bytes.fromhex("ffd8ff") + b"test",
                    )
                return httpx.Response(200, json=PAYLOAD)

            return httpx.AsyncClient(transport=httpx.MockTransport(respond))

        with (
            patch("astrbot_plugin_magnet_delivery.core.preview.resolve_proxy",
                  side_effect=["http://127.0.0.1:7897", None, "http://127.0.0.1:9090"]) as resolve,
            patch.object(service, "_client", side_effect=client),
        ):
            await service.lookup(MAGNET, "group")
            self.assertIsNotNone(await service.image(screenshot))
            self.assertIsNone(await service.image(screenshot))
        self.assertEqual(
            [entry.args[1] for entry in resolve.call_args_list], [ENDPOINT, screenshot, screenshot]
        )
        self.assertEqual(routes, ["http://127.0.0.1:7897", None, "http://127.0.0.1:9090"])
        await service.close()

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
        async with self.service.lock:
            cached = await self.service.lookup(MAGNET, "chatB")
            other = "magnet:?xt=urn:btih:" + "b" * 40
            with self.assertRaisesRegex(ValueError, "服务忙"):
                await self.service.lookup(other, "chatC")
            self.assertNotIn("chatC", self.service.chats)
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
        with self.assertRaisesRegex(ValueError, "间隔"):
            await self.service.lookup(MAGNET, "chatB")
        self.assertEqual(len(requests), 1)

    async def test_lookup_retries_connect_error_once_on_the_same_proxy(self):
        service = PreviewService(proxy="system")
        requests = []

        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(200, json=PAYLOAD)

        proxy = "http://127.0.0.1:7897"
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with (
            patch("astrbot_plugin_magnet_delivery.core.preview.resolve_proxy",
                  return_value=proxy) as resolve,
            patch.object(service, "_client", return_value=client) as create_client,
        ):
            result = await service.lookup(MAGNET + "&dn=Example", "group")
        self.assertEqual(result["name"], PAYLOAD["name"])
        self.assertEqual([request.url.params["url"] for request in requests], [MAGNET, MAGNET])
        resolve.assert_called_once_with("system", ENDPOINT)
        create_client.assert_called_once_with(proxy)

    async def test_batch_previews_two_links_with_one_chat_cooldown_and_global_spacing(self):
        import time

        second = "magnet:?xt=urn:btih:" + "b" * 40
        requests = []

        def respond(request):
            requests.append((request.url.params["url"], time.monotonic()))
            return httpx.Response(200, json=PAYLOAD)

        self.mock(respond)
        results = await self.service.lookup_batch([MAGNET, second], "chatA")
        self.assertEqual([result["hash"] for result in results], ["a" * 40, "b" * 40])
        self.assertEqual([link for link, _ in requests], [MAGNET, second])
        self.assertGreaterEqual(requests[1][1] - requests[0][1], 2.99)
        with self.assertRaisesRegex(ValueError, "频繁"):
            await self.service.lookup(MAGNET, "chatA")

    async def test_batch_stops_network_requests_after_rate_limit(self):
        second = "magnet:?xt=urn:btih:" + "b" * 40
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(429)

        self.mock(respond)
        self.assertEqual(await self.service.lookup_batch([MAGNET, second], "chatA"), [])
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
