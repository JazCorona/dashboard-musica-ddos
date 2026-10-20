#!/usr/bin/env python3
"""
Barrido automático de carga: encuentra el VALOR ESPECÍFICO de carga (req/s) que
un servicio soporta SIN caerse ni activar el ban de Fail2Ban.

Idea: se prueba el objetivo con tasas de petición crecientes (--rate). Para cada
nivel se mide el porcentaje de respuestas NO-200 y los fallos de conexión (000).
El servicio se considera que "AGUANTA" un nivel si:
    - % de NO-200 <= --fail-threshold  (por defecto 5%), y
    - no hay fallos de conexión 000 (que indican ban de Fail2Ban o caída).

El mayor nivel que aún AGUANTA es el "valor que no tumba el servicio".

Uso:
    python sweep.py 127.0.0.1 8001
    python sweep.py 127.0.0.1 8001 --rates 10,20,40,60,80,120,160 --duration 15
    python sweep.py dashboard-backend-protected 8001 --out resultados-backend.txt
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime

from flood import run_test


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Barrido de carga para hallar el máximo sostenible.")
    p.add_argument("host")
    p.add_argument("port", type=int)
    p.add_argument("--path", default="/")
    p.add_argument("--rates", default="10,20,40,60,80,100,150,200",
                   help="Lista de tasas req/s a probar, separadas por comas")
    p.add_argument("--concurrency", type=int, default=100)
    p.add_argument("--duration", type=int, default=15,
                   help="Segundos por nivel (>= findtime de Fail2Ban para que el ban alcance a dispararse)")
    p.add_argument("--fail-threshold", type=float, default=5.0,
                   help="%% de respuestas NO-200 tolerado para considerar que 'aguanta'")
    p.add_argument("--cooldown", type=int, default=20,
                   help="Pausa entre niveles (deja expirar bans cortos / libera conexiones)")
    p.add_argument("--out", default=None, help="Archivo donde guardar la tabla de resultados")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    rates = [int(r) for r in args.rates.split(",") if r.strip()]
    target = f"{args.host}:{args.port}"

    rows = []
    max_sostenible = 0

    print(f"\n#### BARRIDO DE CARGA contra {target}{args.path} ####")
    print(f"Niveles (req/s): {rates} | duración/nivel: {args.duration}s | "
          f"umbral NO-200: {args.fail_threshold}%\n")

    for rate in rates:
        counts = run_test(args.host, args.port, args.path,
                          concurrency=min(args.concurrency, rate * 2),
                          rate=rate, duration=args.duration, quiet=True)
        total = sum(counts.values())
        ok = counts["200"]
        non200 = total - ok
        conn_fail = counts["000"]
        pct = (non200 * 100 / total) if total else 100.0
        aguanta = pct <= args.fail_threshold and conn_fail == 0
        if aguanta:
            max_sostenible = max(max_sostenible, rate)
        estado = "AGUANTA" if aguanta else "CAE/BAN"
        row = (rate, total, ok, non200, conn_fail, round(pct, 1), estado)
        rows.append(row)
        print(f"  rate={rate:>4} req/s -> total={total:>5} ok={ok:>5} "
              f"no200={non200:>5} conn_fail(000)={conn_fail:>4} "
              f"no200%={pct:5.1f}  => {estado}")
        time.sleep(args.cooldown)

    header = f"{'rate':>6} {'total':>7} {'ok200':>7} {'no200':>7} {'conn000':>8} {'no200%':>7}  estado"
    sep = "-" * len(header)
    lines = [
        f"# Barrido de carga - {target}{args.path}",
        f"# Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"# Umbral NO-200: {args.fail_threshold}%  | duracion/nivel: {args.duration}s",
        sep, header, sep,
    ]
    for (rate, total, ok, non200, conn_fail, pct, estado) in rows:
        lines.append(f"{rate:>6} {total:>7} {ok:>7} {non200:>7} {conn_fail:>8} {pct:>7}  {estado}")
    lines.append(sep)
    lines.append(f"VALOR MAXIMO QUE NO TUMBA EL SERVICIO: {max_sostenible} req/s")
    out_text = "\n".join(lines)

    print("\n" + out_text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(out_text + "\n")
        print(f"\n[guardado en {args.out}]")


if __name__ == "__main__":
    main()
