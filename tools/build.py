"""Build the release: dist/MelvorBot.exe (one file, no console) and dist/SHA256SUMS.txt.

    python tools/build.py [version]

Needs the packages in requirements.txt plus PyInstaller (requirements-build.txt). The exe carries
config.default.json and its version; it installs itself on the first start (bot/install.py) and
updates itself from the GitHub releases, which check the download against SHA256SUMS.txt.
"""
import hashlib
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.autostart import icon_image  # noqa: E402


def main() -> None:
    version = (sys.argv[1] if len(sys.argv) > 1 else "0.0.0-dev").lstrip("v")
    build, dist = ROOT / "build", ROOT / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    build.mkdir(exist_ok=True)
    icon = build / "melvor-bot.ico"
    icon_image().save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    stamp = ROOT / "bot" / "_build.py"
    stamp.write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    try:
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", "MelvorBot", "--icon", str(icon),
            "--collect-all", "winrt",  # the WinRT projections are namespace packages PyInstaller misses
            "--hidden-import", "bot._build",
            "--add-data", f"{ROOT / 'config.default.json'};.",
            "--distpath", str(dist), "--workpath", str(build / "work"), "--specpath", str(build),
            str(ROOT / "melvor_bot.py"),
        ], check=True, cwd=ROOT)
    finally:
        stamp.unlink(missing_ok=True)
    exe = dist / "MelvorBot.exe"
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    (dist / "SHA256SUMS.txt").write_text(f"{digest}  MelvorBot.exe\n", encoding="utf-8")
    print(exe, digest)


if __name__ == "__main__":
    main()
