@echo off
REM ============================================================================
REM Durable-лаунчер ML-мониторинга (live_predict --mode live).
REM - Перезапускает процесс при падении (после паузы), чтобы петля не оставалась
REM   остановленной (инцидент 2026-06-23: петля встала и не поднялась).
REM - Оконная загрузка (fetch_data_from_db) делает старт устойчивым к таймаутам БД.
REM - Для выживания после ПЕРЕЗАГРУЗКИ ОС: зарегистрировать этот .bat в Планировщике
REM   задач (триггер "при входе"/"при загрузке") либо как службу через nssm.
REM Остановка: закрыть окно или Ctrl+C (single_instance-lock освободится).
REM ============================================================================
setlocal
cd /d "%~dp0\..\backend"
set PYTHONIOENCODING=utf-8
set PY=..\venv\Scripts\python.exe
if not exist "%PY%" set PY=python

:loop
echo [%date% %time%] start live_predict --mode live ...
"%PY%" -u live_predict.py --mode live --station ohangaron
echo [%date% %time%] live_predict завершился (код %errorlevel%) — перезапуск через 30с...
timeout /t 30 /nobreak >nul
goto loop
