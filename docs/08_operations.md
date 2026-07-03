# 08. Эксплуатация

> ⚠️ **Актуальный запуск — host-логика через `run.py` (2026-07-01), см. [../ARCHITECTURE.md](../ARCHITECTURE.md).**
> Разделы 8.3/8.5 обновлены; двух-процессный запуск и `train_and_save_models` ниже — исторические.

## 8.1 Переменные окружения (`backend/.env`)
| Переменная | Назначение |
|-----------|-----------|
| `CS_DB_HOST` | Хост PostgreSQL |
| `CS_DB_PORT` | Порт (default 5432) |
| `CS_DB_NAME` | Имя БД |
| `CS_DB_USER` | Пользователь |
| `CS_DB_PASSWORD` | Пароль |
| `CS_ENSURE_INDEXES` | `1` → создать индексы при старте API (best-effort) |

Шаблон — `backend/.env.example`. Конфиг станции — `backend/config/stations/<id>.yaml` (схема БД, таблица, единицы, пути моделей/состояния), глобальный — `config/global.yaml`.

## 8.2 Установка
```bat
:: backend (Windows + venv)
cd backend
..\venv\Scripts\python.exe -m pip install -r requirements.txt

:: frontend
cd ..\frontend
npm install
```
Зависимости backend: fastapi, uvicorn[standard], pydantic≥2, pandas≥2, numpy, catboost≥1.2, psycopg2-binary, python-dotenv, PyYAML, scikit-learn.

## 8.3 Запуск (host-логика — единая точка входа)
```bat
:: бэк (FastAPI :8000) + фронт (Vite :5173) + ML-цикл под одним супервизором
python run.py --station ohangaron
::   --no-frontend        только бэк+ML
::   --frontend preview   собранный dist вместо dev-сервера
```
Открывать **`http://localhost:5173`** (Vite слушает `::1`; `127.0.0.1:5173` может не отвечать). API — `:8000`.
На старте `run_system.py` делает миграции → проверку моделей → uvicorn + `live_predict.run_continuous`
(с догоном пропуска `catch_up_missing`). Только бэк без фронта: `cd backend && ..\venv\Scripts\python.exe run_system.py --station ohangaron`.

Разовые операции:
```bat
cd backend
..\venv\Scripts\python.exe live_predict.py --mode once --station ohangaron          :: один прогон детекции
..\venv\Scripts\python.exe live_predict.py --station ohangaron --reprocess-from 2026-02-02  :: полная перезапись derived (idempotent)
```
Docker (только API): `Dockerfile` поднимает uvicorn на `:8000` (предиктор/фронт в образ не входят).

## 8.4 Автозапуск (Windows)
```bat
schtasks /create /tn "CS4 Backend"     /tr "...\scripts\start_backend.bat"      /sc onstart /ru SYSTEM
schtasks /create /tn "CS4 LivePredict" /tr "...\scripts\start_live_predict.bat" /sc onstart /ru SYSTEM
```
Либо NSSM — настоящий Windows-сервис с `restart=always`.

## 8.5 Обучение моделей
```bat
cd backend && ..\venv\Scripts\python.exe train.py --station ohangaron
:: до даты:            --cutoff-date 2026-06-12
:: в staging-каталог:  --output-dir models/ohangaron_staging
:: один ГПА (merge):   --gpa 1
```
Артефакты: `models/<station>/*.joblib` + `metadata.json` (normalized-conformal + эпистемика + pooled-по-типу).
Деплой нового набора: обучить в staging → held-out покрытие (`_validate_corridors.py`) → бэкап `models/<station>` → свап.
Дрейф в `/api/health` — информационный; `aging_watchdog.py` подсказывает, какой ГПА переобучить.

## 8.6 Мониторинг и логи (`backend/logs/`)
- `api.log` — логи API (ротация 10МБ×5).
- `live_predict.log` — логи ML-цикла (`logger.*`).
- `live_predict.out.log` — `print`-вывод и трейсбеки с таймстампами.
- `live_predict.lock` — PID работающего экземпляра предиктора.
- `GET /api/health` — `status: ok|degraded|down` (503 при degraded/down), возраст state-файла, доступность БД, `model_drift`.

## 8.7 Диагностика
| Симптом | Причина / проверка |
|---------|--------------------|
| `/api/health` = `degraded` | state старше 15 мин (предиктор отстал) или БД недоступна |
| `/api/health` = `down` | state старше 60 мин — предиктор не пишет |
| Предиктор не стартует | занят lock (`live_predict.lock`) — уже запущен другой экземпляр |
| Графики пустые | нет данных в `raw_data` за окно; проверить ingest и теги |
| `MemoryError` в логах | цикл сбрасывает накопленное и перезагружает историю (backoff) |
| MAE = 0 в UI | метрика не заполняется обучением (известный баг, план фаза 5) |
| Нет ML-аномалий на свежих данных | проверить `last_train_timestamp`: аномалии только при времени > него (онлайн) |

## 8.8 Резервное копирование / данные
- `sync_raw_data.py` дозаписывает хвост `raw_data` в CSV (`raw_data_2.csv`).
- БД — основной источник; `state.json` и `*.cbm` восстановимы (state — следующим циклом, модели — обучением).

## 8.9 Безопасность (важно для прод)
- API сейчас без аутентификации; CORS только `localhost`. Для общедоступной сети — добавить auth и сетевые ограничения, прописать прод-origin (см. [02_architecture_audit.md](02_architecture_audit.md), A5).
- Секреты БД — в `.env` (не коммитить).

## 8.10 Чек-лист дежурного
1. `GET /api/health` → `ok`.
2. Возраст state < 15 мин.
3. `db: ok`.
4. Критичные события в журнале/тикере отработаны (квитированы).
5. `model_drift.count` — для информации (переобучение не требуется).
