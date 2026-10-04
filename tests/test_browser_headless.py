"""Optional real Linux/headless DOM test. Never evidence for Windows Chrome/UIA.

Install ARISE's existing browser extra and `python -m playwright install chromium`.
No test downloads a browser or contacts a public site; fixture routes are fulfilled locally.
"""

from pathlib import Path

import pytest

from arise.adapters.browser_playwright import PlaywrightActionTool, PlaywrightBrowserProvider
from arise.core.contracts import ActionContract, AuthorizationContext, Condition, RiskLevel
from arise.core.ports import ExecutionStatus, VerificationStatus
from arise.core.resources import ResourceManager


@pytest.mark.asyncio
async def test_headless_dom_navigation_target_dispatch_verification_and_cleanup():
    sdk = pytest.importorskip(
        "playwright.async_api", reason="optional browser extra is not installed"
    )
    async with sdk.async_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("ENVIRONMENT-BLOCKED: Playwright Chromium binary is not installed")
    provider = PlaywrightBrowserProvider(headless=True)
    await provider.start()
    try:

        async def fixture(route):
            await route.fulfill(
                content_type="text/html",
                body="""<!doctype html>
                <title>Before</title>
                <button id="save" onclick="document.title='Saved'">Save</button>
                <input aria-label="Password" type="password" value="must-not-be-observed">
                <button style="display:none">Hidden</button>""",
            )

        await provider._context.route("https://fixture.invalid/**", fixture)
        page_id = provider.default_page_id
        await provider.navigate(page_id, "https://fixture.invalid/start", timeout_seconds=3)
        candidates = await provider.inspect(page_id)
        assert "must-not-be-observed" not in repr(candidates)
        candidate = next(
            item for item in candidates if item.descriptor.identity.semantic_name == "Save"
        )
        action = ActionContract(
            task_id="fixture",
            tool_name="browser.click",
            target=candidate.descriptor.identity,
            risk=RiskLevel.R3,
            authority=AuthorizationContext("alice", "fixture"),
            postconditions=(Condition("browser.title", expected="Saved"),),
        )
        tool = PlaywrightActionTool(provider, "click")
        observation = await provider.observe(action)
        async with ResourceManager().acquire_many(
            "fixture", tool.resources_for(action), lease_seconds=5
        ) as lease:
            outcome = await tool.execute(action, observation, lease)
        assert outcome.status is ExecutionStatus.SUCCEEDED
        assert (await provider.verify(action, outcome=outcome)).status is VerificationStatus.PASSED
        assert not await provider.is_current(observation)
    finally:
        await provider.close()
    assert not provider.started and not provider._pages and not provider._observations
