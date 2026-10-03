@echo off
REM Ручной запуск бэкапа PostgreSQL вне расписания (ТЗ 12: "возможность
REM ручного запуска бэкапа"). Требует запущенного docker-compose (сервис backup).
docker compose exec backup /backup/run.sh
