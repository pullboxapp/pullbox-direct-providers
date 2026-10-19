"""Synthetic browser regression, run in an isolated patched-image container."""

import unittest
from contextlib import aclosing
from types import SimpleNamespace

from src.endpoints import _navigate_and_solve
from src.utils import TimeoutTimer, get_browser


class BrowserWaitTests(unittest.IsolatedAsyncioTestCase):
    async def run_page(self, html, seconds, url="https://fixture.example/search"):
        async with aclosing(get_browser()) as browsers:
            dep = await anext(browsers)

            async def respond(route):
                await route.fulfill(status=200, content_type="text/html", body=html)

            await dep.page.route("**/*", respond)
            return await _navigate_and_solve(
                dep,
                SimpleNamespace(url=url),
                TimeoutTimer(duration=seconds),
            )

    async def test_loading_transition_is_not_mistaken_for_results(self):
        _, html, _ = await self.run_page(
            """<html><head><title>DDoS-Guard</title></head>
            <body>Checking your browser<script>
            setTimeout(() => { document.title = 'Loading...'; }, 500);
            setTimeout(() => {
                document.title = 'Batman - Search';
                document.body.innerHTML = '<form action="/search">Actual results</form>';
            }, 1500);
            </script></body></html>""",
            8,
        )
        self.assertIn('action="/search"', html)
        self.assertNotIn("Checking your browser", html)

    async def test_never_clearing_page_obeys_request_deadline(self):
        with self.assertRaises(TimeoutError):
            await self.run_page("<title>DDoS-Guard</title><body>Checking your browser</body>", 2)

    async def test_anna_loading_shell_waits_for_a_real_search_form(self):
        _, html, _ = await self.run_page(
            """<html><head><title>Loading https://annas-archive.gd/search</title></head>
            <body><script>setTimeout(() => {
                document.title = 'Batman - Search';
                document.body.innerHTML = '<form action="/search"><input name="q">Results</form>';
            }, 1500);</script></body></html>""",
            8,
            url="https://annas-archive.gd/search?q=Batman",
        )
        self.assertIn('name="q"', html)
        self.assertNotIn("Loading", html)

    async def test_anna_empty_search_is_ready_without_result_links(self):
        _, html, _ = await self.run_page(
            '<title>No matches - Search</title><form action="/search"><input name="q"></form>',
            5,
            url="https://annas-archive.gd/search?q=Missing",
        )
        self.assertIn('action="/search"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
