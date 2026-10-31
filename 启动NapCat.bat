@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 936 >nul
title NapCat 启动器
cd /d "%~dp0"

set "NC_DIR="

if not exist "napcat_path.txt" goto :search
set /p NC_DIR=<napcat_path.txt
if exist "!NC_DIR!\napcat.bat" goto :found
echo       上次记的路径不能用了，重新找一次 ...


:search
echo [1/3] 找 NapCat ...
for %%B in (
    "%USERPROFILE%\NapCatQQ"
    "C:\NapCatQQ"
    "D:\NapCatQQ"
    "E:\NapCatQQ"
    "F:\NapCatQQ"
    "%~dp0..\NapCatQQ"
) do (
    if not defined NC_DIR call :probe "%%~B"
)
if defined NC_DIR goto :found
goto :ask


:probe
set "BASE=%~1"
if not exist "%BASE%" goto :eof
if exist "%BASE%\napcat.bat" (
    set "NC_DIR=%BASE%"
    goto :eof
)
for /d %%S in ("%BASE%\NapCat.*.Shell") do (
    if not defined NC_DIR if exist "%%~S\napcat.bat" set "NC_DIR=%%~S"
)
goto :eof


:ask
echo       没找到 NapCat。它不在这个项目里，要单独下载。
echo.
echo       1. 打开 https://github.com/NapNeko/NapCatQQ/releases
echo       2. 下载文件名带 Shell 的那个压缩包
echo       3. 解压到一个好找的位置，比如 D:\NapCatQQ
echo       4. 把解压出来的文件夹路径粘到下面，或者直接回车退出
echo.
set /p "NC_DIR=      路径: "
if not defined NC_DIR goto :bye
for %%P in ("!NC_DIR!") do set "NC_DIR=%%~P"
if not exist "!NC_DIR!\napcat.bat" (
    echo.
    echo [X] 这个目录里没有 napcat.bat，路径不对。
    echo     正确的目录里应该有 napcat.bat、QQ.exe、napcat.mjs 这几个文件。
    goto :bye
)


:found
echo       NapCat 位置：!NC_DIR!
> "napcat_path.txt" echo !NC_DIR!


:step_ws
echo [2/3] 检查 WebSocket 配置 ...
set "PYRUN="
if exist ".venv\Scripts\python.exe" set "PYRUN=.venv\Scripts\python.exe"
if defined PYRUN goto :have_pyrun
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYRUN=py -3"
if defined PYRUN goto :have_pyrun
python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYRUN=python"
:have_pyrun

if not defined PYRUN goto :launch
%PYRUN% "scripts\napcat_ws.py" --home "!NC_DIR!"
if not errorlevel 1 goto :launch
if errorlevel 3 goto :ws_busy

echo.
echo       缺少 3001 端口的服务。可以让我自动补上，会先备份配置文件。
choice /c YN /t 30 /d N /n /m "      自动补上吗？(Y/N) "
if errorlevel 2 goto :launch
echo.
%PYRUN% "scripts\napcat_ws.py" --home "!NC_DIR!" --apply
goto :launch

:ws_busy
echo.
echo       NapCat 正在运行，先关掉它再点一次本文件。
echo       关掉的办法：双击 NapCat 目录里的 KillQQ.bat。
echo       或者直接在任务管理器里结束 QQ.exe。
goto :bye


:launch
echo [3/3] 启动 NapCat ...
echo.
echo       接下来会弹一个权限窗口，点「是」。
echo       起来以后用机器人小号扫码登录。
echo       登录完再回去双击「一键启动.bat」。
echo.
if exist "!NC_DIR!\napcat-user.bat" (
    start "" "!NC_DIR!\napcat-user.bat"
) else (
    start "" "!NC_DIR!\napcat.bat"
)


:bye
echo.
if "%~1"=="--no-pause" goto :eof
echo 按任意键关闭这个窗口。
pause >nul
