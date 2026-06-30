@echo off
REM ComfyBlockout app — local dev launcher (Windows).
REM Run from the project root. Uses the system Python; create a venv first if you want isolation.

cd /d "%~dp0"

if not exist .venv (
  echo [cb-app] no venv detected — creating .venv
  python -m venv .venv || goto :error
  call .venv\Scripts\activate.bat
  pip install --upgrade pip
  pip install -r server\requirements.txt || goto :error
) else (
  call .venv\Scripts\activate.bat
)

echo.
echo [cb-app] starting on http://127.0.0.1:8765
echo.

REM Open the editor in the default browser ~2s after the server starts.
REM `start /b` runs in background so the wait + browser open don't block uvicorn.
start "" /b cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:8765"

python -m uvicorn server.main:app --host 127.0.0.1 --port 8765 --reload
exit /b 0

:error
echo [cb-app] setup failed
pause
exit /b 1
