@echo off
REM Simple static server for the mockups folder — needed because ES modules +
REM importmaps don't work over file:// URLs (browsers block them for CORS).
REM Just double-click this file, then open http://localhost:8081/mannequin-test.html

cd /d "%~dp0"
echo Serving mockups at http://localhost:8081/
echo Open http://localhost:8081/mannequin-test.html
echo Press Ctrl+C to stop.
python -m http.server 8081
