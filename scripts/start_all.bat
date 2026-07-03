@echo off
rem Единый запуск всей системы CS Monitor AI одним файлом:
rem   миграции БД -> проверка моделей -> FastAPI (:8000) + онлайн ML-цикл (5 мин).
rem Авторестарт при фатальном падении интерпретатора; graceful shutdown по Ctrl+C.
rem Защита от двойного запуска: lock logs\run_system.lock (и logs\live_predict.lock для ML).
rem Логи: backend\logs\run_system.log + run_system.out.log
cd /d "%~dp0..\backend"
:loop
echo [%date% %time%] Starting run_system (API + online ML)...
"..\venv\Scripts\python.exe" run_system.py --station ohangaron
echo [%date% %time%] run_system exited (code %errorlevel%), restart in 15s...
timeout /t 15 /nobreak >nul
goto loop
