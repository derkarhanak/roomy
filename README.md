# Roomy

A minimal storage manager for Fedora / GNOME. Roomy shows what is using your disk in plain language and helps you clean it up safely.

- **Apps** – one entry per real app, whether it came from dnf, Flatpak, an AppImage or a manual install. Sizes include the app's settings, data and cache.
- **Games** – Steam games, Proton and runtimes, and non-Steam shortcuts.
- **Cleanup** – app caches, the dnf cache, logs, crash reports, old kernels, unused dependencies and the Trash.
- **Leftovers** – folders left behind by apps and games you already uninstalled.
- **Developer** – package caches, SDKs, AI models and container images.
- **System packages** – every RPM, grouped into plain-language categories, with a "what needs this?" lookup.

Every item has a safety badge (*Safe to remove*, *Came with Fedora*, *Dependency*, *System*). Before any dnf removal, Roomy does a dry run that lists exactly which packages would go. Core system packages are locked. Leftovers are moved to the Trash, and only caches are deleted outright.

## Requirements

Python 3, GTK 4, libadwaita ≥ 1.6 and PyGObject. These are preinstalled on Fedora Workstation.

## Run

```bash
./roomy-app
```

## Install to the app grid

```bash
ln -sf "$PWD/roomy-app" ~/.local/bin/roomy
install -Dm644 io.github.samsyk.Roomy.svg ~/.local/share/icons/hicolor/scalable/apps/io.github.samsyk.Roomy.svg
sed "s|^Exec=.*|Exec=$PWD/roomy-app|" io.github.samsyk.Roomy.desktop > ~/.local/share/applications/io.github.samsyk.Roomy.desktop
```

## Layout

- `roomy/scan.py` – discovers everything (rpm/dnf, Flatpak, AppImages, Steam, caches, leftovers)
- `roomy/actions.py` – uninstall and cleanup actions, with path safety guards
- `roomy/ui.py` – the GTK4/libadwaita interface
