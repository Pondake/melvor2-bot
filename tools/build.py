"""Build the release: dist/MelvorBot.exe (one file, no console) and dist/MelvorBot-<version>.zip.

    python tools/build.py [version]

Needs the packages in requirements.txt plus PyInstaller (requirements-build.txt).
The zip holds the exe and config.default.json; on its first start the bot copies that to
config.json next to the exe and keeps its logs and state in logs/ there.
"""
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.autostart import icon_image  # noqa: E402


def main() -> None:
    version = (sys.argv[1] if len(sys.argv) > 1 else "dev").lstrip("v")
    build, dist = ROOT / "build", ROOT / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    build.mkdir(exist_ok=True)
    icon = build / "melvor-bot.ico"
    icon_image().save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", "MelvorBot", "--icon", str(icon),
        "--collect-all", "winrt",  # the WinRT projections are namespace packages PyInstaller misses
        "--distpath", str(dist), "--workpath", str(build / "work"), "--specpath", str(build),
        str(ROOT / "melvor_bot.py"),
    ], check=True, cwd=ROOT)
    shutil.copy(ROOT / "config.default.json", dist / "config.default.json")
    archive = dist / f"MelvorBot-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(dist / "MelvorBot.exe", "MelvorBot/MelvorBot.exe")
        z.write(dist / "config.default.json", "MelvorBot/config.default.json")
    print(archive)


if __name__ == "__main__":
    main()
