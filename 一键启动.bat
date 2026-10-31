@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 936 >nul
title QQ AI Bot 一键启动
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

echo.
echo ============================================================
echo    QQ AI Bot 一键启动
echo    第一次运行会自动装环境，大约 2 到 5 分钟，别关窗口
echo ============================================================
echo.

goto :step_python


:step_python
echo [1/7] 检查 Python 环境 ...
set "PY="

rem 注意：py -3.12 找不到 3.12 时只往 stderr 报错，退出码仍然是 0，
rem 所以不能看 errorlevel，必须看它有没有真的输出内容。
py -3.12 -c "print('YPICK')" 2>nul | findstr /x /c:"YPICK" >nul
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto :have_python

py -3.13 -c "print('YPICK')" 2>nul | findstr /x /c:"YPICK" >nul
if not errorlevel 1 set "PY=py -3.13"
if defined PY goto :have_python

py -3.14 -c "print('YPICK')" 2>nul | findstr /x /c:"YPICK" >nul
if not errorlevel 1 set "PY=py -3.14"
if defined PY goto :have_python

py -3 -c "import sys;print('YPICK' if sys.version_info>=(3,12) else 'OLD')" 2>nul | findstr /x /c:"YPICK" >nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto :have_python

rem 已经装过 Python 但 PATH 没刷新时，只能靠绝对路径找
call :probe_installed
if defined PY goto :have_python

rem 网上常见的一行安装，装完当前窗口的 PATH 不会更新，所以装完再探一次
winget --version >nul 2>nul
if errorlevel 1 goto :no_python

echo.
echo   没找到 Python，正在用 winget 自动安装 3.12 ...
echo   过程中可能弹出一个权限窗口，点「是」就行。
echo.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --silent
call :probe_installed
if not defined PY goto :no_python
goto :have_python


:probe_installed
for %%D in (
    "%LocalAppData%\Programs\Python\Python312"
    "%LocalAppData%\Programs\Python\Python313"
    "%LocalAppData%\Programs\Python\Python314"
    "%ProgramFiles%\Python312"
    "%ProgramFiles%\Python313"
    "%ProgramFiles%\Python314"
    "C:\Python312"
    "C:\Python313"
) do (
    if not defined PY if exist "%%~D\python.exe" (
        "%%~D\python.exe" -c "print('YPICK')" 2>nul | findstr /x /c:"YPICK" >nul
        if not errorlevel 1 set "PY=%%~D\python.exe"
    )
)
goto :eof


:have_python
echo       Python 找到了：%PY%


:step_venv
echo [2/7] 准备虚拟环境 ...
if exist ".venv\Scripts\python.exe" goto :venv_ready
%PY% -m venv .venv
if errorlevel 1 goto :fail_venv
:venv_ready
echo       虚拟环境就绪


:step_deps
echo [3/7] 检查依赖 ...
".venv\Scripts\python.exe" -c "import nonebot" >nul 2>nul
if not errorlevel 1 goto :deps_ready

echo       第一次安装依赖，要几分钟，让它跑完 ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --disable-pip-version-check --no-input
if not errorlevel 1 goto :deps_ready

echo.
echo       直连下载失败，换国内镜像再试一次 ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn --disable-pip-version-check --no-input
if errorlevel 1 goto :fail_pip
:deps_ready
echo       依赖就绪


:step_config
echo [4/7] 检查配置文件 ...
if not exist ".env" goto :need_wizard
".venv\Scripts\python.exe" scripts\setup_wizard.py --check >nul 2>nul
if errorlevel 1 goto :need_wizard
echo       配置已就绪
goto :step_napcat

:need_wizard
echo.
echo       还差几个配置，跟着提示填一下，大约 30 秒。
echo       不知道填什么就直接回车，用默认值。
echo.
".venv\Scripts\python.exe" scripts\setup_wizard.py
if errorlevel 1 goto :fail_setup


:step_napcat
echo [5/7] 检查 NapCat ...
if exist "napcat_path.txt" goto :napcat_done
echo       还没记录 NapCat 装在哪。
echo       如果还没装，可以先跳过，装好后再双击「启动NapCat.bat」。
choice /c YN /t 20 /d N /n /m "      现在去找 NapCat 吗？(Y/N) "
if errorlevel 2 goto :napcat_done
call "%~dp0启动NapCat.bat" --no-pause
:napcat_done


:step_port
echo [6/7] 检查 NapCat 端口 3001 ...
".venv\Scripts\python.exe" -c "import socket,sys;s=socket.socket();s.settimeout(0.5);sys.exit(0 if s.connect_ex(('127.0.0.1',3001))==0 else 1)"
if errorlevel 1 goto :port_closed
echo       端口正常，NapCat 在运行
goto :step_run

:port_closed
echo       连不上 3001，NapCat 可能还没启动。
echo       不影响先把机器人开起来，但要收到 QQ 消息必须开着 NapCat。
echo       解决：双击「启动NapCat.bat」，扫码登录小号。


:step_run
echo [7/7] 启动机器人 ...
set "BOT_PORT=8080"
for /f "tokens=2 delims==" %%P in ('findstr /b /c:"PORT=" .env 2^>nul') do set "BOT_PORT=%%P"
echo.
echo ============================================================
echo    启动中。等 10 秒左右，看到下面这行就是成功了：
echo.
echo        [OK] 已连上 NapCat
echo.
echo    控制台地址： http://127.0.0.1:%BOT_PORT%/admin
echo    关掉这个窗口 = 关掉机器人
echo ============================================================
echo.

".venv\Scripts\python.exe" bot.py

echo.
echo ============================================================
echo    机器人已停止。
echo    如果是意外退出，往上翻看报错，或者看控制台「错误」面板。
echo ============================================================
pause
exit /b 0


:no_python
echo.
echo [X] 没找到 Python，也没法自动安装。
echo     请手动装一下： https://www.python.org/downloads/
echo     安装时务必勾选 "Add python.exe to PATH"，装完重新双击本文件。
echo.
start "" https://www.python.org/downloads/
pause
exit /b 1


:fail_venv
echo.
echo [X] 创建虚拟环境失败。
echo     常见原因：Python 装得不完整，或者当前目录没有写权限。
echo     办法：把整个文件夹挪到 D:\ 或桌面再试一次。
pause
exit /b 1


:fail_pip
echo.
echo [X] 依赖安装失败。
echo     先确认电脑能上网，然后重试。
echo     如果报 "File contains no section headers"，说明 pip 配置坏了，执行：
echo         set PIP_CONFIG_FILE=NUL
echo     再双击本文件。
pause
exit /b 1


:fail_setup
echo.
echo [X] 配置没写完。
echo     可以重新双击「一键启动.bat」，或者双击「配置向导.bat」单独配。
pause
exit /b 1
