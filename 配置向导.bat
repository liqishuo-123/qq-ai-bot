@echo off
chcp 936 >nul
title QQ AI Bot 配置向导
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

echo.
echo ============================================================
echo    QQ AI Bot 配置向导
echo    改 QQ 号、机器人名字、模型服务商、API Key、控制台密码
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [X] 还没装好环境。先双击「一键启动.bat」，它会自动装。
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\setup_wizard.py
echo.
pause
