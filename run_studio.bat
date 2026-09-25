@echo off
rem X4 animation preview - launcher. Keep CRLF line endings!
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  echo         Install Python 3.10+ or add it to PATH, then retry.
  pause
  exit /b 1
)

python "tools\studio.py" %*
if errorlevel 1 (
  echo.
  echo [FAILED] See the message above.
  echo If a module is missing, run:  pip install -r requirements.txt
  echo If the game is not found, set X4_GAME_DIR or edit config.json.
  pause
)
