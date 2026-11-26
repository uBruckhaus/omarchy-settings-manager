"""Settings Manager: the base of a group of sidebar pages.

A page group owns some page keys (PAGE_KEYS) and builds them into the window's Gtk.Stack. The
window finds the group of a page through `owns()`, calls `refresh(key)` the first time a page is
shown and on Ctrl+R / F5. Subclasses build each page with `add_page()` and fill it in `load()`;
`self.gen[key]` grows with each refresh so a slow answer for an older refresh is dropped
(`current(key, gen)`).
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from i18n import _  # noqa: E402
from ui_helpers import make_header_row, page_box, scrolled  # noqa: E402


class PageGroup:
    PAGE_KEYS: tuple[str, ...] = ()

    def __init__(self, app):
        self.app = app
        self.content: dict[str, Gtk.Box] = {}
        self.gen: dict[str, int] = {key: 0 for key in self.PAGE_KEYS}

    @classmethod
    def owns(cls, key: str) -> bool:
        return key in cls.PAGE_KEYS

    def build_all(self, stack: Gtk.Stack) -> None:
        for key in self.PAGE_KEYS:
            self.build(stack, key)

    def build(self, stack: Gtk.Stack, key: str) -> None:
        """Default: a page with a description row and Refresh. Override for more."""
        self.add_page(stack, key, make_header_row(self.description(key), lambda k=key: self.refresh(k)))

    def description(self, key: str) -> str:
        return ""

    def add_page(self, stack: Gtk.Stack, key: str, header: Gtk.Widget | None) -> Gtk.Box:
        outer = page_box()
        if header is not None:
            outer.append(header)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.append(content)
        self.content[key] = content
        stack.add_named(scrolled(outer), key)
        return content

    def refresh(self, key: str) -> None:
        if key not in self.content:
            return
        self.gen[key] += 1
        self.load(key, self.gen[key])

    def current(self, key: str, gen: int) -> bool:
        return self.gen.get(key) == gen

    def load(self, key: str, gen: int) -> None:
        """Fill self.content[key]; the stub shows that the page is not built yet."""
        box = self.content[key]
        child = box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt
        group = Adw.PreferencesGroup()
        group.add(Adw.ActionRow(title=_("This page is not built yet.")))
        box.append(group)


def placeholder_group(text: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    group.add(Adw.ActionRow(title=text))
    return group
