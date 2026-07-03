"""
Отчёт по обученным моделям v2 (req 9). Читает metadata.json v2 → две формы:
  • CSV — одна строка на (датчик × ГПА), машиночитаемо;
  • HTML — сгруппировано по ГПА, отсортировано так, что БИТЫЕ модели всплывают наверх
    (univariate_only и низкий R²_eval вверху — чтобы сразу видеть проблемные).

Колонки: идентификация (sensor, gpa, tag, detector_mode, cutoff_mode, n_trees, last_train_ts),
метрики на НЕПЕРЕСЕКАЮЩИХСЯ окнах горизонта (MAE/RMSE/R²/nMAE + условный MAPE), R²_eval vs
baseline, калибровка по режимам (thr/n_eff/decision), важность фич (драйверы УРОВНЯ).
"""
from __future__ import annotations

import csv
import html
import json
from typing import Optional


def _fmt(v, nd=3):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else str(v)


def _rows(metadata: dict) -> list[dict]:
    rows = []
    for key, info in (metadata.get("models") or {}).items():
        wins = info.get("metrics_windows") or info.get("eval_windows") or {}
        # сводный MAE/R² по первому непустому окну (для сортировки/обзора)
        rows.append({
            "key": key,
            "sensor": info.get("name", key.split("__")[0]),
            "gpa": info.get("gpa_id", key.split("__GPA")[-1] if "__GPA" in key else "?"),
            "tag": info.get("tag", ""),
            "detector_mode": info.get("detector_mode", "legacy"),
            "corridor_quality": (info.get("corridor_quality") or (
                "genuine" if isinstance(info.get("r2_eval"), (int, float)) and info["r2_eval"] >= 0.3
                else "steady_band" if isinstance(info.get("r2_eval"), (int, float)) and info["r2_eval"] >= 0
                else "—") if info.get("detector_mode") == "ml_corridor" else "—"),
            "cutoff_mode": info.get("cutoff_mode", ""),
            "last_train_ts": info.get("last_train_ts", metadata.get("last_train_timestamp", "")),
            "n_trees": info.get("n_trees", ""),
            "r2_eval": info.get("r2_eval", info.get("r2_val")),
            "r2_baseline": info.get("r2_baseline"),
            "mae_val": info.get("mae_val"),
            "rmse_val": info.get("rmse_val"),
            "nmae_val": info.get("nmae_val"),
            "windows": wins,
            "calibration": (info.get("calibration") or {}).get("by_regime", {}),
            "conformal_thr": info.get("conformal_thr"),
            "top_features": info.get("top_features", []),
            "pre_fault": info.get("pre_fault", {}),
            "note": info.get("note", ""),
        })
    # сортировка: ГПА, затем detector_mode (univariate_only/legacy выше ml_corridor),
    # затем R²_eval по возрастанию → битые модели наверху каждой группы ГПА.
    mode_rank = {"univariate_only": 0, "legacy": 1, "ml_corridor": 2}
    def _r2(x):
        v = x["r2_eval"]
        return v if isinstance(v, (int, float)) else -1e9
    rows.sort(key=lambda x: (str(x["gpa"]), mode_rank.get(x["detector_mode"], 0), _r2(x)))
    return rows


WIN_ORDER = ["0-3д", "3-7д", "7-15д", "15-30д", "30д+"]


def write_csv(metadata: dict, path: str) -> int:
    rows = _rows(metadata)
    base_cols = ["gpa", "sensor", "tag", "detector_mode", "cutoff_mode", "n_trees",
                 "last_train_ts", "r2_eval", "r2_baseline", "mae_val", "rmse_val", "nmae_val",
                 "conformal_thr"]
    win_cols = [f"{w}_{m}" for w in WIN_ORDER for m in ("mae", "r2", "nmae", "mape", "n")]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(base_cols + win_cols + ["top_features", "note"])
        for r in rows:
            base = [r.get(c, "") for c in base_cols]
            wins = []
            for win in WIN_ORDER:
                wv = r["windows"].get(win, {})
                wins += [wv.get("mae", ""), wv.get("r2", ""), wv.get("nmae", ""),
                         wv.get("mape", ""), wv.get("n", "")]
            tf = "; ".join(f"{t['name']}={t['importance']}" for t in r["top_features"][:5])
            w.writerow(base + wins + [tf, r["note"]])
    return len(rows)


def write_html(metadata: dict, path: str) -> int:
    rows = _rows(metadata)
    sv = metadata.get("schema_version", "v1")
    mv = metadata.get("model_version", "?")
    by_gpa: dict = {}
    for r in rows:
        by_gpa.setdefault(str(r["gpa"]), []).append(r)

    def badge(mode):
        c = {"ml_corridor": "#22C55E", "univariate_only": "#F59E0B", "legacy": "#64748b"}.get(mode, "#888")
        return f'<span style="background:{c};color:#0b1020;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600">{html.escape(mode)}</span>'

    def r2cell(v):
        if not isinstance(v, (int, float)):
            return '<td style="color:#64748b">—</td>'
        col = "#22C55E" if v >= 0.7 else "#F59E0B" if v >= 0.3 else "#EF4444"
        return f'<td style="color:{col};font-weight:600">{v:.3f}</td>'

    parts = [f"""<!doctype html><meta charset="utf-8"><title>Отчёт моделей v2</title>
<style>
body{{background:#0b1020;color:#e2e8f0;font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#94a3b8;font-size:12px;margin-bottom:20px}}
h2{{font-size:15px;margin:24px 0 8px;color:#7dd3fc;border-bottom:1px solid #1e293b;padding-bottom:6px}}
table{{border-collapse:collapse;width:100%;margin-bottom:8px}}
th,td{{padding:6px 9px;text-align:left;border-bottom:1px solid #1e293b;white-space:nowrap}}
th{{color:#94a3b8;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
tr:hover td{{background:#0f172a}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
.win{{color:#cbd5e1;font-size:12px}} .feat{{color:#94a3b8;font-size:11px;max-width:340px;white-space:normal}}
.wrap{{overflow-x:auto}}
</style>
<h1>Отчёт по моделям здоровья ГПА — {html.escape(str(mv))}</h1>
<div class="sub">schema={html.escape(str(sv))} · всего моделей: {len(rows)} · сортировка: битые (univariate_only / низкий R²) наверху каждого ГПА · метрики — на непересекающихся окнах горизонта</div>"""]

    for gpa in sorted(by_gpa):
        grp = by_gpa[gpa]
        parts.append(f"<h2>ГПА-{html.escape(gpa)} · {len(grp)} моделей</h2><div class='wrap'><table>")
        parts.append("<tr><th>датчик</th><th>режим детекции</th><th>качество</th><th>cutoff</th><th>деревьев</th>"
                     "<th>R²eval</th><th>R²base</th><th>MAE</th><th>nMAE</th>"
                     "<th>окна (MAE/R²/n)</th><th>калибровка по режимам</th><th>драйверы уровня</th></tr>")
        for r in grp:
            wins_txt = " · ".join(
                f"{w}:{_fmt(r['windows'][w].get('mae'))}/{_fmt(r['windows'][w].get('r2'),2)}/{r['windows'][w].get('n','')}"
                for w in WIN_ORDER if r["windows"].get(w, {}).get("n"))
            calib_txt = "<br>".join(
                f"{html.escape(k)}: thr={_fmt(v.get('threshold'))} n_eff={v.get('n_eff','')} → {html.escape(str(v.get('decision','')))}"
                for k, v in (r["calibration"] or {}).items()) or "<span style='color:#64748b'>—</span>"
            feats = ", ".join(f"{html.escape(t['name'])}={t['importance']}" for t in r["top_features"][:5])
            pf = "⚠️pre-fault" if (r["pre_fault"] or {}).get("suspect_any") else ""
            _cq = r.get("corridor_quality", "—")
            _cqc = {"genuine": "#22C55E", "steady_band": "#F59E0B"}.get(_cq, "#64748b")
            parts.append(
                f"<tr><td><b>{html.escape(r['sensor'])}</b> {pf}</td><td>{badge(r['detector_mode'])}</td>"
                f"<td style='color:{_cqc};font-size:11px'>{html.escape(str(_cq))}</td>"
                f"<td>{html.escape(str(r['cutoff_mode']))}</td><td class='num'>{r['n_trees']}</td>"
                f"{r2cell(r['r2_eval'])}<td class='num' style='color:#64748b'>{_fmt(r['r2_baseline'],2)}</td>"
                f"<td class='num'>{_fmt(r['mae_val'])}</td><td class='num'>{_fmt(r['nmae_val'],4)}</td>"
                f"<td class='win'>{wins_txt}</td><td class='win'>{calib_txt}</td>"
                f"<td class='feat'>{feats}</td></tr>")
        parts.append("</table></div>")
    parts.append("<div class='sub' style='margin-top:24px'>Примечание: модель — эталон режима (E[датчик|режим]), "
                 "не форкастер. detector_mode=univariate_only ⇒ conditioning-response слаб (R²&lt;τ) или режим не "
                 "откалиброван ⇒ работают только univariate-детекторы. Важность фич = драйверы ожидаемого УРОВНЯ "
                 "(не атрибуция остатка).</div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return len(rows)


def generate(metadata_path: str, csv_path: str, html_path: str) -> int:
    with open(metadata_path, encoding="utf-8") as f:
        md = json.load(f)
    n = write_csv(md, csv_path)
    write_html(md, html_path)
    return n


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Отчёт по моделям v2 из metadata.json")
    p.add_argument("metadata", help="путь к metadata.json (v2)")
    p.add_argument("--csv", default="models_report.csv")
    p.add_argument("--html", default="models_report.html")
    a = p.parse_args()
    n = generate(a.metadata, a.csv, a.html)
    print(f"Отчёт: {n} моделей → {a.csv}, {a.html}")
