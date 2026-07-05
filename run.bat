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

REM Auto-open the editor in the default browser once the server is up.
REM Uses PowerShell's Start-Sleep + Start-Process so the delay is reliable
REM regardless of shell quirks. `start /min` runs the launcher minimized so it
REM doesn't steal focus from the visible uvicorn console, and it's detached so
REM uvicorn's blocking call below isn't affected. NO_BROWSER=1 skips this.
if not defined NO_BROWSER (
  start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8765'"
)

python -m uvicorn server.main:app --host 127.0.0.1 --port 8765 --reload
exit /b 0

:error
echo [cb-app] setup failed
pause
exit /b 1
