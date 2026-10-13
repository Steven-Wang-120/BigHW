@echo off
cd /d "%~dp0"
python "流程日志转表格.py" %*
if errorlevel 1 (
    echo.
    echo 如果提示 python 不是内部或外部命令，说明 Python 没装好或没加进 PATH。
    echo 回到第 1 讲的《环境部署指南》，重装时记得勾选 Add python.exe to PATH。
    pause
)
