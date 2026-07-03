@echo off
rem ML-цикл live_predict с авторестартом при падении.
rem Внутри процесса уже есть try/except на цикл и backoff; этот скрипт —
rem страховка от фатальных крашей интерпретатора.
rem Логи: backend\logs\live_predict.log + live_predict.out.log
rem Защита от двойного запуска: файловый lock (logs\live_predict.lock) —
rem второй экземпляр завершится сам.
cd /d "%~dp0..\backend"
:loop
echo [%date% %time%] Starting live_predict...
"..\venv\Scripts\python.exe" live_predict.py --mode live --station ohangaron
echo [%date% %time%] live_predict exited (code %errorlevel%), restart in 30s...
timeout /t 30 /nobreak >nul
goto loop
