"""Settings Manager: Maprisma apps and System apps (port of Maprisma Settings' pages; stub until built)."""

from __future__ import annotations

from pages_base import PageGroup


class AppsPages(PageGroup):
    PAGE_KEYS = ('apps', 'system_apps')
