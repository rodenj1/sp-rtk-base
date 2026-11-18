"""The page's NiceGUI elements, for the e2e tests that check a refresh
updates the page in place (rtk_development#177).

A snapshot maps each element's id (``c<N>``) to what identifies it to a
reader: the ``data-testid`` it or its nearest ancestor carries, its
classes and a short excerpt of its text. When a check fails, the message
names each created and deleted element from these, so the failure says
which widget was rebuilt. A deleted element is described from the
snapshot taken while it still existed.

A snapshot also marks the document it was taken from. A page that
reloaded (e.g. NiceGUI's client lost its connection for longer than the
reconnect timeout) gets the same ids for its static layout, so the
element ids alone would show only the dynamic parts as deleted
(rtk_development#216); the check says the page reloaded instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Error, Page

#: Every NiceGUI element on the page, with what identifies it, and the
#: document's own marker (set on the first snapshot of that document).
_SNAPSHOT_JS = """() => {
    window.__e2eDocument ||= String(performance.timeOrigin) + '/' + Math.random();
    const elements = Array.from(document.querySelectorAll('[id]'))
        .filter(e => /^c\\d+$/.test(e.id))
        .map(e => {
            const own = e.getAttribute('data-testid');
            const near = own ? null : e.closest('[data-testid]');
            return {
                id: e.id,
                testid: own || (near ? near.getAttribute('data-testid') : ''),
                own: !!own,
                classes: (e.getAttribute('class') || '').trim(),
                text: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
            };
        });
    return {document: window.__e2eDocument, elements};
}"""


@dataclass(frozen=True)
class Element:
    """One NiceGUI element as the page showed it."""

    id: str
    testid: str
    own_testid: bool
    classes: str
    text: str

    def describe(self) -> str:
        if self.own_testid:
            where = f"testid={self.testid!r}"
        elif self.testid:
            where = f"inside testid={self.testid!r}"
        else:
            where = "no testid"
        return f"{self.id} ({where}) class={self.classes!r} text={self.text!r}"


@dataclass(frozen=True)
class Snapshot:
    """The page's NiceGUI elements, by id, and the document they were in."""

    document: str
    elements: dict[str, Element]

    @property
    def ids(self) -> set[str]:
        return set(self.elements)

    def same_elements(self, other: Snapshot) -> bool:
        """The same document with the same element ids (text may differ)."""
        return self.document == other.document and self.ids == other.ids


def snapshot(page: Page) -> Snapshot:
    """The page's NiceGUI elements now.

    Raises:
        AssertionError: The page navigated (e.g. reloaded) while it was read.
    """
    try:
        raw = page.evaluate(_SNAPSHOT_JS)
    except Error as exc:
        if "Execution context was destroyed" not in exc.message:
            raise
        raise AssertionError(
            f"the page reloaded or navigated while its elements were read: {exc}"
        ) from exc
    return Snapshot(
        raw["document"],
        {
            e["id"]: Element(e["id"], e["testid"], e["own"], e["classes"], e["text"])
            for e in raw["elements"]
        },
    )


def _by_number(ids: set[str]) -> list[str]:
    return sorted(ids, key=lambda i: int(i[1:]))


def describe_change(baseline: Snapshot, current: Snapshot) -> str:
    """How many elements were created and deleted, then each one."""
    created = _by_number(current.ids - baseline.ids)
    deleted = _by_number(baseline.ids - current.ids)
    lines = [f"{len(created)} elements created, {len(deleted)} deleted"]
    if current.document != baseline.document:
        lines[0] += " (the page reloaded: these ids are a new document's)"
    lines += [f"  created {current.elements[i].describe()}" for i in created]
    lines += [f"  deleted {baseline.elements[i].describe()}" for i in deleted]
    return "\n".join(lines)


def assert_same_elements(baseline: Snapshot, current: Snapshot, when: str) -> None:
    """The page is the baseline's document, with its elements, no more and
    no fewer."""
    assert current.same_elements(baseline), (
        f"{when}: {describe_change(baseline, current)}"
    )
