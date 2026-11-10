import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import actions  # noqa: E402
from .model import CAUTION, SAFE, SYSTEM, Item  # noqa: E402
from .scan import CATEGORY_INFO, Scan  # noqa: E402
from .util import HOME, fmt, run  # noqa: E402

CSS = """
.badge { border-radius: 99px; padding: 2px 9px; font-size: 0.78em; font-weight: 700; }
.badge.safe { background: alpha(@success_color, 0.16); color: @success_color; }
.badge.caution { background: alpha(@warning_color, 0.18); color: @warning_color; }
.badge.system { background: alpha(@view_fg_color, 0.08); color: alpha(@view_fg_color, 0.65); }
.size { font-feature-settings: "tnum"; font-weight: 600; }
listview.items { background: transparent; }
listview.items > row { border-radius: 12px; padding: 7px 10px; margin: 1px 0; }
listview.items > row:hover { background: alpha(@view_fg_color, 0.05); }
.hero { font-size: 2.2em; font-weight: 800; }
.dot { border-radius: 99px; min-width: 10px; min-height: 10px; }
.summary { padding: 4px 6px; }
.sidebar-size { font-size: 0.85em; }
.intro { padding: 0 6px 6px 6px; }
"""

PALETTE = {"System & apps": "#3584e4", "Flatpak": "#9141ac", "Games": "#e66100", "Caches": "#f5c211",
           "Developer": "#2ec27e", "Your files": "#1c71d8", "Other": "#9a9996"}
PALETTE["Your files"] = "#62a0ea"

KIND_ICON = {"rpm": "package-x-generic-symbolic", "flatpak": "application-x-addon-symbolic",
             "runtime": "application-x-addon-symbolic", "appimage": "application-x-executable-symbolic",
             "local": "folder-download-symbolic", "steam": "applications-games-symbolic",
             "cache": "folder-symbolic", "dev": "utilities-terminal-symbolic",
             "leftover": "folder-symbolic", "clean": "user-trash-symbolic"}


def label(text="", css=(), xalign=0.0, wrap=False, ellipsize=False, **kw):
    lb = Gtk.Label(label=text, xalign=xalign, **kw)
    for c in css:
        lb.add_css_class(c)
    if wrap:
        lb.set_wrap(True)
        lb.set_natural_wrap_mode(Gtk.NaturalWrapMode.WORD)
    if ellipsize:
        lb.set_ellipsize(3)  # END
    return lb


def set_icon(img, item):
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    if item.icon is not None:
        if not isinstance(item.icon, Gio.ThemedIcon) or theme.has_gicon(item.icon):
            img.set_from_gicon(item.icon)
            return
    img.set_from_icon_name(item.icon_name or KIND_ICON.get(item.kind, "application-x-executable-symbolic"))


def open_path(path):
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
    except GLib.Error:
        pass


# ---------------------------------------------------------------------------
# List page
# ---------------------------------------------------------------------------

class ItemListPage(Gtk.Box):
    def __init__(self, win, intro, filters, empty_text, extra_button=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.filters = filters
        self.query = ""

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_start=12, margin_end=12)
        top.append(label(intro, css=("dim-label", "intro"), wrap=True))
        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.search.connect("search-changed", self._on_search)
        bar.append(self.search)
        self.filter_dd = Gtk.DropDown.new_from_strings([f[0] for f in filters])
        self.filter_dd.connect("notify::selected", lambda *a: self._refilter())
        bar.append(self.filter_dd)
        self.sort_dd = Gtk.DropDown.new_from_strings(["Largest first", "Name", "Safest to remove first"])
        self.sort_dd.connect("notify::selected", lambda *a: self.sorter.changed(Gtk.SorterChange.DIFFERENT))
        bar.append(self.sort_dd)
        if extra_button:
            eb = Gtk.Button(label=extra_button[0])
            eb.add_css_class("suggested-action")
            eb.connect("clicked", lambda b: extra_button[1]())
            bar.append(eb)
        top.append(bar)
        self.summary = label("", css=("dim-label", "caption", "summary"))
        top.append(self.summary)
        clamp = Adw.Clamp(maximum_size=960, child=top)
        self.append(clamp)

        self.store = Gio.ListStore(item_type=Item)
        self.filter = Gtk.CustomFilter.new(self._match, None)
        self.filtered = Gtk.FilterListModel(model=self.store, filter=self.filter)
        self.sorter = Gtk.CustomSorter.new(self._cmp, None)
        self.sorted = Gtk.SortListModel(model=self.filtered, sorter=self.sorter)
        self.filtered.connect("items-changed", lambda *a: self._update_summary())

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._setup)
        factory.connect("bind", self._bind)
        self.view = Gtk.ListView(model=Gtk.NoSelection(model=self.sorted), factory=factory, single_click_activate=True)
        self.view.add_css_class("items")
        self.view.connect("activate", lambda lv, pos: win.show_details(self.sorted.get_item(pos)))
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(Adw.ClampScrollable(maximum_size=960, child=self.view, margin_start=12, margin_end=12, margin_bottom=12))

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(Adw.StatusPage(icon_name="emblem-ok-symbolic", title="Nothing here", description=empty_text), "empty")
        self.append(self.stack)

    def set_items(self, items):
        # Fresh store each time: items are re-measured between scans, and the list view
        # recycles rows for identical objects (which would leave stale sizes/order).
        self.store = Gio.ListStore(item_type=Item)
        self.store.splice(0, 0, items)
        self.filtered.set_model(self.store)
        self.sorter.changed(Gtk.SorterChange.DIFFERENT)
        self.view.set_model(Gtk.NoSelection(model=self.sorted))
        self._update_summary()

    def _on_search(self, entry):
        self.query = entry.get_text().lower().strip()
        self._refilter()

    def _refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)
        self._update_summary()

    def _match(self, item, _data):
        fn = self.filters[self.filter_dd.get_selected()][1] if self.filters else None
        if fn and not fn(item):
            return False
        if self.query:
            hay = f"{item.title} {item.subtitle} {item.key} {item.badge} {item.category}".lower()
            return all(w in hay for w in self.query.split())
        return True

    def _cmp(self, a, b, _data):
        mode = self.sort_dd.get_selected()
        if mode == 1:
            ka, kb = a.title.lower(), b.title.lower()
        elif mode == 2:
            rank = {SAFE: 0, CAUTION: 1, SYSTEM: 2}
            ka, kb = (rank[a.safety], -a.total), (rank[b.safety], -b.total)
        else:
            ka, kb = -a.total, -b.total
        return Gtk.Ordering.SMALLER if ka < kb else Gtk.Ordering.LARGER if ka > kb else Gtk.Ordering.EQUAL

    def _update_summary(self):
        n = self.filtered.get_n_items()
        total = sum(self.filtered.get_item(i).total for i in range(n))
        self.summary.set_label(f"{n} item{'s' if n != 1 else ''} · {fmt(total)}")
        self.stack.set_visible_child_name("list" if n else "empty")

    def _setup(self, _f, li):
        row = Gtk.Box(spacing=12)
        row.img = Gtk.Image(pixel_size=32)
        row.append(row.img)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        row.title = label(css=("heading",), ellipsize=True)
        row.sub = label(css=("dim-label", "caption"), ellipsize=True)
        texts.append(row.title)
        texts.append(row.sub)
        row.append(texts)
        row.badge = label(css=("badge",), valign=Gtk.Align.CENTER)
        row.append(row.badge)
        row.size = label(css=("size",), xalign=1.0, width_chars=9, valign=Gtk.Align.CENTER)
        row.append(row.size)
        row.btn = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        row.btn.add_css_class("flat")
        row.btn.add_css_class("circular")
        row.btn.connect("clicked", lambda b: self.win.confirm(row.item, row.item.primary))
        row.append(row.btn)
        li.set_child(row)

    def _bind(self, _f, li):
        row, item = li.get_child(), li.get_item()
        row.item = item
        set_icon(row.img, item)
        row.title.set_label(item.title)
        row.sub.set_label(item.subtitle or item.source)
        row.badge.set_label(item.badge)
        for c in (SAFE, CAUTION, SYSTEM):
            row.badge.remove_css_class(c)
        row.badge.add_css_class(item.safety)
        row.size.set_label(fmt(item.total) if item.total else "—")
        can = bool(item.primary) and item.safety != SYSTEM
        row.btn.set_visible(can)
        if can:
            row.btn.set_tooltip_text(item.primary["label"])
            row.btn.set_icon_name("user-trash-symbolic" if item.primary["type"] != "uri" else "external-link-symbolic")


# ---------------------------------------------------------------------------
# Disk usage bar
# ---------------------------------------------------------------------------

class UsageBar(Gtk.DrawingArea):
    def __init__(self):
        super().__init__(content_height=22, hexpand=True)
        self.segments, self.total = [], 1
        self.set_draw_func(self._draw)

    def set_data(self, segments, total):
        self.segments, self.total = segments, max(total, 1)
        self.queue_draw()

    def _draw(self, _a, cr, w, h):
        r = h / 2
        def rounded(x, y, ww, hh):
            import math
            cr.new_sub_path()
            cr.arc(x + ww - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + ww - r, y + hh - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + hh - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()
        fg = self.get_color()
        cr.save()
        rounded(0, 0, w, h)
        cr.clip()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.08)
        cr.paint()
        x = 0
        for _name, b, color in self.segments:
            ww = w * b / self.total
            if ww <= 0:
                continue
            c = Gdk.RGBA()
            c.parse(color)
            cr.set_source_rgba(c.red, c.green, c.blue, 1)
            cr.rectangle(x, 0, ww, h)
            cr.fill()
            x += ww
            cr.set_source_rgba(0, 0, 0, 0)
        cr.restore()


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

PAGES = [
    ("overview", "Overview", "drive-harddisk-symbolic"),
    ("apps", "Apps", "view-app-grid-symbolic"),
    ("games", "Games", "applications-games-symbolic"),
    ("cleanup", "Cleanup", "user-trash-symbolic"),
    ("leftovers", "Leftovers", "edit-find-symbolic"),
    ("dev", "Developer", "utilities-terminal-symbolic"),
    ("system", "System packages", "package-x-generic-symbolic"),
]


class RoomyWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Roomy", default_width=1120, default_height=780)
        self.scan = None
        self.scanning = False

        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        act = Gio.SimpleAction.new("refresh", None)
        act.connect("activate", lambda *a: self.start_scan())
        self.add_action(act)

        # sidebar
        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.side_sizes = {}
        for pid, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            row.pid = pid
            box = Gtk.Box(spacing=12, margin_top=4, margin_bottom=4)
            box.append(Gtk.Image(icon_name=icon))
            box.append(label(title, hexpand=True))
            sz = label("", css=("dim-label", "sidebar-size"))
            self.side_sizes[pid] = sz
            box.append(sz)
            row.set_child(box)
            self.sidebar.append(row)
        self.sidebar.connect("row-activated", self._on_side)
        side_tv = Adw.ToolbarView()
        side_hb = Adw.HeaderBar()
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Scan again (Ctrl+R)", action_name="win.refresh")
        self.spinner = Adw.Spinner(visible=False)
        side_hb.pack_start(self.refresh_btn)
        side_hb.pack_start(self.spinner)
        side_tv.add_top_bar(side_hb)
        side_tv.set_content(Gtk.ScrolledWindow(child=self.sidebar, hscrollbar_policy=Gtk.PolicyType.NEVER))
        self.side_page = Adw.NavigationPage(title="Roomy", child=side_tv)

        # content
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Adw.StatusPage(title="Looking at your disk…", description="Reading installed apps and packages")
        loading.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.loading = loading
        self.stack.add_named(loading, "loading")
        self.overview_holder = Adw.Bin()
        self.stack.add_named(self.overview_holder, "overview")

        def is_app(i): return i.kind == "rpm"
        self.pages = {
            "apps": ItemListPage(self, "Everything you can open from the app grid, plus tools you installed yourself. "
                                       "Sizes include each app's settings, data and cache in your home folder.",
                                 [("All apps", None), ("Flatpak", lambda i: i.kind == "flatpak"),
                                  ("Fedora packages", is_app), ("AppImages & manual", lambda i: i.kind in ("appimage", "local")),
                                  ("Installed by you", lambda i: i.badge in ("You installed", "Flatpak", "AppImage", "Manual")),
                                  ("Came with Fedora", lambda i: i.badge == "Came with Fedora"),
                                  ("Safe to remove", lambda i: i.safety == SAFE)],
                                 "No apps match."),
            "games": ItemListPage(self, "Steam games and the tools Steam uses to run them. Uninstalling opens Steam so your cloud saves stay safe.",
                                  [("All", None), ("Games", lambda i: i.category == "Game"), ("Steam tools (Proton…)", lambda i: i.category == "Tool")],
                                  "No Steam games found."),
            "cleanup": ItemListPage(self, "Caches and junk that are safe to clear — apps rebuild caches automatically when needed.",
                                    [("All", None), ("System", lambda i: i.kind == "clean"), ("App caches", lambda i: i.kind == "cache")],
                                    "Nothing to clean. Nice and tidy!", extra_button=("Clear all caches", self.clear_all_caches)),
            "leftovers": ItemListPage(self, "Folders left behind by apps and games you already uninstalled. "
                                            "Items marked “Probably leftover” are a best guess — have a look first. Removing moves them to the Trash.",
                                      [("All", None), ("Uninstalled Flatpaks", lambda i: i.category == "Uninstalled Flatpak"),
                                       ("Uninstalled games", lambda i: i.category == "Uninstalled game"),
                                       ("Possible leftovers", lambda i: i.category == "Possible leftover")],
                                      "No leftovers found."),
            "dev": ItemListPage(self, "Package caches, SDKs and AI models used by developer tools. Caches are re-downloaded when needed.",
                                [("All", None), ("Safe to clear", lambda i: i.safety == SAFE)], "No developer caches found."),
            "system": ItemListPage(self, "The building blocks of Fedora (libraries, drivers, fonts…). Most of these are needed — "
                                         "only items marked “Unused” are clearly safe. Click any one to see what it is.",
                                   [("All", None), ("Unused (safe)", lambda i: i.badge == "Unused")]
                                   + [(c, (lambda c: lambda i: i.category == c)(c)) for c in CATEGORY_INFO],
                                   "No packages match."),
        }
        for pid, page in self.pages.items():
            self.stack.add_named(page, pid)

        self.content_tv = Adw.ToolbarView()
        self.content_hb = Adw.HeaderBar()
        self.content_tv.add_top_bar(self.content_hb)
        self.content_tv.set_content(self.stack)
        self.content_page = Adw.NavigationPage(title="Overview", child=self.content_tv)

        self.split = Adw.NavigationSplitView(sidebar=self.side_page, content=self.content_page,
                                             min_sidebar_width=220, max_sidebar_width=280)
        self.toasts = Adw.ToastOverlay(child=self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self.sidebar.select_row(self.sidebar.get_row_at_index(0))
        self.current = "overview"
        self.start_scan()

    # ---- navigation ------------------------------------------------------
    def _on_side(self, _lb, row):
        self.go(row.pid)

    def go(self, pid):
        self.current = pid
        title = dict((p[0], p[1]) for p in PAGES)[pid]
        self.content_page.set_title(title)
        for i, p in enumerate(PAGES):
            if p[0] == pid:
                self.sidebar.select_row(self.sidebar.get_row_at_index(i))
        if self.scan is not None:
            self.stack.set_visible_child_name(pid)
        self.split.set_show_content(True)

    def toast(self, text, timeout=4):
        t = Adw.Toast.new(text)
        t.set_timeout(timeout)
        self.toasts.add_toast(t)

    # ---- scanning --------------------------------------------------------
    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.spinner.set_visible(True)
        self.refresh_btn.set_visible(False)
        scan = Scan(progress=lambda m: GLib.idle_add(self.loading.set_description, m))

        def work():
            try:
                scan.quick()
                GLib.idle_add(self.populate, scan)
                scan.deep()
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.toast, f"Scan problem: {e}")
            GLib.idle_add(self.populate, scan)
            GLib.idle_add(self._scan_done)
        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self):
        self.scanning = False
        self.spinner.set_visible(False)
        self.refresh_btn.set_visible(True)

    def populate(self, scan):
        self.scan = scan
        lists = {"apps": scan.apps, "games": scan.games, "cleanup": scan.cleanup, "leftovers": scan.leftovers,
                 "dev": scan.dev, "system": scan.system}
        for pid, items in lists.items():
            self.pages[pid].set_items(items)
            self.side_sizes[pid].set_label(fmt(sum(i.total for i in items)) if items or scan.deep_done else "…")
        self.side_sizes["overview"].set_label(fmt(scan.stats.get("disk_free", 0)) + " free")
        self.overview_holder.set_child(self.build_overview(scan))
        self.stack.set_visible_child_name(self.current)

    # ---- overview --------------------------------------------------------
    def build_overview(self, scan):
        st = scan.stats
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24, margin_top=24, margin_bottom=24, margin_start=16, margin_end=16)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        card.append(inner)
        used, total, free = st.get("disk_used", 0), st.get("disk_total", 0), st.get("disk_free", 0)
        head = Gtk.Box(spacing=12)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        left.append(label(fmt(free) + " free", css=("hero",)))
        left.append(label(f"{fmt(used)} used of {fmt(total)}", css=("dim-label",)))
        head.append(left)
        if not scan.deep_done:
            sp = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
            sp.append(Adw.Spinner())
            sp.append(label("Measuring folders…", css=("dim-label",)))
            head.append(sp)
        inner.append(head)

        segs = [("System & apps", st.get("rpm_total", 0)),
                ("Flatpak", st.get("flatpak_apps", 0) + st.get("flatpak_runtimes", 0)),
                ("Games", sum(g.total for g in scan.games)),
                ("Caches", st.get("cache_total", 0)),
                ("Developer", st.get("dev_total", 0)),
                ("Your files", st.get("home_files", 0))]
        counted = sum(b for _n, b in segs)
        segs.append(("Other", max(used - counted, 0)))
        bar = UsageBar()
        bar.set_data([(n, b, PALETTE[n]) for n, b in segs], total)
        inner.append(bar)
        legend = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, min_children_per_line=2,
                             column_spacing=16, row_spacing=6, homogeneous=True)
        for n, b in segs:
            if not b:
                continue
            it = Gtk.Box(spacing=8)
            dot = Gtk.Box(valign=Gtk.Align.CENTER)
            dot.add_css_class("dot")
            prov = Gtk.CssProvider()
            prov.load_from_string(f"box {{ background: {PALETTE[n]}; }}")
            dot.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            it.append(dot)
            it.append(label(n, hexpand=True))
            it.append(label(fmt(b), css=("dim-label", "size")))
            legend.append(it)
        inner.append(legend)
        box.append(card)

        # what you can free
        g = Adw.PreferencesGroup(title="Space you can free")
        rows = [("cleanup", "Caches and system junk", "Safe to clear — rebuilt automatically", scan.cleanup),
                ("leftovers", "Leftovers from removed apps", "Folders apps and games left behind", scan.leftovers),
                ("dev", "Developer caches and models", "Package caches, SDKs, AI models", scan.dev)]
        for pid, title, sub, items in rows:
            r = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            r.add_suffix(label(fmt(sum(i.total for i in items)) if scan.deep_done else "…", css=("size",)))
            r.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            r.connect("activated", lambda _r, p=pid: self.go(p))
            g.add(r)
        box.append(g)

        # biggest things
        g = Adw.PreferencesGroup(title="Biggest things on your disk",
                                 description="Apps, games and folders using the most space. Click one to see what it is.")
        big = sorted(scan.apps + scan.games + scan.dev + scan.leftovers, key=lambda i: -i.total)[:8]
        for item in big:
            r = Adw.ActionRow(title=GLib.markup_escape_text(item.title), subtitle=GLib.markup_escape_text(item.badge + " · " + (item.source or "")),
                              activatable=True)
            img = Gtk.Image(pixel_size=32)
            set_icon(img, item)
            r.add_prefix(img)
            r.add_suffix(label(fmt(item.total), css=("size",)))
            r.connect("activated", lambda _r, it=item: self.show_details(it))
            g.add(r)
        box.append(g)

        if scan.home_dirs:
            g = Adw.PreferencesGroup(title="Your folders", description="Your own files. Roomy never deletes these — open them to tidy up yourself.")
            for name, path, b in scan.home_dirs[:8]:
                r = Adw.ActionRow(title=GLib.markup_escape_text(name), activatable=True)
                r.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
                r.add_suffix(label(fmt(b), css=("size",)))
                r.add_suffix(Gtk.Image(icon_name="external-link-symbolic"))
                r.connect("activated", lambda _r, p=path: open_path(p))
                g.add(r)
            hidden = st.get("home_hidden", 0)
            if hidden:
                r = Adw.ActionRow(title="Hidden folders (app data, caches, games)",
                                  subtitle="Broken down in Apps, Games, Cleanup and Leftovers")
                r.add_prefix(Gtk.Image(icon_name="view-conceal-symbolic"))
                r.add_suffix(label(fmt(hidden), css=("size",)))
                g.add(r)
            box.append(g)

        clamp = Adw.Clamp(maximum_size=860, child=box)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    # ---- details ---------------------------------------------------------
    def show_details(self, item):
        if item is None:
            return
        dlg = Adw.Dialog(title=item.title, content_width=560, content_height=680)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)
        img = Gtk.Image(pixel_size=72)
        set_icon(img, item)
        head.append(img)
        head.append(label(item.title, css=("title-2",), xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
        if item.subtitle:
            head.append(label(item.subtitle, css=("dim-label",), xalign=0.5, wrap=True, justify=Gtk.Justification.CENTER))
        badges = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        b = label(item.badge, css=("badge", item.safety))
        badges.append(b)
        badges.append(label(item.source, css=("dim-label", "caption")))
        head.append(badges)
        hg = Adw.PreferencesGroup()
        hg.add(head)
        page.add(hg)

        g = Adw.PreferencesGroup(title="What is this?")
        g.add(label(item.note, wrap=True))
        page.add(g)

        g = Adw.PreferencesGroup(title="Space used", description=f"{fmt(item.total)} in total")
        main_label = {"rpm": "Program files", "flatpak": "App", "runtime": "Runtime", "steam": "Game files",
                      "appimage": "AppImage file", "local": "Program folder"}.get(item.kind, "Size")
        r = Adw.ActionRow(title=main_label)
        r.add_suffix(label(fmt(item.size) if item.size else "—", css=("size",)))
        g.add(r)
        for lab, path, size in item.data:
            r = Adw.ActionRow(title=lab, subtitle=GLib.markup_escape_text(path.replace(HOME, "~")), subtitle_lines=2)
            r.add_suffix(label(fmt(size), css=("size",)))
            ob = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open folder")
            ob.add_css_class("flat")
            ob.connect("clicked", lambda _b, p=path: open_path(p))
            r.add_suffix(ob)
            g.add(r)
        page.add(g)

        if item.meta:
            g = Adw.PreferencesGroup(title="Details")
            for k, v in item.meta.items():
                r = Adw.ActionRow(title=k, subtitle=GLib.markup_escape_text(str(v)), subtitle_selectable=True)
                r.add_css_class("property")
                if str(v).startswith("https://"):
                    ob = Gtk.Button(icon_name="external-link-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open in browser")
                    ob.add_css_class("flat")
                    ob.connect("clicked", lambda _b, u=str(v): Gio.AppInfo.launch_default_for_uri(u, None))
                    r.add_suffix(ob)
                if k in ("Folder", "Location", "File") and os.path.exists(str(v)):
                    ob = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Open")
                    ob.add_css_class("flat")
                    target = str(v) if os.path.isdir(str(v)) else os.path.dirname(str(v))
                    ob.connect("clicked", lambda _b, p=target: open_path(p))
                    r.add_suffix(ob)
                g.add(r)
            if item.kind == "rpm":
                needs = Adw.ButtonRow(title="Show what needs this package")
                needs.connect("activated", lambda br: self._load_requires(item, g, br))
                g.add(needs)
            page.add(g)

        if item.actions and item.safety != SYSTEM:
            g = Adw.PreferencesGroup()
            for a in item.actions:
                br = Adw.ButtonRow(title=a["label"])
                if a.get("destructive"):
                    br.add_css_class("destructive-action")
                br.connect("activated", lambda _b, a=a: (dlg.close(), self.confirm(item, a)))
                g.add(br)
            page.add(g)
        elif item.safety == SYSTEM:
            g = Adw.PreferencesGroup()
            g.add(label("🔒  Roomy won't remove this — it's needed for your system to work.", css=("dim-label",), wrap=True, xalign=0.5))
            page.add(g)

        tv.set_content(page)
        dlg.set_child(tv)
        dlg.present(self)

    def _load_requires(self, item, group, button):
        button.set_sensitive(False)
        button.set_title("Checking…")
        name = item.pkg["name"]

        def work():
            out = run(["dnf", "repoquery", "--installed", "--whatrequires", name, "--qf", "%{name}\n"])
            names = sorted({n for n in out.split() if n != name})
            GLib.idle_add(done, names)

        def done(names):
            group.remove(button)
            if not names:
                r = Adw.ActionRow(title="Nothing else needs it", subtitle="Removing it won't take other software with it.")
            else:
                r = Adw.ExpanderRow(title=f"Needed by {len(names)} package{'s' if len(names) > 1 else ''}",
                                    subtitle=GLib.markup_escape_text(", ".join(names[:4]) + ("…" if len(names) > 4 else "")))
                for n in names[:200]:
                    r.add_row(Adw.ActionRow(title=GLib.markup_escape_text(n)))
            group.add(r)
        threading.Thread(target=work, daemon=True).start()

    # ---- actions ---------------------------------------------------------
    def confirm(self, item, action):
        if not action or item.safety == SYSTEM:
            return
        if action["type"] == "rpm-remove":
            self.toast("Checking what would be removed…", 2)

            def work():
                names, freed, err = actions.rpm_preview(action["pkgs"])
                GLib.idle_add(self._confirm_rpm, item, action, names, freed, err)
            threading.Thread(target=work, daemon=True).start()
            return
        if action["type"] == "uri":
            self.run_action(item, action)
            return
        body = {
            "trash": "It will be moved to the Trash, so you can still restore it.",
            "delete": "This permanently deletes the cached files. Apps will recreate what they need.",
        }.get(action["type"], "")
        if action["type"] == "cmd":
            body = f"Roomy will run:\n<tt>{GLib.markup_escape_text(' '.join(action['argv']))}</tt>"
            if action["argv"][0] == "pkexec":
                body += "\n\nYou'll be asked for your password."
        if action.get("also_trash"):
            body += "\n\nIts data folders will be moved to the Trash."
        size = f" ({fmt(item.total)})" if item.total else ""
        d = Adw.AlertDialog(heading=f"{action['label']} “{item.title}”{size}?", body=body, body_use_markup=True)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", action["label"])
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if action.get("destructive") or item.safety != SAFE
                                  else Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "ok" and self.run_action(item, action))
        d.present(self)

    def _confirm_rpm(self, item, action, names, freed, err):
        if err:
            d = Adw.AlertDialog(heading="Can't remove this", body=err)
            d.add_response("ok", "OK")
            d.present(self)
            return
        extra = [n for s, n in names if s != "Removing"]
        dependents = [n for s, n in names if s == "Removing dependent packages"]
        body = f"{len(names)} package{'s' if len(names) > 1 else ''} will be removed"
        body += f", freeing {freed}." if freed else "."
        if dependents:
            body += f"\n\n⚠ {len(dependents)} other program{'s' if len(dependents) > 1 else ''} that depend on it will be removed too."
        body += "\nYou'll be asked for your password."
        d = Adw.AlertDialog(heading=f"Uninstall “{item.title}”?", body=body)
        if extra:
            lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            lb.add_css_class("boxed-list")
            for s, n in names:
                r = Adw.ActionRow(title=GLib.markup_escape_text(n), subtitle={"Removing": "Selected", "Removing dependent packages": "Depends on it",
                                                                            "Removing unused dependencies": "No longer needed"}.get(s, s))
                lb.append(r)
            sc = Gtk.ScrolledWindow(child=lb, min_content_height=220, max_content_height=300, propagate_natural_height=True)
            d.set_extra_child(sc)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Uninstall")
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.set_close_response("cancel")
        d.connect("response", lambda _d, r: r == "ok" and self.run_action(item, action))
        d.present(self)

    def clear_all_caches(self):
        items = [i for i in (self.scan.cleanup if self.scan else []) if i.kind == "cache" and i.safety == SAFE and i.primary]
        if not items:
            self.toast("Nothing to clear.")
            return
        total = sum(i.total for i in items)
        d = Adw.AlertDialog(heading=f"Clear {len(items)} app caches ({fmt(total)})?",
                            body="These are temporary files apps rebuild on their own. Close running apps first for best results. "
                                 "System items (logs, kernels, Trash) are left for you to review one by one.")
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Clear caches")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("cancel")
        d.set_close_response("cancel")

        def go(_d, r):
            if r != "ok":
                return
            paths = [p for i in items for p in i.primary["paths"]]
            item = Item("bulk", "cache", "App caches", size=total)
            self.run_action(item, {"type": "delete", "label": "Clear caches", "paths": paths, "contents_only": True})
        d.connect("response", go)
        d.present(self)

    def run_action(self, item, action):
        self.toast(f"{action['label']}: {item.title}…", 2)

        def work():
            ok, msg = actions.execute(action)
            GLib.idle_add(done, ok, msg)

        def done(ok, msg):
            self.toast(f"{item.title}: {msg}" if ok else f"Couldn't finish: {msg}", 5)
            if ok and action["type"] != "uri":
                self.start_scan()
        threading.Thread(target=work, daemon=True).start()


class RoomyApp(Adw.Application):
    def __init__(self):
        from . import APP_ID
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.set_accels_for_action("win.refresh", ["<Control>r", "F5"])
        self.set_accels_for_action("window.close", ["<Control>w", "<Control>q"])

    def do_activate(self):
        win = self.get_active_window() or RoomyWindow(self)
        win.present()


def main():
    import sys
    return RoomyApp().run(sys.argv)
