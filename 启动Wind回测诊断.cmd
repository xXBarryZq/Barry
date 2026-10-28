@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 只检查近期Wind日线和60分钟数据，不抽样、不回测、不下单。
python tools\windows\random_backtest.py --diagnose
echo.
echo 请查看自动打开的诊断页面及同文件夹中的接口诊断.json。
pause
endlocal
