@echo off
chcp 65001 >nul
setlocal

echo === FaceWatch — запуск ===

where docker >nul 2>nul
if errorlevel 1 (
  echo [ОШИБКА] Docker Desktop не найден. Установите Docker Desktop for Windows и запустите его.
  echo https://www.docker.com/products/docker-desktop/
  pause
  exit /b 1
)

if not exist .env (
  echo Создаю .env из шаблона...
  copy .env.example .env >nul
)

echo Сборка и запуск контейнеров...
docker compose up -d --build
if errorlevel 1 (
  echo [ОШИБКА] Не удалось запустить docker compose
  pause
  exit /b 1
)

echo Ожидание готовности сервиса...
timeout /t 8 /nobreak >nul

start "" "http://localhost:8080"

echo.
echo === FaceWatch запущен ===
echo Веб-интерфейс: http://localhost:8080
echo API:          http://localhost:8000/docs
echo Логин: admin    Пароль: см. ADMIN_PASSWORD в .env (по умолчанию admin)
echo.
echo Остановить: docker compose down
pause
