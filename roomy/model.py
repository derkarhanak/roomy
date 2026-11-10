from gi.repository import GObject

SAFE, CAUTION, SYSTEM = "safe", "caution", "system"


class Item(GObject.Object):
    """One thing that uses disk space (an app, a game, a cache, a leftover…)."""

    def __init__(self, key, kind, title, **kw):
        super().__init__()
        self.key = key
        self.kind = kind              # rpm | flatpak | runtime | appimage | local | steam | cache | dev | leftover | clean
        self.title = title
        self.subtitle = kw.get("subtitle", "")
        self.icon = kw.get("icon")     # Gio.Icon or None
        self.icon_name = kw.get("icon_name", "application-x-executable-symbolic")
        self.size = kw.get("size", 0)             # program / main size in bytes
        self.data = kw.get("data", [])            # list of [label, path, bytes]
        self.source = kw.get("source", "")
        self.category = kw.get("category", "")
        self.safety = kw.get("safety", CAUTION)
        self.badge = kw.get("badge", "")
        self.note = kw.get("note", "")            # plain-language explanation
        self.meta = kw.get("meta", {})            # extra key -> value rows
        self.actions = kw.get("actions", [])      # list of action dicts
        self.tokens = kw.get("tokens", set())
        self.path = kw.get("path")

    @property
    def data_size(self):
        return sum(d[2] for d in self.data)

    @property
    def total(self):
        return self.size + self.data_size

    @property
    def primary(self):
        return self.actions[0] if self.actions else None
