@echo off
rem Start the bot control window without a console.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m bot.gui %*
