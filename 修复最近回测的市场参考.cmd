@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 读取最近一次已保存的原始回测数据，只补市场参考，不重查20只股票。
echo 将生成新报告，保留原报告和原股票数据。请保持Wind登录。
python tools\windows\random_backtest.py --repair-latest
echo.
echo 若未找到原数据，请保留原报告文件夹中的回测数据.json并按提示处理。
pause
endlocal
