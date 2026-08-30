# wirespec

A browser driver for Python end-to-end tests. It speaks the Chrome DevTools
Protocol directly, over a pipe, with no intermediate process and no bundled
browser.

```python
from wirespec import Browser, expect

async with Browser.launch() as browser:
    async with browser.new_context(base_url="http://127.0.0.1:8000") as context:
        page = await context.new_page()
        await page.goto("/solutions")
        await expect(page.get_by_role("heading", name="Solutions")).to_be_visible()
```

- **No Node, and no JavaScript.** No driver process, no RPC hop, no 132 MB of
  bundled JavaScript — and nothing injected into your page. wirespec is Python
  and CDP; the only JavaScript that runs is what you pass to `page.evaluate`.
- **No browser download.** Chrome is discovered, not fetched.
- **One dependency.** `msgspec`. The transport is a pipe, so there is no HTTP
  client and no open debugging port.
- **Semantic queries.** Roles and accessible names, computed by **Chrome
  itself** — the same answer your users' screen readers get, with no ARIA
  implementation here to drift from it.
- **Cost that does not grow with the page.** A locator resolves in a few
  protocol calls plus one pipelined batch, never one round trip per matched
  element. A role-and-name query against ten candidates measures 0.465 ms.
- **A failure artefact worth opening.** The recorded screen and the network
  traffic on one timeline, in a single self-contained HTML file, kept only for
  the tests that fail. No encoder, no viewer to install, no `npx` — it opens
  offline, out of a CI artefact bundle, and clicking a request seeks to the
  frame it belongs with.

## Status

**Usable.** Locators, auto-waiting, assertions, actions and routing all work.

- **Built** — the pipe transport, the connection, and the CDP subset: 81
  commands and 27 events, every one exercised against a real Chrome, every field
  name checked against Chrome's own protocol definition. Above it, `Browser`,
  `BrowserContext` and `Page`; the resolution layer that answers every query
  with CDP and no JavaScript; locators with all nine step kinds, every reader
  and every action; the push-driven wait loop; `expect` with every assertion and
  its negation; mouse, keyboard and native HTML5 drag; request interception;
  `page.request`, which issues a real request from inside the browser on the
  context's own cookie jar — with, still, no JavaScript anywhere; and the
  failure artefact: the screencast, the traffic and the console reconciled onto
  one clock, written as a single HTML file for the tests that failed.
- **Playwright compatibility** — `wirespec.compat.async_api` is built: a suite
  that imports `playwright.async_api` runs with the import line changed and
  nothing else. Milliseconds, dict viewports and synchronous `page.on` are
  translated; anything wirespec does not have refuses and names itself rather
  than doing something subtly different.
  The sync form works too, and so do `pytest-playwright`'s fixtures and options.
  With the optional import shim, a Chromium Playwright suite runs with **zero
  changed lines**:

  ```
  pytest -p wirespec.compat.shim -p wirespec.compat.pytest_playwright
  ```

  The shim is opt-in and never a side effect of installing wirespec; it refuses
  to shadow a Playwright that is already imported.
- **Built since** — iframes, tabs and popups, `select_option` including
  `<select multiple>`, file upload, dialogs, and the failure artefact. A
  complete working prototype lives in a private application's repository,
  where it replaced Playwright in a 229-test suite with 54 changed lines and
  ran it 7% faster on 13% less CPU.
- **Checked against Playwright, not against a reading of it** — `differential/`
  runs one 41-test spec file under both drivers, against the same Chrome and
  the same fixture pages, and diffs the outcomes. They agree 41 out of 41. The
  first run did not: it found seven divergences, three of them bugs in the
  driver.
- **Not built** — cross-origin (out-of-process) iframes, which refuse by name
  rather than timing out. Every other trade-off is written down in the source,
  next to the code it constrains.

What works today reads like this:

```python
from wirespec import Browser, expect

async with Browser.launch() as browser:
    async with browser.new_context(base_url="https://example.test") as context:
        page = await context.new_page()
        await page.goto("/")

        # Roles and accessible names, computed by Chrome itself.
        await page.get_by_role("button", name="Save").click()

        # Chains are descriptions, re-resolved on every use. No explicit waits.
        rows = page.get_by_role("list", name="Solution packs").get_by_role("listitem")
        await expect(rows.filter(has_text="Acme")).to_have_count(2)
        await expect(rows.first).to_contain_text("Acme")

        # Actions wait for the element to be there, still and reachable.
        await page.get_by_label("Email address").fill("someone@example.test")

        # Stub an API without touching the server.
        await page.route("**/api/**", lambda route: route.fulfill(body="[]"))
```

When a test fails, keep the recording of it — the screen, the traffic and the
console on one timeline, in one HTML file. From `conftest.py`:

```python
pytest_plugins = ["wirespec.pytest_plugin"]


@pytest_asyncio.fixture
async def page(context, artefacts):
    page = await context.new_page()
    async with artefacts.record(page):  # kept on failure, dropped on pass
        yield page
```

**The architecture, the settled decisions and the browser traps that cost a day
each to find are documented in the source**, in the module that each one is
about.

## Where this is going

wirespec began as one project's way out of a 132 MB Node dependency. The goal
now is that **any Chromium Playwright suite can move across without changing a
line** — reached in stages, with a bounded definition of what "drop-in" means
and what is permanently excluded (Firefox, WebKit, codegen, and Playwright's
trace file format — though not the capability, which is the bullet above).

## Requirements

Python ≥ 3.14, and a Chrome or Chromium on the machine.

## Licence

MIT.
