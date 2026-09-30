# PyInstaller spec for the Windows app (V2 P6). Built by build.ps1:
#   pyinstaller --noconfirm --distpath <dist> --workpath <work> packaging/windows/sortandpost.spec
# Onedir (not onefile): a onefile app would unpack torch into a temp dir on
# every launch. Paths come from environment variables build.ps1 sets.
# ruff: noqa
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

from sortandpost import __version__

HERE = Path(SPECPATH)
REPO = HERE.parent.parent
EXIFTOOL_DIR = os.environ["SP_EXIFTOOL_DIR"]  # exiftool.exe + exiftool_files/
ICO = os.environ["SP_ICO"]

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

# torchvision >= 0.29 names its op libraries `_C_stable.pyd` / `image_stable.pyd`
# and loads them by path at import; the stock hook misses them (-> "operator
# torchvision::nms does not exist" at startup). open_clip imports torchvision.
# image_stable.pyd also needs the codec DLLs next to it.
import torchvision

_tv = Path(torchvision.__file__).parent
binaries += [(str(p), "torchvision") for p in (*_tv.glob("*.pyd"), *_tv.glob("*.dll"))]

datas += collect_data_files("sortandpost")  # resources/: tailwind.css, default config + tags
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += [
    "sortandpost.desktop._windows",
    "sortandpost.desktop.tray",
    "sortandpost.indexer.workers",  # imported by name in spawned pool workers
    "pystray._win32",
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
        "AppKit",
        "Foundation",
        "PyObjCTools",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

_ver = tuple(int(x) for x in (__version__.split(".") + ["0", "0", "0"])[:4])
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_ver, prodvers=_ver),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "sortandpost"),
                        StringStruct("FileDescription", "sortandpost"),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", "sortandpost"),
                        StringStruct("OriginalFilename", "sortandpost.exe"),
                        StringStruct("ProductName", "sortandpost"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sortandpost",
    console=False,  # tray app: no console window
    icon=ICO,
    version=version_info,
    upx=False,  # UPX-packed torch DLLs trip antivirus and gain little
)
coll = COLLECT(exe, a.binaries, a.datas, name="sortandpost", upx=False)
