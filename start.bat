@echo off
setlocal

rem All paths below (.env, .env.example, docker-compose.yml) are relative, so
rem the working directory has to be the repository root. It is not guaranteed:
rem "Run as administrator" starts .bat files in C:\Windows\system32, and so
rem does launching from some file managers. Without this the .env checks below
rem would look at the wrong directory and report a bogus failure.
cd /d "%~dp0"

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
)

rem No silent fallback to .env.example here on purpose. Its SECRET_KEY,
rem RTSP_ENCRYPTION_KEY and ADMIN_PASSWORD are the public defaults from the
rem repository, and the backend refuses to boot on them by design
rem (backend/app/config.py: insecure_secret_problems). Copying the template
rem produced a stack that came up "successfully" and then crash-looped the
rem backend, so the only symptom the user ever saw was a login error.
if not exist .env goto :no_env

rem Same check for an .env left behind by that older fallback: once it
rem exists, the block above never runs again, so a broken file would stay
rem broken across every restart.
findstr /X /C:"SECRET_KEY=please-change-me-to-a-long-random-string" .env >nul 2>nul
if not errorlevel 1 goto :insecure_env
findstr /X /C:"RTSP_ENCRYPTION_KEY=ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=" .env >nul 2>nul
if not errorlevel 1 goto :insecure_env
findstr /X /C:"ADMIN_PASSWORD=admin" .env >nul 2>nul
if not errorlevel 1 goto :insecure_env

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
exit /b 0

:no_env
echo.
echo [ERROR] Could not generate .env with random secrets.
echo [ERROR] Not falling back to .env.example: the backend refuses to start
echo [ERROR] on its public default secrets, so the stack would come up and
echo [ERROR] then crash-loop with no working login.
echo.
echo Fix it manually:
echo   1. copy .env.example .env
echo   2. edit .env and replace these three values with your own:
echo        SECRET_KEY            any long random string
echo        RTSP_ENCRYPTION_KEY   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
echo        ADMIN_PASSWORD        10+ characters, mix letters and digits
echo   3. run start.bat again
echo.
pause
exit /b 1

:insecure_env
echo.
echo [ERROR] .env still contains the public default secrets from .env.example.
echo [ERROR] The backend refuses to start on them, so the login page would
echo [ERROR] only ever show an error. This usually means an older version of
echo [ERROR] start.bat copied the template after .env generation failed.
echo.
echo Fix it:
echo   1. del .env
echo   2. run start.bat again  ^(it will generate fresh random secrets^)
echo.
echo   If generation keeps failing, edit .env by hand and replace
echo   SECRET_KEY, RTSP_ENCRYPTION_KEY and ADMIN_PASSWORD with your own values.
echo.
pause
exit /b 1
