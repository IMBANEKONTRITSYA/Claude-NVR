@echo off
setlocal

echo === FaceWatch - startup ===

where docker >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Docker Desktop not found. Install Docker Desktop for Windows and start it.
  echo https://www.docker.com/products/docker-desktop/
  pause
  exit /b 1
)

if not exist .env (
  echo Creating .env with random secrets...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\generate_env.ps1"
  if errorlevel 1 (
    echo [ERROR] Could not generate .env automatically. Falling back to template
    echo [ERROR] ^(remember to change SECRET_KEY, RTSP_ENCRYPTION_KEY, ADMIN_PASSWORD^).
    copy .env.example .env >nul
  )
)

echo Building and starting containers...
docker compose up -d --build
if errorlevel 1 (
  echo [ERROR] docker compose failed to start
  pause
  exit /b 1
)

echo Waiting for services to become ready...
timeout /t 8 /nobreak >nul

start "" "http://localhost:8080"

echo.
echo === FaceWatch is running ===
echo Web UI:  http://localhost:8080
echo API:     http://localhost:8000/docs
echo Login: admin   Password: see ADMIN_PASSWORD in .env (printed above on first run)
echo.
echo To stop: docker compose down  (or run stop.bat)
pause
