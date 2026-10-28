@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 请保持Wind登录。本次使用你提供的三个EDB编号自动查询宏观历史。
echo 复用原股票数据，不重新抽样。历史首发时间和版本未经核验时，增强模型保持未评估。
python tools\windows\random_backtest.py --wind-macro-latest
echo.
echo 新报告文件夹中包含宏观接口检查.json，可用于检查返回数量和错误码。
pause
endlocal
