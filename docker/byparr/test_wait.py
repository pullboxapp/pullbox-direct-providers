"""Run against the upstream and patched images with stdlib unittest, no installs."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.endpoints import _navigate_and_solve
from src.utils import TimeoutTimer

CHALLENGE = "<title>DDoS-Guard</title><body>Checking your browser</body>"
RESULTS = '<title>Batman - Search</title><form action="/search">Results</form>'


class WaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.html = CHALLENGE
        self.page = SimpleNamespace(
            goto=AsyncMock(return_value=object()),
            wait_for_load_state=AsyncMock(),
            wait_for_function=AsyncMock(),
            title=AsyncMock(return_value="DDoS-Guard"),
            content=AsyncMock(side_effect=lambda: self.html),
        )
        self.dep = SimpleNamespace(page=self.page)
        self.request = SimpleNamespace(url="https://example.org/search")
        self.detector = patch("src.endpoints.challenge_present", AsyncMock(return_value=False))
        self.detector.start()
        self.addCleanup(self.detector.stop)
        self.sleeper = patch("src.endpoints.sleep", AsyncMock(side_effect=self.finish))
        self.sleep = self.sleeper.start()
        self.addCleanup(self.sleeper.stop)

    async def finish(self, *_args, **_kwargs):
        self.html = RESULTS
        self.page.title.return_value = "Batman - Search"

    async def navigate(self):
        return await _navigate_and_solve(self.dep, self.request, TimeoutTimer(duration=30))

    async def test_waits_for_ddos_guard_and_returns_fresh_results(self):
        _, html, _ = await self.navigate()
        self.assertEqual(html, RESULTS)
        self.sleep.assert_awaited_once()
        self.page.wait_for_function.assert_not_awaited()

    async def test_plain_page_snapshot_is_taken_after_settling(self):
        self.page.title.return_value = "Search"
        self.html = "old results"

        async def settle(state=None, **_kwargs):
            if state == "networkidle":
                await self.finish()

        self.page.wait_for_load_state.side_effect = settle
        _, html, _ = await self.navigate()
        self.assertEqual(html, RESULTS)

    async def test_stuck_challenge_times_out_instead_of_returning_success(self):
        timer = SimpleNamespace(remaining=Mock(side_effect=[30, 30, 0]))
        with self.assertRaises(TimeoutError):
            await _navigate_and_solve(self.dep, self.request, timer)

    async def test_cancelled_wait_does_not_return_challenge_html(self):
        self.sleep.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.navigate()

    async def test_incidental_challenge_words_are_not_interstitials(self):
        self.page.title.return_value = "DDoS-Guard documentation"
        _, html, _ = await self.navigate()
        self.assertEqual(html, CHALLENGE)
        self.page.wait_for_function.assert_not_awaited()

    async def test_cloudflare_keeps_existing_solver(self):
        self.page.title.return_value = "Just a moment..."
        with (
            patch("src.endpoints.challenge_present", AsyncMock(return_value=True)),
            patch("src.endpoints.solve_challenge", AsyncMock()) as solver,
        ):
            detected, _, _ = await self.navigate()
        self.assertTrue(detected)
        solver.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
