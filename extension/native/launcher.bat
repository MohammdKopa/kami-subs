@echo off
REM Kami Subs — Native Messaging host launcher.
REM Chrome execs this with stdin/stdout wired to the extension.
REM We delegate to launcher.py via the project's venv python so deps resolve.
REM -u = unbuffered stdio (length-prefixed protocol can't tolerate buffering).

setlocal
set "HOST_DIR=%~dp0"
set "VENV_PY=%HOST_DIR%..\..\backend\.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo {"type":"error","message":"venv python missing: %VENV_PY%"} 1>&2
  exit /b 1
)

"%VENV_PY%" -u "%HOST_DIR%launcher.py"
