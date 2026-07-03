# Запуск сервисов

## Единый запуск (рекомендуется)

- `start_all.bat` — поднимает ВСЁ одним процессом: миграции БД → проверка моделей →
  FastAPI (:8000) + онлайн ML-цикл (5 мин). Авторестарт; Ctrl+C гасит оба сервиса.
  Под капотом — `backend/run_system.py`:

  ```bat
  cd backend && ..\venv\Scripts\python.exe run_system.py --station ohangaron
  ```

  Флаги: `--no-api` (только ML), `--no-ml` (только API), `--once` (один проход ML),
  `--train-if-missing` (обучить, если нет моделей), `--api-port N`.
  Защита от двойного запуска: lock `logs/run_system.lock` (+ `logs/live_predict.lock` для ML-цикла).

## Вручную, по отдельности (с авторестартом)

- `start_backend.bat` — FastAPI на :8000, рестарт через 10с после падения
- `start_live_predict.bat` — ML-цикл (5 мин), рестарт через 30с; защищён от двойного запуска файловым lock-ом

> Не запускайте `start_all.bat` вместе с `start_backend.bat`/`start_live_predict.bat` —
> используйте либо единый запуск, либо отдельные сервисы.

## Миграции и бэкафилл (однократно)

```bat
cd backend && ..\venv\Scripts\python.exe migrate_db.py --station ohangaron
cd backend && ..\venv\Scripts\python.exe backfill_health.py --station ohangaron
```

`migrate_db.py` идемпотентен (health→TEXT, журнал уведомлений, индексы).
`backfill_health.py` заполняет `raw_data.health` кодами аномалий по всей истории
(`--with-journal` — также пишет в `anomalies` и журнал).

## Автозапуск при загрузке Windows (Task Scheduler)

```bat
schtasks /create /tn "CS4 Backend" /tr "C:\Users\Timur\Desktop\UTG\КС\cs_4\scripts\start_backend.bat" /sc onstart /ru SYSTEM
schtasks /create /tn "CS4 LivePredict" /tr "C:\Users\Timur\Desktop\UTG\КС\cs_4\scripts\start_live_predict.bat" /sc onstart /ru SYSTEM
```

Либо NSSM (https://nssm.cc): `nssm install CS4Backend ...\scripts\start_backend.bat` — даёт настоящий Windows-сервис с restart=always.

## Диагностика

- `backend/logs/api.log` — логи API (ротация 10МБ×5)
- `backend/logs/live_predict.log` — логи ML-цикла (logger.*)
- `backend/logs/live_predict.out.log` — print-вывод и трейсбеки с таймстампами
- `GET /api/health` — `status: ok|degraded|down` (503 при degraded/down), возраст state-файла, доступность БД, model_drift
- `backend/logs/live_predict.lock` — PID работающего экземпляра

## Переобучение моделей (раз в 30 дней)

```bat
cd backend && ..\venv\Scripts\python.exe train_and_save_models.py --station ohangaron
```

Триггер внепланового переобучения: поле `model_drift.retrain_recommended=true` в `/api/health`.
