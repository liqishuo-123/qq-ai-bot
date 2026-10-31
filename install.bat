@echo off
chcp 936 >nul
cd /d "%~dp0"

echo.
echo   安装步骤已经合并到「一键启动.bat」了。
echo   现在帮你打开它，跟着提示走就行。
echo.
timeout /t 2 >nul
call "%~dp0一键启动.bat"
