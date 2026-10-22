@echo off
chcp 65001 >nul
title PromptSmith
cd /d %~dp0

set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo [错误] 找不到 Python。请先安装 Python 3.9 或更高版本，
  echo        并勾选 "Add Python to PATH"。
  pause
  exit /b 1
)

echo 使用 Python: %PY%
echo 启动 PromptSmith ...
start "" /b %PY% src\server.py 8765 --no-browser

rem 等服务起来再开浏览器
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8765/

echo.
echo ============================================================
echo  PromptSmith 已启动：http://127.0.0.1:8765/
echo  端口被占用时会自动顺延到 8776，以界面左上角显示为准。
echo ============================================================
echo.
echo 提示：关闭本窗口不会停止服务。
echo       要停止服务，请结束对应的 python 进程。
pause
