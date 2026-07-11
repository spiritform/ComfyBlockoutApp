@echo off
REM ComfyBlockout app — first-time setup + launch (Windows).
REM Double-click this after cloning the repo. It verifies prerequisites,
REM creates .venv, installs server dependencies, and starts the app.
REM Subsequent launches should use run.bat (faster path — no reinstall).

cd /d "%~dp0"

echo.
echo ==============================================================
echo   ComfyBlockout — first-time setup
echo ==============================================================
echo.

REM ---------- 1. verify Python ----------
where python >nul 2>&1
if errorlevel 1 (
  echo [cb-app] Python was not found on your PATH.
  echo.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo and be sure to tick "Add Python to PATH" during install.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [cb-app] Python %PYVER% detected.

REM ---------- 2. venv + dependencies ----------
if not exist .venv (
  echo [cb-app] Creating isolated .venv ^(one-time^)...
  python -m venv .venv || goto :error
) else (
  echo [cb-app] Reusing existing .venv.
)

call .venv\Scripts\activate.bat || goto :error

echo [cb-app] Upgrading pip...
python -m pip install --upgrade pip >nul || goto :error

echo [cb-app] Installing server requirements ^(this may take a minute^)...
pip install -r server\requirements.txt || goto :error

echo.
echo ==============================================================
echo   Setup complete. Launching ComfyBlockout on http://127.0.0.1:8765
echo ==============================================================
echo.

REM Open with ?firstrun=1 so the frontend clears its "welcome-seen" flag and
REM re-shows the onboarding modal — even when re-running firstrun.bat on an
REM already-installed copy. The frontend strips the param after applying it
REM so a plain refresh doesn't keep re-triggering the modal.
if not defined NO_BROWSER (
  start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:8765/?firstrun=1'"
)

python -m uvicorn server.main:app --host 127.0.0.1 --port 8765 --reload
exit /b 0

:error
echo.
echo [cb-app] Setup failed. Scroll up to see the error, then either:
echo   - fix the issue and re-run firstrun.bat, OR
echo   - delete the .venv folder to start clean.
echo.
pause
exit /b 1
