#!/usr/bin/env python3
"""Drive the server's web page in a real Chrome (Playwright) against the fake printer.

    .venv/bin/python tools/web_smoke.py OUT_DIR

Checks: pairing a new browser end to end, the admin view (status, queue, devices),
confirming the plate from the browser, the live camera, and no JavaScript errors.
"""

import asyncio
import os
import ssl
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
os.environ["OPENBOT_NO_KEYCHAIN"] = "1"

from playwright.async_api import async_playwright  # noqa: E402

from fake_printer import FakePrinter  # noqa: E402
from openbot.backend import Role  # noqa: E402
from openbot.local_backend import LocalBackend  # noqa: E402
from openbot.printer import Printer  # noqa: E402
from openbot.printer.auth import FileCredentialStore  # noqa: E402
from openbot.printer.trust import TrustStore  # noqa: E402
from openbot.remote.client import RemoteBackend  # noqa: E402
from openbot.server.app import OpenBotServer  # noqa: E402
from openbot.server.host import PrinterHost  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


async def main():
    d = tempfile.mkdtemp()
    fake = await FakePrinter().start()
    p = Printer("127.0.0.1", port=fake.port, trust=TrustStore(d),
                credentials=FileCredentialStore(d))
    await p.hello()
    p.trust.trust(p.info.serial, await p.fetch_certificate())
    await p.pair("x")
    host = PrinterHost()
    await host.attach(LocalBackend(p))
    server = OpenBotServer(d, host, name="garage-pi")
    port = await server.start("127.0.0.1", 0)
    base = f"https://127.0.0.1:{port}/"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.load_verify_locations(server.cert_path)
    errors = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, headless=True)

        # 1) A new browser pairs with the setup code -> becomes the first Admin.
        page = await (await browser.new_context(ignore_https_errors=True,
                                                viewport={"width": 1100, "height": 950})).new_page()
        page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        await page.goto(base)
        await page.wait_for_selector("#pair:not([hidden])")
        await page.screenshot(path=os.path.join(OUT, "web_pair.png"))
        await page.fill("#pair-name", "Joe's browser")
        await page.fill("#setup-code", server.clients.setup_code.lower())
        await page.click("#pair-form button[type=submit]")
        await page.wait_for_selector("#app:not([hidden])", timeout=10000)
        assert "admin" in await page.inner_text("#who")
        print("browser paired with the setup code ->", await page.inner_text("#who"))

        # 2) An operator queues a job; another device asks to join.
        op, op_token = server.clients._create("Shop iPad", Role.OPERATOR)
        remote = await RemoteBackend.connect("127.0.0.1", port, op_token, ctx)
        await remote.start_print("tests/fixtures/openbot_box.makerbot")
        await remote.close()
        server.clients.request("Kitchen laptop")
        await page.wait_for_selector("text=Plate is clear: start", timeout=10000)
        await page.reload()
        await page.wait_for_selector("#pending li", timeout=10000)
        await page.click("#btn-camera")
        # (Python-side polling: the page's CSP forbids the eval wait_for_function uses.)
        for _ in range(100):
            if await page.evaluate("() => document.getElementById('camera').naturalWidth"):
                break
            await asyncio.sleep(0.1)
        print("camera shows", await page.evaluate(
            "document.getElementById('camera').naturalWidth"), "px wide frames")
        await page.screenshot(path=os.path.join(OUT, "web_admin.png"), full_page=True)

        # 3) Confirm the plate from the browser -> the printer receives the file.
        page.on("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
        await page.click("text=Plate is clear: start")
        for _ in range(100):
            if fake.files:
                break
            await asyncio.sleep(0.1)
        assert fake.files, "printer never received the job"
        await page.wait_for_selector("td.state-printing", timeout=10000)
        print("confirmed in the browser -> printer received", list(fake.files))

        # 4) Admin allows the waiting device from the browser.
        await page.click("#pending li button.primary")
        for _ in range(50):
            if any(c.name == "Kitchen laptop" for c in server.clients.clients.values()):
                break
            await asyncio.sleep(0.1)
        assert any(c.name == "Kitchen laptop" for c in server.clients.clients.values())
        print("allowed 'Kitchen laptop' from the browser")
        await page.screenshot(path=os.path.join(OUT, "web_printing.png"), full_page=True)

        # 5) Phone-sized layout.
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=os.path.join(OUT, "web_phone.png"), full_page=True)
        await browser.close()

    await server.stop()
    await p.close()
    await fake.stop()
    if errors:
        raise SystemExit("FAILED: " + "; ".join(errors))
    print("WEB SMOKE TEST PASSED")


asyncio.run(main())
