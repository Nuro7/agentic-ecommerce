@echo off
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo   WooAgent Start Script
echo ============================================
echo.

REM ── Step 1: Stop old ngrok if running ────────────────────────────────────────
echo [1/5] Stopping old ngrok if running...
taskkill /f /im ngrok.exe >nul 2>&1
timeout /t 2 /nobreak >nul

REM ── Step 2: Start Docker (backend + redis only, NO ngrok in compose) ─────────
echo [2/5] Starting Docker containers...
docker compose up -d --build
if %errorlevel% neq 0 (
    echo ERROR: Docker failed. Is Docker Desktop running?
    pause & exit /b 1
)

REM ── Step 3: Start ngrok natively (stays alive across backend rebuilds) ────────
echo [3/5] Starting ngrok tunnel on port 8000...
set NGROK_AUTHTOKEN=2wmG6MG2AnXyVrCue0jz4KKbHKS_5thXB9ZaJ78brguTvXmGi
set NGROK_EXE=C:\Users\hp\Documents\Doc\ngrok-v3-stable-windows-amd64\ngrok.exe
start "ngrok" /min "%NGROK_EXE%" http 8000 --authtoken %NGROK_AUTHTOKEN%
echo Waiting for ngrok to establish tunnel...
timeout /t 8 /nobreak >nul

REM ── Step 4: Update WordPress with the stable ngrok URL ───────────────────────
echo [4/5] Updating WordPress backend URL...
python update-ngrok-url.py
if %errorlevel% neq 0 (
    echo WARNING: Auto-update failed. Check http://localhost:4040 for URL.
)

REM ── Step 5: Sync widget JS to WordPress ──────────────────────────────────────
echo [5/5] Syncing widget JS to WordPress...
xcopy /y "wooagent\widget\wooagent-widget.js" "C:\Users\hp\Local Sites\ecomify\app\public\wp-content\plugins\wooagent\widget\wooagent-widget.js*" >nul 2>&1
echo Widget JS synced.

echo.
echo ============================================
echo   All services started!
echo.
echo   Backend : http://localhost:8000/health
echo   ngrok   : http://localhost:4040
echo.
echo   Now open https://ecomify.local and press
echo   Ctrl+Shift+R to hard-refresh the browser.
echo ============================================
echo.
pause
