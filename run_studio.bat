@echo off
chcp 65001 >nul
cd /d "%~dp0"
python tools\studio.py %*
if errorlevel 1 (
  echo.
  echo [启动失败] 上面是错误信息。若提示缺少模块，请先运行:
  echo     pip install -r requirements.txt
  pause
)
