"""Offline contract tests: fake transport/browser and mocked storage operations."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

spec = importlib.util.spec_from_file_location("pinzi_auth", Path(__file__).resolve().parents[1] / "pinzi_auth.py")
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)
STATE = {"cookies": [{"name": "fixture", "value": "SECRET", "domain": ".yuewen.com", "path": "/"}], "origins": []}
MATERIAL = {"code": 0, "data": {"ywGuid": 123456789, "ywKey": "SECRET", "ticket": "SECRET"}}


class Response:
    status = 200
    def __init__(self, payload):
        self.payload = payload
        self.disposed = False
    async def json(self):
        return self.payload
    async def dispose(self):
        self.disposed = True


class HTTP:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.disposed = False
    async def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return Response(value)
    async def storage_state(self):
        return json.loads(json.dumps(STATE))
    async def dispose(self):
        self.disposed = True


class BrowserContext:
    def __init__(self):
        self.routes = []
        self.closed = False
    def set_default_timeout(self, value):
        pass
    async def route(self, pattern, callback):
        self.routes.append((pattern, callback))
    async def storage_state(self):
        return STATE
    async def close(self):
        self.closed = True


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = types.SimpleNamespace(name="isolated-fixture-data")
        self.notify = AsyncMock()
        self.manager = auth.AuthManager(self.directory.name, self.notify)
        self.manager._path = MagicMock()
        self.manager._path.exists.return_value = True
        self.manager._path.is_symlink.return_value = False
        self.manager._path.read_text.return_value = json.dumps(STATE)
        self.manager._path.read_bytes.return_value = json.dumps(STATE).encode()
        self.manager._path.parent.glob.return_value = []
        self.manager._persist = MagicMock()
        self.old = HTTP()
        self.manager._playwright = types.SimpleNamespace(stop=AsyncMock())
        self.manager._http = self.old
        self.manager._authenticated = True
        self.manager._persist(STATE)

    async def test_review_transient_renew_keeps_authenticated(self):
        self.manager._exchange = AsyncMock(side_effect=auth.AuthError("网络暂不可用"))
        self.candidate([])
        with self.assertRaises(auth.AuthError):
            await self.manager.renew()
        self.assertTrue((await self.manager.status())["authenticated"])

    def candidate(self, responses):
        context = HTTP(responses)
        self.manager._new_http = AsyncMock(return_value=context)
        return context

    def test_material_accepts_numeric_or_string_guid_but_not_bool(self):
        for guid in (123456789, "123456789"):
            self.assertTrue(auth.AuthManager._valid_material({"ywGuid":guid,"ywKey":"fixture","ticket":"fixture"}))
        for guid in (True, False, 0, -1, None, [], 1.5):
            self.assertFalse(auth.AuthManager._valid_material({"ywGuid":guid,"ywKey":"fixture","ticket":"fixture"}))

    async def test_first_run_no_state_is_not_authorized(self):
        self.manager._http = None
        self.manager._authenticated = False
        self.manager._path.exists.return_value = False
        with self.assertRaises(auth.AuthError) as caught:
            await self.manager.request("GET", auth.ROOM_PATH)
        self.assertIn("尚未授权", str(caught.exception))
        self.assertFalse((await self.manager.status())["authenticated"])

    async def test_corrupt_saved_state_allows_reauthorization(self):
        manager = auth.AuthManager(self.directory.name, self.notify)
        manager._path = self.manager._path
        manager._path.read_text.return_value = "invalid-json-fixture"
        factory = AsyncMock()
        runtime = types.SimpleNamespace(request=types.SimpleNamespace(new_context=factory), stop=AsyncMock())
        fake_module = types.ModuleType("playwright.async_api")
        fake_module.async_playwright = lambda: types.SimpleNamespace(start=AsyncMock(return_value=runtime))
        with patch.dict("sys.modules", {"playwright.async_api": fake_module}):
            await manager.start()
        self.assertFalse((await manager.status())["authenticated"])
        self.assertTrue((await manager.status())["started"])
        factory.assert_not_awaited()
        manager._path.unlink.assert_not_called()
        await manager.close()

    async def test_restore_saved_state_without_browser(self):
        manager = auth.AuthManager(self.directory.name, self.notify)
        manager._path = self.manager._path
        factory = AsyncMock(return_value=self.old)
        runtime = types.SimpleNamespace(request=types.SimpleNamespace(new_context=factory), stop=AsyncMock())
        fake_module = types.ModuleType("playwright.async_api")
        fake_module.async_playwright = lambda: types.SimpleNamespace(start=AsyncMock(return_value=runtime))
        with patch.dict("sys.modules", {"playwright.async_api": fake_module}):
            await manager.start()
        self.assertEqual(factory.call_args.kwargs["storage_state"], STATE)
        self.assertEqual(factory.call_args.kwargs["extra_http_headers"]["Content-Type"], "application/json")
        self.assertTrue((await manager.status())["authenticated"])
        await manager.close()
        self.assertTrue(self.old.disposed)

    async def test_renew_checks_auth_then_room_and_commits(self):
        candidate = self.candidate([MATERIAL, {"returnCode": 2000}, {"returnCode": 2000}])
        result = await self.manager.renew()
        self.assertTrue(result["renewed"])
        self.assertIs(self.manager._http, candidate)
        self.assertTrue(self.old.disposed)
        self.assertEqual(candidate.calls[0][1]["params"], auth.CHECK_PARAMS)
        self.assertEqual(candidate.calls[0][1]["headers"]["Referer"], "https://ptlogin.yuewen.com")
        self.assertEqual(candidate.calls[-1][0], auth.PCWRITE + auth.ROOM_PATH)
        self.assertEqual(json.loads(self.manager._path.read_text()), STATE)

    async def test_failed_business_validation_preserves_old_file_and_context(self):
        before = self.manager._path.read_bytes()
        candidate = self.candidate([MATERIAL, {"returnCode": 2000}, {"returnCode": 4001, "returnMsg": "SECRET"}])
        self.manager._persist.reset_mock()
        with self.assertRaises(auth.AuthError) as caught:
            await self.manager.renew()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.manager._path.read_bytes(), before)
        self.assertIs(self.manager._http, self.old)
        self.assertFalse(self.old.disposed)
        self.assertTrue(candidate.disposed)
        self.manager._persist.assert_not_called()

    async def test_atomic_replace_failure_leaves_old_and_removes_temporary(self):
        before = self.manager._path.read_bytes()
        self.candidate([MATERIAL, {"returnCode": 2000}, {"returnCode": 2000}])
        modes = []
        self.manager._persist = types.MethodType(auth.AuthManager._persist, self.manager)
        def chmod(path, mode):
            modes.append(mode)
        with patch.object(auth.tempfile, "mkstemp", return_value=(7,"fixture-temp")), patch.object(auth.os,"fdopen",mock_open()), patch.object(auth.os,"fsync"), patch.object(auth.os,"unlink"), patch.object(auth.os, "replace", side_effect=OSError("SECRET")), patch.object(auth.os, "chmod", side_effect=chmod):
            with self.assertRaises(auth.AuthError) as caught:
                await self.manager.renew()
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(modes, [0o600])
        self.assertEqual(self.manager._path.read_bytes(), before)
        self.assertEqual(list(self.manager._path.parent.glob(".auth-state-*")), [])

    async def test_storage_commit_only_after_room_success(self):
        candidate = self.candidate([MATERIAL, {"returnCode": 2000}, {"returnCode": 2000}])
        persist = self.manager._persist
        def commit(state):
            self.assertEqual(len(candidate.calls), 3)
            persist(state)
        with patch.object(self.manager, "_persist", side_effect=commit):
            await self.manager.renew()

    async def test_network_exception_has_no_secret_or_visible_chain(self):
        self.old.responses = [RuntimeError("SECRET Cookie: ywkey=value")]
        with self.assertRaises(auth.AuthError) as caught:
            await self.manager.request("GET", auth.ROOM_PATH)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)

    async def test_path_escape_and_redirect_rejected(self):
        for path in ("https://evil/ccauthorweb/a", "//evil/ccauthorweb/a", "/ccauthorweb/../login", "/ccauthorweb/%2e%2e/login", "/ccauthorweb/a?secret=1", "/ccauthorweb/%252e%252e/a"):
            with self.assertRaises(auth.AuthError):
                await self.manager.request("GET", path)
        response = Response({"returnCode": 2000})
        response.status = 302
        self.old.fetch = AsyncMock(return_value=response)
        with self.assertRaises(auth.AuthError):
            await self.manager.request("GET", auth.ROOM_PATH)
        self.assertEqual(self.old.fetch.call_args.kwargs["max_redirects"], 0)
        self.assertTrue(response.disposed)

    async def test_request_and_renew_serialized(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def fetch(url, **kwargs):
            entered.set()
            await release.wait()
            return Response({"returnCode": 2000})
        self.old.fetch = fetch
        self.candidate([MATERIAL, {"returnCode": 2000}, {"returnCode": 2000}])
        query = asyncio.create_task(self.manager.request("GET", auth.ROOM_PATH))
        await entered.wait()
        renewal = asyncio.create_task(self.manager.renew())
        await asyncio.sleep(0)
        self.manager._new_http.assert_not_called()
        release.set()
        await asyncio.gather(query, renewal)

    def browser(self):
        context = BrowserContext()
        browser = types.SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        self.manager._playwright.chromium = types.SimpleNamespace(launch=AsyncMock(return_value=browser))
        return browser, context

    async def test_login_new_context_and_success_commit(self):
        browser, context = self.browser()
        self.manager._scan = AsyncMock()
        candidate = self.candidate([{"returnCode": 2000}])
        result = await self.manager.login()
        self.assertTrue(result["login_completed"])
        browser.new_context.assert_awaited_once_with()
        self.manager._playwright.chromium.launch.assert_awaited_once_with(headless=True)
        self.assertEqual(len(context.routes), 3)
        self.assertTrue(context.closed)
        browser.close.assert_awaited_once()
        self.assertIs(self.manager._http, candidate)

    async def test_cancel_login_releases_browser_lock_and_preserves_old(self):
        browser, context = self.browser()
        entered = asyncio.Event()
        async def scan(_):
            entered.set()
            await asyncio.Event().wait()
        self.manager._scan = scan
        before = self.manager._path.read_bytes()
        task = asyncio.create_task(self.manager.login())
        await entered.wait()
        with self.assertRaises(auth.AuthError):
            await self.manager.login()
        await self.manager.cancel_login()
        self.assertTrue(task.cancelled())
        self.assertTrue(context.closed)
        browser.close.assert_awaited_once()
        self.assertFalse(self.manager._lock.locked())
        self.assertEqual(self.manager._path.read_bytes(), before)
        self.assertFalse((await self.manager.status())["login_in_progress"])

    async def test_cancel_renew_before_commit_keeps_old_file(self):
        entered = asyncio.Event()
        candidate = self.candidate([])
        async def exchange(_):
            entered.set()
            await asyncio.Event().wait()
        self.manager._exchange = exchange
        before = self.manager._path.read_bytes()
        task = asyncio.create_task(self.manager.renew())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.manager._path.read_bytes(), before)
        self.assertTrue(candidate.disposed)

    async def test_scan_sends_png_and_exchanges_real_protocol_shape(self):
        locator = types.SimpleNamespace(
            click=AsyncMock(), wait_for=AsyncMock(), count=AsyncMock(return_value=1),
            get_attribute=AsyncMock(return_value="loginArgeeAcount6"),
            is_checked=AsyncMock(return_value=True), is_visible=AsyncMock(return_value=True),
            screenshot=AsyncMock(return_value=auth.PNG_SIGNATURE + b"fixture"), evaluate=AsyncMock())
        locator.first = locator
        official = types.SimpleNamespace(wait_for_url=AsyncMock(), wait_for_function=AsyncMock())
        locator.element_handle = AsyncMock(return_value=types.SimpleNamespace(content_frame=AsyncMock(return_value=official)))
        frame = types.SimpleNamespace(locator=lambda selector: locator)
        frame.get_by_text = lambda *a, **kw: locator
        page = types.SimpleNamespace(route=AsyncMock(), goto=AsyncMock(), frames=[frame],
                                     locator=lambda selector: locator, frame_locator=lambda selector: frame)
        context = types.SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[page],
                                        request=HTTP([MATERIAL, {"returnCode": 2000}, {"returnCode": 2000}]))
        await self.manager._scan(context)
        self.notify.assert_awaited_once_with(auth.PNG_SIGNATURE + b"fixture")
        self.assertEqual(context.request.calls[0][1]["params"], auth.CHECK_PARAMS)
        self.assertEqual(len(context.request.calls), 3)
        self.notify.reset_mock()
        self.notify.side_effect = RuntimeError("SECRET recipient transport")
        with self.assertRaises(auth.AuthError) as caught:
            await self.manager._scan(context)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(len(context.request.calls), 3)
        self.assertEqual(self.notify.await_count, 1)

    async def test_login_timeout_cleans_up_and_preserves_old(self):
        _, context = self.browser()
        self.manager._timeout = 0.01
        async def scan(_):
            await asyncio.Event().wait()
        self.manager._scan = scan
        self.manager._persist.reset_mock()
        with self.assertRaises(auth.AuthError) as caught:
            await self.manager.login()
        self.assertIn("超时", str(caught.exception))
        self.assertTrue(context.closed)
        self.manager._persist.assert_not_called()

    async def test_atomic_success_orders_flush_before_replace(self):
        self.manager._persist = types.MethodType(auth.AuthManager._persist, self.manager)
        operations = []
        stream = mock_open()
        stream.return_value.flush.side_effect = lambda: operations.append("flush")
        with patch.object(auth.tempfile, "mkstemp", return_value=(7, "fixture-temp")), patch.object(
                auth.os, "fdopen", stream), patch.object(auth.os, "chmod") as chmod, patch.object(
                auth.os, "fsync", side_effect=lambda fd: operations.append("fsync")), patch.object(
                auth.os, "replace", side_effect=lambda *args: operations.append("replace")) as replace, patch.object(auth.os, "unlink"):
            self.manager._persist(STATE)
        self.assertEqual(operations, ["flush", "fsync", "replace"])
        chmod.assert_called_once_with("fixture-temp", 0o600)
        replace.assert_called_once_with("fixture-temp", self.manager._path)

    async def test_close_cancels_login_and_disposes_runtime(self):
        _, context = self.browser()
        entered = asyncio.Event()
        async def scan(_):
            entered.set()
            await asyncio.Event().wait()
        self.manager._scan = scan
        task = asyncio.create_task(self.manager.login())
        await entered.wait()
        runtime = self.manager._playwright
        await self.manager.close()
        self.assertTrue(task.cancelled())
        self.assertTrue(context.closed)
        runtime.stop.assert_awaited_once()
        with self.assertRaises(auth.AuthError):
            await self.manager.start()


if __name__ == "__main__":
    unittest.main()
