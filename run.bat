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

REM Auto-open the editor in the default browser once the server is actually
REM serving requests. PowerShell polls http://127.0.0.1:8765 every 500ms (up
REM to 60s) and launches the browser on the first successful response, so
REM slow first-boot imports don't drop the user on a "can't connect" page.
REM `start /min` runs the launcher minimized and detached so it doesn't steal
REM focus from the uvicorn console. NO_BROWSER=1 skips this.
if not defined NO_BROWSER (
  start "" /min powershell -NoProfile -Command "$u='http://127.0.0.1:8765'; for($i=0; $i -lt 120; $i++){ try { $r=Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 2; if($r.StatusCode -lt 500){ Start-Process $u; break } } catch {} Start-Sleep -Milliseconds 500 }"
)

python -m uvicorn server.main:app --host 127.0.0.1 --port 8765 --reload
exit /b 0

:error
echo [cb-app] setup failed
pause
exit /b 1
