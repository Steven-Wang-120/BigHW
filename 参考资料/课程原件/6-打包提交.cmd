@echo off
setlocal
set "PS1=%~dp0打包提交.ps1"
if not exist "%PS1%" set "PS1=%~dp0%~n0.ps1"
if not exist "%PS1%" (
    echo.
    echo 找不到打包脚本。
    echo 请确认 打包提交.ps1 与本文件在同一个文件夹里，再双击本文件。
    echo.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
    echo.
    echo 打包没有成功。请把上面的提示截图发给课程组。
    echo.
    pause
)
