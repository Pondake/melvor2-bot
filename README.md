# Melvor Idle 2 helper bot

Screen-reading click bot for Melvor Idle 2 (Steam, Windows). It keeps the character busy: follows a
rotation of skills, picks the best unlocked option, tends the farm, cooks, smelts and sells by rule.
It reads the game window with Windows' built-in OCR and clicks with posted messages; no game files
are touched.

## Use the release

1. Download `MelvorBot-<version>.zip` from Releases and unzip it anywhere.
2. Start `MelvorBot.exe`. On the first start it copies `config.default.json` to `config.json`;
   logs and state go to `logs/` next to the exe.
3. Press Start. The bot starts the game through Steam when it is not running and loads the character
   when there is only one.

Hotkeys: Ctrl+Alt+P pause/resume, Ctrl+Alt+Q stop. Settings > General has "Start the bot when I sign
in to Windows".

## Run from source

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\pythonw -m bot.gui
```

## Release

Push a tag (`git tag v0.2.0; git push origin v0.2.0`). GitHub Actions builds the exe on Windows
(`tools/build.py`, PyInstaller) and attaches the zip to a release. Local build:
`.venv\Scripts\python tools\build.py 0.2.0` (needs `requirements-build.txt`).
