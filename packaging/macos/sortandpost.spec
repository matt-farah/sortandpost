# PyInstaller spec for the macOS app bundle (V2 P5). Built by build.sh:
#   pyinstaller --noconfirm --distpath <dist> --workpath <work> packaging/macos/sortandpost.spec
# Onedir (not onefile): a onefile app would unpack torch into a temp dir on
# every launch. Paths come from environment variables build.sh sets.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

from sortandpost import __version__

HERE = Path(SPECPATH)
REPO = HERE.parent.parent
EXIFTOOL_DIR = os.environ["SP_EXIFTOOL_DIR"]  # unpacked Image-ExifTool (exiftool + lib/)
ICNS = os.environ["SP_ICNS"]

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
    "sortandpost.desktop._macos",
    "sortandpost.desktop.tray",
    "sortandpost.indexer.workers",  # imported by name in spawned pool workers
    "AppKit",
    "Foundation",
    "PyObjCTools.AppHelper",
]

a = Analysis(
    [str(REPO / "packaging" / "entry.py")],
    pathex=[str(REPO / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "tkinter",
        "pytest",
        "mypy",
        "ruff",
        "IPython",
        "matplotlib",
        "torch.utils.tensorboard",
        "tensorboard",
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
    console=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # build.sh signs the finished bundle
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="sortandpost")
app = BUNDLE(
    coll,
    name="sortandpost.app",
    icon=ICNS,
    bundle_identifier="app.sortandpost",
    version=__version__,
    info_plist={
        "CFBundleName": "sortandpost",
        "CFBundleDisplayName": "sortandpost",
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # menu bar only, no Dock icon
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "sortandpost",
    },
)
