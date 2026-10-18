"""Isolated Yuewen authentication. The caller enforces administrator and consent checks."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode, urlsplit, unquote

PCWRITE = "https://pcwrite.yuewen.com"
ROOM_PATH = "/ccauthorweb/desk/spelling/getCauthorHomeid"
LOGIN_PARAMS = dict(appId="34", areaId="4", returnUrl="https://write.qq.com",
                    target="callback", auto="1", autotime="60", ticket="1",
                    popup="1", popup_close="1", unionshow="00001")
CHECK_PARAMS = {k: v for k, v in LOGIN_PARAMS.items()
                if k not in ("popup", "popup_close", "unionshow")}
CHECK_PARAMS.update(jumpdm="yuewen", ajaxdm="yuewen", method="checkStatusCallback", format="json")
APP_HEADERS = {"Content-Type": "application/json", "Referer": PCWRITE, "ptfrom": "authorwrite", "version": "5.19.0", "isTrial": "-1"}
PNG_SIGNATURE = bytes((137, 80, 78, 71, 13, 10, 26, 10))


class AuthError(Exception):
    """User-safe failure; never constructed with a remote exception or response body."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code if isinstance(code, int) and not isinstance(code, bool) else None


async def _dispose(resource, method):
    if resource is not None:
        with suppress(Exception):
            await getattr(resource, method)()


class AuthManager:
    def __init__(self, data_dir, notify_qr, *, timeout_seconds=180, request_timeout=15):
        self._path = Path(data_dir) / "auth-state.json"
        self._notify_qr = notify_qr
        self._timeout = max(1.0, float(timeout_seconds))
        self._request_timeout = max(0.1, float(request_timeout)) * 1000
        self._lock = asyncio.Lock()
        self._playwright = self._http = self._login_task = None
        self._closed = self._authenticated = False

    async def _ensure_started(self):
        if self._closed:
            raise AuthError("认证服务已关闭")
        if self._playwright is not None:
            return
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            if self._path.exists():
                if self._path.is_symlink():
                    raise ValueError("unsafe state")
                try:
                    saved = json.loads(self._path.read_text(encoding="utf-8"))
                    self._validate_state(saved)
                except (ValueError, TypeError):
                    # Keep invalid bytes for diagnosis; allow private reauthorization to replace them.
                    saved = None
                if saved is not None:
                    self._http = await self._new_http(saved)
                    self._authenticated = True
        except asyncio.CancelledError:
            await _dispose(self._http, "dispose")
            await _dispose(self._playwright, "stop")
            self._http = self._playwright = None
            raise
        except Exception:
            await _dispose(self._http, "dispose")
            await _dispose(self._playwright, "stop")
            self._http = self._playwright = None
            raise AuthError("认证服务启动失败，请检查浏览器依赖或重新授权") from None

    async def start(self):
        async with self._lock:
            await self._ensure_started()

    async def close(self):
        self._closed = True
        await self.cancel_login()
        async with self._lock:
            await _dispose(self._http, "dispose")
            await _dispose(self._playwright, "stop")
            self._http = self._playwright = None
            self._authenticated = False

    async def cancel_login(self):
        task = self._login_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def status(self):
        return {"started": self._playwright is not None, "closed": self._closed,
                "authenticated": self._authenticated, "login_in_progress": self._login_task is not None}

    async def _new_http(self, storage):
        return await self._playwright.request.new_context(
            storage_state=storage, extra_http_headers=APP_HEADERS, timeout=self._request_timeout)

    @staticmethod
    def _validate_state(state):
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
            raise ValueError("invalid storage state")

    def _persist(self, state):
        """Atomic POSIX 0600 commit. Windows additionally requires a private data-dir ACL."""
        self._validate_state(state)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".auth-state-", dir=self._path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    async def _json(self, context, method, url, *, params=None, data=None, headers=None):
        response = None
        try:
            response = await context.fetch(url, method=method, params=params, data=data,
                                           headers=headers, timeout=self._request_timeout, max_redirects=0)
            if response.status < 200 or response.status >= 300:
                raise AuthError("阅文服务暂时不可用，请稍后重试")
            result = await response.json()
            if not isinstance(result, dict):
                raise AuthError("阅文服务返回无效数据")
            return result
        finally:
            await _dispose(response, "dispose")

    @staticmethod
    def _code(result):
        value = result.get("returnCode", result.get("code"))
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    async def _room(self, context):
        result = await self._json(context, "GET", PCWRITE + ROOM_PATH, headers=APP_HEADERS)
        if self._code(result) != 2000:
            raise AuthError("授权验证失败，请重新授权", self._code(result))

    @staticmethod
    def _valid_material(material):
        if not isinstance(material, dict):
            return False
        guid = material.get("ywGuid")
        valid_guid = (isinstance(guid, str) and bool(guid)) or (type(guid) is int and guid > 0)
        return valid_guid and all(isinstance(material.get(k), str) and bool(material[k]) for k in ("ywKey", "ticket"))

    async def _exchange(self, context):
        result = await self._json(context, "GET", "https://ptlogin.yuewen.com/login/checkstatus",
                                  params=CHECK_PARAMS, headers={"Referer": "https://ptlogin.yuewen.com"})
        material = result.get("data") or {}
        if result.get("code") != 0 or not self._valid_material(material):
            raise AuthError("授权已失效，请重新扫码授权", self._code(result))
        await self._auth(context, material)

    async def _auth(self, context, material):
        result = await self._json(context, "POST", PCWRITE + "/ccauthorweb/pc/login/auth",
                                  data=json.dumps({k: material[k] for k in ("ywGuid", "ywKey", "ticket")}),
                                  headers={**APP_HEADERS, "Content-Type": "application/json",
                                           "Origin": PCWRITE, "timestamp": str(int(time.time() * 1000))})
        if self._code(result) != 2000:
            raise AuthError("登录会话交换失败，请重新授权", self._code(result))
        await self._room(context)

    async def _commit_context(self, candidate):
        state = await candidate.storage_state()
        self._persist(state)
        # Commit and swap have no cancellation point between them.
        previous = self._http
        self._http = candidate
        self._authenticated = True
        await _dispose(previous, "dispose")

    async def renew(self):
        candidate = None
        async with self._lock:
            try:
                await self._ensure_started()
                if self._http is None:
                    raise AuthError("尚未授权，请先私聊管理员扫码授权", 4001)
                candidate = await self._new_http(await self._http.storage_state())
                await self._exchange(candidate)
                await self._commit_context(candidate)
                candidate = None
                return {"authenticated": True, "renewed": True}
            except AuthError as exc:
                if exc.code in (4001, -31008):
                    self._authenticated = False
                raise
            except Exception:
                raise AuthError("授权续期失败，请稍后重试或重新授权") from None
            finally:
                if candidate is not self._http:
                    await _dispose(candidate, "dispose")

    @staticmethod
    def _check_path(path):
        if not isinstance(path, str) or not path.startswith("/ccauthorweb/"):
            raise AuthError("不允许的房间接口路径")
        parsed = urlsplit(path)
        decoded = unquote(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or "\\" in decoded or any(
                segment in (".", "..") for segment in decoded.split("/")) or "%" in decoded or any(ord(c) < 32 for c in decoded):
            raise AuthError("不允许的房间接口路径")

    async def request(self, method, path, *, params=None, data=None):
        self._check_path(path)
        if not isinstance(method, str) or method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            raise AuthError("不允许的请求方法")
        async with self._lock:
            try:
                await self._ensure_started()
                if self._http is None:
                    raise AuthError("尚未授权，请先私聊管理员扫码授权", 4001)
                result = await self._json(self._http, method.upper(), PCWRITE + path,
                                          params=params, data=data, headers=APP_HEADERS)
                if self._code(result) == 4001:
                    self._authenticated = False
                    raise AuthError("授权已失效，请重新扫码授权", 4001)
                return result
            except AuthError:
                raise
            except Exception:
                raise AuthError("房间请求失败，请稍后重试") from None

    async def login(self):
        if self._login_task is not None:
            raise AuthError("已有扫码授权正在进行，请完成或取消后重试")
        self._login_task = asyncio.current_task()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._lock:
                    await self._ensure_started()
                    return await self._login_locked()
        except TimeoutError:
            raise AuthError("扫码授权已超时，请重新发起授权") from None
        except AuthError:
            raise
        except Exception:
            raise AuthError("扫码授权失败，请检查浏览器依赖或重新尝试") from None
        finally:
            self._login_task = None

    async def _login_locked(self):
        browser = context = candidate = None
        try:
            browser = await self._playwright.chromium.launch(headless=True)
            context = await browser.new_context()
            context.set_default_timeout(self._request_timeout)
            async def application_headers(route):
                target = urlsplit(route.request.url)
                headers = await route.request.all_headers()
                headers["referer"] = f"{target.scheme}://{target.netloc}"
                await route.continue_(headers=headers)
            for host in ("ptlogin.yuewen.com", "oaptlogin.yuewen.com", "pcwrite.yuewen.com"):
                await context.route(f"https://{host}/**", application_headers)
            await self._scan(context)
            candidate = await self._new_http(await context.storage_state())
            await self._room(candidate)
            await self._commit_context(candidate)
            candidate = None
            return {"authenticated": True, "login_completed": True}
        finally:
            if candidate is not self._http:
                await _dispose(candidate, "dispose")
            await _dispose(context, "close")
            await _dispose(browser, "close")

    async def _scan(self, context):
        page = await context.new_page()
        shell = PCWRITE + "/__pinzi_isolated_login__"
        await page.route(shell, lambda route: route.fulfill(status=200, content_type="text/html",
                         body='<html><iframe id="loginIframe" width="900" height="800"></iframe></html>'))
        await page.goto(shell, wait_until="domcontentloaded")
        await page.locator("#loginIframe").evaluate("(el,url)=>el.src=url",
                    "https://passport.yuewen.com/yuewen.html?" + urlencode(LOGIN_PARAMS))
        handle = await page.locator("#loginIframe").element_handle()
        official = await handle.content_frame()
        await official.wait_for_url("https://passport.yuewen.com/**", wait_until="domcontentloaded")
        await official.wait_for_function("typeof window.jQuery === 'function' && document.querySelector('.qq-login')")
        login = page.frame_locator("#loginIframe")
        await login.get_by_text("QQ登录", exact=True).click()
        await login.locator(".qq-login").wait_for(state="visible")
        checkbox = login.locator(".qq-login input[type=checkbox]")
        if await checkbox.count() != 1:
            raise AuthError("官方登录页面已变化，请稍后重试")
        identifier = await checkbox.get_attribute("id")
        if not identifier or not identifier.replace("_", "").isalnum():
            raise AuthError("官方登录页面已变化，请稍后重试")
        if not await checkbox.is_checked():
            await login.locator(f'.qq-login label[for="{identifier}"]').click()
        if not await checkbox.is_checked():
            raise AuthError("无法确认登录协议，请重新尝试")
        await login.get_by_text("前去登录QQ", exact=True).click()
        notified = False
        while True:
            png = None
            for frame in [f for pg in context.pages for f in pg.frames]:
                if notified:
                    break
                try:
                    tab = frame.locator("#switcher_qlogin")
                    if await tab.count() and await tab.is_visible():
                        await tab.click(timeout=1500)
                    image = frame.locator("#qrlogin_img, #qrcode img, .qrlogin_img").first
                    if await image.count() and await image.is_visible():
                        png = await image.screenshot(type="png", timeout=2000)
                        if not png.startswith(PNG_SIGNATURE):
                            raise AuthError("二维码生成失败")
                        break
                except AuthError:
                    raise
                except Exception:
                    # Frames can detach during the official redirect.
                    continue
            if png is not None:
                try:
                    await self._notify_qr(png)
                except Exception:
                    raise AuthError("二维码发送失败，请检查私聊发图能力") from None
                notified = True
            if notified:
                result = await self._json(context.request, "GET", "https://ptlogin.yuewen.com/login/checkstatus",
                                          params=CHECK_PARAMS, headers={"Referer": "https://ptlogin.yuewen.com"})
                material = result.get("data") or {}
                if result.get("code") == 0 and self._valid_material(material):
                    await self._auth(context.request, material)
                    return
            await asyncio.sleep(1)
