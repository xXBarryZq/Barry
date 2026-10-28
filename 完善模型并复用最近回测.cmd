@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 复用最近回测的股票价格，导入宏观和事件资料，不重新读取行情。
echo 第一次会创建空模板并打开资料文件夹；缺少资料的实验显示未评估。
python tools\windows\random_backtest.py --context-latest
echo.
echo 详细规则请查看 docs\宏观与事件模型.md。
pause
endlocal
