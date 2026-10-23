@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0wind_check.py"
  goto finished
)
python --version >nul 2>nul
if not errorlevel 1 (
  python "%~dp0wind_check.py"
  goto finished
)
echo 检查结果：W09：电脑上暂未找到可用的 Python。请先向 Wind 客服确认 WindPy 支持的 Python 安装版本。
echo 不需要分享账号、密码或验证码。
:finished
echo.
echo 请把“检查结果”这一行发给开发者，然后按任意键关闭。
pause >nul
endlocal
