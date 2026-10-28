@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo 请保持Wind登录。自动检查宏观原始返回，不重新查询股票，不改交易规则。
echo 注意：诊断最多9次EDB请求，会消耗API流量配额（168小时滚动恢复）；配额不足时请勿重复运行。
python tools\windows\wind_macro_check.py
echo.
echo 检查完成后，请提供自动打开文件夹中的宏观接口诊断.json。
pause
endlocal
