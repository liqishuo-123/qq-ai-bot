@echo off
chcp 936 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [X] 还没安装。请先双击「一键启动.bat」。
    pause
    exit /b 1
)

if not exist ".env" (
    echo [X] 缺少 .env。双击「配置向导.bat」生成一个。
    pause
    exit /b 1
)

set "BOT_PORT=8080"
for /f "tokens=2 delims==" %%P in ('findstr /b /c:"PORT=" .env 2^>nul') do set "BOT_PORT=%%P"

echo ==========================================
echo   QQ AI Bot 启动中...
echo   控制台: http://127.0.0.1:%BOT_PORT%/admin
echo   关闭窗口或按 Ctrl+C 停止
echo ==========================================
echo.

".\.venv\Scripts\python.exe" bot.py

echo.
echo 机器人已停止。按任意键关闭窗口。
pause >nul
