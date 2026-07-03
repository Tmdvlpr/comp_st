ML = 1
NEG = 2
FROZEN = 3
ROC = 4
SEASONAL = 5
REGIME = 6
CROSS = 7
DRIFT = 8       # медленный дрейф остатка (EWMA/CUSUM/Page-Hinkley)
INDEX = 9       # отклонение доменного индекса здоровья (η_p/shaft/specific_fuel)

CODE_TO_KIND: dict[int, str] = {
    1: "ml",
    2: "neg",
    3: "frozen",
    4: "roc",
    5: "seasonal",
    6: "regime",
    7: "cross",
    8: "drift",
    9: "index",
}

KIND_TO_CODE: dict[str, int] = {v: k for k, v in CODE_TO_KIND.items()}

KIND_SEVERITY: dict[str, str] = {
    "ml":       "crit",
    "neg":      "crit",
    "frozen":   "warn",
    "roc":      "warn",
    "seasonal": "info",
    "regime":   "info",
    "cross":    "info",
    "drift":    "warn",     # устойчивый дрейф — деградация, предупреждение
    "index":    "warn",     # доменный индекс ушёл из нормы
}

# Порядок severity (общий источник для live и API; не дублировать в main.py).
SEVERITY_ORDER: dict[str, int] = {"crit": 3, "warn": 2, "info": 1, "ok": 0}


def severity_rank(sev: str) -> int:
    return SEVERITY_ORDER.get(sev or "ok", 0)


def max_severity(sevs) -> str:
    """Максимальная severity из набора (crit>warn>info>ok). Используется для карточки
    датчика: берём УЖЕ записанную severity записей anomalies_t (с применённым D3-downgrade),
    а не пересчитываем из kinds."""
    best = "ok"
    for s in sevs:
        if SEVERITY_ORDER.get(s or "ok", 0) > SEVERITY_ORDER.get(best, 0):
            best = s
    return best


# ── Спец-значения столбца raw_data.health (НЕ аномалии, а состояние точки) ──
# NULL          — точка не оценивалась
# HEALTH_OK     — оценена, аномалий нет (агрегат работает в норме)
# "1".."9"      — коды сработавших детекторов (через запятую: "1,4")
# HEALTH_STOPPED— ГПА остановлен: детекторы подавлены, значение не «норма» и не авария
#                 (нечисловой маркер 'S' — чтобы не пересекаться с числовыми кодами аномалий)
HEALTH_OK      = "0"
HEALTH_STOPPED = "S"
