"""End-to-end: the in-place check names what changed (rtk_development#216).

A stability check that fails says which widget was rebuilt, from each
created or deleted element's ``data-testid`` (its own or its nearest
ancestor's), classes and text, and says when the page reloaded. These
tests break a plain page on purpose and read the message.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.e2e.stable_elements import assert_same_elements, snapshot

_PAGE = """<div id="c1" data-testid="card">
  <div id="c2" class="row" data-testid="row-a"><span id="c3" class="dot">a</span></div>
  <span id="c4" class="label">Total</span>
  <span id="not-nicegui">x</span>
</div>"""


@pytest.mark.e2e
def test_a_deleted_element_is_named_by_its_testid_classes_and_text(
    page: Page,
) -> None:
    page.set_content(_PAGE)
    baseline = snapshot(page)
    assert baseline.ids == {"c1", "c2", "c3", "c4"}

    page.evaluate("document.getElementById('c2').remove()")
    page.evaluate(
        "document.getElementById('c1').insertAdjacentHTML("
        "'beforeend', '<b id=\"c5\" class=\"new\">fresh</b>')"
    )
    with pytest.raises(AssertionError) as failure:
        assert_same_elements(baseline, snapshot(page), "refresh 3")

    message = str(failure.value)
    assert "refresh 3: 1 elements created, 2 deleted" in message
    assert "created c5 (inside testid='card') class='new' text='fresh'" in message
    assert "deleted c2 (testid='row-a') class='row' text='a'" in message
    assert "deleted c3 (inside testid='row-a') class='dot' text='a'" in message
    assert "reloaded" not in message


@pytest.mark.e2e
def test_text_changes_alone_pass(page: Page) -> None:
    page.set_content(_PAGE)
    baseline = snapshot(page)
    page.evaluate("document.getElementById('c4').textContent = 'Total: 5'")
    assert_same_elements(baseline, snapshot(page), "refresh 1")


@pytest.mark.e2e
def test_a_reload_with_the_same_ids_fails_and_says_so(page: Page) -> None:
    url = "http://stable-elements.test/"
    page.route(url, lambda route: route.fulfill(body=_PAGE, content_type="text/html"))
    page.goto(url)
    baseline = snapshot(page)
    # The same layout in a new document, as a reloaded NiceGUI page has.
    page.reload()
    with pytest.raises(AssertionError) as failure:
        assert_same_elements(baseline, snapshot(page), "refresh 1")

    assert "0 elements created, 0 deleted (the page reloaded" in str(failure.value)
