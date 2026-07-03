# -*- coding: utf-8 -*-
"""Применяет per-sensor выбор персонального CV (_interval_cv.py) в metadata модели.

Пишет в metadata['models'][key]['corridor_mode'] активный режим коридора на КАЖДЫЙ датчик.
Пользователь хочет conformal+hybrid ВЕЗДЕ → при FORCE-обучении оба порога построены на всех 48;
здесь выбираем, какой АКТИВЕН, по CV:
  • chosen ∈ {conformal, hybrid}          → служим его;
  • chosen == self (self-band сильнее)    → служим ЛУЧШИЙ из {conformal,hybrid} сейчас (self-band
                                            артефактов в ml_corridor пока нет), помечаем needs_self_band;
  • chosen == None (SUBTARGET)            → служим лучший из {conformal,hybrid}, помечаем flag честно.
Истинное CV-покрытие, лучший режим (вкл. self) и флаг кладём в model['cv'] — для дашборда/аудита
(коридор есть везде, но оператор видит РЕАЛЬНОЕ покрытие). НЕ раздуваем и НЕ прячем недобор.

Запуск:
    python _apply_cv_selection.py --models-dir models/ohangaron_forcecv_staging \
        [--selection <path>]   # default: <models-dir>/interval_cv_selection.json
"""
import os
import io
import sys
import json
import argparse

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass


def _best_of(test_cov, modes=("conformal", "hybrid")):
    """Имя режима с максимальным test-покрытием среди modes (для авто-выбора служимого)."""
    best, bv = None, -1.0
    for m in modes:
        v = test_cov.get(m)
        if isinstance(v, (int, float)) and v == v and v > bv:   # v==v отсекает nan
            best, bv = m, v
    return best, (bv if best else float("nan"))


def main():
    ap = argparse.ArgumentParser(description="Применить per-sensor CV-выбор corridor_mode в metadata")
    ap.add_argument("--models-dir", required=True)
    ap.add_argument("--selection", default=None)
    ap.add_argument("--dry-run", action="store_true", help="показать план, не писать metadata")
    args = ap.parse_args()

    meta_path = os.path.join(args.models_dir, "metadata.json")
    sel_path = args.selection or os.path.join(args.models_dir, "interval_cv_selection.json")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    with open(sel_path, encoding="utf-8") as f:
        sel = json.load(f)

    models = meta["models"]
    applied = {"conformal": 0, "hybrid": 0, "self": 0}
    self_served, self_missing, subtarget, missing = [], [], [], []
    rows = []

    for key, s in sel.items():
        if key not in models:
            missing.append(key)
            continue
        chosen = s.get("corridor_mode")            # None для SUBTARGET (флагнутых); иначе conformal/hybrid/self
        best_diag = s.get("best_mode_diag")        # может быть 'self'
        tc = s.get("test_cov") or {}
        flag = s.get("flag") or ""
        m = models[key]
        cal_mode = (m.get("calibration") or {}).get("mode")

        if cal_mode != "dual" and m.get("self_centers"):
            # НЕТ dual-калибровки (родной univariate_band: FORCE не построил кросс-сенсорный коридор)
            # → единственный доступный режим self; live и так обслуживает его по detector_mode.
            served = "self"
            self_served.append((s.get("name"), s.get("gpa_id"), tc.get("self")))
        elif chosen == "self":
            # self-победитель: обслуживаем через СУЩЕСТВУЮЩИЙ путь univariate_band (self_calibration
            # уже построена train'ом как кандидат). Никаких новых веток в live не нужно.
            if m.get("self_calibration") and m.get("self_centers"):
                m["detector_mode"] = "univariate_band"
                m["calibration"] = m["self_calibration"]
                m["corridor_quality"] = "self_band"
                served = "self"
                self_served.append((s.get("name"), s.get("gpa_id"), tc.get("self")))
            else:
                served, _ = _best_of(tc)           # self_calibration нет (не переобучено) → лучший conf/hybr
                served = served or "conformal"
                self_missing.append((s.get("name"), s.get("gpa_id")))
        elif chosen in ("conformal", "hybrid"):
            served = chosen
        else:
            served, _ = _best_of(tc)               # None/SUBTARGET → лучший из conformal|hybrid
            served = served or "conformal"
        applied[served] = applied.get(served, 0) + 1

        if flag == "SUBTARGET":
            subtarget.append((s.get("name"), s.get("gpa_id"), _best_of(tc)[1]))

        m["corridor_mode"] = served
        m["cv"] = dict(best_mode=best_diag, served=served, test_cov=tc,
                       n_test=s.get("n_test"), flag=flag, level_shift_std=s.get("level_shift_std"))
        rows.append((s.get("gpa_id"), s.get("name"), served, best_diag, _best_of(tc)[1], flag))

    print(f"Датчиков в выборе: {len(sel)}, применено: {len(sel) - len(missing)}")
    print("Активный режим (служимый):", applied)
    if self_served:
        print(f"\nself-band применён (detector_mode→univariate_band, self_calibration): {len(self_served)}")
        for name, gid, sv in sorted(self_served, key=lambda x: (x[1], x[0])):
            print(f"   ГПА-{gid} {name}: self_cov={sv}")
    if self_missing:
        print(f"\n⚠ self выбран, но self_calibration НЕТ (нужен ретрейн с CS_SELF_BAND=1): {len(self_missing)}")
        for name, gid in sorted(self_missing, key=lambda x: (x[1], x[0])):
            print(f"   ГПА-{gid} {name} → служим лучший conformal/hybrid")
    if subtarget:
        print(f"\n⚠ SUBTARGET (коридор есть, но <0.95 — честно помечен в cv.flag, инженеру): {len(subtarget)}")
        for name, gid, bv in sorted(subtarget, key=lambda x: (x[1], x[0])):
            print(f"   ГПА-{gid} {name}: best_conf/hybr={bv:.3f}")
    if missing:
        print(f"\n⚠ в metadata нет {len(missing)} ключей из выбора: {missing[:5]}...")

    if args.dry_run:
        print("\n(dry-run: metadata НЕ изменена)")
        return
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"\n✓ metadata обновлена: {meta_path}")


if __name__ == "__main__":
    main()
