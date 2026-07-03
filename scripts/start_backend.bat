@echo off
rem FastAPI backend с авторестартом при падении.
rem Логи: backend\logs\api.log (ротация 10МБ x5)
cd /d "%~dp0..\backend"
:loop
echo [%date% %time%] Starting uvicorn...
"..\venv\Scripts\uvicorn.exe" main:app --host 0.0.0.0 --port 8000
echo [%date% %time%] uvicorn exited (code %errorlevel%), restart in 10s...
timeout /t 10 /nobreak >nul
goto loop
