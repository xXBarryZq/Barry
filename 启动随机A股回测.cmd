@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 将只读查询Wind历史行情，不会下单，也不改变看板策略。
echo 默认固定随机20只沪深A股，近3年。历史查询可能较慢并占用Wind额度。
echo 程序先检查近期数据；检查不通过时停止批量查询并打开接口诊断。
python tools\windows\random_backtest.py
echo.
echo 若浏览器未自动打开，请复制上面显示的报告路径打开。
pause
endlocal
