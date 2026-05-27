"""End-to-end browser tests for SP-Base.

These tests drive the live NiceGUI UI through Playwright in a real
Chromium browser.  They are intentionally excluded from the default
``pytest`` run (see ``norecursedirs`` in pyproject.toml) and from the
unit-coverage measurement — run them explicitly with:

    uv run pytest tests/e2e --no-cov

Prerequisites:
    uv sync --all-extras
    uv run playwright install chromium
"""
