"""Settings Manager: the Theme page (Omarchy themes; a port of Maprisma Settings' Theme page).

A grid of the themes in small under a big preview of the selected one (a pointer resting on a
card, a click or the arrow keys; else the one in use): its picture with ‹ › through its
wallpapers, name, dark/light mode, colours and Apply. Double-click or Enter applies too
(`omarchy theme set`, through theme_core). Dark/Light/All narrows the grid; the theme in use is
framed with a check, Theme Manager's favourites carry a star and its hidden themes stay out
unless asked for. "More in Theme Manager…" opens Theme Manager when it is installed.

Images are decoded off the GTK thread, one at a time, scaled down while they load, and cached
per path + mtime + size; theme_core checks each one first (size caps, links, metadata). No timer
runs while nothing happens: a card turns to its first wallpaper only under a resting pointer and
the big preview steps on by itself once, after Theme Manager's delay, when animations are on.
"""

from __future__ import annotations

import itertools
import queue
import threading
from collections import OrderedDict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402

import theme_core  # noqa: E402
from i18n import _, ngettext  # noqa: E402
from pages_base import PageGroup  # noqa: E402
from ui_helpers import clear, escape, label, make_header_row, page_box, run_async, scrolled  # noqa: E402

CARD_SIZE = (320, 180)          # decode size of a card's picture
BIG_SIZE = (960, 540)           # decode size of the big preview
HOVER_SELECT_MS = 220           # a pointer resting this long on a card shows it in the big preview
CACHE_TEXTURES = 160
DEFAULT_CORNER = 8


# --- images ------------------------------------------------------------------------------------

def _decode(path: str, size: tuple[int, int]) -> Gdk.Texture | None:
    """The image at path scaled down to cover size (never up), or None. Runs off the GTK thread:
    theme_core reads it once and checks it; the loader scales while it decodes."""
    checked = theme_core.load_image(path)
    if checked is None:
        return None
    data, width, height = checked
    scale = min(1.0, max(size[0] / width, size[1] / height))
    loader = GdkPixbuf.PixbufLoader()

    def prepared(ld, w, h):
        if (w, h) != (width, height):          # the decoder disagrees with the checked header
            ld.set_size(1, 1)
            return
        ld.set_size(max(1, round(w * scale)), max(1, round(h * scale)))

    loader.connect("size-prepared", prepared)
    try:
        try:
            loader.write(data)
        finally:
            loader.close()
        pixbuf = loader.get_pixbuf()
    except (GLib.Error, TypeError):
        return None
    if pixbuf is None or (pixbuf.get_width() <= 1 < width):
        return None
    fmt = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt,
                                 pixbuf.read_pixel_bytes(), pixbuf.get_rowstride())


class Thumbnails:
    """Textures per (path, mtime, size, decode size); decoded by one worker thread, nearest first."""

    def __init__(self):
        self.cache: OrderedDict = OrderedDict()
        self.failed: set = set()
        self.waiting: dict = {}
        self.jobs: queue.PriorityQueue = queue.PriorityQueue()
        self.order = itertools.count()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def peek(self, path: str | None, size: tuple[int, int]) -> Gdk.Texture | None:
        key = self._key(path, size)
        return self.cache.get(key) if key else None

    def get(self, path: str | None, size: tuple[int, int], callback, priority: int = 1) -> Gdk.Texture | None:
        """The texture now when cached; else None and callback(texture or None) later."""
        key = self._key(path, size)
        if key is None or key in self.failed:
            return None
        texture = self.cache.get(key)
        if texture is not None:
            self.cache.move_to_end(key)
            return texture
        if key in self.waiting:
            self.waiting[key].append(callback)
            return None
        self.waiting[key] = [callback]
        self.jobs.put((priority, next(self.order), key, path, size))
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True)
                self.thread.start()
        return None

    @staticmethod
    def _key(path, size):
        if not path:
            return None
        key = theme_core.image_key(path)
        return key + tuple(size) if key else None

    def _work(self):
        while True:
            try:
                _priority, _n, key, path, size = self.jobs.get(timeout=20)
            except queue.Empty:
                with self.lock:
                    if self.jobs.empty():
                        self.thread = None      # idle: the next request starts a new one
                        return
                continue
            try:
                texture = _decode(path, size)
            except Exception:  # noqa: BLE001
                texture = None
            GLib.idle_add(self._deliver, key, texture)

    def _deliver(self, key, texture):
        if texture is None:
            self.failed.add(key)
        else:
            self.cache[key] = texture
            while len(self.cache) > CACHE_TEXTURES:
                self.cache.popitem(last=False)
        for callback in self.waiting.pop(key, []):
            try:
                callback(texture)
            except Exception:  # noqa: BLE001
                pass
        return False


def _rgb(hex_color: str | None, fallback=(0.2, 0.2, 0.2)) -> tuple[float, float, float]:
    try:
        return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (TypeError, ValueError, IndexError):
        return fallback


def _swatch_area(colors: dict) -> Gtk.DrawingArea:
    """A theme without a picture: its background, "Aa" in its foreground and its colours as bars."""
    area = Gtk.DrawingArea(hexpand=True, vexpand=True)
    bars = [colors[k] for k in ("accent", "red", "green", "yellow", "blue", "magenta", "cyan") if k in colors]

    def draw(widget, cr, width, height):
        cr.set_source_rgb(*_rgb(colors.get("background"), (0.15, 0.15, 0.18)))
        cr.paint()
        layout = widget.create_pango_layout("Aa")
        desc = Pango.FontDescription.from_string("Sans Bold")
        desc.set_absolute_size(max(8, height * 0.18) * Pango.SCALE)
        layout.set_font_description(desc)
        cr.set_source_rgb(*_rgb(colors.get("foreground"), (0.85, 0.85, 0.85)))
        cr.move_to(width * 0.06, height * 0.08)
        PangoCairo.show_layout(cr, layout)
        if bars:
            gap, margin = 3, width * 0.06
            bar_w = (width - 2 * margin - gap * (len(bars) - 1)) / len(bars)
            for i, color in enumerate(bars):
                cr.set_source_rgb(*_rgb(color))
                cr.rectangle(margin + i * (bar_w + gap), height * 0.78, bar_w, max(4, height * 0.1))
                cr.fill()

    area.set_draw_func(draw)
    return area


class RatioBin(Gtk.Widget):
    """One child at a fixed aspect ratio, as high as its width asks (height-for-width): a picture's
    own natural size never decides how big a card or the big preview is."""

    def __init__(self, child: Gtk.Widget, ratio: float = 16 / 9, min_width: int = 120, natural_width: int = 320):
        super().__init__()
        self.ratio, self.min_width, self.natural_width = ratio, min_width, natural_width
        self.child = child
        child.set_parent(self)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return self.min_width, max(self.min_width, self.natural_width), -1, -1
        width = for_size if for_size > 0 else self.natural_width
        height = max(1, round(width / self.ratio))
        return height, height, -1, -1

    def do_size_allocate(self, width, height, baseline):
        self.child.allocate(width, height, baseline, None)

    def do_dispose(self):
        if self.child is not None:
            self.child.unparent()
            self.child = None


class SideBySide(Gtk.Widget):
    """Two children side by side (the first at its natural width, the second taking the rest) when
    there is room for both, else one above the other; measured height-for-width, so the second's
    wrapped text never gets cut off."""

    def __init__(self, first: Gtk.Widget, second: Gtk.Widget, wide_from: int = 560, spacing: int = 16):
        super().__init__()
        self.first, self.second, self.wide_from, self.spacing = first, second, wide_from, spacing
        first.set_parent(self)
        second.set_parent(self)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def _widths(self, width: int) -> tuple[int, int] | None:
        """(first, second) widths side by side, or None when they go one above the other."""
        if width < self.wide_from:
            return None
        first_nat = self.first.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        second_min = self.second.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
        first_w = max(1, min(first_nat, width - self.spacing - second_min))
        return first_w, max(1, width - first_w - self.spacing)

    def _first_width(self, width: int) -> int:
        return max(1, min(width, self.first.measure(Gtk.Orientation.HORIZONTAL, -1)[1]))

    def do_measure(self, orientation, for_size):
        first = self.first.measure(Gtk.Orientation.HORIZONTAL, -1)
        second = self.second.measure(Gtk.Orientation.HORIZONTAL, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return max(first[0], second[0]), first[1] + self.spacing + second[1], -1, -1
        width = for_size if for_size > 0 else first[1] + self.spacing + second[1]
        widths = self._widths(width)
        if widths:
            a = self.first.measure(Gtk.Orientation.VERTICAL, widths[0])
            b = self.second.measure(Gtk.Orientation.VERTICAL, widths[1])
            return max(a[0], b[0]), max(a[1], b[1]), -1, -1
        a = self.first.measure(Gtk.Orientation.VERTICAL, self._first_width(width))
        b = self.second.measure(Gtk.Orientation.VERTICAL, width)
        return a[0] + self.spacing + b[0], a[1] + self.spacing + b[1], -1, -1

    def do_size_allocate(self, width, height, baseline):
        widths = self._widths(width)
        if widths:
            first_h = self.first.measure(Gtk.Orientation.VERTICAL, widths[0])[1]
            self.first.allocate(widths[0], min(height, first_h), -1, None)
            self.second.allocate(widths[1], height, -1, _at(widths[0] + self.spacing, 0))
            return
        first_w = self._first_width(width)
        first_h = self.first.measure(Gtk.Orientation.VERTICAL, first_w)[1]
        self.first.allocate(first_w, first_h, -1, _at((width - first_w) // 2, 0))
        top = first_h + self.spacing
        self.second.allocate(width, max(1, height - top), -1, _at(0, top))

    def do_dispose(self):
        for name in ("first", "second"):
            child = getattr(self, name, None)
            if child is not None:
                child.unparent()
                setattr(self, name, None)


def _at(x: float, y: float) -> Gsk.Transform:
    point = Graphene.Point()
    point.init(x, y)
    return Gsk.Transform.new().translate(point)


def _picture() -> Gtk.Picture:
    picture = Gtk.Picture(can_shrink=True, hexpand=True, vexpand=True)
    picture.set_content_fit(Gtk.ContentFit.COVER)
    return picture


def _animations() -> bool:
    settings = Gtk.Settings.get_default()
    return bool(settings.get_property("gtk-enable-animations")) if settings else False


def _mode_icon(mode: str) -> str:
    return "weather-clear-symbolic" if mode == "light" else "weather-clear-night-symbolic"


def _bold() -> Pango.AttrList:
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_weight_new(Pango.Weight.SEMIBOLD))
    return attrs


# --- one card ----------------------------------------------------------------------------------

class ThemeCard:
    """A theme in small: its preview (else first wallpaper, else colours), name, mode, star; the
    one in use framed with a check; turns to its first wallpaper under a resting pointer."""

    def __init__(self, page: "ThemePages", theme: dict):
        self.page, self.theme, self.slug = page, theme, theme["slug"]
        walls = theme.get("wallpapers") or []
        self.picture_path = theme.get("preview") or (walls[0] if walls else None)
        self.wall_path = walls[0] if theme.get("preview") and walls else None
        self.flip_timer = 0
        self.select_timer = 0

        self.child = Gtk.FlowBoxChild()
        self.child.slug = self.slug
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.add_css_class("sm-theme-card")
        self.box = box

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=450)
        self.stack.add_named(_swatch_area(theme.get("colors") or {}), "swatch")
        self.preview_pic = _picture()
        self.stack.add_named(self.preview_pic, "preview")
        self.wall_pic = _picture()
        self.stack.add_named(self.wall_pic, "wall")
        self.stack.set_visible_child_name("swatch")

        overlay = Gtk.Overlay(child=self.stack)
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.add_css_class("sm-theme-pic")
        self.badge = Gtk.Box(spacing=3, halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=6, margin_bottom=6)
        self.badge.add_css_class("sm-theme-badge")
        self.badge.append(Gtk.Image(icon_name="object-select-symbolic", pixel_size=12))
        self.badge.append(Gtk.Label(label=_("In use")))
        overlay.add_overlay(self.badge)
        self.spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=28, height_request=28)
        overlay.add_overlay(self.spinner)
        box.append(RatioBin(overlay, min_width=130, natural_width=150))

        row = Gtk.Box(spacing=4, margin_start=4, margin_end=4, margin_bottom=2)
        self.star = Gtk.Image(icon_name="starred-symbolic", pixel_size=14, tooltip_text=_("Favourite in Theme Manager"))
        self.star.add_css_class("sm-theme-star")
        row.append(self.star)
        self.name = Gtk.Label(label=theme["name"], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        row.append(self.name)
        mode = Gtk.Image(icon_name=_mode_icon(theme["mode"]), pixel_size=14,
                         tooltip_text=_("Light") if theme["mode"] == "light" else _("Dark"))
        mode.add_css_class("sm-dim")
        row.append(mode)
        box.append(row)
        self.child.set_child(box)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self.on_enter())
        motion.connect("leave", lambda *_a: self.on_leave())
        self.child.add_controller(motion)
        self.update()
        self.load_picture()

    def update(self):
        t = self.theme
        self.star.set_visible(bool(t.get("favorite")))
        self.badge.set_visible(bool(t.get("current")))
        (self.box.add_css_class if t.get("current") else self.box.remove_css_class)("sm-current")
        self.name.set_attributes(_bold() if t.get("current") else None)
        busy = self.page.applying == self.slug
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)

    def load_picture(self):
        texture = self.page.thumbs.get(self.picture_path, CARD_SIZE, self._got_picture)
        if texture is not None:
            self._got_picture(texture)

    def _live(self) -> bool:
        return self.page.cards.get(self.slug) is self

    def _got_picture(self, texture):
        if texture is None or not self._live():
            return
        self.preview_pic.set_paintable(texture)
        if self.stack.get_visible_child_name() == "swatch":
            self.stack.set_visible_child_full("preview", Gtk.StackTransitionType.NONE)

    def on_enter(self):
        self.cancel()
        self.select_timer = GLib.timeout_add(HOVER_SELECT_MS, self._hover_select)
        page = self.page
        if self.wall_path and page.flip and page.delay > 0 and _animations():
            self.flip_timer = GLib.timeout_add(int(page.delay * 1000), self._flip)

    def on_leave(self):
        self.cancel()
        if self.stack.get_visible_child_name() == "wall":
            self.stack.set_visible_child_name("preview")

    def cancel(self):
        for name in ("select_timer", "flip_timer"):
            source = getattr(self, name)
            if source:
                GLib.source_remove(source)
                setattr(self, name, 0)

    def _hover_select(self):
        self.select_timer = 0
        self.page.select(self.slug)
        return False

    def _flip(self):
        self.flip_timer = 0
        texture = self.page.thumbs.get(self.wall_path, CARD_SIZE, self._got_wall)
        if texture is not None:
            self._got_wall(texture)
        return False

    def _got_wall(self, texture):
        if texture is None or not self._live():
            return
        self.wall_pic.set_paintable(texture)
        if self.stack.get_visible_child_name() == "preview":
            self.stack.set_visible_child_name("wall")


# --- the page ----------------------------------------------------------------------------------

class ThemePages(PageGroup):
    PAGE_KEYS = ('theme',)

    def __init__(self, app):
        super().__init__(app)
        self.thumbs = Thumbnails()
        self.data: dict = {}
        self.themes: dict[str, dict] = {}
        self.cards: dict[str, ThemeCard] = {}
        self.selected = ""
        self.applying = ""
        self.mode = ""                      # "", "dark" or "light"
        self.show_hidden = False
        self.flip, self.auto_rotate, self.delay = True, True, 2.0
        self.big_slug = ""
        self.big_sources: list[str] = []
        self.big_index = 0
        self.big_front = "a"
        self.auto_timer = 0
        self.watch_timer = 0
        self.monitor = None
        self.css = None
        self.flow: Gtk.FlowBox | None = None
        self.empty: Gtk.Label | None = None

    # -- building -------------------------------------------------------------------------------

    def build(self, stack: Gtk.Stack, key: str) -> None:
        self._install_css()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.all_btn = Gtk.ToggleButton(label=_("All"), active=True)
        self.dark_btn = Gtk.ToggleButton(label=_("Dark"), group=self.all_btn)
        self.light_btn = Gtk.ToggleButton(label=_("Light"), group=self.all_btn)
        self.mode_box = Gtk.Box(valign=Gtk.Align.CENTER, visible=False)
        self.mode_box.add_css_class("linked")
        for button, mode in ((self.all_btn, ""), (self.dark_btn, "dark"), (self.light_btn, "light")):
            button.connect("toggled", lambda b, m=mode: b.get_active() and self._set_mode(m))
            self.mode_box.append(button)
        self.hidden_btn = Gtk.ToggleButton(icon_name="view-reveal-symbolic", valign=Gtk.Align.CENTER, visible=False)
        self.hidden_btn.connect("toggled", lambda b: self._set_show_hidden(b.get_active()))
        self.more_btn = Gtk.Button(label=_("More in Theme Manager…"), valign=Gtk.Align.CENTER, visible=False,
                                   tooltip_text=_("Wallpapers, favourites, new themes and more"))
        self.more_btn.add_css_class("flat")
        self.more_btn.connect("clicked", lambda *_a: self._open_theme_manager())
        header = make_header_row(_("Loading themes…"), lambda: self.refresh(key), (self.mode_box, self.hidden_btn))
        self.status = header.get_first_child()

        top = page_box()
        top.set_margin_bottom(6)
        top.append(header)
        self.big_bin = self._build_big()
        self.big_bin.set_visible(False)
        top.append(self.big_bin)
        self.hint = Gtk.Box(spacing=8, visible=False)
        hint_text = label(_("Click a theme to preview it; double-click or Enter uses it."), ("sm-dim", "caption"), wrap=True)
        hint_text.set_hexpand(True)
        hint_text.set_valign(Gtk.Align.CENTER)
        self.hint.append(hint_text)
        self.hint.append(self.more_btn)
        top.append(self.hint)
        root.append(Adw.Clamp(maximum_size=860, tightening_threshold=600, child=top))

        body = page_box()
        body.set_margin_top(6)
        self.content[key] = body
        root.append(scrolled(body))
        stack.add_named(root, key)

    def _build_big(self) -> Gtk.Widget:

        self.big_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=450)
        self.big_swatch_holder = Gtk.Box()
        self.big_stack.add_named(self.big_swatch_holder, "swatch")
        self.big_pics = {"a": _picture(), "b": _picture()}
        for name, pic in self.big_pics.items():
            self.big_stack.add_named(pic, name)
        overlay = Gtk.Overlay(child=self.big_stack)
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.add_css_class("sm-theme-pic")
        self.prev_btn = Gtk.Button(icon_name="go-previous-symbolic", halign=Gtk.Align.START, valign=Gtk.Align.CENTER,
                                   margin_start=8, tooltip_text=_("Previous picture"))
        self.next_btn = Gtk.Button(icon_name="go-next-symbolic", halign=Gtk.Align.END, valign=Gtk.Align.CENTER,
                                   margin_end=8, tooltip_text=_("Next picture"))
        for button, step in ((self.prev_btn, -1), (self.next_btn, 1)):
            button.add_css_class("circular")
            button.add_css_class("osd")
            button.connect("clicked", lambda *_a, s=step: self._step(s, user=True))
            overlay.add_overlay(button)
        self.counter = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8, margin_bottom=8)
        self.counter.add_css_class("sm-theme-counter")
        overlay.add_overlay(self.counter)
        self.big_spinner = Gtk.Spinner(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, width_request=36, height_request=36)
        overlay.add_overlay(self.big_spinner)
        self.big_frame = RatioBin(overlay, min_width=240, natural_width=440)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True, valign=Gtk.Align.START)
        title = Gtk.Box(spacing=6)
        self.big_star = Gtk.Image(icon_name="starred-symbolic", pixel_size=18, tooltip_text=_("Favourite in Theme Manager"))
        self.big_star.add_css_class("sm-theme-star")
        title.append(self.big_star)
        self.big_name = label("", ("title-3",), wrap=True)
        self.big_name.set_hexpand(True)
        title.append(self.big_name)
        info.append(title)
        meta = Gtk.Box(spacing=6)
        self.big_mode_icon = Gtk.Image(pixel_size=16)
        self.big_mode_icon.add_css_class("sm-dim")
        meta.append(self.big_mode_icon)
        self.big_meta = label("", ("sm-dim",), wrap=True)
        meta.append(self.big_meta)
        info.append(meta)
        self.swatches = Adw.WrapBox(child_spacing=4, line_spacing=4, margin_top=6)
        info.append(self.swatches)
        self.big_walls = label("", ("sm-dim",), wrap=True)
        info.append(self.big_walls)
        self.apply_btn = Gtk.Button(halign=Gtk.Align.START, margin_top=10)
        self.apply_btn.add_css_class("pill")
        self.apply_content = Adw.ButtonContent(icon_name="media-playback-start-symbolic", label=_("Apply theme"))
        self.apply_btn.set_child(self.apply_content)
        self.apply_btn.connect("clicked", lambda *_a: self.apply(self.big_slug))
        info.append(self.apply_btn)
        big = SideBySide(self.big_frame, info)
        big.add_css_class("sm-theme-big")
        self.big_box = big
        return big

    def _install_css(self):
        radius = DEFAULT_CORNER
        try:
            import corners
            radius = int(corners.radius(self.app.settings["corners"], self.app.window_rounding))
        except Exception:  # noqa: BLE001
            pass
        inner = 0 if radius == 0 else max(2, min(radius - 3, 12))
        css = (
            "flowbox.sm-theme-grid > flowboxchild, flowbox.sm-theme-grid > flowboxchild:selected "
            f"{{ padding: 0; background: none; border-radius: {radius}px; }}\n"
            ".sm-theme-card { background: var(--card-bg-color); padding: 4px; "
            "border: 1px solid color-mix(in srgb, var(--window-fg-color) 14%, transparent); "
            f"border-radius: {radius}px; }}\n"
            "flowboxchild:hover .sm-theme-card, flowboxchild:selected .sm-theme-card { "
            "border-color: var(--accent-color); "
            "background: color-mix(in srgb, var(--accent-color) 12%, var(--card-bg-color)); }\n"
            ".sm-theme-card.sm-current { border: 2px solid var(--accent-color); padding: 3px; }\n"
            f".sm-theme-pic {{ border-radius: {inner}px; }}\n"
            ".sm-theme-badge { background: var(--accent-bg-color); color: var(--accent-fg-color); "
            f"border-radius: {min(radius, 6)}px; padding: 1px 6px; font-size: 0.8em; }}\n"
            ".sm-theme-star { color: var(--accent-color); }\n"
            ".sm-theme-big { background: var(--card-bg-color); padding: 12px; "
            "border: 1px solid color-mix(in srgb, var(--window-fg-color) 14%, transparent); "
            f"border-radius: {radius}px; }}\n"
            ".sm-theme-big.sm-current { border: 2px solid var(--accent-color); padding: 11px; }\n"
            ".sm-theme-swatch { border: 1px solid color-mix(in srgb, var(--window-fg-color) 25%, transparent); "
            f"border-radius: {min(radius, 4)}px; }}\n"
            ".sm-theme-counter { background: rgba(0, 0, 0, 0.55); color: white; border-radius: 999px; "
            "padding: 1px 8px; font-size: 0.8em; }\n"
        )
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_string(css)
        if self.css is not None:
            Gtk.StyleContext.remove_provider_for_display(display, self.css)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        self.css = provider

    # -- loading --------------------------------------------------------------------------------

    def load(self, key: str, gen: int) -> None:
        body = self.content[key]
        if not self.themes:
            clear(body)
            body.append(_loading_group(_("Loading themes…")))
        self._install_css()                 # the Corners setting may have changed

        def done(result):
            if not self.current(key, gen):
                return
            if not isinstance(result, dict) or not isinstance(result.get("themes"), list):
                error = result.get("error") if isinstance(result, dict) else ""
                self._show_problem(body, error or _("Could not read the themes."), key)
                return
            self._render(body, result)

        run_async(self.app, theme_core.list_themes, done)

    def _show_problem(self, body: Gtk.Box, text: str, key: str):
        self._drop_cards()
        self.themes = {}
        clear(body)
        self.big_bin.set_visible(False)
        self.hint.set_visible(False)
        group = Adw.PreferencesGroup()
        row = Adw.ActionRow(title=escape(text))
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        retry = Gtk.Button(label=_("Try again"), valign=Gtk.Align.CENTER)
        retry.connect("clicked", lambda *_a: self.refresh(key))
        row.add_suffix(retry)
        group.add(row)
        body.append(group)
        self.status.set_text(_("The themes could not be read."))

    def _drop_cards(self):
        for card in self.cards.values():
            card.cancel()
        self.cards = {}
        self.flow = None

    def _render(self, body: Gtk.Box, data: dict):
        self.data = data
        self.flip = bool(data.get("flip", True))
        self.auto_rotate = bool(data.get("auto_rotate", True))
        try:
            self.delay = max(0.0, float(data.get("delay", 2.0) or 0))
        except (TypeError, ValueError):
            self.delay = 2.0
        themes = [t for t in data["themes"] if isinstance(t, dict) and theme_core.valid_slug(t.get("slug"))]
        self.themes = {t["slug"]: t for t in themes}
        self._drop_cards()
        clear(body)

        modes = {t["mode"] for t in themes}
        self.mode_box.set_visible(len(modes) > 1)
        if len(modes) <= 1 and self.mode:
            self.all_btn.set_active(True)
        hidden = sum(1 for t in themes if t.get("hidden"))
        self.hidden_btn.set_visible(hidden > 0)
        self.hidden_btn.set_tooltip_text(ngettext("Show the {0} theme hidden in Theme Manager",
                                                  "Show the {0} themes hidden in Theme Manager", hidden).format(hidden))
        self.more_btn.set_visible(bool(data.get("theme_manager")))

        if not data.get("can_apply"):
            body.append(_note(_("The omarchy command was not found: themes can be looked at here, not applied.")))
        if not themes:
            body.append(_note(_("No themes found in ~/.config/omarchy/themes or /usr/share/omarchy/themes.")))
            self.big_bin.set_visible(False)
            self.hint.set_visible(False)
            self._update_status()
            self._watch()
            return

        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=False,
                           homogeneous=True, min_children_per_line=2, max_children_per_line=6,
                           column_spacing=12, row_spacing=12, valign=Gtk.Align.START)
        flow.add_css_class("sm-theme-grid")
        flow.set_filter_func(self._visible_child)
        for theme in themes:
            card = ThemeCard(self, theme)
            self.cards[theme["slug"]] = card
            flow.append(card.child)
        flow.connect("selected-children-changed", self._on_selection)
        flow.connect("child-activated", lambda _f, child: self.apply(getattr(child, "slug", "")))
        self.flow = flow
        body.append(flow)
        self.empty = label("", ("sm-dim",), xalign=0.5, wrap=True)
        self.empty.set_margin_top(24)
        body.append(self.empty)
        self._ensure_selection(prefer=self.selected)
        self._update_status()
        self._watch()

    # -- selection and filter -------------------------------------------------------------------

    def _visible(self, theme: dict) -> bool:
        if theme.get("hidden") and not self.show_hidden:
            return False
        return not self.mode or theme.get("mode") == self.mode

    def _visible_child(self, child) -> bool:
        theme = self.themes.get(getattr(child, "slug", ""))
        return bool(theme) and self._visible(theme)

    def _set_mode(self, mode: str):
        if mode != self.mode:
            self.mode = mode
            self._refilter()

    def _set_show_hidden(self, on: bool):
        self.show_hidden = on
        self._refilter()

    def _refilter(self):
        if self.flow is None:
            return
        self.flow.invalidate_filter()
        self._ensure_selection(prefer=self.selected)
        self._update_status()

    def _ensure_selection(self, prefer: str = ""):
        """Select prefer if shown, else the theme in use, else the first shown one."""
        shown = [slug for slug, t in self.themes.items() if self._visible(t)]
        self._update_empty(not shown)
        current = self.data.get("current", "")
        for slug in (prefer, current, shown[0] if shown else ""):
            if slug and slug in shown:
                self.select(slug)
                return
        if self.flow is not None:
            self.flow.unselect_all()
        self.big_bin.set_visible(False)
        self.hint.set_visible(False)
        self.big_slug = ""

    def _update_empty(self, empty: bool):
        if self.flow is None:
            return
        self.flow.set_visible(not empty)
        self.empty.set_visible(empty)
        if not empty:
            return
        hidden = sum(1 for t in self.themes.values() if t.get("hidden") and (not self.mode or t["mode"] == self.mode))
        text = {"dark": _("No dark themes shown."), "light": _("No light themes shown.")}.get(self.mode, _("No themes shown."))
        if hidden and not self.show_hidden:
            text += " " + ngettext("{0} is hidden in Theme Manager; the eye button shows it.",
                                   "{0} are hidden in Theme Manager; the eye button shows them.", hidden).format(hidden)
        self.empty.set_text(text)

    def select(self, slug: str):
        card = self.cards.get(slug)
        if card is None or self.flow is None:
            return
        if card.child in self.flow.get_selected_children():
            self._show_big(slug)
        else:
            self.flow.select_child(card.child)       # → _on_selection

    def _on_selection(self, flow):
        children = flow.get_selected_children()
        if children:
            self._show_big(getattr(children[0], "slug", ""))

    # -- big preview ----------------------------------------------------------------------------

    def _show_big(self, slug: str):
        theme = self.themes.get(slug)
        if theme is None:
            return
        self.big_bin.set_visible(True)
        self.hint.set_visible(True)
        self.selected = slug
        if slug != self.big_slug or self.big_swatch_holder.get_first_child() is None:
            self.big_slug = slug
            self._cancel_auto()
            clear(self.big_swatch_holder)
            self.big_swatch_holder.append(_swatch_area(theme.get("colors") or {}))
            self.big_sources = ([theme["preview"]] if theme.get("preview") else []) + list(theme.get("wallpapers") or [])
            self.big_index = 0
            self._show_source(new_theme=True)
            if (self.auto_rotate and self.delay > 0 and len(self.big_sources) > 1 and theme.get("preview")
                    and _animations()):
                self.auto_timer = GLib.timeout_add(int(self.delay * 1000), self._auto_step)
        self._update_big()

    def _update_big(self):
        theme = self.themes.get(self.big_slug)
        if theme is None:
            return
        current = bool(theme.get("current"))
        (self.big_box.add_css_class if current else self.big_box.remove_css_class)("sm-current")
        self.big_star.set_visible(bool(theme.get("favorite")))
        self.big_name.set_text(theme["name"])
        self.big_mode_icon.set_from_icon_name(_mode_icon(theme["mode"]))
        parts = [_("Light") if theme["mode"] == "light" else _("Dark"),
                 _("Your theme") if theme.get("scope") == "user" else _("Stock theme")]
        if theme.get("hidden"):
            parts.append(_("hidden in Theme Manager"))
        self.big_meta.set_text(" · ".join(parts))
        clear(self.swatches)
        colors = theme.get("colors") or {}
        for key in theme_core.SWATCH_KEYS:
            if key in colors:
                self.swatches.append(_swatch(key, colors[key]))
        self.swatches.set_visible(bool(colors))
        walls = len(theme.get("wallpapers") or [])
        self.big_walls.set_text(ngettext("{0} wallpaper (‹ › on the picture)",
                                         "{0} wallpapers (‹ › on the picture)", walls).format(walls) if walls else "")
        self.big_walls.set_visible(bool(walls))
        busy = self.applying == self.big_slug
        self.big_spinner.set_visible(busy)
        self.big_spinner.set_spinning(busy)
        can = bool(self.data.get("can_apply"))
        if current:
            self.apply_content.set_icon_name("object-select-symbolic")
            self.apply_content.set_label(_("In use"))
            self.apply_btn.remove_css_class("suggested-action")
        else:
            self.apply_content.set_icon_name("media-playback-start-symbolic")
            self.apply_content.set_label(_("Applying…") if busy else _("Apply theme"))
            self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_sensitive(can and not current and not self.applying)
        self.apply_btn.set_tooltip_text(None if can else _("The omarchy command was not found."))

    def _show_source(self, new_theme: bool = False):
        count = len(self.big_sources)
        for widget in (self.prev_btn, self.next_btn, self.counter):
            widget.set_visible(count > 1)
        self.counter.set_text(f"{self.big_index + 1} / {count}")
        if not count:
            self.big_stack.set_visible_child_full("swatch", Gtk.StackTransitionType.NONE)
            return
        path = self.big_sources[self.big_index]
        slug, index = self.big_slug, self.big_index

        def show(texture, transition=Gtk.StackTransitionType.CROSSFADE):
            if texture is None or slug != self.big_slug or index != self.big_index:
                return
            back = "b" if self.big_front == "a" else "a"
            self.big_pics[back].set_paintable(texture)
            self.big_front = back
            self.big_stack.set_visible_child_full(back, transition)

        texture = self.thumbs.get(path, BIG_SIZE, show, priority=0)
        if texture is not None:
            show(texture)
            return
        early = self.thumbs.peek(path, CARD_SIZE)    # the card's copy at once; the sharp one fades over
        if early is not None:
            show(early, Gtk.StackTransitionType.NONE if new_theme else Gtk.StackTransitionType.CROSSFADE)
        elif new_theme:
            self.big_stack.set_visible_child_full("swatch", Gtk.StackTransitionType.NONE)

    def _step(self, step: int, user: bool = False):
        if user:
            self._cancel_auto()
        count = len(self.big_sources)
        if count > 1:
            self.big_index = (self.big_index + step) % count
            self._show_source()

    def _auto_step(self):
        self.auto_timer = 0
        self._step(1)
        return False

    def _cancel_auto(self):
        if self.auto_timer:
            GLib.source_remove(self.auto_timer)
            self.auto_timer = 0

    # -- status, in use, apply ------------------------------------------------------------------

    def _update_status(self):
        if self.applying:
            name = self.themes.get(self.applying, {}).get("name", self.applying)
            self.status.set_text(_("Applying {0}…").format(name))
            return
        current = self.themes.get(self.data.get("current", ""))
        count = sum(1 for t in self.themes.values() if self._visible(t))
        shown = ngettext("{0} theme", "{0} themes", count).format(count)
        self.status.set_text(_("In use: {0}").format(current["name"]) + " · " + shown if current else shown)

    def _set_current(self, slug: str):
        self.data["current"] = slug
        for theme in self.themes.values():
            theme["current"] = theme["slug"] == slug
            if theme["current"]:
                theme["hidden"] = False
        for card in self.cards.values():
            card.update()
        if self.flow is not None:
            self.flow.invalidate_filter()
        self._update_big()
        self._update_status()

    def apply(self, slug: str):
        theme = self.themes.get(slug)
        if theme is None or self.applying or theme.get("current") or not self.data.get("can_apply"):
            return
        self.applying = slug
        self._cancel_auto()
        self._busy_changed()
        name = theme["name"]

        def done(result):
            self.applying = ""
            if isinstance(result, dict) and result.get("ok"):
                self.app.toast(_("{0} is the theme now").format(name))
            else:
                error = (result.get("error") if isinstance(result, dict) else "") or _("unknown error")
                self.app.toast(_("Could not apply {0}: {1}").format(name, error))
            self._busy_changed()
            self._refresh_current()

        run_async(self.app, lambda: theme_core.apply_theme(slug), done, busy_text=_("Applying {0}…").format(name))

    def _busy_changed(self):
        for card in self.cards.values():
            card.update()
        self._update_big()
        self._update_status()

    def _refresh_current(self):
        run_async(self.app, theme_core.current_slug,
                  lambda slug: self._set_current(slug) if isinstance(slug, str) else None)

    def _watch(self):
        """Follow `omarchy theme set` from elsewhere (menu, switcher, Theme Manager): the mark moves."""
        if self.monitor is not None:
            return
        try:
            folder = Gio.File.new_for_path(str(theme_core.STATE_CURRENT))
            self.monitor = folder.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            self.monitor = None
            return
        self.monitor.connect("changed", self._state_changed)

    def _state_changed(self, _monitor, file, other, _event):
        names = {f.get_basename() for f in (file, other) if f is not None}
        if "theme.name" not in names or self.watch_timer:
            return
        self.watch_timer = GLib.timeout_add(500, self._state_settled)

    def _state_settled(self):
        self.watch_timer = 0
        if not self.applying:
            self._refresh_current()
        return False

    def _open_theme_manager(self):
        def done(result):
            if not (isinstance(result, dict) and result.get("ok")):
                error = (result.get("error") if isinstance(result, dict) else "") or _("unknown error")
                self.app.toast(_("Could not open Theme Manager: {0}").format(error))

        run_async(self.app, theme_core.open_theme_manager, done, busy_text=_("Opening Theme Manager…"))


def _swatch(key: str, color: str) -> Gtk.Widget:
    area = Gtk.DrawingArea(width_request=20, height_request=20, tooltip_text=f"{key} {color}")
    area.add_css_class("sm-theme-swatch")
    area.set_overflow(Gtk.Overflow.HIDDEN)
    rgb = _rgb(color)

    def draw(_w, cr, _width, _height):
        cr.set_source_rgb(*rgb)
        cr.paint()

    area.set_draw_func(draw)
    return area


def _loading_group(text: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(text))
    row.add_prefix(Gtk.Spinner(spinning=True, valign=Gtk.Align.CENTER))
    group.add(row)
    return group


def _note(text: str) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup()
    row = Adw.ActionRow(title=escape(text))
    row.set_title_lines(3)
    row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
    row.add_css_class("sm-dim")
    group.add(row)
    return group
