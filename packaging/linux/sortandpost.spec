# PyInstaller spec for the Linux app (V3 P7). Built by build.sh:
#   pyinstaller --noconfirm --distpath <dist> --workpath <work> packaging/linux/sortandpost.spec
# Onedir (not onefile): a onefile app would unpack torch into a temp dir on
# every launch; the AppImage wraps the onedir folder instead. Paths come from
# environment variables build.sh sets.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

from sortandpost import __version__

HERE = Path(SPECPATH)
REPO = HERE.parent.parent
EXIFTOOL_DIR = os.environ["SP_EXIFTOOL_DIR"]  # unpacked Image-ExifTool (exiftool + lib/)

datas = [(EXIFTOOL_DIR, "exiftool")]
binaries = []
hiddenimports = []

# Packages whose data files / native libs / lazily-imported submodules PyInstaller
# can't see from the import graph alone.
for pkg in ("nicegui", "open_clip", "pillow_heif", "rawpy", "engineio", "socketio", "pystray"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# torchvision >= 0.29 names its op libraries `_C_stable.so` / `image_stable.so` and
# loads them by path at import; the stock hook misses them (-> "operator
# torchvision::nms does not exist" at startup). open_clip imports torchvision.
import torchvision

_tv = Path(torchvision.__file__).parent
binaries += [(str(p), "torchvision") for p in _tv.glob("*.so")]

datas += collect_data_files("sortandpost")  # resources/: tailwind.css, default config + tags
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += [
    "sortandpost.desktop._linux",
    "sortandpost.desktop.tray",
    "sortandpost.indexer.workers",  # imported by name in spawned pool workers
    # The tray backends sortandpost.desktop._linux picks between (pystray
    # imports the chosen one by name), and what they and the keyring need.
    "pystray._appindicator",
    "pystray._xorg",
    "gi",
    "gi.repository.GLib",
    "gi.repository.Gtk",
    "gi.repository.AyatanaAppIndicator3",
    "Xlib",
    "jeepney",
    "jeepney.io.blocking",
    "secretstorage",
]

a = Analysis(
    [str(REPO / "packaging" / "entry.py")],
    pathex=[str(REPO / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hooksconfig={
        "gi": {
            "module-versions": {"Gtk": "3.0", "AyatanaAppIndicator3": "0.1"},
            # The tray's menu is drawn by the desktop's tray host (over D-Bus),
            # not by our GTK, so no icon themes / GTK themes / translations.
            "icons": [],
            "themes": [],
            "languages": [],
        },
    },
    excludes=[
        "tkinter",
        "pytest",
        "mypy",
        "ruff",
        "IPython",
        "matplotlib",
        "torch.utils.tensorboard",
        "tensorboard",
        "AppKit",
        "Foundation",
        "PyObjCTools",
        "pystray._darwin",
        "pystray._win32",
        "pystray._gtk",  # GtkStatusIcon: _linux never picks it
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sortandpost",
    console=True,  # no effect on Linux; launched from a .desktop file it has no terminal
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="sortandpost", strip=False, upx=False)
