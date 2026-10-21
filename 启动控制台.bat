@echo off
title ComfyUI 手机控制台
cd /d "%~dp0"

set "PY="

rem ---------- 1. 手动指定的路径 ----------
if not exist "%~dp0python.txt" goto try_runtime
set /p PYPATH=<"%~dp0python.txt"
if not exist "%PYPATH%" goto try_runtime
set "PY=%PYPATH%"
goto run

rem ---------- 2. 自带的 runtime ----------
:try_runtime
if not exist "%~dp0runtime\python.exe" goto try_py
set "PY=%~dp0runtime\python.exe"
goto run

rem ---------- 3. py 启动器（真的能跑才算） ----------
:try_py
py -3 -c "import sys" >nul 2>nul
if errorlevel 1 goto try_python
set "PY=py -3"
goto run

rem ---------- 4. python 命令 ----------
:try_python
python -c "import sys" >nul 2>nul
if errorlevel 1 goto try_python3
set "PY=python"
goto run

rem ---------- 5. python3 命令 ----------
:try_python3
python3 -c "import sys" >nul 2>nul
if errorlevel 1 goto nopython
set "PY=python3"
goto run

:nopython
echo.
echo   没有找到可用的 Python。
echo.
echo   办法一：安装 Python 3.9 或更高版本
echo           下载时务必勾选 "Add Python to PATH"
echo.
echo   办法二：把便携版 Python 解压到本目录，文件夹命名为 runtime
echo           也就是让 runtime\python.exe 存在
echo.
echo   办法三：如果电脑里已经有 Python，但上面都找不到，
echo           新建一个文本文件 python.txt，第一行写 python.exe 的完整路径
echo           例如：C:\Python312\python.exe
echo.
pause
exit /b 1

:run
echo.
echo   ComfyUI 手机控制台
echo   =========================================
echo   启动前请确保 ComfyUI 已经开着
echo   管理页会自动在浏览器打开
echo   关闭这个窗口 = 停止服务
echo   =========================================
echo.

%PY% server.py

echo.
echo 服务已停止，可以关闭窗口。
pause
