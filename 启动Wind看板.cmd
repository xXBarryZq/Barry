@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
python tools\windows\wind_dashboard.py
echo.
echo 窗口已停止。如出现错误，请将错误提示告诉开发者，无需发送账号密码。
pause
endlocal
