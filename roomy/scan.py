"""Everything that discovers what uses disk space. Runs in a worker thread."""
import glob
import json
import os
import re
import shlex
import time
from concurrent.futures import ThreadPoolExecutor

from gi.repository import Gio

from .model import CAUTION, SAFE, SYSTEM, Item
from .util import HOME, du_children, du_paths, norm, parse_size, run, ago

J = os.path.join
CFG, SHARE, CACHE, VARAPP = J(HOME, ".config"), J(HOME, ".local/share"), J(HOME, ".cache"), J(HOME, ".var/app")

# ---------------------------------------------------------------------------
# Plain-language knowledge
# ---------------------------------------------------------------------------

CATEGORY_INFO = {
    "Core system": "Core of Fedora itself (boot, login, package manager, system services). Never remove.",
    "Kernel": "The Linux kernel and its modules — the heart of the OS. Old versions can be cleaned up from Cleanup.",
    "Hardware firmware": "Small programs loaded into your hardware (Wi-Fi, GPU, Bluetooth…). Removing one can make a device stop working.",
    "Graphics & drivers": "Graphics drivers and GPU libraries. Games, video and the desktop need these.",
    "Fonts": "A font family. Removing it is harmless unless a document or app uses it.",
    "Languages & spelling": "Translations, dictionaries and spell-check data for a language.",
    "Libraries": "Shared code that other programs use. Usually installed automatically — let dnf decide if it's still needed.",
    "Programming languages": "A programming language runtime or one of its modules. Many system tools are written in Python/Perl.",
    "Developer tools": "Compilers, headers and build tools. Only needed if you build software.",
    "GNOME desktop": "Part of the GNOME desktop you are using.",
    "Network & Bluetooth": "Networking, VPN, mobile-broadband or Bluetooth support.",
    "Sound & media": "Audio system, codecs or media framework.",
    "Printing & scanning": "Printer and scanner support. Safe to drop if you never print or scan.",
    "Documentation": "Manual pages and documentation.",
    "Virtualization & containers": "Virtual machines / containers support.",
    "Other system tools": "A system utility that came with Fedora or was pulled in by something else.",
    "Flatpak runtimes": "A shared base that Flatpak apps run on. Flatpak removes unused ones for you.",
}

CORE_PATTERNS = re.compile(
    r"^(systemd.*|dnf.*|libdnf.*|rpm.*|glibc.*|bash|coreutils.*|util-linux.*|grub2.*|shim.*|selinux-policy.*|"
    r"dbus.*|polkit.*|sudo|filesystem|setup|kmod.*|dracut.*|plymouth.*|fedora-.*|gnome-shell.*|mutter.*|gdm|"
    r"pam.*|shadow-utils|openssl.*|ca-certificates|crypto-policies.*|efi.*|e2fsprogs.*|btrfs-progs|xfsprogs|"
    r"NetworkManager|NetworkManager-libnm|NetworkManager-wifi|pipewire.*|wireplumber.*|xdg-desktop-portal.*|"
    r"gnome-session.*|gnome-settings-daemon|linux-firmware.*|mesa-dri-drivers|mesa-vulkan-drivers|mesa-libGL.*|"
    r"mesa-libEGL.*|basesystem|tzdata.*|zram-generator.*|audit.*|firewalld.*|sssd.*|authselect.*|"
    r"xorg-x11-server-Xwayland|flatpak|ostree.*|bubblewrap|nautilus|gnome-control-center.*|ptyxis|gnome-software|"
    r"PackageKit|PackageKit-glib|accountsservice.*|upower.*|udisks2.*|gvfs.*|tracker.*|localsearch.*|tinysparql.*)$"
)

CORE_APPS = {"nautilus", "gnome-control-center", "ptyxis", "gnome-software", "gnome-shell", "gnome-terminal",
             "gnome-system-monitor", "gnome-disk-utility", "yelp", "gnome-tour", "gnome-initial-setup"}


def rpm_category(name):
    n = name.lower()
    rules = [
        (r"^kernel", "Kernel"),
        (r"firmware|^microcode", "Hardware firmware"),
        (r"fonts?(-|$)|-fonts", "Fonts"),
        (r"langpack|-l10n|^hunspell|^hyphen|^mythes|^autocorr|^glibc-langpack|^words$", "Languages & spelling"),
        (r"^mesa|nvidia|^xorg-x11-drv|^vulkan|^libva|^libvdpau|-drivers?$|^intel-|^amd-", "Graphics & drivers"),
        (r"^(python|perl|ruby|nodejs|golang|rust|java|lua|php|tcl|ghc)", "Programming languages"),
        (r"-devel$|^gcc|^cmake|^make$|^git|^gdb|^binutils|^autoconf|^automake|^libtool|^meson|^ninja", "Developer tools"),
        (r"^(podman|container|buildah|skopeo|crun|runc|qemu|libvirt|virt-|toolbox|passt|netavark|aardvark)", "Virtualization & containers"),
        (r"^(networkmanager|modemmanager|wpa_supplicant|bluez|iw|avahi|dnsmasq|openvpn|openconnect|vpnc|ppp|"
         r"wireless|iproute|iputils|net-tools|nmap|mtr|ethtool|usb_modeswitch|mobile-broadband)", "Network & Bluetooth"),
        (r"^(pipewire|pulseaudio|alsa|wireplumber|gstreamer|ffmpeg|libav|sound|speex|opus|flac|lame|x26)", "Sound & media"),
        (r"^(cups|hplip|gutenprint|sane|printer|foomatic|ghostscript|ipp|system-config-printer)|printer", "Printing & scanning"),
        (r"-docs?$|^man-|-help$|^info$", "Documentation"),
        (r"^(lib|.*-libs?$|.*-common$|.*-data$)", "Libraries"),
        (r"^(gnome|gtk|glib|gdk|gsettings|gjs|gvfs|evolution|tracker|mutter|gdm|adwaita|yelp|xdg-)|-gnome$", "GNOME desktop"),
    ]
    if CORE_PATTERNS.match(name):
        return "Core system"
    for pat, cat in rules:
        if re.search(pat, n):
            return cat
    return "Other system tools"


CACHE_INFO = {
    "mesa_shader_cache": "Graphics shader cache (Mesa). Rebuilt automatically — games may stutter briefly the first time.",
    "mesa_shader_cache_db": "Graphics shader cache (Mesa). Rebuilt automatically.",
    "nvidia": "NVIDIA graphics shader cache. Rebuilt automatically.",
    "thumbnails": "Picture and video previews shown in Files. Recreated when you browse folders.",
    "fontconfig": "Font lookup cache. Recreated instantly.",
    "pip": "Python pip download cache.",
    "uv": "Python uv package cache.",
    "yarn": "Yarn (JavaScript) package cache.",
    "pnpm": "pnpm (JavaScript) package cache.",
    "go-build": "Go compiler build cache.",
    "huggingface": "Downloaded AI models from Hugging Face. Clearing means re-downloading them.",
    "ms-playwright": "Browsers downloaded by Playwright for testing.",
    "mozilla": "Firefox web cache.",
    "google-chrome": "Chrome web cache.",
    "chromium": "Chromium web cache.",
    "BraveSoftware": "Brave browser web cache.",
    "gnome-software": "GNOME Software app-store cache.",
    "tracker3": "Desktop search index cache.",
    "libdnf5": "Package metadata cache.",
    "JetBrains": "JetBrains IDE caches and indexes.",
    "electron": "Electron framework downloads.",
    "node-gyp": "Native Node.js module build headers.",
    "typescript": "TypeScript type cache.",
    "gstreamer-1.0": "Media framework plugin registry.",
    "flatpak": "Flatpak download/temp cache.",
    "lutris": "Lutris game launcher cache.",
    "spotify": "Spotify offline/streaming cache.",
    "deno": "Deno module cache.",
    "bun": "Bun package cache.",
    "Microsoft": "Microsoft apps (VS Code, Edge…) cache.",
    "evolution": "Mail/calendar cache.",
    "keepassxc": "KeePassXC small state cache.",
    "qBittorrent": "qBittorrent cache.",
    "matplotlib": "Matplotlib font cache.",
    "Google": "Google apps cache (e.g. Android Studio).",
}

# Folder names in ~/.config / ~/.local/share that belong to the desktop itself.
SYSTEM_DIRS = {norm(x) for x in """
dconf gtk-2.0 gtk-3.0 gtk-4.0 autostart systemd environment.d pulse user-dirs.dirs user-dirs.locale mimeapps.list
monitors.xml gnome-session goa-1.0 ibus keyrings recently-used.xbel Trash applications icons fonts flatpak containers
gvfs-metadata mime desktop-directories sounds themes backgrounds gnome-shell evolution tracker3 xdg-desktop-portal
nautilus gnome-control-center gnome-initial-setup-done menus session wireplumber pipewire fish nvim zeitgeist webkitgtk
geoclue gnome-settings-daemon abrt fontconfig gnome-remote-desktop gnome-boxes libvirt nemo localsearch tinysparql
org.gnome.TextEditor org.gnome.Ptyxis gnome-background-properties pki xorg mc htop git gh vlc kdeconnect
gsconnect gnome-builder totem rhythmbox epiphany yelp gnome-online-accounts Kvantum qt5ct qt6ct kdeglobals
color-schemes kio baloo kded6 plasma-localerc trolltech.conf QtProject.conf user-places.xbel recently-used
gnome-calculator gnome-weather gnome-maps gnome-contacts gnome-photos gnome-calendar gnome-clocks snapshot loupe
""".split()}

DEV_SPOTS = [
    # (title, description, paths, command, safety)
    ("npm cache", "Downloaded JavaScript packages. npm re-downloads what it needs.", ["~/.npm/_cacache"], ["npm", "cache", "clean", "--force"], SAFE),
    ("Cargo registry", "Downloaded Rust crates and their sources. Rebuilt on next build.", ["~/.cargo/registry"], None, SAFE),
    ("Cargo git checkouts", "Rust dependencies fetched from git.", ["~/.cargo/git"], None, SAFE),
    ("Rust toolchains", "Installed Rust compilers (rustup). Remove old ones with `rustup toolchain uninstall`.", ["~/.rustup/toolchains"], None, CAUTION),
    ("pip cache", "Downloaded Python packages.", ["~/.cache/pip"], None, SAFE),
    ("uv cache", "Python uv package cache.", ["~/.cache/uv"], None, SAFE),
    ("Go module cache", "Downloaded Go modules.", ["~/go/pkg/mod"], ["go", "clean", "-modcache"], SAFE),
    ("Go build cache", "Go compiler cache.", ["~/.cache/go-build"], None, SAFE),
    ("Gradle caches", "Gradle/Android build caches and wrappers.", ["~/.gradle/caches", "~/.gradle/wrapper"], None, SAFE),
    ("Maven repository", "Downloaded Java libraries.", ["~/.m2/repository"], None, SAFE),
    ("Android SDK", "Android SDK, emulators and system images. Needed by Android Studio.", ["~/Android/Sdk"], None, CAUTION),
    ("Android emulators (AVDs)", "Virtual Android devices.", ["~/.android/avd"], None, CAUTION),
    ("Hugging Face models", "Downloaded AI models. Deleting means re-downloading.", ["~/.cache/huggingface"], None, CAUTION),
    ("LM Studio models", "Local AI models downloaded in LM Studio.", ["~/.lmstudio/models", "~/.cache/lm-studio/models"], None, CAUTION),
    ("Ollama models", "Local AI models pulled with Ollama.", ["~/.ollama/models"], None, CAUTION),
    ("Conda / Miniconda", "Python environments and packages from Conda.", ["~/miniconda3", "~/anaconda3", "~/.conda"], None, CAUTION),
    ("Python user packages", "Packages installed with `pip install --user`.", glob.glob(os.path.expanduser("~/.local/lib/python3*")), None, CAUTION),
    ("Yarn cache", "Yarn package cache.", ["~/.cache/yarn"], None, SAFE),
    ("pnpm store", "pnpm content-addressed package store.", ["~/.local/share/pnpm/store", "~/.cache/pnpm"], ["pnpm", "store", "prune"], SAFE),
    ("Bun cache", "Bun package cache.", ["~/.bun/install/cache"], None, SAFE),
    ("Deno cache", "Deno module cache.", ["~/.cache/deno"], None, SAFE),
    ("Playwright browsers", "Browsers downloaded for automated testing.", ["~/.cache/ms-playwright"], None, SAFE),
    ("JetBrains caches", "IDE indexes and caches. Rebuilt when you open a project.", ["~/.cache/JetBrains"], None, SAFE),
    ("VS Code extensions", "Extensions installed in VS Code.", ["~/.vscode/extensions"], None, CAUTION),
    ("Toolbox / distrobox containers", "Podman containers and images (toolbox, distrobox…).", ["~/.local/share/containers"], None, CAUTION),
    ("Electron downloads", "Cached Electron builds.", ["~/.cache/electron"], None, SAFE),
    ("node-gyp headers", "Node.js headers for building native modules.", ["~/.cache/node-gyp"], None, SAFE),
]


def act_delete(paths, label="Clear", contents_only=True, safety=SAFE):
    return {"type": "delete", "label": label, "paths": paths, "contents_only": contents_only,
            "destructive": safety != SAFE}


def act_trash(paths, label="Move to Trash"):
    return {"type": "trash", "label": label, "paths": paths, "destructive": True}


def dai_file(path):
    try:
        return Gio.DesktopAppInfo.new_from_filename(path)
    except TypeError:
        return None


def dai_id(desktop_id):
    try:
        return Gio.DesktopAppInfo.new(desktop_id)
    except TypeError:
        return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scan:
    def __init__(self, progress=lambda msg: None):
        self.progress = progress
        self.apps, self.games, self.system, self.cleanup, self.dev, self.leftovers = [], [], [], [], [], []
        self.home_dirs = []      # (name, path, bytes)
        self.stats = {}
        self.known = set()       # normalized tokens of everything installed
        self.flatpak_ids = set()
        self.steam_ids = {}
        self.steam_libs = []
        self.deep_done = False
        self.install_roots = set()

    # ---- phase 1: fast (package databases) --------------------------------
    def quick(self):
        self.progress("Reading installed packages…")
        self._rpm()
        self.progress("Reading Flatpak apps…")
        self._flatpak()
        self.progress("Looking for AppImages and local apps…")
        self._local_apps()
        self.progress("Reading Steam library…")
        self._steam()
        self._binaries()
        st = os.statvfs("/")
        self.stats["disk_total"] = st.f_blocks * st.f_frsize
        self.stats["disk_free"] = st.f_bavail * st.f_frsize
        self.stats["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize

    # ---- phase 2: measure folders -----------------------------------------
    def deep(self):
        self.progress("Measuring folders…")
        roots = [CFG, SHARE, CACHE, VARAPP, HOME]
        for lib in self.steam_libs:
            roots += [J(lib, "steamapps/compatdata"), J(lib, "steamapps/shadercache")]
        dev_paths = []
        for _t, _d, paths, _c, _s in DEV_SPOTS:
            dev_paths += [os.path.expanduser(p) for p in paths]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {r: ex.submit(du_children, r) for r in roots}
            fdev = ex.submit(du_paths, dev_paths)
            fsys = ex.submit(self._system_cleanup_sizes)
            du = {r: f.result() for r, f in futs.items()}
            dev_sizes = fdev.result()
            sys_sizes = fsys.result()
        self._attach_data(du)
        self._caches(du.get(CACHE, {}), dev_paths)
        self._dev(dev_sizes)
        self._system_cleanup(sys_sizes, du)
        self._leftovers(du)
        self._steam_orphans()
        self._home(du.get(HOME, {}))
        self.deep_done = True

    # ---- rpm ----------------------------------------------------------------
    def _rpm(self):
        out = run(["rpm", "-qa", "--qf", "%{NAME}\t%{ARCH}\t%{SIZE}\t%{VERSION}-%{RELEASE}\t%{INSTALLTIME}\t%{SUMMARY}\n"])
        pkgs = {}
        for line in out.splitlines():
            parts = line.split("\t", 5)
            if len(parts) < 6 or parts[0] == "gpg-pubkey":
                continue
            name, arch, size, ver, itime, summary = parts
            pkgs[f"{name}.{arch}"] = dict(name=name, arch=arch, size=int(size or 0), ver=ver,
                                          itime=int(itime or 0), summary=summary)
            self.known.add(norm(name))
        reasons = {}
        for line in run(["dnf", "repoquery", "--installed", "--qf", "%{name}.%{arch}\t%{reason}\n"]).splitlines():
            k, _, r = line.partition("\t")
            reasons[k] = r.strip()
        unneeded = set(run(["dnf", "repoquery", "--unneeded", "--qf", "%{name}.%{arch}\n"]).split())
        baseline = self._baseline()
        protected = set()
        for f in glob.glob("/etc/dnf/protected.d/*.conf"):
            try:
                protected |= {l.strip() for l in open(f) if l.strip() and not l.startswith("#")}
            except OSError:
                pass
        kernel_rel = os.uname().release  # e.g. 7.2.15-300.fc45.x86_64
        old_kernels = set(run(["dnf", "repoquery", "--installonly", "--latest-limit=-1", "-q",
                               "--qf", "%{name}.%{arch}\t%{version}-%{release}.%{arch}\n"]).splitlines())
        self.old_kernel_pkgs = []
        for line in old_kernels:
            k, _, vr = line.partition("\t")
            if vr and vr != kernel_rel:
                self.old_kernel_pkgs.append(f"{k.rsplit('.', 1)[0]}-{vr}")
        self.old_kernel_bytes = 0
        self.unneeded_bytes = sum(pkgs[k]["size"] for k in unneeded if k in pkgs)
        self.unneeded = unneeded

        # desktop files -> owning package
        desktop_files = sorted(glob.glob("/usr/share/applications/*.desktop"))
        owners = run(["rpm", "-qf", "--qf", "%{NAME}.%{ARCH}\n", *desktop_files]).splitlines() if desktop_files else []
        apps_by_pkg = {}
        for f, owner in zip(desktop_files, owners):
            if owner not in pkgs:
                continue
            info = dai_file(f)
            if not info or info.get_nodisplay() or info.get_is_hidden():
                continue
            prev = apps_by_pkg.get(owner)
            stem = os.path.basename(f)[:-8].lower()
            if prev is None or pkgs[owner]["name"].lower() in stem:
                apps_by_pkg[owner] = info

        for key, p in pkgs.items():
            name = p["name"]
            reason = reasons.get(key, "")
            info = apps_by_pkg.get(key)
            cat = rpm_category(name)
            locked = (name in protected or cat == "Core system" or name in CORE_APPS
                      or (name.startswith("kernel") and kernel_rel in f"{p['ver']}.{p['arch']}"))
            from_fedora = name in baseline or reason == "Group"
            if locked:
                safety, badge, note = SYSTEM, "System", "Core part of Fedora. Removing it could break your computer, so Roomy won't do it."
            elif key in unneeded:
                safety, badge, note = SAFE, "Unused", "Nothing installed needs this anymore. Safe to remove."
            elif reason in ("User", "External User") and not from_fedora:
                safety, badge, note = SAFE, "You installed", "You (or an app store) installed this. Safe to remove if you don't use it."
            elif reason in ("Dependency", "Weak Dependency"):
                safety, badge = CAUTION, "Dependency"
                note = ("Installed because other software needs it. Removing it also removes the software that depends on it."
                        if reason == "Dependency" else "An optional extra other software pulled in. Usually safe, but check what uses it.")
            elif info:
                safety, badge, note = CAUTION, "Came with Fedora", "Preinstalled with Fedora Workstation. Fine to remove if you don't use it."
            else:
                safety, badge, note = CAUTION, "Came with Fedora", "Part of the standard Fedora Workstation install. Leave it unless you know you don't need it."
            if p["arch"] == "i686":
                note += " (32-bit version — usually for Steam/Wine games.)"
            meta = {"Package": key, "Version": p["ver"], "Installed": time.strftime("%d %b %Y", time.localtime(p["itime"])),
                    "Install reason": reason or "unknown"}
            actions = [] if safety == SYSTEM else [{"type": "rpm-remove", "label": "Uninstall", "pkgs": [key], "destructive": True}]
            title = info.get_display_name() if info else name + (" (32-bit)" if p["arch"] == "i686" else "")
            desc = (info.get_description() if info else None) or p["summary"]
            tokens = {norm(name)}
            if info:
                did = (info.get_id() or "")[:-8]
                tokens |= {norm(did), norm(did.split(".")[-1]), norm(info.get_startup_wm_class() or ""),
                           norm(os.path.basename(info.get_executable() or "")), norm(info.get_name())}
                self.known |= tokens
            item = Item(key, "rpm", title, subtitle=desc, icon=info.get_icon() if info else None,
                        icon_name="package-x-generic-symbolic", size=p["size"], source="Fedora package",
                        category=cat, safety=safety, badge=badge,
                        note=note + ("" if info else "\n\n" + CATEGORY_INFO.get(cat, "")),
                        meta=meta, actions=actions, tokens=tokens - {""})
            item.pkg = p
            item.is_app = bool(info)
            if info or (safety == SAFE and badge == "You installed" and cat not in ("Libraries", "Fonts", "Programming languages")):
                if not info:
                    item.source = "Fedora package (no window — command-line or background)"
                self.apps.append(item)
            else:
                self.system.append(item)
            if f"{name}-{p['ver']}.{p['arch']}" in self.old_kernel_pkgs:
                self.old_kernel_bytes += p["size"]
        self.stats["rpm_total"] = sum(p["size"] for p in pkgs.values())

    def _baseline(self):
        """Package names installed when Fedora itself was installed (first day of dnf history)."""
        try:
            hist = json.loads(run(["dnf", "history", "list", "--json"]) or "[]")
        except ValueError:
            return set()
        if not hist:
            return set()
        first = min(t["start_time"] for t in hist)
        ids = [str(t["id"]) for t in hist if t["start_time"] < first + 86400 and t.get("user_id", 0) == 0]
        if not ids:
            return set()
        try:
            info = json.loads(run(["dnf", "history", "info", *ids, "--json"]) or "[]")
        except ValueError:
            return set()
        names = set()
        for t in info:
            for p in t.get("packages", []):
                names.add(p["nevra"].rsplit("-", 2)[0])
        return names

    # ---- flatpak --------------------------------------------------------------
    def _flatpak(self):
        cols = "application,name,description,size,installation,branch,origin,version,runtime"
        used_runtimes = set()
        for line in run(["flatpak", "list", "--app", f"--columns={cols}"]).splitlines():
            f = (line.split("\t") + [""] * 9)[:9]
            app_id, name, desc, size, inst, branch, origin, version, runtime = f
            if not app_id:
                continue
            self.flatpak_ids.add(app_id)
            used_runtimes.add(runtime)
            info = dai_id(app_id + ".desktop")
            tokens = {norm(app_id), norm(app_id.split(".")[-1]), norm(name)}
            self.known |= tokens
            meta = {"App ID": app_id, "Version": version or branch, "From": origin, "Installed for": inst,
                    "Runs on": runtime}
            flag = "--user" if inst == "user" else "--system"
            actions = [
                {"type": "cmd", "label": "Uninstall", "argv": ["flatpak", "uninstall", "-y", "--noninteractive", flag, app_id], "destructive": True},
                {"type": "cmd", "label": "Uninstall and delete its data", "argv": ["flatpak", "uninstall", "-y", "--noninteractive", "--delete-data", flag, app_id], "destructive": True},
            ]
            item = Item("flatpak:" + app_id, "flatpak", name or app_id, subtitle=desc,
                        icon=info.get_icon() if info else None, size=parse_size(size), source="Flatpak",
                        category="Flatpak app", safety=SAFE, badge="Flatpak",
                        note="A sandboxed app from Flathub or another Flatpak remote. Uninstalling is safe and clean.",
                        meta=meta, actions=actions, tokens=tokens)
            item.flatpak_id = app_id
            self.apps.append(item)
        rt_total = 0
        for line in run(["flatpak", "list", "--runtime", "--columns=application,name,size,installation,branch,ref"]).splitlines():
            f = (line.split("\t") + [""] * 6)[:6]
            rid, name, size, inst, branch, ref = f
            if not rid:
                continue
            b = parse_size(size)
            rt_total += b
            self.flatpak_ids.add(rid)
            short = f"{rid}/x86_64/{branch}"
            used = any(short == r for r in used_runtimes)
            item = Item(f"runtime:{inst}:{ref}", "runtime", f"{name or rid} {branch}", subtitle=f"{rid} · {inst}", icon_name="application-x-addon-symbolic",
                        size=b, source="Flatpak runtime", category="Flatpak runtimes",
                        safety=SYSTEM if used else CAUTION, badge="In use" if used else "Shared",
                        note=CATEGORY_INFO["Flatpak runtimes"] + (" At least one of your apps runs on this exact version." if used
                                                                   else " Use “Remove unused Flatpak runtimes” in Cleanup — it only removes ones nothing needs."),
                        meta={"Ref": ref, "Installed for": inst})
            self.system.append(item)
        refs = {}
        for i in self.system:
            if i.kind == "runtime":
                refs.setdefault(i.meta["Ref"], []).append(i)
        user_apps = any(i.meta.get("Installed for") == "user" for i in self.apps if i.kind == "flatpak")
        for dupes in refs.values():
            if len(dupes) > 1:
                for i in dupes:
                    if i.meta.get("Installed for") == "user" and not user_apps:
                        i.safety, i.badge = SAFE, "Duplicate"
                        i.note = ("This runtime is installed twice — once just for you and once for the whole system. "
                                  "None of your apps use the per-user copy, so it's safe to remove.")
                        i.actions = [{"type": "cmd", "label": "Remove duplicate", "destructive": True,
                                      "argv": ["flatpak", "uninstall", "--user", "-y", "--noninteractive", i.meta["Ref"]]}]
        self.stats["flatpak_apps"] = sum(i.size for i in self.apps if i.kind == "flatpak")
        self.stats["flatpak_runtimes"] = rt_total

    # ---- AppImages & hand-installed apps ------------------------------------
    def _local_apps(self):
        local_desktops = {}
        for f in glob.glob(J(SHARE, "applications", "*.desktop")):
            info = dai_file(f)
            if info:
                local_desktops[f] = info
        seen = set()
        for d in [J(HOME, "AppImages"), J(HOME, "Applications"), J(HOME, "Apps"), J(HOME, "Downloads"), HOME, J(HOME, ".local/bin"), J(HOME, "bin")]:
            for f in glob.glob(J(d, "*")):
                if not f.lower().endswith(".appimage") or not os.path.isfile(f) or f in seen:
                    continue
                seen.add(f)
                desk = next((p for p, i in local_desktops.items() if f in (i.get_commandline() or "")), None)
                info = local_desktops.pop(desk) if desk else None
                base = re.sub(r"(?i)\.appimage$", "", os.path.basename(f))
                title = info.get_display_name() if info else base
                tokens = {norm(base), norm(base.split("-")[0].split("_")[0]), norm(title)}
                self.known |= tokens
                paths = [f] + ([desk] if desk else [])
                item = Item("appimage:" + f, "appimage", title, subtitle=(info.get_description() if info else None) or "AppImage app",
                            icon=info.get_icon() if info else None, size=os.path.getsize(f), source="AppImage",
                            category="AppImage", safety=SAFE, badge="AppImage",
                            note="A portable app that is just one file. Removing it moves the file (and its menu shortcut) to the Trash.",
                            meta={"File": f, "Menu shortcut": desk or "none"},
                            actions=[act_trash(paths, "Move to Trash")], tokens=tokens)
                item.path = f
                self.apps.append(item)
        # Hand-installed apps: local .desktop files pointing into $HOME
        for desk, info in local_desktops.items():
            if info.get_nodisplay() or info.get_is_hidden():
                continue
            cmd = info.get_commandline() or ""
            if "steam://" in cmd or "--app-id" in cmd or "wine" in cmd.lower() or "flatpak run" in cmd:
                continue
            try:
                exe = shlex.split(cmd)[0]
            except (ValueError, IndexError):
                continue
            exe = self._unwrap(os.path.realpath(os.path.expanduser(exe)))
            if not exe.startswith(HOME + os.sep) or not os.path.exists(exe):
                continue
            install = self._install_root(exe)
            if install:
                self.install_roots.add(install)
            size = du_paths([install]).get(install, 0) if install else os.path.getsize(exe)
            title = info.get_display_name()
            tokens = {norm(title), norm(os.path.basename(exe)), norm(os.path.basename(desk)[:-8].split(".")[-1])}
            if install:
                tokens.add(norm(os.path.basename(install)))
            self.known |= tokens
            targets = [install or exe, desk]
            item = Item("local:" + desk, "local", title, subtitle=info.get_description() or "Installed by hand in your home folder",
                        icon=info.get_icon(), size=size, source="Installed manually", category="Manual install",
                        safety=CAUTION, badge="Manual",
                        note="Installed by hand (a downloaded archive or installer script), not through a package manager. "
                             "Removing it moves its folder and menu shortcut to the Trash.",
                        meta={"Location": install or exe, "Menu shortcut": desk},
                        actions=[act_trash(targets, "Move to Trash")], tokens=tokens)
            self.apps.append(item)

    @staticmethod
    def _unwrap(exe):
        """Follow small launcher scripts (e.g. ~/.local/bin/foo that does `exec /path/to/real`)."""
        try:
            if os.path.getsize(exe) < 8192:
                with open(exe, errors="ignore") as fh:
                    txt = fh.read()
                if txt.startswith("#!"):
                    for m in re.finditer(r"(?:\$HOME|~|" + re.escape(HOME) + r")(/[^\s\"']+)", txt):
                        cand = os.path.realpath(HOME + m.group(1))
                        if os.path.isfile(cand) and cand != exe:
                            return cand
        except OSError:
            pass
        return exe

    @staticmethod
    def _install_root(exe):
        containers = [HOME, J(HOME, "Applications"), J(HOME, "Apps"), J(HOME, "opt"), SHARE, J(HOME, ".local/opt"), J(HOME, ".local/lib")]
        never = {J(HOME, x) for x in (".local", ".config", ".cache", ".var", "Documents", "Downloads", "Desktop",
                                      "Pictures", "Music", "Videos", "Projects", "bin", ".local/bin")}
        p = exe
        while p != HOME and p != "/":
            parent = os.path.dirname(p)
            if parent in containers:
                return p if os.path.isdir(p) and p not in never else None
            p = parent
        return None

    # ---- Steam ----------------------------------------------------------------
    def _steam(self):
        root = next((p for p in [J(SHARE, "Steam"), J(HOME, ".steam/steam"), J(VARAPP, "com.valvesoftware.Steam/.local/share/Steam")]
                     if os.path.isdir(J(p, "steamapps"))), None)
        if not root:
            return
        libs = {os.path.realpath(root)}
        try:
            for m in re.finditer(r'"path"\s+"([^"]+)"', open(J(root, "steamapps/libraryfolders.vdf")).read()):
                libs.add(os.path.realpath(m.group(1)))
        except OSError:
            pass
        self.steam_libs = [l for l in libs if os.path.isdir(J(l, "steamapps"))]
        for lib in self.steam_libs:
            for acf in glob.glob(J(lib, "steamapps", "appmanifest_*.acf")):
                try:
                    txt = open(acf, errors="replace").read()
                except OSError:
                    continue
                def g(k):
                    m = re.search(rf'"{k}"\s+"([^"]*)"', txt)
                    return m.group(1) if m else ""
                appid, name = g("appid"), g("name")
                size = int(g("SizeOnDisk") or 0)
                last = int(g("LastPlayed") or 0)
                tool = bool(re.search(r"Proton|Steam Linux Runtime|Steamworks Common|Steam Controller|Steam\.dll", name))
                self.steam_ids[appid] = name
                self.known.add(norm(name))
                icon = None
                if glob.glob(os.path.expanduser(f"~/.local/share/icons/hicolor/*/apps/steam_icon_{appid}.png")):
                    icon = Gio.ThemedIcon.new(f"steam_icon_{appid}")
                if tool:
                    note = ("A Steam compatibility tool (Proton / runtime) that runs Windows games on Linux. "
                            "Steam reinstalls it if a game needs it again.")
                else:
                    note = "A Steam game. Uninstalling through Steam keeps your cloud saves."
                item = Item(f"steam:{appid}", "steam", name, subtitle=("Last played " + ago(last)) if last else ("Never played" if not tool else "Compatibility tool"),
                            icon=icon, icon_name="applications-games-symbolic", size=size, source="Steam",
                            category="Tool" if tool else "Game", safety=CAUTION if tool else SAFE,
                            badge="Steam tool" if tool else "Game", note=note,
                            meta={"Steam App ID": appid, "Folder": J(lib, "steamapps/common", g("installdir")),
                                  "Last played": time.strftime("%d %b %Y", time.localtime(last)) if last else "never"},
                            actions=[{"type": "uri", "label": "Uninstall in Steam", "uri": f"steam://uninstall/{appid}", "destructive": False}],
                            tokens={norm(name)})
                item.appid = appid
                item.lib = lib
                item.last = last
                self.games.append(item)

    def _shortcut_names(self):
        """Names of non-Steam games added to Steam (binary shortcuts.vdf)."""
        names = {}
        for lib in self.steam_libs[:1]:
            for f in glob.glob(J(lib, "userdata", "*", "config", "shortcuts.vdf")):
                try:
                    data = open(f, "rb").read()
                except OSError:
                    continue
                for m in re.finditer(rb"\x02appid\x00(.{4})\x01(?:AppName|appname)\x00([^\x00]*)\x00", data, re.S):
                    appid = int.from_bytes(m.group(1), "little")
                    names[str(appid)] = m.group(2).decode("utf-8", "replace")
        return names

    def _binaries(self):
        dirs = ["/usr/bin", "/usr/local/bin", J(HOME, ".local/bin"), J(HOME, "bin")]
        dirs += os.environ.get("PATH", "").split(":") + glob.glob(J(HOME, ".*/bin"))
        for d in glob.glob(J(HOME, ".*/bin")):
            self.known.add(norm(os.path.basename(os.path.dirname(d))))
        if any(n.startswith("bravebrowser") for n in self.known):
            self.known.add("bravesoftware")
        if "androidstudio" in self.known:
            self.known |= {"androidjdks", "android"}
        if any(n.startswith("googlechrome") for n in self.known):
            self.known.add("google")
        for d in dirs:
            try:
                self.known |= {norm(x) for x in os.listdir(d)}
            except OSError:
                pass
        self.known.discard("")

    # ---- phase-2 helpers --------------------------------------------------
    def _all_apps(self):
        return self.apps + self.games

    def _attach_data(self, du):
        index = {}
        for item in self._all_apps():
            for t in item.tokens:
                if len(t) >= 3:
                    index.setdefault(t, []).append(item)
        self.claimed = set()
        labels = {CFG: "Settings", SHARE: "App data", CACHE: "Cache"}
        for base, label in labels.items():
            for path, b in du.get(base, {}).items():
                if path == base:
                    continue
                n = norm(os.path.basename(path))
                owners = index.get(n)
                if not owners and "." in os.path.basename(path):
                    owners = index.get(norm(os.path.basename(path).split(".")[-1]))
                if path in self.install_roots:
                    self.claimed.add(path)
                    continue
                if owners and not (base == SHARE and n == "steam"):
                    owners[0].data.append([label, path, b])
                    self.claimed.add(path)
        for path, b in du.get(VARAPP, {}).items():
            fid = os.path.basename(path)
            for item in self.apps:
                if getattr(item, "flatpak_id", None) == fid:
                    item.data.append(["App data & settings", path, b])
                    self.claimed.add(path)
        for g in self.games:
            for sub, label in (("compatdata", "Windows prefix (saves, settings)"), ("shadercache", "Shader cache")):
                p = J(g.lib, "steamapps", sub, g.appid)
                b = du.get(J(g.lib, "steamapps", sub), {}).get(p)
                if b:
                    g.data.append([label, p, b])
        # "uninstall + delete data" for rpm apps / appimages
        for item in self.apps:
            home_data = [d[1] for d in item.data]
            if not home_data or item.kind == "flatpak" or not item.actions:
                continue
            a = dict(item.actions[0])
            a["label"] = a["label"] + " and its data"
            a["also_trash"] = home_data
            item.actions.append(a)

    def _caches(self, cache_du, dev_paths):
        skip = {p for p in dev_paths}
        small_total, small_paths = 0, []
        owners = {d[1]: i.title for i in self._all_apps() for d in i.data if d[0] == "Cache"}
        for path, b in sorted(cache_du.items(), key=lambda kv: -kv[1]):
            if path == CACHE or path in skip:
                continue
            name = os.path.basename(path)
            if b < 20 * 1024**2:
                small_total += b
                small_paths.append(path)
                continue
            desc = CACHE_INFO.get(name) or (f"Cache for {owners[path]}." if path in owners else "Temporary files an app keeps to start faster.")
            safety = CAUTION if name in ("huggingface",) else SAFE
            self.cleanup.append(Item("cache:" + path, "cache", name, subtitle=desc, icon_name="folder-symbolic",
                                     size=b, source="Cache (~/.cache)", category="App caches", safety=safety,
                                     badge="Safe to clear" if safety == SAFE else "Re-download",
                                     note=desc + " Caches are rebuilt automatically when needed. Close the app first for best results.",
                                     meta={"Folder": path}, actions=[act_delete([path], "Clear cache", safety=safety)]))
        if small_paths:
            self.cleanup.append(Item("cache:small", "cache", "Other small caches", subtitle=f"{len(small_paths)} folders, each under 20 MB",
                                     icon_name="folder-symbolic", size=small_total, source="Cache (~/.cache)", category="App caches",
                                     safety=SAFE, badge="Safe to clear", note="Many small app caches. Safe to clear; apps rebuild them.",
                                     actions=[act_delete(small_paths, "Clear all")]))
        self.stats["cache_total"] = cache_du.get(CACHE, 0)

    def _dev(self, sizes):
        total = 0
        for title, desc, paths, cmd, safety in DEV_SPOTS:
            ps = [os.path.expanduser(p) for p in paths]
            ps = [p for p in ps if p in sizes]
            b = sum(sizes[p] for p in ps)
            if not ps or b < 1024**2:
                continue
            if not any(p.startswith(CACHE + os.sep) for p in ps):
                total += b
            if cmd and run(["which", cmd[0]]).strip():
                action = {"type": "cmd", "label": "Clean", "argv": cmd, "destructive": safety != SAFE}
            else:
                action = act_delete(ps, "Clear" if safety == SAFE else "Delete", safety=safety)
            self.dev.append(Item("dev:" + title, "dev", title, subtitle=desc, icon_name="utilities-terminal-symbolic", size=b,
                                 source="Developer tools", category="Developer", safety=safety,
                                 badge="Safe to clear" if safety == SAFE else "Check first",
                                 note=desc + (" It's a download cache — tools fetch things again when needed." if safety == SAFE
                                              else " This isn't just a cache; only delete it if you no longer use it."),
                                 meta={"Location": "\n".join(ps)}, actions=[action]))
        # podman
        out = run(["podman", "system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"], timeout=30)
        rec = 0
        for line in out.splitlines():
            t, _, r = line.partition("\t")
            rec += parse_size(r)
        if rec > 1024**2:
            self.dev.append(Item("dev:podman", "dev", "Unused container images", subtitle="Podman images and stopped containers nothing uses",
                                 icon_name="package-x-generic-symbolic", size=rec, source="Podman", category="Developer", safety=SAFE,
                                 badge="Safe to clear", note="Container images and stopped containers that aren't in use. They can be pulled again.",
                                 actions=[{"type": "cmd", "label": "Prune", "argv": ["podman", "system", "prune", "-a", "-f"], "destructive": True}]))
        self.stats["dev_total"] = total

    def _system_cleanup_sizes(self):
        s = {}
        j = run(["journalctl", "--disk-usage"])
        m = re.search(r"take up ([\d.]+[KMGT]?)", j)
        if m:
            s["journal"] = parse_size(m.group(1).replace("K", "KiB").replace("M", "MiB").replace("G", "GiB").replace("T", "TiB"))
        s.update(du_paths(["/var/cache/libdnf5", "/var/lib/systemd/coredump", J(SHARE, "Trash"), J(CACHE, "thumbnails")]))
        return s

    def _system_cleanup(self, s, du):
        def add(key, title, desc, b, action, safety=SAFE, badge="Safe to clear", icon="user-trash-symbolic"):
            if b == -1 or (b and b > 512 * 1024):
                b = max(b, 0)
                self.cleanup.append(Item("clean:" + key, "clean", title, subtitle=desc, icon_name=icon, size=b, source="System cleanup",
                                         category="System", safety=safety, badge=badge, note=desc, actions=[action]))
        add("trash", "Trash", "Files you deleted but haven't emptied yet.", s.get(J(SHARE, "Trash")),
            {"type": "cmd", "label": "Empty Trash", "argv": ["gio", "trash", "--empty"], "destructive": True})
        add("kernels", "Old kernels", f"{len(self.old_kernel_pkgs)} packages from older Linux kernel versions. Your current kernel ({os.uname().release}) stays.",
            self.old_kernel_bytes, {"type": "rpm-remove", "label": "Remove old kernels", "pkgs": self.old_kernel_pkgs, "destructive": True},
            icon="system-run-symbolic")
        add("unneeded", "Unused dependencies", f"{len(self.unneeded)} packages that were installed for software you already removed.",
            self.unneeded_bytes, {"type": "cmd", "label": "Remove", "argv": ["pkexec", "dnf", "autoremove", "-y"], "destructive": True},
            icon="package-x-generic-symbolic")
        add("dnfcache", "Package download cache", "Downloaded update files and repository metadata kept by dnf.",
            s.get("/var/cache/libdnf5"), {"type": "cmd", "label": "Clean", "argv": ["pkexec", "dnf", "clean", "all"], "destructive": False})
        add("journal", "System logs", "System journal (logs). Roomy trims it down to the last 2 weeks.", s.get("journal"),
            {"type": "cmd", "label": "Trim logs", "argv": ["pkexec", "journalctl", "--vacuum-time=2weeks"], "destructive": False},
            icon="text-x-generic-symbolic")
        add("coredumps", "Crash reports", "Memory dumps saved when programs crashed. Only useful for debugging.",
            s.get("/var/lib/systemd/coredump"),
            {"type": "cmd", "label": "Delete", "argv": ["pkexec", "find", "/var/lib/systemd/coredump", "-type", "f", "-delete"], "destructive": True},
            icon="dialog-warning-symbolic")
        rt_unused = [i for i in self.system if i.kind == "runtime" and i.safety != SYSTEM]
        add("flatpak-unused", "Unused Flatpak runtimes", "Shared app bases left behind by apps you removed. Flatpak checks which ones nothing needs — size is shown after.",
            -1 if rt_unused else 0,
            {"type": "cmd", "label": "Remove unused", "argv": ["flatpak", "uninstall", "--unused", "-y", "--noninteractive"], "destructive": False},
            icon="application-x-addon-symbolic")

    def _is_known(self, name):
        n = norm(name)
        if not n or n in self.known or n in SYSTEM_DIRS:
            return True
        parts = [norm(p) for p in re.split(r"[.\-_ ]", name) if len(norm(p)) >= 4]
        generic = {"gnome", "linux", "share", "config", "local", "data", "github", "gitlab", "desktop", "settings", "cache"}
        if any(p in self.known for p in parts if p not in generic):
            return True
        if len(n) >= 5:
            for t in self.known:
                if len(t) >= 5 and (t.startswith(n) or n.startswith(t)):
                    return True
        return False

    @staticmethod
    def _has_programs(path, depth=2):
        try:
            for root, dirs, files in os.walk(path):
                if root[len(path):].count(os.sep) >= depth:
                    dirs[:] = []
                for f in files:
                    fp = J(root, f)
                    if f.endswith((".so", ".AppImage")) or ".so." in f or (os.access(fp, os.X_OK) and not os.path.islink(fp)
                                                                          and os.path.isfile(fp) and not f.endswith((".sh", ".py", ".js"))):
                        return True
        except OSError:
            pass
        return False

    def _leftovers(self, du):
        for path, b in du.get(VARAPP, {}).items():
            fid = os.path.basename(path)
            if path == VARAPP or fid in self.flatpak_ids or b < 256 * 1024:
                continue
            self.leftovers.append(Item("left:" + path, "leftover", fid, subtitle="Data from a Flatpak app that is no longer installed",
                                       icon_name="folder-symbolic", size=b, source="~/.var/app", category="Uninstalled Flatpak",
                                       safety=SAFE, badge="Leftover",
                                       note=f"The Flatpak app “{fid}” isn't installed anymore, but its saved data and settings are still here. "
                                            "Safe to remove unless you plan to reinstall it and want to keep its settings.",
                                       meta={"Folder": path}, actions=[act_trash([path])]))
        for base, where in ((CFG, "settings"), (SHARE, "app data")):
            for path, b in du.get(base, {}).items():
                name = os.path.basename(path)
                if path == base or path in self.claimed or b < 1024**2 or not os.path.isdir(path) or self._is_known(name):
                    continue
                has_prog = self._has_programs(path)
                if has_prog:
                    sub, badge = "Contains programs — maybe something you installed by hand", "Unknown"
                    note = (f"“{name}” contains program files (executables or libraries), so it may be a tool or mod you installed manually "
                            "rather than leftover data. Only remove it if you know you don't use it.")
                else:
                    sub, badge = f"No installed app matches this {where} folder", "Probably leftover"
                    note = (f"Roomy couldn't find any installed app called “{name}”. It's most likely left over from an app you "
                            "uninstalled — but it may belong to a tool Roomy doesn't know about. Have a look before trashing it.")
                self.leftovers.append(Item("left:" + path, "leftover", name, subtitle=sub,
                                           icon_name="folder-symbolic", size=b, source=base.replace(HOME, "~"),
                                           category="Possible leftover", safety=CAUTION, badge=badge, note=note,
                                           meta={"Folder": path}, actions=[act_trash([path])]))
        shortcuts = self._shortcut_names()
        for lib in self.steam_libs:
            for sub, what in (("compatdata", "Windows prefix"), ("shadercache", "shader cache")):
                base = J(lib, "steamapps", sub)
                for path, b in du.get(base, {}).items():
                    appid = os.path.basename(path)
                    if path == base or appid in self.steam_ids or appid == "0" or b < 1024**2:
                        continue
                    nonsteam = appid.isdigit() and int(appid) > 2**31
                    gname = shortcuts.get(appid)
                    if nonsteam and gname:
                        # still in the Steam library -> it's a game, not a leftover
                        g = next((x for x in self.games if x.key == "nonsteam:" + appid), None)
                        if g is None:
                            g = Item("nonsteam:" + appid, "steam", gname, subtitle="Non-Steam game added to Steam",
                                     icon_name="applications-games-symbolic", source="Steam (non-Steam shortcut)",
                                     category="Game", safety=CAUTION, badge="Non-Steam",
                                     note="A game or program you added to Steam yourself. Roomy only measures the data Steam keeps for it "
                                          "(its Windows prefix, which can contain saves). The game files live wherever you put them.",
                                     meta={"Shortcut ID": appid})
                            self.games.append(g)
                        g.data.append([what.capitalize(), path, b])
                        continue
                    title = f"{gname or ('Non-Steam game ' if nonsteam else 'Steam game ') + appid} — {what}"
                    self.leftovers.append(Item("left:" + path, "leftover", title,
                                               subtitle=("Non-Steam game shortcut" + ("" if gname else " (removed from Steam)")) if nonsteam
                                               else "Game is no longer installed",
                                               icon_name="applications-games-symbolic", size=b, source="Steam",
                                               category="Uninstalled game", safety=CAUTION if sub == "compatdata" else SAFE,
                                               badge="Leftover",
                                               note=("Data Steam keeps for a game that's no longer installed. "
                                                     + ("Windows prefixes can contain local save games — check before deleting if you may reinstall."
                                                        if sub == "compatdata" else "Shader caches are safe to delete.")),
                                               meta={"Folder": path, **({} if nonsteam else {"Steam store page": f"https://store.steampowered.com/app/{appid}"})},
                                               actions=[act_trash([path])]))

    def _steam_orphans(self):
        for lib in self.steam_libs:
            common = J(lib, "steamapps", "common")
            registered = {os.path.basename(g.meta.get("Folder", "")) for g in self.games if g.lib == lib}
            try:
                names = os.listdir(common)
            except OSError:
                continue
            paths = [J(common, n) for n in names if n not in registered and os.path.isdir(J(common, n))
                     and n not in ("Steam Controller Configs", "Steamworks Shared")]
            for path, b in du_paths(paths).items():
                name = os.path.basename(path)
                self.leftovers.append(Item("left:" + path, "leftover", name, subtitle="Game folder Steam no longer tracks",
                                           icon_name="applications-games-symbolic", size=b, source="Steam",
                                           category="Uninstalled game", safety=SAFE, badge="Leftover",
                                           note="This game isn't installed in Steam anymore (no install record), but its folder is still there. "
                                                "Usually just config files or a half-finished download. Safe to remove.",
                                           meta={"Folder": path}, actions=[act_trash([path])]))

    def _home(self, home_du):
        hidden = 0
        for path, b in home_du.items():
            if path == HOME:
                continue
            name = os.path.basename(path)
            if name.startswith("."):
                hidden += b
            else:
                self.home_dirs.append((name, path, b))
        self.home_dirs.sort(key=lambda x: -x[2])
        self.stats["home_files"] = sum(x[2] for x in self.home_dirs)
        self.stats["home_hidden"] = hidden
        self.stats["home_total"] = home_du.get(HOME, 0)
