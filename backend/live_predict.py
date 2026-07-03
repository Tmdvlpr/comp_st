"""
Онлайн-предикт аномалий из PostgreSQL.
Загружает обученные модели (pickle/cbm), забирает данные из прода,
делает предикт и генерирует HTML-дашборд.

Логика:
1. Загружает метаданные и модели из папки models/
2. Подключается к БД и забирает ВСЕ данные после последней метки обучения
3. Делает предикт по каждому датчику
4. Каждые 5 минут берет свежий срез и добавляет предикт
5. Генерирует HTML-дашборд с результатами
"""
import logging
import os, re, sys, time, json
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import joblib
import psycopg2
from datetime import timedelta
import warnings

import detection_methods as DM
import weather as WX

# Вся методология обучения консолидирована в train.py; live импортирует её оттуда.
import train as TR
DF = DQ = RG = CAL = TR     # совместимость со старыми алиасами модулей (domain/quality/regime/calibrator)

if sys.platform == 'win32' and 'pytest' not in sys.modules:
    import io
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf8')
    except (AttributeError, ValueError):
        pass   # у подменённого stdout (Tee и т.п.) нет .buffer — оставляем как есть

warnings.filterwarnings('ignore')

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from station_config import load_station_config, get_db_connection, StationConfig
from data_loader import PostgresDataLoader
from anomaly_types import KIND_SEVERITY as _KIND_SEVERITY, KIND_TO_CODE, HEALTH_OK, HEALTH_STOPPED

logger = logging.getLogger('live_predict')

# ── Paths ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Interval (seconds) between live refreshes ──
REFRESH_INTERVAL = 300  # 5 minutes
# ── Max days of history to load on first run (prevents OOM on large DBs) ──
MAX_HISTORY_DAYS = 30
# ── Размер окна догона пропуска на старте (catch_up_missing). Меньше, чем
#    MAX_HISTORY_DAYS: оконные чанки держат пиковую память/длину запроса низкими
#    (statement_timeout=300с), а пропуск любой длины догоняется без OOM. ──
GAP_WINDOW_DAYS = 2

# ── Station globals (initialised by _init_station) ──
_station_cfg: "StationConfig | None" = None
_loader: "PostgresDataLoader | None" = None


def _init_station(station_id: str) -> None:
    """Ленивая инициализация конфига и загрузчика данных для станции."""
    global _station_cfg, _loader
    _station_cfg = load_station_config(station_id)
    _loader = PostgresDataLoader(_station_cfg)
    try:
        _loader.ensure_anomalies_table()
    except Exception:
        # БД недоступна на старте — не повод умирать: run_continuous
        # переживёт и дождётся возвращения БД (retry + backoff).
        logger.exception('ensure_anomalies_table при старте не выполнен — продолжаем')
    print(f'🏭 Станция: {_station_cfg.display_name} ({station_id})', flush=True)


def _require_station():
    if _station_cfg is None:
        raise RuntimeError("Станция не инициализирована. Вызовите _init_station(station_id) перед запуском.")


def load_models_and_metadata():
    """Загружает метаданные и все обученные модели из папки models/."""
    _require_station()
    models_dir = str(_station_cfg.models_path)
    meta_path = os.path.join(models_dir, 'metadata.json')
    if not os.path.exists(meta_path):
        raise FileNotFoundError(
            f"Файл метаданных {meta_path} не найден. "
            f"Сначала запустите train.py --station {_station_cfg.station_id}"
        )

    with open(meta_path, 'r', encoding='utf-8') as f:
        metadata = json.load(f)

    # Газовые константы методологии (для доменных фич в prepare_wide_data)
    try:
        DF.configure_gas((_station_cfg.methodology or {}).get('gas'))
    except Exception:
        pass

    models = {}
    skipped = 0
    _file_cache = {}     # общий __POOLED.joblib грузим ОДИН раз (на 3 ГПА), не дублируем в памяти
    for sensor_name, info in metadata['models'].items():
        model_path = os.path.join(models_dir, info['model_file'])
        if os.path.exists(model_path):
            try:
                if model_path.endswith('.joblib'):
                    # research-формат: dict {model, model_type, feat_cols, impute_median, needs_impute}.
                    # pooled: один файл на имя датчика → кэшируем (per-ГПА norm берётся из metadata).
                    if model_path not in _file_cache:
                        _file_cache[model_path] = joblib.load(model_path)
                    models[sensor_name] = _file_cache[model_path]
                else:
                    # legacy .cbm (CatBoost RMSEWithUncertainty) — обёртка для совместимости
                    from catboost import CatBoostRegressor
                    m = CatBoostRegressor(); m.load_model(model_path)
                    models[sensor_name] = {'model': m, 'model_type': 'CatBoostUnc',
                                           'feat_cols': info.get('feat_cols', []),
                                           'needs_impute': False, 'legacy_cbm': True}
                print(f"  ✅ {sensor_name.split('__')[0]}: загружена [{models[sensor_name].get('model_type','?')}]",
                      flush=True)
            except Exception as e:
                skipped += 1
                print(f"  ⚠️ {sensor_name.split('__')[0]}: пропущена (битый файл: {e})", flush=True)
        else:
            print(f"  ⚠️ {sensor_name}: модель не найдена ({model_path})", flush=True)

    if skipped:
        print(f"  ℹ️ Пропущено {skipped} моделей", flush=True)

    return metadata, models


def fetch_data_from_db(since_timestamp=None, until_timestamp=None, points=None):
    """
    Забирает данные из PostgreSQL (только SELECT!).
    Если since_timestamp задан, берет только после этой метки.
    until_timestamp (опц.) ограничивает окно сверху — важно для /explain старых
    аномалий: иначе тянется всё от since до NOW (дни данных вместо часов).
    points (опц., список точек) ограничивает выборку конкретными тегами: вместе с
    окном это включает индекс (point, datetime) → точечные range-scan вместо
    полного seq-scan всей raw_data (~2.5M строк). Для /explain — теги нужного ГПА.
    """
    try:
        from psycopg2 import sql as _sql
        _require_station()
        schema  = _station_cfg.db['schema']
        table   = _station_cfg.data['table']
        dt_col  = _station_cfg.data['datetime_col']
        pt_col  = _station_cfg.data['point_col']
        val_col = _station_cfg.data['value_col']
        print(f"🔗 Подключение к БД {_station_cfg.db['host']}:{_station_cfg.db['port']}/{_station_cfg.db['name']}...", flush=True)

        # Идентификаторы — через sql.Identifier (защита от инъекции, даже если конфиг
        # станции окажется скомпрометирован); значения — параметрами %s.
        _cols = _sql.SQL(", ").join(map(_sql.Identifier, [dt_col, pt_col, val_col]))
        _tbl  = _sql.SQL("{}.{}").format(_sql.Identifier(schema), _sql.Identifier(table))
        _dt   = _sql.Identifier(dt_col)

        # ── Границы окна [from_ts, to_ts] в UTC-aware (TIMESTAMPTZ хранится в UTC).
        #    Один большой ORDER BY-запрос на млн строк роняет коннект по таймауту —
        #    тянем ОКНАМИ GAP_WINDOW_DAYS (как catch_up/обучение), с ретраями на окно.
        def _utc(ts):
            t = pd.Timestamp(ts)
            return t.tz_localize('UTC') if t.tzinfo is None else t.tz_convert('UTC')

        with get_db_connection(_station_cfg) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT now()")
                _now = _utc(cur.fetchone()[0])
        if since_timestamp:
            from_ts = _utc(since_timestamp) - timedelta(seconds=30)
            to_ts = (_utc(until_timestamp) + timedelta(seconds=30)) if until_timestamp else (_now + timedelta(seconds=1))
        else:
            from_ts = _now - pd.Timedelta(days=int(MAX_HISTORY_DAYS))   # safety cap
            to_ts = _now + timedelta(seconds=1)

        print(f"📡 Оконная загрузка {str(from_ts)[:19]} .. {str(to_ts)[:19]}"
              f"{' (' + str(len(points)) + ' тегов)' if points else ''}, окно {GAP_WINDOW_DAYS}д...", flush=True)
        win = pd.Timedelta(days=GAP_WINDOW_DAYS)
        frames, w_lo, n_win = [], from_ts, 0
        while w_lo < to_ts:
            w_hi = min(w_lo + win, to_ts)
            conds = [_sql.SQL("{} > %s").format(_dt), _sql.SQL("{} <= %s").format(_dt)]
            params = [str(w_lo), str(w_hi)]
            if points:
                conds.append(_sql.SQL("{} = ANY(%s)").format(_sql.Identifier(pt_col)))
                params.append(list(points))
            q = _sql.SQL("SELECT {cols} FROM {tbl} WHERE {where} ORDER BY {dt}").format(
                cols=_cols, tbl=_tbl, where=_sql.SQL(" AND ").join(conds), dt=_dt)
            for attempt in range(3):
                try:
                    with get_db_connection(_station_cfg) as conn:
                        dfw = pd.read_sql_query(q.as_string(conn), conn, params=params)
                    break
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    from station_config import reset_pool
                    try: reset_pool(_station_cfg)
                    except Exception: pass
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            if len(dfw):
                frames.append(dfw)
            n_win += 1
            w_lo = w_hi
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        print(f"  📊 Получено {len(df)} строк ({n_win} окон)", flush=True)
        return df

    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}", flush=True)
        return pd.DataFrame()


def fetch_latest_slice_from_db(since_timestamp):
    """Забирает только свежий срез данных."""
    try:
        schema  = _station_cfg.db['schema']
        table   = _station_cfg.data['table']
        dt_col  = _station_cfg.data['datetime_col']
        pt_col  = _station_cfg.data['point_col']
        val_col = _station_cfg.data['value_col']
        ts = pd.Timestamp(since_timestamp) - timedelta(seconds=30)
        query = f"""
            SELECT {dt_col}, {pt_col}, {val_col}
            FROM {schema}.{table}
            WHERE {dt_col} > %s
            ORDER BY {dt_col};
        """
        with get_db_connection(_station_cfg) as conn:
            df = pd.read_sql_query(query, conn, params=[str(ts)])
        return df
    except Exception as e:
        print(f"❌ Ошибка: {e}", flush=True)
        return pd.DataFrame()


def prepare_wide_data(raw_df, tag_to_name):
    """Преобразует сырые данные в wide-формат."""
    df = raw_df.copy()
    if 'datetime' not in df.columns or df.empty:
        return pd.DataFrame()

    df['datetime'] = pd.to_datetime(df['datetime'], utc=True) \
                       .dt.tz_convert('Etc/GMT-5') \
                       .dt.tz_localize(None)

    df['value'] = pd.to_numeric(df['value'], errors='coerce')
    df['datetime'] = df['datetime'].dt.round('5min')
    df = df.sort_values('datetime').drop_duplicates(['datetime', 'point'])

    is_running_tags = [p for p in df['point'].unique() if 'is_gtd_status_running' in str(p) or '.STATES_GTD.5' in str(p)]
    if is_running_tags:
        status_df = df[df['point'].isin(is_running_tags)].pivot_table(
            index='datetime', columns='point', values='value'
        ).ffill().fillna(0)
        working_counts = status_df.sum(axis=1)
    else:
        status_df = None
        working_counts = pd.Series(0, index=df['datetime'].unique())

    df['feature'] = df['point'].map(tag_to_name)
    df_wide = df.dropna(subset=['feature']).pivot_table(
        index='datetime', columns='feature', values='value'
    ).sort_index().ffill(limit=2)

    df_wide['working_gpas_count'] = working_counts.reindex(df_wide.index).ffill().fillna(0)

    # Per-GPA running flag (__running_GPAn) — для подавления ложных аномалий
    # на остановленных агрегатах (датчики константны => "залипание", ML-модели
    # обучены на работе => огромные отклонения).
    if status_df is not None:
        for col in status_df.columns:
            m = re.search(r'GPA-(\d+)', str(col))
            if m:
                df_wide[f'__running_GPA{m.group(1)}'] = (
                    status_df[col].reindex(df_wide.index).ffill()
                )

    # ── research-методология: чистка LIMITS + ambient + доменные фичи ──
    meth = (_station_cfg.methodology or {}) if _station_cfg else {}
    limits = {k: tuple(v) for k, v in (meth.get('limits') or {}).items()}
    gpa_ids = [u.replace('GPA', '') for u in (_station_cfg.units if _station_cfg else [])]

    # физическая чистка по LIMITS (по базовому имени фичи для каждой __GPAn колонки)
    if limits:
        for c in list(df_wide.columns):
            base = re.sub(r'__GPA\d+$', '', c)
            lo_hi = limits.get(base)
            if lo_hi:
                lo, hi = lo_hi
                col = df_wide[c]
                df_wide[c] = col.mask((col < lo) | (col > hi))

    # ambient_temp на 5-мин сетку (Open-Meteo) — приведённые обороты/shaft/avo
    try:
        amb = WX.get_ambient_series(_station_cfg, df_wide.index)
        if amb is not None and amb.notna().any():
            for g in gpa_ids:
                df_wide[f'ambient_temp__GPA{g}'] = amb.values
    except Exception as _e:
        logger.debug('ambient в live недоступен: %s', _e)

    # доменные фичи по каждому ГПА (η_p, H_p, shaft mismatch, приведённые обороты, ...)
    try:
        running_by_gpa = {}
        for g in gpa_ids:
            rc = f'__running_GPA{g}'
            if rc in df_wide.columns:
                running_by_gpa[g] = (df_wide[rc].ffill().fillna(1.0) >= 0.5)
        df_wide = DF.add_domain_features_wide(df_wide, gpa_ids,
                                              running_by_gpa=running_by_gpa or None,
                                              train_cutoff=None)
    except Exception as _e:
        logger.warning('доменные фичи в live не посчитаны: %s', _e)

    return df_wide


def _num(v, default):
    """Число или default. Защита read-сайтов от None в metadata (v2 эмитит None для nan-метрик;
    .get(default) не спасает — ключ присутствует со значением None). isinstance, НЕ `or`:
    реальный 0.0 не должен подменяться default'ом."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _parse_train_ts(v):
    """Парс last_train_timestamp / пер-юнитного cutoff в naive Etc/GMT-5.
    tz-aware → конвертим в Etc/GMT-5 и снимаем tz; naive (так пишут train_all/train_v2 —
    это УЖЕ локаль станции Etc/GMT-5) оставляем как есть. Прежний безусловный utc=True
    трактовал naive-метку как UTC → +5ч сдвиг is_live (мёртвая зона детекции после cutoff)."""
    t = pd.to_datetime(v)
    if getattr(t, 'tzinfo', None) is not None:
        t = t.tz_convert('Etc/GMT-5').tz_localize(None)
    return t


def _v2_regime_keys(df_wide, target, index, info, metadata):
    """regime_key на каждую точку index — ТЕМИ ЖЕ порогами, что train (regime_config).
    Строится на срезе одного ГПА (семантические имена), включая доменные фичи из df_wide."""
    import dataclasses
    gpa_num = target.rsplit('__GPA', 1)[-1]
    suf = f'__GPA{gpa_num}'
    gcols = [c for c in df_wide.columns if c.endswith(suf)]
    df_gpa = df_wide[gcols].copy()
    df_gpa.columns = [c[:-len(suf)] for c in gcols]
    df_gpa = df_gpa.reindex(index)
    _fields = {f.name for f in dataclasses.fields(RG.RegimeConfig)}
    cfg = RG.RegimeConfig(**{k: v for k, v in (metadata.get('regime_config') or {}).items()
                            if k in _fields})
    lab = RG.label_regime(df_gpa, cfg)
    sm = RG.sub_mode(df_gpa, cfg)
    lbm = info.get('load_binning') or {}
    binning = RG.LoadBinning(axis=lbm.get('axis'), edges=lbm.get('edges'),
                             cv=lbm.get('cv', 0.0), n_bins=lbm.get('n_bins', 1))
    return RG.regime_key(lab, sm, RG.load_bin_labels(df_gpa, binning))


def predict_sensor(model, df_wide, target, info, metadata):
    """Делает предикт для одного датчика и возвращает результат."""
    # Пер-юнитный last_train_ts (v2, вариант B): standby-агрегат имеет свой cutoff;
    # legacy → глобальный. Оба парсятся одинаково (та же tz-конвенция → сравнимы в is_live).
    last_train_ts = _parse_train_ts(metadata['last_train_timestamp'])
    _unit_ltt = info.get('last_train_ts')
    if _unit_ltt:
        last_train_ts = _parse_train_ts(_unit_ltt)

    # feat_cols — из обёртки модели (совпадает с feature_names_in_; authoritative),
    # fallback на metadata. Имена как в обучении (без суффикса ГПА).
    _wrap_feats = (model.get('feat_cols') if isinstance(model, dict) else None)
    feat_raw = list(_wrap_feats or info.get('feat_cols', []))
    if not feat_raw or target not in df_wide.columns:
        return None
    # В обучении фичи un-suffixed (срез одного ГПА), в live колонки суффиксированы
    # `<feature>__GPAn`. Резолвим каждую фичу к колонке нужного ГПА; для предикта
    # переименуем обратно к обучающим именам (модели хранят feature_names_in_).
    gpa_suf = '__GPA' + target.rsplit('__GPA', 1)[-1]
    resolved = {}            # обучающее_имя -> live-колонка
    for f in feat_raw:
        if f in df_wide.columns:
            resolved[f] = f
        elif (f + gpa_suf) in df_wide.columns:
            resolved[f] = f + gpa_suf
        else:
            return None       # фича недоступна live — модель не применима
    df_t = df_wide[[target] + list(resolved.values())].copy()
    df_t = df_t.dropna(subset=[target])
    if len(df_t) < 2:
        return None
    feat_cols = feat_raw      # дальше работаем в обучающих именах (после rename)

    # ── Running-гейт: на остановленном агрегате ML/frozen/roc/seasonal не
    # имеют смысла (датчики константны, модели обучены на работе).
    # gas_leak-датчики мониторятся всегда. fail-open: нет статуса => работа.
    gpa_num = target.rsplit('__GPA', 1)[-1]
    run_col = f'__running_GPA{gpa_num}'
    status_known = run_col in df_wide.columns        # D3: известен ли статус ГПА
    if status_known:
        running = df_wide[run_col].reindex(df_t.index).ffill().fillna(1.0)
    else:
        running = pd.Series(1.0, index=df_t.index)   # fail-open: нет статуса => работа
    is_stopped = np.asarray(running < 0.5)
    _restart = (running.diff() > 0.5)
    warm = np.asarray(_restart.rolling(6, min_periods=1).max().fillna(0) > 0)   # 30 мин прогрева
    transition = np.asarray(                                                    # ±2 точки вокруг пуска/останова
        (running.diff().abs() > 0.5).astype(float)
        .rolling(5, min_periods=1, center=True).max().fillna(0) > 0
    )
    if 'gas_leak' in target.lower():
        # Загазованность мониторим всегда (ml/roc активны), но frozen/neg на
        # стоянке глушим: анализатор отключён вместе с САУ — константа и дрейф
        # нуля не авария.
        suppress = np.zeros_like(is_stopped, dtype=bool)
        suppress_static = is_stopped
    else:
        suppress = is_stopped
        suppress_static = is_stopped

    wrapper = model if isinstance(model, dict) else {'model': model, 'needs_impute': False}
    mdl = wrapper['model']
    # POOLED (кросс-ГПА термо/механика): общая модель обучена на z-нормализованных данных.
    # Оборачиваем её в адаптер с norm-параметрами ЭТОГО ГПА: вход нормализуется, выход
    # (mean/var/виртуальные ансамбли) де-нормализуется в сырые единицы ГПА. Дальше predict_sensor
    # работает как с обычной RMSEWithUncertainty-моделью — потоково, без батча/переобучения.
    if info.get('pooled') and info.get('norm'):
        try:
            _nrm = info['norm']
            mdl = TR._PooledAdapter(mdl, list(_nrm['feat']),
                                    pd.Series(_nrm['feat_mu']), pd.Series(_nrm['feat_sd']),
                                    _nrm['tgt_mu'], _nrm['tgt_sd'])
        except Exception as _pe:
            logger.warning('pooled-адаптер %s не построен (%s) — модель как есть', target, _pe)
    # X с ИМЕНАМИ как в обучении (un-suffixed): берём live-колонки и переименовываем
    Xdf = df_t[list(resolved.values())].rename(columns={v: k for k, v in resolved.items()})
    Xdf = Xdf[feat_cols]    # порядок столбцов как в обучении
    if wrapper.get('needs_impute'):
        Xdf = Xdf.fillna(pd.Series(wrapper.get('impute_median', {}) or {})).fillna(0.0)
    y = df_t[target].values
    # Предикт: CatBoost RMSEWithUncertainty возвращает 2D [mean, var].
    _raw = np.asarray(mdl.predict(Xdf.values if wrapper.get('legacy_cbm') else Xdf), float)
    if _raw.ndim == 2 and _raw.shape[1] >= 2:
        y_mean, y_var = _raw[:, 0], _raw[:, 1]
    else:
        y_mean, y_var = _raw, None

    # ── UNIVARIATE_BAND (self-conformal): датчик НЕ предсказуем из соседей (напр. вибрация) →
    #    «предикт» = healthy-медиана режима (центр нормального диапазона). Кросс-сенсорный предикт
    #    модели игнорируем. hw (self-порог) переопределяется ниже. resid/ml_mask/полоса дальше
    #    считаются от этого y_mean автоматически. Ветка активна ТОЛЬКО для detector_mode=univariate_band. ──
    _ub = bool(metadata.get('schema_version') == 'v2'
               and info.get('detector_mode') == 'univariate_band'
               and info.get('self_centers') and (info.get('calibration') or {}).get('by_regime'))
    _ub_rk = None
    if _ub:
        try:
            _ub_rk = _v2_regime_keys(df_wide, target, df_t.index, info, metadata)
            _centers = info['self_centers'] or {}
            _cvals = _ub_rk.map(lambda k: _centers.get(str(k), _centers.get(k))).astype(float).values
            y_mean = np.where(np.isfinite(_cvals), _cvals, y_mean)   # центр режима где известен
        except Exception as _e:
            logger.debug('univariate_band %s: центры режимов не построены (%s)', target, _e)
            _ub = False

    _th = (_station_cfg.methodology or {}).get('thresholds', {}) if _station_cfg else {}
    sr = info.get('sensor_range') or max(float(np.nanmax(y) - np.nanmin(y)), 1.0)
    min_buf = float(_th.get('min_buffer_pct', metadata.get('min_buffer_pct', 0.15)) or 0.15)

    # Откалиброванный остаток (research) — ПОЛ коридора.
    calib_scale = float(info.get('calib_scale', 0.0) or 0.0)
    residual_std_val = float(info.get('residual_std_val', 0.0) or 0.0)
    base_scale = max(calib_scale, residual_std_val, sr * 1e-4, 1e-9)

    # Коридор нормы. Если есть НЕОПРЕДЕЛЁННОСТЬ модели (CatBoost) — y_std из сглаженной
    # дисперсии (как раньше), но не ниже калиброванного остатка. Иначе — базовый масштаб.
    if y_var is not None:
        n_sigma_default = float(metadata.get('anomaly_n_sigma', _th.get('n_sigma', 5.0)) or 5.0)
        var_smooth = int(metadata.get('var_smoothing', _th.get('var_smoothing', 24)) or 24)
        y_var_s = pd.Series(np.maximum(y_var, 0.0)).rolling(var_smooth, min_periods=1, center=True).mean().values
        y_std = np.maximum(np.sqrt(np.maximum(y_var_s, 1e-12)), base_scale)
    else:
        n_sigma_default = float(_th.get('n_sigma', 3.0))
        y_std = np.full(len(y), base_scale, dtype=float)

    # ── Калибровка n_sigma под целевую долю ложных ~1.5% (D1/D2) ──
    # На рабочем режиме (running, без переходов) нормальные остатки должны
    # укладываться в коридор n_sigma·y_std. Берём 98.5-й перцентиль |resid|/y_std
    # как калиброванный n_sigma (FP≈1.5%), границы [4,7] — не уходим далеко от 5.
    # Это же — эмпирическая проверка калибровки y_var (uncertainty CatBoost):
    # если y_std занижен, перцентиль вырастет (коридор расширится), и наоборот.
    resid = np.abs(y - y_mean)
    _calib_mask = np.asarray(running >= 0.5) & ~transition
    n_sigma = n_sigma_default
    n_sigma_cal = None
    if int(_calib_mask.sum()) >= 200:
        _rn = (resid / np.maximum(y_std, 1e-9))[_calib_mask]
        _rn = _rn[np.isfinite(_rn)]
        if _rn.size >= 200:
            n_sigma_cal = float(np.clip(np.percentile(_rn, 98.5), 3.0, 7.0))
            n_sigma = n_sigma_cal

    y_mean_ser = pd.Series(y_mean)
    speed_now = y_mean_ser.diff().abs()
    speed_future = y_mean_ser.diff().abs().shift(-1)
    max_speed = np.maximum(speed_now, speed_future.fillna(0))

    transient_speed = max_speed / np.maximum(y_std, 0.01)
    transient_boost = 1.0 + np.minimum(transient_speed * 0.5, 3.0)

    # Порог коридора завязан на нормированную MAE (nMAE = MAE/sensor_range):
    # хуже модель (больше nMAE) → шире минимальный коридор. Заменяет старую
    # лестницу по in-sample R² (был завышен → коридор занижался).
    nmae = info.get('nmae_val')
    if nmae is None:
        # модель обучена до миграции на MAE — fallback на старую логику по R²
        r2 = _num(info.get('r2_train'), 0.5)   # v2 может дать None → не крашим на сравнении
        min_abs_pct = (0.10 if r2 >= 0.95 else 0.20 if r2 >= 0.80
                       else 0.30 if r2 >= 0.50 else 0.50)
    else:
        min_abs_pct = (0.10 if nmae <= 0.02 else 0.20 if nmae <= 0.05
                       else 0.30 if nmae <= 0.10 else 0.50)
    mae_val = float(info.get('mae_val', 0.0) or 0.0)
    min_abs_error = max(sr * min_abs_pct, 3.0 * mae_val)

    hw = np.maximum(n_sigma * y_std, sr * min_buf) * transient_boost.values
    hw = np.maximum(hw, min_abs_error)
    # пол коридора — conformal-порог (research, на свежей норме holdout)
    _conf = info.get('conformal_thr')
    if _conf:
        hw = np.maximum(hw, float(_conf))

    y_abs_mean = float(np.abs(np.nanmean(y)))
    if y_abs_mean > 0:
        hw = np.maximum(hw, y_abs_mean * 0.003)

    # ── НОРМАЛИЗОВАННЫЙ conformal-коридор ПО РЕЖИМУ (ДЕТЕКТОР 1, гибрид): hw = q̂(режим)·σ,
    #    σ из ВИРТУАЛЬНОГО АНСАМБЛЯ (σ²=u_epi+u_ale). Узкий в знакомом режиме, САМ раздувается
    #    там, где модель не уверена (σ велик). q̂ — безразмерный множитель (нонконформити-скор
    #    при калибровке = |факт−предикт|/σ). Эпистемика u_epi (+маргинальная feat_range) →
    #    ДЕТЕКТОР 2 (новизна, kind=regime). nominal hw выше — fallback для некалиброванных точек. ──
    _v2 = (metadata.get('schema_version') == 'v2') and isinstance(info.get('detector_mode'), str)
    _v2_corr = bool(_v2 and info.get('detector_mode') == 'ml_corridor'
                    and (info.get('calibration') or {}).get('by_regime'))
    rk_vals = None
    corridor_valid = np.ones(len(y), dtype=bool)
    ood_flag = np.zeros(len(y), dtype=bool)         # ДЕТЕКТОР 2 (новизна): эпистемич ∨ маргинальная
    epi_series = np.full(len(y), np.nan)            # u_epi по точкам (для отображения, как в anomaly-html)
    epi_thr = None                                  # порог детектора 2 (κ·1.5) для линии на графике
    hw_alt = None                                   # АЛЬТЕРНАТИВНЫЙ коридор (оба считаются; для UI-тумблера)
    corridor_active = corridor_alt = None
    if _v2_corr:
        try:
            rk_ser = _v2_regime_keys(df_wide, target, df_t.index, info, metadata)
            art = CAL.CalibrationArtifact.from_dict(info['calibration'])
            rk_vals = rk_ser.values
            # σ и эпистемика u_epi — из ОДНОГО вызова виртуальных ансамблей (тот же источник,
            # что в калибровке train.train_sensor → покрытие валидно). σ²=u_epi+u_ale.
            _ntrees = int(getattr(mdl, 'tree_count_', 0) or 0)
            sigma_t, u_epi_t = TR.ensemble_sigma_uepi(
                mdl, Xdf, _ntrees, int(info.get('ve_count', TR.VE_COUNT)))
            epi_series = np.asarray(u_epi_t, float)      # сохраняем эпистемику по точкам
            # ДЕТЕКТОР 2, ось 1 — ЭПИСТЕМИЧЕСКАЯ: u_epi выше эталона healthy (×1.5 запас от флуда).
            er = info.get('epistemic_ref') or {}
            if er.get('know_p95'):
                epi_thr = float(er['know_p95']) * 1.5
                ood_flag = np.asarray(u_epi_t > epi_thr)
            # ДЕТЕКТОР 2, ось 2 — МАРГИНАЛЬНАЯ: вход вне обученного диапазона feat_ranges (±10%).
            # Это РАЗНАЯ ось (вскрыта фактом: ГПА-1/ГПА-3-лето); на ней предикт ненадёжен, σ её не ловит.
            fr = info.get('feat_ranges') or {}
            marg = np.zeros(len(y), dtype=bool)
            if fr:
                for _f, _rng in fr.items():
                    if _f in Xdf.columns and _rng and _rng[0] is not None and _rng[1] is not None:
                        _lo, _hi = float(_rng[0]), float(_rng[1]); _sp = (_hi - _lo) or 1.0
                        _v = np.asarray(Xdf[_f].values, float)
                        marg |= ((_v < _lo - 0.10 * _sp) | (_v > _hi + 0.10 * _sp)) & np.isfinite(_v)
            ood_flag = np.asarray(ood_flag) | marg
            # ── ДЕТЕКТОР 1 + ПЕРЕКЛЮЧАТЕЛЬ КОРИДОРА (прод, без рефита; хранятся оба порога):
            #   'conformal' — плоский: hw = q̂_abs (фикс. порог на |факт−предикт|)
            #   'hybrid'    — нормализованный: hw = q̂_norm·σ (раздувается с σ из ансамбля)
            # Источник: env CS_CORRIDOR_MODE > PER-SENSOR corridor_mode (из CV) > station-cfg > metadata > 'conformal'.
            # per-sensor выбор из _interval_cv.py (персональный CV) хранится в info['corridor_mode'];
            # None → фолбэк на глобальный (старые модели без поля тоже дают None). env перекрывает всё (ручной A/B).
            # corridor_valid = режим откалиброван И НЕ маргинальная экстраполяция (там предикт
            # ненадёжен). Эпистемику НЕ глушим — гибрид-коридор сам раздут σ («оба сразу»). ──
            _psm = info.get('corridor_mode')
            _psm = _psm.lower() if isinstance(_psm, str) else None
            _cmode = (os.environ.get('CS_CORRIDOR_MODE')
                      or _psm
                      or ((_station_cfg.methodology or {}).get('corridor_mode') if _station_cfg else None)
                      or metadata.get('corridor_mode') or 'conformal').lower()
            if _cmode not in ('conformal', 'hybrid'):
                _cmode = 'conformal'   # защита от опечатки в env/cfg: иначе corridor_active — мусор-строка
                                       # → _active_conf=False → conformal-полоса не гаснет на OOD (слом инварианта)
            # СЧИТАЕМ ОБА КОРИДОРА (оба порога хранятся в калибровке): активный ведёт детекцию +
            # основную полосу, альтернативный пишется в hw_alt → state.series.lo2/hi2 (UI-тумблер потом).
            _qabs = rk_ser.map({k: art.threshold_abs_for(k) for k in set(rk_vals)}).astype(float).values
            _qnrm = rk_ser.map({k: art.threshold_for(k) for k in set(rk_vals)}).astype(float).values
            hw_conf = _qabs                          # плоский: q̂_abs (на |факт−предикт|)
            hw_hyb = _qnrm * sigma_t                  # нормализованный: q̂_norm·σ (раздувается с σ)
            _active = hw_hyb if _cmode == 'hybrid' else hw_conf
            _altc = hw_conf if _cmode == 'hybrid' else hw_hyb
            corridor_active = _cmode
            corridor_alt = 'conformal' if _cmode == 'hybrid' else 'hybrid'
            corridor_valid = np.isfinite(_active) & ~marg
            hw = np.where(np.isfinite(_active), _active, hw)       # активный где валиден, иначе nominal
            hw = np.where(np.isfinite(hw), hw, sr * min_buf)
            hw_alt = np.where(np.isfinite(_altc), _altc, hw)       # альтернативный коридор (для кнопки)
        except Exception as _e:
            logger.warning('v2-коридор для %s не построен (%s) — nominal', target, _e)
            _v2_corr = False

    # UNIVARIATE_BAND: hw = self-conformal порог по режиму (calibrated на |значение−центр|).
    # Полоса = y_mean(центр режима) ± hw. Один коридор (без hybrid). Валиден где режим откалиброван
    # и агрегат работает. resid/oob/ml_mask ниже считаются от этого y_mean+hw автоматически.
    if _ub:
        try:
            _art_s = CAL.CalibrationArtifact.from_dict(info['calibration'])
            _thr = _ub_rk.map(lambda k: _art_s.threshold_for(k)).astype(float).values
            hw = np.where(np.isfinite(_thr), _thr, hw)
            corridor_active, corridor_alt, hw_alt = 'self', None, None
            corridor_valid = np.isfinite(_thr) & np.asarray(running >= 0.5)
        except Exception as _e:
            logger.debug('univariate_band %s: self-порог не построен (%s)', target, _e)
            _ub = False

    resid = np.abs(y - y_mean)

    is_big_enough = resid > (sr * 0.25)
    is_live = df_t.index > last_train_ts
    # Стохастические (механические) датчики: сигнал рваный, без диурнальной сезонности.
    # Для них грубые univariate-детекторы (seasonal/roc) и точечный пробой коридора (ml)
    # шумят — ниже они загрублены/гейтятся. Гладкие (temp/pressure/rpm) не трогаем.
    _stoch = any(t in target.lower() for t in ('vibro', 'axial', 'shift'))

    # 1. ML-аномалия (ДЕТЕКТОР 1, на стоянке подавлена; в прогрев — только грубые отклонения).
    # out-of-band: resid>hw, где hw = q̂(режим)·σ на калиброванных режимах (нормализованный
    # конформный коридор), иначе nominal. Эквивалентно нонконформити-скор |факт−предикт|/σ > q̂.
    oob = resid > hw
    ml_mask = oob & is_live & is_big_enough & ~suppress
    ml_mask = ml_mask & ~(warm & (resid <= hw * 2.0))
    # v2: единый fallback. ml_corridor — гасим ML на OOD/некалиброванных режимах
    # (univariate-детекторы frozen/neg/roc остаются); univariate_only — ML-коридор отключён весь.
    if _v2_corr or _ub:
        ml_mask = ml_mask & corridor_valid   # univariate_band: значение вне self-полосы своего режима
    elif _v2:
        ml_mask = np.zeros(len(y), dtype=bool)

    # PERSISTENCE на ml для стохастических датчиков. Конформный коридор по построению
    # выпускает ~(1−покрытие)≈2% точек наружу — это хвост, а НЕ событие; на рваном вибро
    # он рассыпает одиночные ромбы. Требуем K-из-N в трейлинг-окне: одиночный хвост гасится,
    # реальный кластер (зреющий дефект двигает много точек подряд) проходит. K/N из metadata
    # (тюнинг без кода). Trend (1a) добавляется ПОСЛЕ — он сам сустейнед, не фильтруется.
    if _stoch and ml_mask.any():
        _pk = int(metadata.get('ml_persist_k', 3) or 3)
        _pn = int(metadata.get('ml_persist_n', 5) or 5)
        if _pk > 1:
            _cnt = (pd.Series(ml_mask.astype(int), index=df_t.index)
                    .rolling(_pn, min_periods=1).sum().values)
            ml_mask = ml_mask & (_cnt >= _pk)

    # 1a. Трендовый дрейф (T5). AR-признаки (lag1/2/3 + roll6) позволяют модели
    # «следовать» за медленным дрейфом — резидуал остаётся мал и ml_mask молчит.
    # Ловим НЕ-AR сигналом: устойчивое монотонное смещение РЕАЛЬНОГО значения за
    # TREND_STEPS точек. Консервативно (>25% диапазона/6ч, сильная монотонность),
    # только process-переменные (давление/обороты/сдвиг/клапан) — temp/vibro/leak
    # дрейфуют естественно. Сворачивается в ml_mask (kind=1), словарь 1–7 не трогаем.
    TREND_STEPS = 72   # 6 ч при шаге 5 мин
    _t_low = target.lower()
    _trend_ok = any(k in _t_low for k in ('pressure', 'rpm', 'shift', 'valve', 'flow'))
    if _trend_ok and len(y) > TREND_STEPS:
        _y_s   = pd.Series(y, index=df_t.index)
        _delta = _y_s - _y_s.shift(TREND_STEPS)
        _net   = _y_s.diff().rolling(TREND_STEPS, min_periods=TREND_STEPS // 2).mean() * TREND_STEPS
        _run_b2 = pd.Series(np.asarray(running >= 0.5), index=df_t.index)
        _trend = (_delta.abs() > 0.25 * sr) & (_net.abs() > 0.7 * _delta.abs()) \
            & pd.Series(is_live, index=df_t.index) & _run_b2 \
            & ~pd.Series(suppress, index=df_t.index)
        ml_mask = ml_mask | np.asarray(_trend.fillna(False))

    # 1b. Скоростная аномалия (Rate of Change) — адаптивный порог по типу
    dy = pd.Series(y, index=df_t.index).diff().abs()
    t_lower = target.lower()
    if   'rpm' in t_lower:                                  roc_pct = 0.10
    elif 'temp' in t_lower:                                 roc_pct = 0.30
    elif 'vibro' in t_lower:                                roc_pct = 0.15
    elif 'gas_leak' in t_lower:                             roc_pct = 0.40
    elif 'pressure' in t_lower:                             roc_pct = 0.15
    elif 'valve' in t_lower or 'shift' in t_lower:         roc_pct = 0.25
    else:                                                   roc_pct = 0.20
    roc_threshold = sr * roc_pct
    # Шумовой пол: порог не ниже 8× медианного шага в train-периоде — шумные
    # по природе датчики (вибрация, газоанализаторы) не спамят roc-событиями,
    # стабильные не теряют чувствительность.
    _dy_train = dy[np.asarray(df_t.index <= last_train_ts)]
    if len(_dy_train) > 100:
        roc_threshold = max(roc_threshold, 8.0 * float(_dy_train.median()))
    if _stoch:
        # Рваный механический сигнал: |Δ| между соседними точками по природе большой,
        # 8×медиана недостаточна (ГПА-2 rear_support roc=574). Поднимаем порог до P99
        # реальной дисперсии шагов на рабочем режиме → звенит только верхний ~1% скачков.
        _dy_run = dy[np.asarray(running >= 0.5)].dropna()
        if len(_dy_run) > 100:
            roc_threshold = max(roc_threshold, float(np.percentile(_dy_run.values, 99)))
    # пуск/останов — легальный скачок (transition); на стоянке скачков нет.
    # Анти-спайк: скачок, вернувшийся за 2 точки, — выброс телеметрии, не roc.
    dy2 = pd.Series(y, index=df_t.index).diff(2).abs()
    roc_mask = (dy > roc_threshold) & (dy2 > 0.5 * roc_threshold) \
        & is_live & ~ml_mask & ~(suppress | transition)

    # 1c. Сезонная аномалия
    _train_mask = df_t.index <= last_train_ts
    _y_ser      = pd.Series(y, index=df_t.index)
    _hours      = pd.Series(df_t.index.hour, index=df_t.index)
    # почасовые статистики только по рабочим точкам — простои не загрязняют медианы
    _run_b      = np.asarray(running >= 0.5)
    _tr_b       = np.asarray(_train_mask) & _run_b
    _y_run      = _y_ser[_run_b]
    _hstats_tr  = _y_ser[_tr_b].groupby(df_t.index[_tr_b].hour).agg(['median', 'std'])
    _hstats_all = _y_run.groupby(_y_run.index.hour).agg(['median', 'std'])
    _BLEND = 0.6  # 60% training, 40% all-data
    _h_med      = _BLEND * _hours.map(_hstats_tr['median']) + (1 - _BLEND) * _hours.map(_hstats_all['median'])
    _h_std      = (_BLEND * _hours.map(_hstats_tr['std']).fillna(sr * 0.10) +
                   (1 - _BLEND) * _hours.map(_hstats_all['std']).fillna(sr * 0.10)).clip(lower=sr * 0.01)
    _season_thr = np.maximum(3.0 * _h_std.values, sr * 0.15)
    seasonal_mask = (np.abs(y - _h_med.values) > _season_thr) & np.asarray(is_live) & ~np.asarray(ml_mask) & ~suppress
    seasonal_mask = pd.Series(seasonal_mask, index=df_t.index)
    if _stoch:
        # Механический сигнал (вибро/осевой сдвиг) не имеет диурнальной сезонности —
        # почасовая медиана для него бессмысленна и флудит (ГПА-1 в univariate: 2600+
        # ложных). Сезонный детектор осмыслен для температур/давления, не для вибрации.
        seasonal_mask = pd.Series(False, index=df_t.index)
    else:
        # (a) Сезонка осмысленна только на РАБОТАЮЩЕМ агрегате: на стоянке значение ≠
        # рабочей почасовой медиане → ложные срабатывания (как у frozen). Гейтим по running
        # (это и давало ~400 на ГПА-1 — стоп-точки в univariate).
        seasonal_mask = seasonal_mask & pd.Series(np.asarray(running >= 0.5), index=df_t.index)
        # (b) Гейт доли: если сезонка звенит на БОЛЬШОЙ доле live-рабочих точек — это сдвиг
        # baseline train→live (летнее потепление сдвинуло уровень), а НЕ диурнальная аномалия
        # (уровень уже моделирует коридор). Иначе флуд на масляных/подшипниковых температурах
        # (oil_temp_in_pod GPA2=365). Диурнальная аномалия по природе редкая (<15%) — не задеваем.
        # Порог 0.05: измеренный разрыв между baseline-shift флудом (6–15% точек на
        # oil/bearing температурах летом) и редкими диурнальными аномалиями (≤1.6%).
        _lr = np.asarray(is_live) & np.asarray(running >= 0.5)
        if _lr.sum() > 50 and float(np.asarray(seasonal_mask.values)[_lr].mean()) > 0.05:
            seasonal_mask = pd.Series(False, index=df_t.index)

    # 2. Залипание (exact equality — 12+ одинаковых подряд = 60 мин;
    # на стоянке константные сигналы — норма, не залипание)
    s_y = pd.Series(y)
    blocks = (s_y != s_y.shift()).cumsum()
    frozen_mask = (s_y.groupby(blocks).transform('size') >= 12).values & is_live & ~suppress_static
    if _stoch:
        # На стоянке осевой сдвиг/вибро читаются константой → ложное «залипание». Реальное
        # залипание датчика имеет смысл только на РАБОТАЮЩЕМ агрегате — гейтим по running
        # (закрывает frozen=2673 на ГПА-1 axial во время простоя).
        frozen_mask = frozen_mask & np.asarray(running >= 0.5)

    # 3. Физичность: deadband 0.5% range (дрожание трансмиттера около нуля),
    # подтверждение 2 точки подряд (одиночный глюк телеметрии — не crit)
    is_negative = pd.Series(False, index=df_t.index)
    phys_sensors = ['pressure', 'fuel_gas', 'gas_leak', 'vibro', 'flow']
    if any(p in target.lower() for p in phys_sensors):
        neg_raw = pd.Series(
            (y < -max(0.005 * sr, 1e-6)) & is_live & ~suppress_static,
            index=df_t.index,
        )
        is_negative = neg_raw & neg_raw.shift(1, fill_value=False)

    # 1d. ДРЕЙФ ОСТАТКА (research): УСТОЙЧИВОЕ смещение остатка → kind=drift. Ловит
    # медленную деградацию, которую ml-коридор пропускает. Консервативно: только
    # предсказуемые датчики (nmae≤0.10), EWMA|остатка| > 4·σ_калибр, удержано ≥1ч,
    # на рабочем режиме, is_live, не дубль ml. Иначе у плохо-предсказуемых датчиков
    # смещённый остаток давал бы постоянные ложные срабатывания.
    drift_mask = pd.Series(False, index=df_t.index)
    _nmae_g = float(info.get('nmae_val', 1.0) or 1.0)
    if _nmae_g <= 0.10 and len(y) > 24:
        _work = np.asarray(running >= 0.5) & ~transition
        _zsig = np.where(_work, (y - y_mean), 0.0)
        _ew = DM.ewma(_zsig, float(_th.get('ewma_alpha', 0.05)))
        _draw = (np.abs(_ew) > 4.0 * base_scale) & np.asarray(is_live) & ~np.asarray(suppress) & _work
        _dfl = DM.run_length_filter(_draw, min_len=12)    # ≥1ч устойчиво (12×5мин)
        drift_mask = (pd.Series(_dfl, index=df_t.index)
                      & ~pd.Series(np.asarray(ml_mask), index=df_t.index))

    if _v2 and not _v2_corr:
        # univariate_only: модель ненадёжна (R²<τ) → дрейф остатка не считаем.
        # frozen/neg/roc/seasonal/trend — univariate (на сыром значении) — остаются.
        drift_mask = pd.Series(False, index=df_t.index)
    elif _v2_corr:
        # drift — на остатке модели; на OOD/некалиброванных точках (где ML заглушён
        # corridor_valid) ему тоже не доверяем → гейтим тем же corridor_valid.
        drift_mask = drift_mask & pd.Series(corridor_valid, index=df_t.index)

    # ДЕТЕКТОР 2 (новизна) — ПЕР-СЕНСОРНЫЙ сигнал: идёт ТОЛЬКО в фиолетовую полосу (series[].e =
    # u_epi) и в OOD-гейт коридора (детектор 1 молчит там, где не доверяет). НЕ порождает маркер
    # «смена режима» — иначе на OOD-агрегате (ГПА-1) звёзды сыплются ежедневно (шум).
    # «СМЕНА РЕЖИМА» (kind=regime) = СИСТЕМНОЕ событие УРОВНЯ ГПА: одинаковый дрейф по многим
    # core-датчикам агрегата одновременно (drift_ratio в _compute_results). regime_mask здесь
    # пуст — его заполнит GPA-цикл только на реальных системных сменах режима.
    regime_mask = pd.Series(False, index=df_t.index)

    anom_mask = ml_mask | frozen_mask | is_negative | roc_mask | seasonal_mask | drift_mask | regime_mask

    y_ser = pd.Series(y)
    drift_sr = np.maximum(sr, 0.5)
    drift = y_ser.diff(periods=6).abs() / drift_sr

    # Явные границы коридора для графика: симметрично предикт ± hw (активный коридор).
    # Альтернативный коридор (другой режим) — band_*_alt → series.lo2/hi2 (UI-тумблер).
    band_lo = y_mean - hw
    band_hi = y_mean + hw
    if hw_alt is not None:
        band_lo_alt = y_mean - hw_alt
        band_hi_alt = y_mean + hw_alt
    else:
        band_lo_alt = band_hi_alt = None

    return {
        'times': df_t.index,
        'reality': y,
        'prediction': y_mean,
        'hw': hw,
        'lo': band_lo,
        'hi': band_hi,
        # ── ОБА коридора: активный (lo/hi) + альтернативный (lo_alt/hi_alt) для UI-переключателя ──
        'lo_alt': band_lo_alt,
        'hi_alt': band_hi_alt,
        'corridor_active': corridor_active,
        'corridor_alt': corridor_alt,
        'anom_mask': anom_mask,
        'ml_mask': ml_mask,
        'frozen_mask': frozen_mask,
        'negative_mask': is_negative,
        'roc_mask': roc_mask,
        'seasonal_mask': seasonal_mask,
        'cross_mask': pd.Series(False, index=df_t.index),
        'regime_mask': regime_mask,   # FIX: детектор 2 (новизна) — было заглушкой False, не писался
        'drift_mask': drift_mask,
        'drift': drift.fillna(0).values,
        'running': np.asarray(running, dtype=float),
        'r2_train': info.get('r2_train', 0.0),   # честный r2_val; в UI не выводится (только MAE)
        'mae_val': float(info.get('mae_val', 0.0) or 0.0),
        'nmae_val': float(info.get('nmae_val', 0.0) or 0.0),
        'n_sigma_cal': n_sigma_cal,               # калиброванный n_sigma (None если данных мало)
        'status_known': status_known,             # D3: известен ли статус ГПА
        # ── v2 (опциональные; legacy-потребители игнорируют) ──
        'regime': rk_vals if rk_vals is not None else np.array(['n/a'] * len(y), dtype=object),
        'detector_mode': info.get('detector_mode', 'legacy'),
        'ood_flag': np.asarray(ood_flag, dtype=bool),
        'transition': np.asarray(transition, dtype=bool),   # ±2 точки вокруг пуска/останова: предикт скачет → коридор гасим (пики)
        # ── эпистемическая неопределённость (ДЕТЕКТОР 2) для отображения, как в anomaly-html ──
        'epistemic': epi_series,                  # u_epi по точкам (NaN где недоступна)
        'epistemic_thr': epi_thr,                 # порог новизны (κ·1.5); None если нет эталона
    }


# Глобальные кэши
_cached_metadata = None
_cached_models = None
_accumulated_raw_df = None


def _compute_results(df_wide, metadata, models):
    """Прогон всех детекторов по df_wide: predict_sensor (ml/frozen/neg/roc/
    seasonal/тренд) + кросс-ГПА + смены режима. Возвращает results-словарь
    (как для дашборда/состояния), БЕЗ HTML и записи в БД. Переиспользуется
    онлайн-циклом (run_once) и бэкафиллом по истории (backfill_health)."""
    results = {}
    for sensor_name, model in models.items():
        info = metadata['models'][sensor_name]
        result = predict_sensor(model, df_wide, sensor_name, info, metadata)
        if result is not None:
            results[sensor_name] = result
    if not results:
        return results

    # Кросс-ГПА анализ
    last_train_ts_loc = _parse_train_ts(metadata['last_train_timestamp'])
    base_names = set(s.rsplit('__', 1)[0] for s in results)
    for base in base_names:
        peers = [s for s in results if s.rsplit('__', 1)[0] == base]
        if len(peers) < 2:
            continue
        peer_cols = [s for s in peers if s in df_wide.columns]
        if len(peer_cols) < 2:
            continue
        df_p = df_wide[peer_cols].copy()
        # Остановленные агрегаты не участвуют в peer-сравнении (NaN):
        # они и статистику искажают, и сами дают ложный z-score
        for s in peer_cols:
            rc = f"__running_GPA{s.rsplit('__GPA', 1)[-1]}"
            if rc in df_wide.columns:
                df_p[s] = df_p[s].mask(df_wide[rc].ffill().fillna(1.0) < 0.5)
        peer_n   = df_p.notna().sum(axis=1)
        row_mean = df_p.mean(axis=1)
        row_std  = df_p.std(axis=1).clip(lower=1e-6)
        for s in peer_cols:
            sr_s  = metadata['models'][s].get('sensor_range', 1.0)
            z     = (df_p[s] - row_mean) / row_std
            abs_d = (df_p[s] - row_mean).abs()
            # peer_n >= 3: z-score из двух значений вырожден
            cross = (z.abs() > 2.5) & (abs_d > sr_s * 0.10) & (peer_n >= 3)
            cross = cross & (df_wide.index > last_train_ts_loc)
            # подтверждение 3 точки подряд (15 мин)
            cross = cross & cross.shift(1, fill_value=False) & cross.shift(2, fill_value=False)
            r_idx = results[s]['times']
            cross_aligned = cross.reindex(r_idx).fillna(False)
            cross_aligned = cross_aligned & ~results[s]['ml_mask']
            results[s]['cross_mask'] = cross_aligned

    # Детекция смен режима
    for gid in metadata['gpa_ids']:
        unit_sensors = [s for s in results.keys() if metadata['models'][s]['gpa_id'] == gid]
        if len(unit_sensors) < 5: continue

        master_keywords = ['rpm', 'pressure', 'flow', 'valve_pos', 'shift']
        core_sensors = [s for s in unit_sensors if any(kw in s.lower() for kw in master_keywords)
                        and not any(x in s.lower() for x in ['temp', 'vibro', 'leak'])]
        if not core_sensors: core_sensors = unit_sensors

        drift_matrix_data = {}
        for s in core_sensors:
            if s not in df_wide.columns:
                continue
            drift_sr = max(metadata['models'][s].get('sensor_range', 1.0), 0.5)
            s_drift = df_wide[s].diff(periods=6).abs() / drift_sr
            drift_matrix_data[s] = s_drift.fillna(0.0)

        drift_matrix = pd.DataFrame(drift_matrix_data)

        ranges = pd.Series({s: metadata['models'][s]['sensor_range'] for s in core_sensors})
        drift_mask_pct = drift_matrix.abs() > 0.17
        drift_mask_abs = (drift_matrix.abs().mul(ranges, axis=1)) > 0.02
        drift_mask = drift_mask_pct & drift_mask_abs

        drift_ratio = drift_mask.mean(axis=1).rolling(3, min_periods=1, center=True).mean()

        rpm_cols = [s for s in drift_mask.columns if 'rpm' in s.lower()]
        if rpm_cols:
            rpm_moving = drift_mask[rpm_cols].max(axis=1).rolling(3, min_periods=1, center=True).mean()
            event_mask = (drift_ratio >= 0.40) & (rpm_moving >= 0.5)
        else:
            event_mask = drift_ratio >= 0.7

        events = []
        if event_mask.any():
            event_id = (event_mask != event_mask.shift()).cumsum()
            active_events = event_id[event_mask]
            for eid in active_events.unique():
                block_indices = active_events[active_events == eid].index
                mid_t = block_indices[len(block_indices)//2]
                window = drift_ratio[mid_t - pd.Timedelta(minutes=60) : mid_t + pd.Timedelta(minutes=60)]
                if not window.empty:
                    peak_t = window.idxmax()
                    if not events or (peak_t - events[-1] > pd.Timedelta(hours=2)):
                        events.append(peak_t)

        regime_shifts = pd.DatetimeIndex(events)
        print(f"  📍 ГПА-{gid}: смены режима ({len(events)}): {[t.strftime('%m-%d %H:%M') for t in events]}", flush=True)

        if not regime_shifts.empty:
            suppression_mask = pd.Series(False, index=df_wide.index)
            for t in regime_shifts:
                t_start = t - pd.Timedelta(minutes=15)
                t_end   = t + pd.Timedelta(minutes=15)
                suppression_mask |= (df_wide.index >= t_start) & (df_wide.index <= t_end)
        else:
            suppression_mask = pd.Series(False, index=df_wide.index)

        # Смена режима — событие уровня ГПА: маркер должен быть виден на графике
        # ЛЮБОГО датчика агрегата (а не только там, где датчик сам «поехал»).
        # Ставим один ближайший тик сетки на каждую обнаруженную смену.
        gpa_shift_mask = pd.Series(False, index=df_wide.index)
        for t in regime_shifts:
            pos = df_wide.index.get_indexer([t], method='nearest')
            if len(pos) and pos[0] >= 0:
                gpa_shift_mask.iloc[pos[0]] = True

        for s in unit_sensors:
            s_drift_full = pd.Series(results[s]['drift'], index=results[s]['times']).reindex(df_wide.index, fill_value=0.0)
            sensor_stars = []
            individual_suppression_mask = pd.Series(False, index=df_wide.index)

            for t in regime_shifts:
                t_window = s_drift_full[t - pd.Timedelta(minutes=10) : t + pd.Timedelta(minutes=10)]
                if not t_window.empty and t_window.max() > 0.30:
                    sensor_stars.append(t)
                    t_start = t - pd.Timedelta(minutes=15)
                    t_end   = t + pd.Timedelta(minutes=15)
                    individual_suppression_mask |= (df_wide.index >= t_start) & (df_wide.index <= t_end)

            # is_live-гейт: regime-маркеры (как и остальные детекторы) фиксируются
            # только ПОСЛЕ last_train_timestamp (chart_anoms всё равно режет по cutoff).
            # МАРКЕР смены режима — на КАЖДОМ датчике ГПА (gpa_shift_mask), чтобы
            # оператор видел переход на любом графике агрегата.
            regime_pts = results[s]['times'].isin(df_wide.index[gpa_shift_mask]) \
                         & (results[s]['times'] > last_train_ts_loc)
            results[s]['regime_mask'] |= regime_pts
            # стат-счётчик «Режим» (regime_sensors) — по датчикам, что реально «поехали»
            results[s]['regime_points'] = sensor_stars

            # ПОДАВЛЕНИЕ остальных детекторов (roc/seasonal/ml) — только в окне ±15 мин
            # тех смен, где ЭТОТ датчик действительно сдвинулся (individual mask).
            mask_to_clear = results[s]['times'].isin(df_wide.index[individual_suppression_mask]) \
                            & (results[s]['times'] > last_train_ts_loc)
            # Selective: keep ML anomalies where residual > 2× threshold
            _resid = np.abs(results[s]['reality'] - results[s]['prediction'])
            _hw = results[s]['hw']
            _regime_keep = _resid > (_hw * 2.0)
            results[s]['ml_mask'][mask_to_clear & ~_regime_keep] = False
            # Смена режима — легальный переходный процесс и для roc/seasonal
            results[s]['roc_mask'][mask_to_clear] = False
            results[s]['seasonal_mask'][mask_to_clear] = False
            results[s]['anom_mask'] = (
                results[s]['ml_mask'] |
                results[s]['frozen_mask'] |
                results[s]['negative_mask'] |
                results[s]['roc_mask'] |
                results[s]['seasonal_mask'] |
                results[s]['cross_mask'] |
                results[s].get('drift_mask', False)
            )

    return results


def run_once(existing_df=None):
    """Выполняет один цикл предикта."""
    global _accumulated_raw_df, _cached_metadata, _cached_models
    t0 = time.time()

    if _cached_models is None:
        print('=' * 60, flush=True)
        print('🚀 ОНЛАЙН-ПРЕДИКТ: Загрузка моделей и истории...', flush=True)
        _cached_metadata, _cached_models = load_models_and_metadata()

    metadata = _cached_metadata
    models = _cached_models
    tag_to_name = metadata['tag_to_name']

    if existing_df is None:
        last_train_ts = metadata['last_train_timestamp']
        # Cap to MAX_HISTORY_DAYS to avoid OOM on large historical datasets
        cutoff = (pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=MAX_HISTORY_DAYS)).isoformat()
        since_ts = cutoff if pd.Timestamp(last_train_ts, tz='UTC') < pd.Timestamp(cutoff) else str(last_train_ts)
        print(f'📡 Загрузка истории из БД (с {since_ts[:10]}, макс {MAX_HISTORY_DAYS} дн)...', flush=True)
        raw_df = fetch_data_from_db(since_timestamp=since_ts)
    else:
        last_ts_in_mem = pd.to_datetime(existing_df['datetime'], utc=True).max()
        print(f'🔄 ОБНОВЛЕНИЕ: Запрос данных после {last_ts_in_mem}...', flush=True)
        new_db_df = fetch_latest_slice_from_db(str(last_ts_in_mem))
        if new_db_df.empty:
            print("  ℹ️ Новых срезов в БД пока нет", flush=True)
            return existing_df
        print(f"  📊 Добавлено {len(new_db_df)} строк", flush=True)
        raw_df = pd.concat([existing_df, new_db_df], ignore_index=True)
        # Trim rows older than MAX_HISTORY_DAYS — prevents unbounded memory growth
        _trim_cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days=MAX_HISTORY_DAYS)
        _dt_all = pd.to_datetime(raw_df['datetime'], utc=True)
        before_trim = len(raw_df)
        raw_df = raw_df[_dt_all >= _trim_cutoff].reset_index(drop=True)
        if len(raw_df) < before_trim:
            print(f"  ✂️ Обрезано {before_trim - len(raw_df)} строк старше {MAX_HISTORY_DAYS} дн", flush=True)

    if raw_df.empty:
        print("❌ Нет данных!", flush=True)
        return None

    _accumulated_raw_df = raw_df
    df_wide = prepare_wide_data(raw_df, tag_to_name)

    results = _compute_results(df_wide, metadata, models)
    if not results:
        print("❌ Нет результатов для дашборда!", flush=True)
        return raw_df

    # Статический HTML-дашборд удалён как legacy: фронт-UI (Vite/React) читает API + live_state.json.
    _write_live_state(results, metadata, models=models, df_wide=df_wide)
    print(f'✅ Цикл завершен за {int(time.time()-t0)} сек', flush=True)
    return raw_df


_MASK_TO_KIND = {
    'ml_mask': 'ml', 'frozen_mask': 'frozen', 'negative_mask': 'neg',
    'roc_mask': 'roc', 'seasonal_mask': 'seasonal',
    'regime_mask': 'regime', 'cross_mask': 'cross',
    'drift_mask': 'drift',
}
# _KIND_SEVERITY imported from anomaly_types — единый источник правды

# Сколько последних часов мониторинга писать в raw_data.health на каждом цикле.
# Полную историю заполняет backfill_health.py (однократно); live-цикл держит
# свежий хвост, не переписывая всё окно каждые 5 мин.
HEALTH_WINDOW_HOURS = 6
# Полуширина 5-мин бакета (под pandas .round('5min') — округление к ближайшему).
_HEALTH_BUCKET = pd.Timedelta(seconds=150)

# Человекочитаемые русские метки для message журнала уведомлений.
_KIND_LABEL_RU = {
    'ml':       'Статистический выброс',
    'neg':      'Нефизичное значение',
    'frozen':   'Залипание датчика',
    'roc':      'Резкий скачок значения',
    'seasonal': 'Сезонное отклонение',
    'regime':   'Смена режима',
    'cross':    'Кросс-ГПА отклонение',
    'drift':    'Дрейф (деградация)',
    'index':    'Отклонение доменного индекса',
}


def _event_message(ev):
    """Человекочитаемый RU-message для эпизода аномалии (журнал и anomalies_t)."""
    kind = ev.get('kind', 'ml')
    s = ev.get('sensor_id')
    gpa_num = str(ev.get('gpa', '')).replace('GPA', '')
    label = _KIND_LABEL_RU.get(kind, kind)
    msg = f"{str(ev.get('sensor_name', s)).replace('_', ' ')} (ГПА-{gpa_num}): {label}"
    if ev.get('value') is not None:
        msg += f", значение {ev['value']}"
    if ev.get('deviation') is not None:
        msg += f", отклонение {ev['deviation']}%"
    return msg


def _local_naive_to_utc(ts_local):
    """Локальная naive-метка (Etc/GMT-5) -> aware UTC pandas Timestamp."""
    return pd.Timestamp(ts_local).tz_localize('Etc/GMT-5').tz_convert('UTC')


def _collect_health_rows(results, metadata, full=False):
    """Строит строки для update_health: на свежих точках мониторинга — коды
    сработавших масок ("1,4"), на здоровых оценённых — "0". Окно — последние
    HEALTH_WINDOW_HOURS (но не раньше last_train_timestamp).
    full=True (historical) — пишем за ПОЛНОЕ окно results (без 6ч-усечения):
    для reprocess_history (полная перезапись истории / самовосстанавливающийся догон)."""
    name_to_tag = metadata.get('name_to_tag', {})
    last_train_ts = _parse_train_ts(metadata['last_train_timestamp'])
    win = pd.Timedelta(hours=HEALTH_WINDOW_HOURS)
    rows = []
    for s, r in results.items():
        point = name_to_tag.get(s)
        if not point:
            continue
        times = r['times']
        if len(times) == 0:
            continue
        cutoff = None if full else max(last_train_ts, times[-1] - win)
        masks = {kind: np.asarray(r.get(mk)).astype(bool)
                 for mk, kind in _MASK_TO_KIND.items() if r.get(mk) is not None}
        running = np.asarray(r.get('running', np.ones(len(times))), dtype=float)
        for i in range(len(times)):
            ti = times[i]
            if cutoff is not None and ti <= cutoff:
                continue
            codes = sorted(KIND_TO_CODE[k] for k, arr in masks.items()
                           if i < len(arr) and arr[i])
            # приоритет: аномалии → остановлен → норма
            if codes:
                health = ",".join(str(c) for c in codes)
            elif i < len(running) and running[i] < 0.5:
                health = HEALTH_STOPPED      # ГПА остановлен
            else:
                health = HEALTH_OK
            utc = _local_naive_to_utc(ti)
            rows.append({
                'point':  point,
                'health': health,
                't0':     (utc - _HEALTH_BUCKET).to_pydatetime(),
                't1':     (utc + _HEALTH_BUCKET).to_pydatetime(),
            })
    return rows


def _build_notifications(state_events, metadata):
    """Преобразует эпизоды state_events в записи журнала уведомлений
    (point из name_to_tag, event_ts в UTC, человекочитаемый message)."""
    name_to_tag = metadata.get('name_to_tag', {})
    station_id = _station_cfg.station_id if _station_cfg is not None else metadata.get('station_id', '')
    notif = []
    for ev in state_events:
        s = ev.get('sensor_id')
        kind = ev.get('kind', 'ml')
        try:
            ev_utc = _local_naive_to_utc(ev['timestamp']).to_pydatetime()
        except Exception:
            continue
        notif.append({
            'station_id':   station_id,
            'sensor_id':    s,
            'point':        name_to_tag.get(s),
            'gpa':          ev.get('gpa'),
            'event_ts':     ev_utc,
            'kind':         kind,
            'severity':     ev.get('severity'),
            'value':        ev.get('value'),
            'deviation':    ev.get('deviation'),
            'message':      _event_message(ev),
            'status':       'new',
        })
    return notif


def _shap_top_for_event(models, df_wide, sensor_id, peak_ts_local, k=5, info=None):
    """Локальная атрибуция (SHAP) одной аномалии: топ-K признаков-вкладчиков на
    момент пика остатка. Возвращает list[{name, contrib, value}] или None при
    мягкой деградации (не CatBoost / нет признаков / ошибка).
    info (опц.) — запись metadata датчика; для pooled даёт norm-параметры: SHAP
    считаем в НОРМАЛИЗОВАННОМ пространстве (как обучали общую модель), значение
    для отображения — сырое."""
    try:
        if df_wide is None or not models or df_wide.empty:
            return None
        wrapper = models.get(sensor_id)
        if not isinstance(wrapper, dict):
            return None
        if 'CatBoost' not in str(wrapper.get('model_type', '')):
            return None        # ShapValues через get_feature_importance — только CatBoost
        mdl = wrapper.get('model')
        feat_raw = list(wrapper.get('feat_cols') or [])
        if mdl is None or not feat_raw:
            return None
        # суффикс ГПА: un-suffixed имя фичи -> реальная колонка df_wide (как в /explain)
        gsuf = '__GPA' + sensor_id.rsplit('__GPA', 1)[-1]
        resolved = {}
        for f in feat_raw:
            col = f if f in df_wide.columns else (f + gsuf if (f + gsuf) in df_wide.columns else None)
            if col is None:
                return None
            resolved[f] = col
        Xdf = df_wide[list(resolved.values())].rename(
            columns={v: kk for kk, v in resolved.items()})[feat_raw]
        if wrapper.get('needs_impute'):
            Xdf = Xdf.fillna(pd.Series(wrapper.get('impute_median', {}) or {})).fillna(0.0)
        # POOLED: общая модель обучена на z-нормализованных фичах → SHAP считаем на
        # НОРМАЛИЗОВАННОМ входе (иначе вклад на сырых значениях неверен). Сырое X — для 'value'.
        Xmodel = Xdf
        if info and info.get('pooled') and info.get('norm'):
            _n = info['norm']
            Xmodel = ((Xdf[list(_n['feat'])] - pd.Series(_n['feat_mu'])) / pd.Series(_n['feat_sd']))[feat_raw]
        pos = int(df_wide.index.get_indexer([pd.Timestamp(peak_ts_local)], method='nearest')[0])
        if pos < 0:
            return None
        Xrow = Xmodel.iloc[[pos]]
        Xrow_raw = Xdf.iloc[[pos]]
        from catboost import Pool
        sv = np.asarray(mdl.get_feature_importance(Pool(Xrow), type='ShapValues'))
        row = sv[0]
        if row.ndim == 2:          # uncertainty: [mean, var] -> выход mean
            row = row[0]
        contribs = row[:-1]        # последний столбец — bias
        order = np.argsort(np.abs(contribs))[::-1][:k]
        out = []
        for i in order:
            i = int(i)
            xv = Xrow_raw.iloc[0, i]
            out.append({
                'name':    feat_raw[i],
                'contrib': round(float(contribs[i]), 5),
                'value':   (round(float(xv), 4) if pd.notna(xv) else None),
            })
        return out
    except Exception:
        logger.debug('SHAP для %s @ %s не посчитан', sensor_id, peak_ts_local, exc_info=True)
        return None


def _build_anomalies_t_records(state_events, metadata, models, df_wide):
    """Полная запись по каждой аномалии для anomalies_t: метаданные эпизода +
    коридор/z-score + качество модели + SHAP-вкладчики (на пике остатка).
    event_ts/ts_end/peak_ts — в UTC (согласованы с anomalies/journal)."""
    name_to_tag = metadata.get('name_to_tag', {})
    station_id = _station_cfg.station_id if _station_cfg is not None else metadata.get('station_id', '')
    mmodels = metadata.get('models', {})

    def _utc(v):
        try:
            return _local_naive_to_utc(v).to_pydatetime() if v else None
        except Exception:
            return None

    recs = []
    for ev in state_events:
        s = ev.get('sensor_id')
        kind = ev.get('kind', 'ml')
        ev_utc = _utc(ev.get('timestamp'))
        if ev_utc is None:
            continue
        peak_local = ev.get('peak_ts') or ev.get('timestamp')
        info = mmodels.get(s, {})
        wrapper = models.get(s) if models else None
        model_type = wrapper.get('model_type') if isinstance(wrapper, dict) else None
        nm = ev.get('sensor_name') or ''
        subsystem = (nm.split('_')[0].upper() if '_' in nm else nm[:4].upper()) if nm else None
        recs.append({
            'station_id':   station_id,
            'sensor_id':    s,
            'sensor_name':  nm or None,
            'point':        name_to_tag.get(s),
            'gpa':          ev.get('gpa'),
            'subsystem':    subsystem,
            'event_ts':     ev_utc,
            'ts_end':       _utc(ev.get('ts_end')),
            'peak_ts':      _utc(ev.get('peak_ts')),
            'kind':         kind,
            'severity':     ev.get('severity'),
            'value':        ev.get('value'),
            'expected':     ev.get('expected'),
            'deviation':    ev.get('deviation'),
            'residual':     ev.get('residual'),
            'corridor_lo':  ev.get('corridor_lo'),
            'corridor_hi':  ev.get('corridor_hi'),
            'z_score':      ev.get('z_score'),
            'points':       ev.get('points'),
            'duration_min': ev.get('duration_min'),
            'r2_val':       info.get('r2_val'),
            'mae_val':      info.get('mae_val', info.get('mae_train')),
            'nmae_val':     info.get('nmae_val'),
            'n_sigma_cal':  ev.get('n_sigma_cal'),
            'model_type':   model_type,
            'shap_top':     _shap_top_for_event(models, df_wide, s, peak_local, k=5, info=info),
            'message':      _event_message(ev),
            'status':       'new',
        })
    return recs


def _detection_diagnostics(results, metadata):
    """D1/D2/D4: измеряет покрытие коридора (доля точек в [p-hw, p+hw]) и частоту
    срабатываний на рабочем режиме за live-период. Только лог/диагностика —
    калибровка n_sigma остаётся за оператором (нет размеченных ложных/пропусков)."""
    last_train_ts = _parse_train_ts(metadata['last_train_timestamp'])
    coverages, alarm_rates, ncals, hot = [], [], [], []
    for s, r in results.items():
        times = r['times']
        if len(times) == 0:
            continue
        live = np.asarray(times > last_train_ts)
        run  = np.asarray(r.get('running', np.ones(len(times))), dtype=float) >= 0.5
        sel  = live & run
        n = int(sel.sum())
        if n < 20:
            continue
        y  = np.asarray(r['reality'], dtype=float)[sel]
        p  = np.asarray(r['prediction'], dtype=float)[sel]
        hw = np.asarray(r['hw'], dtype=float)[sel]
        cov = float(np.mean(np.abs(y - p) <= hw))
        anom = np.asarray(r.get('anom_mask', np.zeros(len(times)))).astype(bool)[sel]
        rate = float(np.mean(anom))
        coverages.append(cov)
        alarm_rates.append(rate)
        if r.get('n_sigma_cal') is not None:
            ncals.append(float(r['n_sigma_cal']))
        if rate > 0.05:          # >5% точек в алармах — кандидат на пере-калибровку
            hot.append((s, rate))
    if coverages:
        hot.sort(key=lambda z: -z[1])
        # покрытие коридора (D2: калибровка y_var) и доля алармов (D1) на рабочем режиме;
        # n_sigma(медиана) — фактический калиброванный порог под FP≈1.5%
        logger.info(
            "detect-diag: датчиков=%d покрытие(медиана)=%.3f алармы(медиана)=%.4f "
            "n_sigma_cal(медиана)=%s горячих(>5%%)=%d %s",
            len(coverages), float(np.median(coverages)), float(np.median(alarm_rates)),
            f"{np.median(ncals):.2f}" if ncals else "n/a",
            len(hot), ", ".join(f"{s.split('__')[0]}:{r:.2f}" for s, r in hot[:5]),
        )

_SEV_DOWNGRADE = {'crit': 'warn', 'warn': 'info', 'info': 'info', 'ok': 'ok'}


def _severity_rank(kinds):
    if any(k in ('ml', 'neg') for k in kinds): return 'crit'
    if any(k in ('frozen', 'roc', 'drift', 'index') for k in kinds): return 'warn'
    if any(k in ('seasonal', 'regime', 'cross') for k in kinds): return 'info'
    return 'ok'


def _sensor_drift_calib(r, info, metadata, last_train_ts):
    """Реальная пер-сенсорная аналитика для DetailPanel: дрейф (тренд остатка,
    reversibility, CUSUM/Page-Hinkley) и калибровка (коридоры conformal/POT в
    единицах остатка, n_sigma, покрытие нормой, доля алармов на рабочем режиме)."""
    out = {'drift': {}, 'calibration': {}}
    try:
        times = r.get('times')
        if times is None or len(times) == 0:
            return out
        reality    = np.asarray(r['reality'], float)
        prediction = np.asarray(r['prediction'], float)
        hw         = np.asarray(r['hw'], float)
        running    = np.asarray(r.get('running', np.ones(len(times))), float) >= 0.5
        live       = np.asarray(times > last_train_ts)
        sel        = live & running
        resid      = reality - prediction
        n_sigma_def = float(metadata.get('anomaly_n_sigma', 5.0) or 5.0)

        # ── калибровка (пороги в единицах остатка датчика) ──
        nsig_cal = r.get('n_sigma_cal')
        cov = float(np.mean(np.abs(resid[sel]) <= hw[sel])) if int(sel.sum()) >= 10 else None
        anom = np.asarray(r.get('anom_mask', np.zeros(len(times)))).astype(bool)
        alarm = float(np.mean(anom[sel])) if int(sel.sum()) >= 10 else None
        out['calibration'] = {
            'conformal_thr': (round(float(info['conformal_thr']), 4) if info.get('conformal_thr') is not None else None),
            'pot_thr':       (round(float(info['pot_thr']), 4)       if info.get('pot_thr') is not None else None),
            'n_sigma_cal':   (round(float(nsig_cal), 2) if nsig_cal else None),
            'n_sigma':       round(n_sigma_def, 2),
            'coverage':      (round(cov, 4) if cov is not None else None),
            'alarm_rate':    (round(alarm, 4) if alarm is not None else None),
        }

        # ── дрейф: окно последних 7 рабочих дней ──
        recent = np.asarray(times >= (times[-1] - pd.Timedelta(days=7))) & running
        rmv = float(info.get('residual_mean_val', 0.0) or 0.0)
        rsv = float(info.get('residual_std_val', 0.0) or 0.0) or 1e-6
        dscore = trend = revers = cusum_hit = ph_hit = None
        if int(recent.sum()) >= 20:
            rr = resid[recent]
            dscore = round(float(np.median(np.abs(rr))) / rmv, 3) if rmv > 0 else None
            z = rr / rsv
            ew = np.asarray(DM.ewma(z))
            trend = round(float((ew[-1] - ew[0]) * rsv), 4)   # сдвиг EWMA-остатка за окно (ед. датчика)
            try: revers = DM.reversibility(rr)
            except Exception: revers = None
            try: cusum_hit = bool(np.asarray(DM.cusum(z)).any())
            except Exception: cusum_hit = None
            try: ph_hit = bool(np.asarray(DM.page_hinkley(z)).any())
            except Exception: ph_hit = None
        dmask = np.asarray(r.get('drift_mask', np.zeros(len(times)))).astype(bool)
        out['drift'] = {
            'score': dscore, 'trend': trend, 'reversibility': revers,
            'cusum': cusum_hit, 'ph': ph_hit, 'fired': bool((dmask & live).any()),
        }
    except Exception:
        logger.debug('analytics не посчитана', exc_info=True)
    return out


def _write_domain_to_db(df_wide, metadata, tail=600):
    """Онгоинг-пополнение {schema}.domain (WIDE) доменными фичами свежих точек (последние
    `tail`), upsert ON CONFLICT. df_wide уже содержит `<feat>__GPAn` (prepare_wide_data).
    datetime → UTC (как raw_data/бэкфилл). Гард CS_DISABLE_DB_WRITE — в save_domain.
    tail=None → ПОЛНОЕ окно (для reprocess_history; иначе исторические окна писались бы с дырами)."""
    try:
        if _loader is None or df_wide is None or df_wide.empty:
            return
        feats = list(DF.DOMAIN_COLS)
        gpa_ids = metadata.get('gpa_ids', []) or []
        dfw = df_wide if tail is None else df_wide.tail(int(tail))
        try:
            idx_utc = dfw.index.tz_localize('Etc/GMT-5').tz_convert('UTC')
        except Exception:
            idx_utc = dfw.index   # уже tz-aware/иное — пишем как есть
        rows = []
        for g in gpa_ids:
            gnum = str(g).replace('GPA', '')
            colmap = {f: f'{f}__GPA{gnum}' for f in feats if f'{f}__GPA{gnum}' in dfw.columns}
            if not colmap:
                continue
            arr = {f: dfw[colmap[f]].values if f in colmap else None for f in feats}
            for i, ts in enumerate(idx_utc):
                vals, anynan = [], True
                for f in feats:
                    v = arr[f][i] if arr[f] is not None else None
                    v = None if (v is None or (isinstance(v, float) and np.isnan(v))) else float(v)
                    vals.append(v)
                    if v is not None:
                        anynan = False
                if not anynan:
                    rows.append((pd.Timestamp(ts).to_pydatetime(), int(gnum), *vals))
        if rows:
            _loader.ensure_domain(feats)
            n = _loader.save_domain(rows, feats)
            if n:
                print(f'💾 domain: upsert {n} строк', flush=True)
    except Exception as _e:
        logger.warning('domain-запись в БД не выполнена: %s', _e)


def _write_live_state(results, metadata, models=None, df_wide=None, historical=False, emit_from=None):
    """Writes live_state.json consumed by main.py FastAPI backend.
    models/df_wide (опц.) нужны для SHAP-атрибуции при записи в anomalies_t.
    historical=True (reprocess_history): пишет ВСЕ derived-таблицы за ПОЛНОЕ окно
    (health/domain без онлайн-усечений), а state-JSON файл НЕ переписывает (state осмыслен
    только для «сейчас» — его обновит первый штатный цикл монитора после рестарта)."""
    SERIES_DAYS = 30
    state_sensors        = {}
    state_series         = {}
    state_chart_anomalies = {}
    state_events         = []
    drifted_sensors      = []   # модели, у которых ошибка выросла вдвое — пора переобучать

    # граница обучения (для live-маски в аналитике дрейфа/калибровки)
    try:
        _ltt = _parse_train_ts(metadata['last_train_timestamp'])
    except Exception:
        _ltt = None

    # Доменные индексы по ГПА (последние значения наблюдаемых индексов из df_wide):
    # η_p, H_p, shaft_resid, specific_fuel, ΔT… — для вкладки «Доменные индексы».
    gpa_indices: dict = {}
    hidx = metadata.get('health_index', []) or []
    if df_wide is not None and not df_wide.empty and hidx:
        for col in df_wide.columns:
            if '__GPA' not in col:
                continue
            base, _, g = col.rpartition('__GPA')
            if base not in hidx:
                continue
            ser = df_wide[col].dropna()
            if len(ser):
                gpa_indices.setdefault(f'GPA{g}', {})[base] = round(float(ser.iloc[-1]), 4)

    # Серия модели в БД (predictions): курсор для ИНКРЕМЕНТАЛЬНОЙ записи (только новые
    # точки/цикл, не весь 30-дн массив) + аккумулятор. CS_WRITE_PREDICTIONS=0 — отключить.
    _station_id = metadata.get('station_id', '')
    _pred_write = os.environ.get('CS_WRITE_PREDICTIONS', '1') != '0'
    state_pred_records: list = []
    _last_pred_ts: dict = {}
    if _pred_write and _loader is not None:
        try:
            _loader.ensure_predictions()
            _last_pred_ts = _loader.get_last_prediction_ts(list(results.keys()))
        except Exception:
            logger.debug('predictions: ensure/last_ts пропущены', exc_info=True)
            _pred_write = False

    for s, r in results.items():
        parts = s.rsplit('__', 1)
        name  = parts[0]
        gpa   = parts[1] if len(parts) > 1 else 'GPA1'
        info  = metadata['models'][s]
        tag   = info.get('tag', s)
        r2    = _num(info.get('r2_train'), 0.0)    # честный r2_val; в UI не выводится
        mae   = _num(info.get('mae_train'), 0.0)   # None-толерантно (v2 metadata)
        sub   = name.split('_')[0].upper() if '_' in name else name[:4].upper()

        times      = r['times']
        reality    = np.asarray(r['reality'],    dtype=float)
        prediction = np.asarray(r['prediction'], dtype=float)
        hw         = np.asarray(r['hw'],         dtype=float)
        # асимметричные границы коридора (CQR); fallback на симметричные p±hw
        band_lo    = np.asarray(r['lo'], dtype=float) if r.get('lo') is not None else (prediction - hw)
        band_hi    = np.asarray(r['hi'], dtype=float) if r.get('hi') is not None else (prediction + hw)
        # эпистемика (ДЕТЕКТОР 2) — сохраняем по точкам для отдельной полосы (как в anomaly-html).
        # НЕ маскируем на OOD: именно там она и должна расти (это её сигнал новизны).
        epi_arr    = np.asarray(r.get('epistemic', np.full(len(prediction), np.nan)), dtype=float)
        # АЛЬТЕРНАТИВНЫЙ коридор (другой режим, для UI-тумблера) — оба коридора в выходе
        _alo = r.get('lo_alt'); _ahi = r.get('hi_alt')
        band_lo_alt = np.asarray(_alo, dtype=float) if _alo is not None else None
        band_hi_alt = np.asarray(_ahi, dtype=float) if _ahi is not None else None
        # МАСКИРОВАНИЕ ОТОБРАЖЕНИЯ (per-slot, см. ниже): модель-линия видна на всех РАБОЧИХ точках
        # (гаснет только на стоянке running<0.5); conformal-коридор гаснет на стоянке ∨ OOD (строгий);
        # hybrid-коридор гаснет только на стоянке (σ-масштабирован, честно расширяется на OOD).
        # Линия значения остаётся всегда; фронт рвёт линию/заливку там, где p/lo/hi = None.
        # ВАЖНО: маскируем ТОЛЬКО display-копии для серии. `prediction`/`band_*` ниже
        # нужны для resid/эпизодов — если занулить их в NaN, resid=NaN утечёт в episode-loop
        # и json.dump запишет литерал NaN → сломает strict-JSON парсер фронта.
        _stopped = np.asarray(r.get('running', np.ones(len(prediction))), dtype=float) < 0.5
        _ood     = np.asarray(r.get('ood_flag', np.zeros(len(prediction))), dtype=bool)
        _trans   = np.asarray(r.get('transition', np.zeros(len(prediction))), dtype=bool)
        # ПЕРЕХОД (пуск/останов/резкая смена нагрузки, ±2 точки): предикт CatBoost скачком уходит
        # (lag/roll-фичи ловят скачок входа) → коридор p±hw даёт вертикальные «пики». Гасим модель И
        # оба коридора на переходах — как на стоянке. _hide = стоянка ∨ переход.
        _hide    = _stopped | _trans
        _invalid = _hide | _ood
        # НАДЁЖНЫЙ УНИВЕРСАЛЬНЫЙ КОРИДОР: hybrid (σ-масштабированный) присутствует на ВСЕХ рабочих
        # точках (гаснет только на _hide) и честно РАСШИРЯЕТСЯ на OOD (там растёт σ). conformal —
        # СТРОГИЙ (q̂_abs): дополнительно гаснет на OOD. Маскируем каждый слот по ЕГО режиму.
        _active_conf = (r.get('corridor_active') == 'conformal')
        _alt_conf    = (r.get('corridor_alt') == 'conformal')
        _mask_active = _invalid if _active_conf else _hide
        _mask_alt    = _invalid if _alt_conf    else _hide
        # модель — на рабочих точках вне переходов (центр hybrid-полосы + непрерывная линия).
        p_disp  = np.where(_hide, np.nan, prediction)
        lo_disp = np.where(_mask_active, np.nan, band_lo)
        hi_disp = np.where(_mask_active, np.nan, band_hi)
        lo2_disp = np.where(_mask_alt, np.nan, band_lo_alt) if band_lo_alt is not None else None
        hi2_disp = np.where(_mask_alt, np.nan, band_hi_alt) if band_hi_alt is not None else None

        # Series — last SERIES_DAYS
        cutoff  = times[-1] - pd.Timedelta(days=SERIES_DAYS) if len(times) else None
        t_mask  = times >= cutoff if cutoff is not None else slice(None)
        tf, rf, pf, hf = times[t_mask], reality[t_mask], p_disp[t_mask], hw[t_mask]
        lof, hif = lo_disp[t_mask], hi_disp[t_mask]
        lo2f = lo2_disp[t_mask] if lo2_disp is not None else None
        hi2f = hi2_disp[t_mask] if hi2_disp is not None else None
        ef = epi_arr[t_mask]

        def _r4(x):
            return None if (x is None or not np.isfinite(x)) else round(float(x), 4)

        def _r6(x):
            return None if (x is None or not np.isfinite(x)) else round(float(x), 6)
        series = []
        _skipped_pts = 0
        for i in range(len(tf)):
            try:
                _pt = {'t': pd.Timestamp(tf[i]).isoformat(),
                       'v': round(float(rf[i]), 4),
                       'p': _r4(pf[i]),
                       'lo': _r4(lof[i]),
                       'hi': _r4(hif[i]),
                       'e': _r6(ef[i])}                  # эпистемика u_epi (полоса детектора 2)
                if lo2f is not None:                     # альт. коридор (UI-тумблер conformal↔hybrid)
                    _pt['lo2'] = _r4(lo2f[i]); _pt['hi2'] = _r4(hi2f[i])
                series.append(_pt)
            except Exception:
                _skipped_pts += 1
        if _skipped_pts:
            logger.debug('%s: пропущено %s точек серии (NaN/None)', s, _skipped_pts)
        state_series[s] = series

        # Серия → БД (predictions): инкрементально (новые точки после курсора, overlap 10 мин
        # под ON CONFLICT DO UPDATE), после train-границы; p/lo/hi = display (None→NULL на
        # стоянке/OOD). Время local-naive (Etc/GMT-5) → UTC, как domain/anomalies_t.
        if _pred_write and len(tf):
            try:
                _tf_utc = pd.DatetimeIndex(tf).tz_localize('Etc/GMT-5').tz_convert('UTC')
                _last = _last_pred_ts.get(s)
                _last_cut = (_last - pd.Timedelta(minutes=10)) if _last is not None else None
                # порог новизны κ·1.5 — константа сенсора, но время-зависим между ретрейнами,
                # поэтому пишем по строкам (историческая полоса покажет порог того момента).
                _epi_thr = _r6(r.get('epistemic_thr'))
                for i in range(len(tf)):
                    if _ltt is not None and pd.Timestamp(tf[i]) <= _ltt:
                        continue                      # train-период не отдаём (in-sample leak)
                    _tu = _tf_utc[i]
                    # historical (reprocess): ПЕРЕЗАПИСЫВАЕМ все точки окна (ON CONFLICT DO UPDATE) —
                    # иначе новые колонки lo2/hi2/e не дозапишутся в существующие строки. НО левый
                    # overlap-край окна (< emit_from) НЕ эмитим: там AR-фичи (lag/roll6/var) усечены
                    # по контексту → деградированные p/lo/hi; этот участок корректно эмитит пред.окно.
                    if historical:
                        if emit_from is not None and _tu < emit_from:
                            continue                  # левый overlap-контекст — не перезаписываем
                    elif _last_cut is not None and _tu <= _last_cut:
                        continue                      # уже записано (инкремент, live)
                    state_pred_records.append((_station_id, s, _tu.to_pydatetime(),
                                               _r4(pf[i]), _r4(lof[i]), _r4(hif[i]),
                                               _r4(lo2f[i]) if lo2f is not None else None,
                                               _r4(hi2f[i]) if hi2f is not None else None,
                                               _r6(ef[i]), _epi_thr))   # + альт.коридор lo2/hi2, эпистемика u_epi и её порог
            except Exception:
                logger.debug('%s: predictions-записи не собраны', s, exc_info=True)

        # Chart anomalies — all kinds within series window, per sensor
        chart_anoms = []
        for mask_key, kind in _MASK_TO_KIND.items():
            m = r.get(mask_key)
            if m is None:
                continue
            arr = np.asarray(m).astype(bool)
            if not arr.any():
                continue
            for i in np.where(arr)[0]:
                if cutoff is not None and times[i] < cutoff:
                    continue
                try:
                    chart_anoms.append({
                        't': pd.Timestamp(times[i]).isoformat(),
                        'v': round(float(reality[i]), 3),
                        'kind': kind,
                        'severity': _KIND_SEVERITY[kind],
                    })
                except Exception:
                    logger.debug('%s: точка аномалии %s/%s не сериализована', s, kind, i,
                                 exc_info=True)
        state_chart_anomalies[s] = chart_anoms

        # Anomaly kinds + events.
        # Эпизодизация: True-тики с разрывом <=2 тика (10 мин) — один эпизод-событие.
        # Severity/счётчик карточки датчика — только последние 24 ч (затухание),
        # полный счётчик за окно — anomaly_count_30d.
        kinds = []
        anom_count = 0
        anom_count_30d = 0
        recent24 = (
            np.asarray(times >= (times[-1] - pd.Timedelta(hours=24)))
            if len(times) else np.array([], dtype=bool)
        )
        for mask_key, kind in _MASK_TO_KIND.items():
            m = r.get(mask_key)
            if m is None:
                continue
            arr = np.asarray(m).astype(bool)
            if not arr.any():
                continue
            anom_count_30d += int(arr.sum())
            arr24 = arr & recent24
            if arr24.any():
                kinds.append(kind)
                anom_count += int(arr24.sum())
            idx = np.where(arr)[0]
            breaks = np.where(np.diff(idx) > 2)[0]
            for ep in np.split(idx, breaks + 1):
                if len(ep) == 0:
                    continue
                try:
                    i0, i1 = int(ep[0]), int(ep[-1])
                    seg_resid = np.abs(reality[ep] - prediction[ep])
                    ipk = int(ep[int(np.argmax(seg_resid))])
                    t   = pd.Timestamp(times[i0]).isoformat()
                    # полная точность для расчётов (round только для отображения/хранения):
                    # резидуал/z/коридор из НЕокруглённых значений (иначе ошибка ~5e-4 тянется в z)
                    rval = float(reality[ipk])    if ipk < len(reality)    else None
                    rprd = float(prediction[ipk]) if ipk < len(prediction) else None
                    val = round(rval, 3) if rval is not None else None
                    prd = round(rprd, 3) if rprd is not None else None
                    dev = (round((rval - rprd) / abs(rprd) * 100, 2)
                           if (rval is not None and rprd is not None and abs(rprd) > 1e-9) else None)
                    sev = _KIND_SEVERITY[kind]
                    if kind == 'ml' and len(ep) < 2:
                        sev = 'warn'    # одиночный ML-тик не подтверждён — не crit
                    if not r.get('status_known', True):
                        # D3: статус ГПА неизвестен (fail-open) — меньше доверия к
                        # аномалии (может быть ложной на скрыто остановленном) → ниже severity
                        sev = _SEV_DOWNGRADE.get(sev, sev)
                    # ── обогащение для anomalies_t: коридор/остаток/z-score/длительность ──
                    hwpk = float(hw[ipk]) if ipk < len(hw) else None
                    has_vp = rval is not None and rprd is not None
                    resid = round(rval - rprd, 4) if has_vp else None
                    clo = round(rprd - hwpk, 4) if (rprd is not None and hwpk is not None) else None
                    chi = round(rprd + hwpk, 4) if (rprd is not None and hwpk is not None) else None
                    nsig = r.get('n_sigma_cal')
                    # hw = n_sigma · scale → z = resid/scale = resid·n_sigma/hw
                    zsc = (round((rval - rprd) * float(nsig) / hwpk, 3)
                           if (has_vp and hwpk and hwpk > 1e-9 and nsig) else None)
                    try:
                        _dur = (times[i1] - times[i0]).total_seconds() / 60.0
                        dur_min = round(_dur, 2) if 0 <= _dur <= 10080 else None   # cap 1 нед.
                    except Exception:
                        dur_min = None
                    state_events.append({
                        'id': f'{s}__{kind}__{t}',
                        'timestamp': t,
                        'ts_end': pd.Timestamp(times[i1]).isoformat(),
                        'peak_ts': pd.Timestamp(times[ipk]).isoformat(),
                        'points': int(len(ep)),
                        'sensor_id': s, 'sensor_name': name,
                        'gpa': gpa, 'kind': kind, 'severity': sev,
                        'value': val, 'deviation': dev,
                        'expected': prd, 'residual': resid,
                        'corridor_lo': clo, 'corridor_hi': chi, 'z_score': zsc,
                        'duration_min': dur_min,
                        'n_sigma_cal': (round(float(nsig), 3) if nsig else None),
                        'description': f'{name.replace("_"," ")}: {kind}'
                                       + (f' ×{len(ep)}' if len(ep) > 1 else ''),
                        'acked': False,
                    })
                except Exception:
                    # потеря события скрывает аномалию от оператора — логируем
                    logger.warning('Эпизод %s/%s не сформирован', s, kind, exc_info=True)

        # Model drift: медиана |resid| за 7 рабочих дней > 2× residual_mean_val
        try:
            run_arr = np.asarray(r.get('running', np.ones(len(times))), dtype=float) >= 0.5
            recent7 = np.asarray(times >= (times[-1] - pd.Timedelta(days=7))) & run_arr
            rmv = float(info.get('residual_mean_val', 0.0) or 0.0)
            if rmv > 0 and recent7.any():
                med_resid = float(np.median(np.abs(reality[recent7] - prediction[recent7])))
                if med_resid > 2.0 * rmv:
                    drifted_sensors.append(s)
        except Exception:
            logger.debug('%s: drift-проверка не выполнена', s, exc_info=True)

        cur  = round(float(reality[-1]),    3) if len(reality)    else None
        pred = round(float(prediction[-1]), 3) if len(prediction) else None
        state_sensors[s] = {
            'id': s, 'name': name, 'gpa': gpa, 'tag': tag,
            'r2': round(r2, 4), 'mae': round(mae, 4),
            'anomaly_count': anom_count, 'anomaly_count_30d': anom_count_30d,
            'anomaly_types': kinds,
            'severity': _severity_rank(kinds), 'subsystem': sub,
            'cur': cur, 'pred': pred,
            # аналитика research-методологии (для DetailPanel «Качество модели»)
            'nmae':        info.get('nmae_val'),
            'rmse':        info.get('rmse_val'),
            'r2_val':      info.get('r2_val'),
            'r2_insample': info.get('r2_insample'),
            'best_model':  info.get('best_model'),
            # v2: режим детекции и текущий режим работы (опционально, для DetailPanel)
            'detector_mode': r.get('detector_mode'),
            'regime':        (str(r['regime'][-1]) if r.get('regime') is not None
                              and len(r.get('regime')) else None),
            # ДЕТЕКТОР 2 (новизна): порог эпистемики для линии на графике (как в anomaly-html);
            # сама полоса u_epi — в series[].e. None если эталон healthy не построен.
            'epistemic_thr': r.get('epistemic_thr'),
        }
        # реальные пер-сенсорные Дрейф + Калибровка + Доменные индексы (DetailPanel)
        if _ltt is not None:
            _ac = _sensor_drift_calib(r, info, metadata, _ltt)
            state_sensors[s]['drift'] = _ac['drift']
            state_sensors[s]['calibration'] = _ac['calibration']
            state_sensors[s]['drift_score'] = _ac['drift'].get('score')
        state_sensors[s]['domain'] = gpa_indices.get(gpa, {})

    try:
        _detection_diagnostics(results, metadata)
    except Exception:
        logger.debug('detect-diag не выполнен', exc_info=True)

    state_events.sort(key=lambda x: x['timestamp'], reverse=True)
    n_drift = len(drifted_sensors)
    if n_drift:
        # Переобучение в рантайме НЕ предусмотрено (модели обучаются однократно).
        # Дрейф — только информационный сигнал для оператора.
        print(f'ℹ️ MODEL DRIFT (информационно): {n_drift} моделей с ошибкой '
              f'>2× валидационной ({", ".join(sorted(drifted_sensors)[:5])}'
              f'{"..." if n_drift > 5 else ""})', flush=True)
    # Честный фронтир данных предикта = max последней посчитанной точки по датчикам.
    # В отличие от last_updated (время ЗАПИСИ файла стенными часами), отражает
    # «докуда реально посчитано»; нужен фронту для честной свежести и как фолбэк-
    # курсор для catch_up_missing, если raw_data.health пуст.
    _pred_through = None
    for _r in results.values():
        _t = _r.get('times')
        if _t is not None and len(_t):
            _last = pd.Timestamp(_t[-1])
            if _pred_through is None or _last > _pred_through:
                _pred_through = _last

    state = {
        'last_updated':    pd.Timestamp.now().isoformat(),
        'predicted_through': _pred_through.isoformat() if _pred_through is not None else None,
        'model_trained_at': metadata.get('last_train_timestamp', ''),
        'model_drift': {
            'count': n_drift,
            'sensors': sorted(drifted_sensors)[:20],
            'retrain_recommended': False,   # рантайм не переобучает — всегда False
        },
        'sensors':         state_sensors,
        'series':          state_series,
        'chart_anomalies': state_chart_anomalies,
        'events':          state_events[:500],
    }

    if state_events and _loader is not None:
        # Единый источник event_ts (UTC) для обеих таблиц: anomalies и journal
        # получают ИДЕНТИЧНУЮ метку → запись по исходному UTC и согласованность
        # anomalies ↔ journal (раньше anomalies писались наивной локальной меткой).
        notif = _build_notifications(state_events, metadata)
        # Легаси-таблица `anomalies` больше НЕ пишется (был дубль `anomalies_t`; дашборд
        # читает только anomalies_t). `notif` нужен для журнала уведомлений (ack) ниже.

        # Журнал уведомлений (ON CONFLICT DO NOTHING — повтор безвреден)
        try:
            if notif:
                _loader.save_notifications(notif)
        except Exception as _e:
            print(f'⚠️ Ошибка записи журнала уведомлений: {_e}', flush=True)

        # anomalies_t: ПОЛНАЯ запись по аномалии (+ SHAP-атрибуция на пике остатка).
        # Отдельная таблица; SHAP считаем здесь, пока модель (models) и срез (df_wide)
        # ещё в памяти — без повторной загрузки из БД. Best-effort: сбой не рушит цикл.
        try:
            recs_t = _build_anomalies_t_records(state_events, metadata, models, df_wide)
            if recs_t:
                saved_t = _loader.save_anomalies_t(recs_t)
                n_shap = sum(1 for x in recs_t if x.get('shap_top'))
                if saved_t:
                    print(f'💾 БД: anomalies_t — {saved_t} новых записей (SHAP у {n_shap}/{len(recs_t)})', flush=True)
        except Exception as _e:
            print(f'⚠️ Ошибка записи anomalies_t: {_e}', flush=True)

    # raw_data.health: коды/«0». В обычном цикле — свежий хвост; historical — ПОЛНОЕ окно.
    if _loader is not None:
        try:
            health_rows = _collect_health_rows(results, metadata, full=historical)
            if health_rows:
                _loader.update_health(health_rows)
                print(f'💾 health: обновлено {len(health_rows)} точек', flush=True)
        except Exception as _e:
            print(f'⚠️ Ошибка записи raw_data.health: {_e}', flush=True)
        # пополнение ohangaron.domain (upsert). historical → ПОЛНОЕ окно (tail=None).
        _write_domain_to_db(df_wide, metadata, tail=(None if historical else 600))

        # серия модели → predictions (инкрементальный upsert; аккумулятор собран в цикле)
        if _pred_write and state_pred_records:
            try:
                n_pred = _loader.save_predictions(state_pred_records)
                if n_pred:
                    print(f'💾 БД: predictions — {n_pred} точек серии', flush=True)
            except Exception as _e:
                print(f'⚠️ Ошибка записи predictions: {_e}', flush=True)

    # state-JSON файл пишем только в обычном режиме; в historical (reprocess) — пропуск
    # (state = снимок «сейчас»; его обновит первый штатный цикл монитора).
    if not historical:
        out = str(_station_cfg.state_path)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp  = out + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, out)
        print(f'💾 {os.path.basename(out)}: {len(state_sensors)} датчиков, {len(state_events)} событий', flush=True)


_shutdown_requested = False


def _request_shutdown():
    global _shutdown_requested
    _shutdown_requested = True


def _cursor_from_state(cfg):
    """Фолбэк-курсор из live_state.json: predicted_through (метка данных) →
    last_updated (время записи). Возвращает aware UTC Timestamp или None.
    Метки в state наивны и в локальном времени станции (Etc/GMT-5)."""
    try:
        with open(cfg.state_path, 'r', encoding='utf-8') as f:
            st = json.load(f)
    except Exception:
        return None
    for key in ('predicted_through', 'last_updated'):
        v = st.get(key)
        if not v:
            continue
        try:
            ts = pd.Timestamp(v)
        except Exception:
            continue
        return ts.tz_localize('Etc/GMT-5').tz_convert('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    return None


def _cursor_from_metadata(cfg):
    """Последний фолбэк-курсор из metadata.json: data_end → last_train_timestamp.
    Читаем файл напрямую (без загрузки моделей). Возвращает aware UTC или None."""
    try:
        with open(cfg.models_path / 'metadata.json', 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:
        return None
    for key in ('data_end', 'last_train_timestamp'):
        v = meta.get(key)
        if not v:
            continue
        try:
            ts = pd.Timestamp(v)
        except Exception:
            continue
        return ts.tz_localize('Etc/GMT-5').tz_convert('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    return None


def reprocess_history(from_ts, to_ts, window_days=2, overlap_days=1, should_stop=None):
    """Оконно пересчитывает [from_ts, to_ts] под ТЕКУЩЕЙ моделью и пишет ВСЕ derived-таблицы
    (raw_data.health, anomalies_t, журнал, predictions, domain) через _write_live_state(historical=True).

    ЕДИНЫЙ путь записи для (1) разовой полной перезаписи истории (CLI --reprocess-from) и
    (2) самовосстанавливающегося догона catch_up_missing. Оконно (как backfill_health) — полная
    выгрузка истории роняет удалённую БД (OOM/обрыв стриминга, известная хрупкость cs_4).
    window_days=2 (≤ domain-tail 600 → domain пишется без дыр). overlap_days — контекст для
    lag/rolling/roc на стыках окон. Идемпотентно (UPDATE health + ON CONFLICT).
    should_stop()->bool — кооперативная отмена (Ctrl+C/SIGTERM)."""
    import gc
    _require_station()
    metadata, models = load_models_and_metadata()
    tag_to_name = metadata['tag_to_name']

    def _utc(x):
        ts = pd.Timestamp(x)
        return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    start, end = _utc(from_ts), _utc(to_ts)
    win = pd.Timedelta(days=window_days)
    overlap = pd.Timedelta(days=max(overlap_days, 1))
    print(f'🔁 reprocess_history: {start} → {end}, окно {window_days}д, перекрытие {overlap_days}д', flush=True)

    a = start
    total_win = 0
    while a < end:
        if should_stop is not None and should_stop():
            print('⏹  reprocess прерван по запросу остановки (на текущей границе)', flush=True)
            break
        b = min(a + win, end)
        load_from = a - overlap          # контекст слева для lag/rolling/roc
        try:
            raw = _loader.fetch_raw_window(load_from.isoformat(), b.isoformat())
        except Exception:
            logger.exception('reprocess окно %s..%s: загрузка не удалась — пропуск', a.date(), b.date())
            a = b
            continue
        if raw is None or raw.empty:
            print(f'  • {a.date()}..{b.date()}: данных нет', flush=True)
            a = b
            continue
        # Тяжёлые шаги под try/except: сбой/OOM одного окна не роняет весь прогон.
        df_wide = results = None
        try:
            df_wide = prepare_wide_data(raw, tag_to_name)
            if not df_wide.empty:
                results = _compute_results(df_wide, metadata, models)
                _write_live_state(results, metadata, models=models, df_wide=df_wide, historical=True, emit_from=a)
                total_win += 1
                print(f'  ✓ {a.date()}..{b.date()}: записано '
                      f'(health/anomalies_t/журнал/predictions/domain)', flush=True)
        except MemoryError:
            logger.exception('reprocess окно %s..%s: MemoryError — пропуск окна', a.date(), b.date())
        except Exception:
            logger.exception('reprocess окно %s..%s: обработка не удалась — пропуск', a.date(), b.date())
        finally:
            df_wide = results = raw = None
            gc.collect()
        a = b
    print(f'🎉 reprocess_history готов: обработано окон {total_win}', flush=True)


def catch_up_missing():
    """На старте: догнать пропущенный с прошлого запуска интервал и записать в БД.

    Сверяет now с меткой последнего посчитанного предикта (raw_data.health, с
    фолбэками на live_state.json и metadata.json) и, если отставание больше одного
    интервала цикла, ОКОННЫМИ запросами досчитывает [last_ts .. now], записывая
    health + аномалии + журнал. Так после простоя/перезапуска не остаётся
    «не посчитанных участков». Идемпотентно (UPDATE health + ON CONFLICT для
    аномалий/журнала) — повторный вызов безвреден.

    Догон ОБЯЗАТЕЛЬНО оконный (через reprocess_history): полная выгрузка истории
    роняет БД (OOM/обрыв стриминга) — известная хрупкость cs_4. reprocess_history пишет
    ВСЕ derived-таблицы (health + anomalies_t + журнал + predictions + domain) → после
    простоя не остаётся «не посчитанных участков» НИ В ОДНОЙ таблице.
    """
    _require_station()
    cfg = _station_cfg
    # Верхнюю границу берём с сервера БД (не с часов ноутбука — они могут плыть).
    try:
        now = _loader.db_now()
    except Exception:
        now = pd.Timestamp.now(tz='UTC')

    last_ts = None
    try:
        last_ts = _loader.get_last_health_ts()
    except Exception:
        logger.exception('catch-up: запрос метки последнего предикта не выполнен')
    if last_ts is None:
        last_ts = _cursor_from_state(cfg)
    if last_ts is None:
        last_ts = _cursor_from_metadata(cfg)
    if last_ts is None:
        print('⏭️  catch-up: метку последнего предикта определить не удалось — '
              'старт обычным циклом', flush=True)
        return

    gap = now - last_ts
    # Порог = HEALTH_WINDOW_HOURS: хвост такой длины обычный цикл размечает сам
    # (_collect_health_rows на каждом тике), поэтому мелкий пропуск догонять не
    # нужно — это и экономит лишнюю суточную выгрузку из-за overlap окна.
    if gap <= pd.Timedelta(hours=HEALTH_WINDOW_HOURS):
        print(f'✅ catch-up: отставание {gap} ≤ {HEALTH_WINDOW_HOURS}ч — '
              f'свежий хвост закроет обычный цикл, догонять нечего', flush=True)
        return

    print(f'🕳️  catch-up: догоняю пропуск {last_ts} → {now} '
          f'(отставание {gap}), окно {GAP_WINDOW_DAYS}д — все derived-таблицы...', flush=True)
    try:
        # ЕДИНЫЙ путь записи: reprocess_history пишет ВСЁ (health + anomalies_t + журнал +
        # predictions + domain) за полное окно → после простоя нет дыр НИ В ОДНОЙ таблице.
        # naive-UTC (reprocess_history навесит tz="UTC" сам).
        reprocess_history(
            last_ts.tz_convert('UTC').tz_localize(None),
            now.tz_convert('UTC').tz_localize(None),
            window_days=GAP_WINDOW_DAYS,
            overlap_days=1,
            should_stop=lambda: _shutdown_requested,   # кооперативная отмена по Ctrl+C
        )
        print('✅ catch-up завершён (все derived-таблицы) — перехожу в обычный режим', flush=True)
    except Exception:
        logger.exception('catch-up: оконный догон не завершён — продолжаю обычным циклом')


def run_continuous():
    """Непрерывный мониторинг с инкрементальным накоплением в памяти.
    Исключение одного цикла не убивает процесс — лог + backoff + продолжение."""
    print('=' * 60, flush=True)
    print('🔄 РЕЖИМ НЕПРЕРЫВНОГО МОНИТОРИНГА', flush=True)
    print(f'   Интервал обновления: {REFRESH_INTERVAL} сек ({REFRESH_INTERVAL // 60} мин)', flush=True)
    print('=' * 60, flush=True)

    import logging as _logging
    _log = _logging.getLogger('live_predict')

    # На старте сверяем now с меткой последнего предикта и догоняем пропуск в БД
    # ОКОННЫМИ запросами — затем обычный 5-мин цикл. Ошибка догона не мешает циклу.
    try:
        catch_up_missing()
    except Exception:
        _log.exception('catch-up на старте не выполнен — продолжаю обычным циклом')

    # Ретеншн серии: разовая чистка predictions старше CS_PREDICTIONS_RETENTION_DAYS (def 60).
    try:
        if _loader is not None:
            _rd = int(os.environ.get('CS_PREDICTIONS_RETENTION_DAYS', '60') or 60)
            n_pr = _loader.prune_predictions(_rd)
            if n_pr:
                print(f'🧹 predictions: ретеншн удалил {n_pr} точек старше {_rd}д', flush=True)
    except Exception:
        _log.exception('prune predictions на старте пропущен')

    current_raw_df = None
    consecutive_failures = 0
    iteration = 0
    while not _shutdown_requested:
        iteration += 1
        if iteration > 1:
            print(f"\n⏳ Ожидание {REFRESH_INTERVAL // 60} мин...", flush=True)
            slept = 0
            while slept < REFRESH_INTERVAL and not _shutdown_requested:
                time.sleep(5)
                slept += 5
            if _shutdown_requested:
                break
            print(f'\n{"=" * 60}')
            print(f'🔄 Обновление #{iteration} ({pd.Timestamp.now().strftime("%H:%M:%S")})')

        try:
            updated_df = run_once(existing_df=current_raw_df)
            if updated_df is not None:
                current_raw_df = updated_df
            consecutive_failures = 0
        except MemoryError:
            _log.exception('MemoryError в цикле #%s — сбрасываю накопленные данные', iteration)
            current_raw_df = None     # полная перезагрузка истории на следующем цикле
            consecutive_failures += 1
        except Exception:
            _log.exception('Ошибка цикла #%s (подряд: %s)', iteration, consecutive_failures + 1)
            consecutive_failures += 1

        if consecutive_failures:
            backoff = min(60 * consecutive_failures, REFRESH_INTERVAL)
            _log.warning('Backoff %s сек после %s ошибок подряд', backoff, consecutive_failures)
            time.sleep(backoff)

    _log.info('Остановлен корректно (iteration=%s)', iteration)


if __name__ == '__main__':
    import argparse
    from logging_config import setup as _log_setup, single_instance, install_signal_handlers

    parser = argparse.ArgumentParser(description='Онлайн-предикт аномалий из PostgreSQL')
    parser.add_argument('--mode', choices=['once', 'live'], default='once',
                        help='once = однократный предикт, live = непрерывный мониторинг')
    parser.add_argument('--station', default='ohangaron',
                        help='ID компрессорной станции (default: ohangaron)')
    parser.add_argument('--reprocess-from', default=None,
                        help='Разовая ПОЛНАЯ перезапись derived-данных по истории: нижняя граница ISO '
                             '(naive=UTC). Пишет health/anomalies_t/журнал/predictions/domain под текущей '
                             'моделью. По умолчанию --reprocess-to = сейчас.')
    parser.add_argument('--reprocess-to', default=None, help='Верхняя граница перезаписи ISO (default: сейчас)')
    args = parser.parse_args()

    _log_setup('live_predict')

    if args.mode == 'live':
        if not single_instance('live_predict'):
            print('❌ live_predict уже запущен (lock занят) — второй экземпляр не стартует', flush=True)
            sys.exit(1)
        install_signal_handlers(_request_shutdown)

    _init_station(args.station)

    if args.reprocess_from:
        # Разовая полная перезапись истории (все derived-таблицы) — тот же путь, что у догона.
        _to = args.reprocess_to or _loader.db_now().tz_convert('UTC').tz_localize(None).isoformat()
        reprocess_history(args.reprocess_from, _to, window_days=GAP_WINDOW_DAYS, overlap_days=1)
    elif args.mode == 'live':
        run_continuous()
    else:
        run_once()
