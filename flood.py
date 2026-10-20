#!/usr/bin/env python3
"""
Prueba de carga HTTP acotada para el lab de Fail2Ban (uso EXCLUSIVO contra tus
propios servicios en el entorno aislado del lab).

Versión parametrizada: a diferencia del script original de la práctica (que
tenía los objetivos fijos en una tupla TARGETS), este recibe el HOST y el
PUERTO a probar como argumentos de línea de comandos. Así el mismo script sirve
para medir el frontend, el backend y la API LDAP sin tocar el código.

Ejemplos:
    python flood.py 127.0.0.1 8001                 # backend por puerto publicado
    python flood.py dashboard-backend-protected 8001 --concurrency 150 --rate 150 --duration 45
    python flood.py 127.0.0.1 3000 --path /login   # frontend

Mantiene topes de seguridad (concurrencia/tasa/duración máximas) para que sea
una prueba de carga controlada y no una herramienta de abuso.
"""

from __future__ import annotations

import argparse
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

# --- Topes de seguridad de la prueba acotada ---
REQUEST_TIMEOUT_SECONDS = 2
REPORT_INTERVAL_SECONDS = 3
MAX_DURATION_SECONDS = 300
MAX_CONCURRENCY = 1000
MAX_REQUESTS_PER_SECOND = 2000


def positive_bounded(value: str, maximum: int) -> int:
    """Valida un entero positivo acotado para los argumentos numéricos."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"debe ser entero; recibí {value!r}") from error
    if not 1 <= number <= maximum:
        raise argparse.ArgumentTypeError(f"debe estar entre 1 y {maximum}; recibí {number}")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prueba de carga HTTP acotada (host y puerto como argumentos).",
    )
    parser.add_argument("host", help="Host/IP o nombre de servicio Docker a probar")
    parser.add_argument("port", type=lambda v: positive_bounded(v, 65535),
                        help="Puerto TCP del servicio a probar (1-65535)")
    parser.add_argument("--path", default="/",
                        help="Ruta HTTP a solicitar (por defecto '/')")
    parser.add_argument("--concurrency", type=lambda v: positive_bounded(v, MAX_CONCURRENCY),
                        default=40, help=f"Workers en paralelo (1-{MAX_CONCURRENCY}, def. 40)")
    parser.add_argument("--rate", type=lambda v: positive_bounded(v, MAX_REQUESTS_PER_SECOND),
                        default=50, help=f"Tope agregado de peticiones/seg (1-{MAX_REQUESTS_PER_SECOND}, def. 50)")
    parser.add_argument("--duration", type=lambda v: positive_bounded(v, MAX_DURATION_SECONDS),
                        default=20, help=f"Duración en segundos (1-{MAX_DURATION_SECONDS}, def. 20)")
    parser.add_argument("--quiet", action="store_true",
                        help="No imprime reportes intermedios, solo el resultado final")
    return parser.parse_args()


def request_status(opener: urllib.request.OpenerDirector, url: str) -> str:
    """Hace un GET y devuelve el status HTTP, o '000' si falla la conexión."""
    request = urllib.request.Request(url, method="GET")
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return str(response.status)
    except urllib.error.HTTPError as error:
        return str(error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        return "000"


class RateLimiter:
    """Limita el ritmo agregado de inicios de petición entre todos los workers."""

    def __init__(self, requests_per_second: int) -> None:
        self.interval = 1 / requests_per_second
        self.next_request_at = 0.0
        self.lock = threading.Lock()

    def wait_for_turn(self, stop_event: threading.Event) -> bool:
        with self.lock:
            now = time.monotonic()
            scheduled_at = max(now, self.next_request_at)
            self.next_request_at = scheduled_at + self.interval
        return not stop_event.wait(max(0.0, scheduled_at - now))


def worker(url, deadline, stop_event, rate_limiter, counts, counts_lock) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while not stop_event.is_set() and time.monotonic() < deadline:
        if not rate_limiter.wait_for_turn(stop_event):
            break
        if time.monotonic() >= deadline:
            break
        code = request_status(opener, url)
        with counts_lock:
            counts[code] += 1


def snapshot(counts, counts_lock) -> Counter:
    with counts_lock:
        return counts.copy()


def report(target: str, counts: Counter, elapsed: float, final: bool = False) -> None:
    total = sum(counts.values())
    ok = counts["200"]
    failed = total - ok
    rate = total / elapsed if elapsed > 0 else 0.0
    label = "RESULTADO" if final else f"[{datetime.now():%H:%M:%S}] parcial"
    print(f"  {label} para {target}: total={total} ok(200)={ok} "
          f"bloqueadas/fallidas={failed} ~{rate:.0f} req/s", flush=True)
    if final and total:
        pct = failed * 100 // total
        print(f"  -> {pct}% de respuestas NO fueron 200", flush=True)
        conn_fail = counts["000"]
        if conn_fail:
            print(f"  -> {conn_fail} fallos de conexión (000): posible BAN de Fail2Ban "
                  f"o servicio saturado/caído", flush=True)


def run_test(host: str, port: int, path: str, concurrency: int,
             rate: int, duration: int, quiet: bool = False) -> Counter:
    """Ejecuta UNA prueba de carga acotada y devuelve el conteo de status.

    Reutilizable desde sweep.py para barrer varios niveles de carga.
    """
    target = f"{host}:{port}"
    url = f"http://{target}{path}"

    print(
        f"=== Prueba de carga acotada contra {url} ===\n"
        f"    workers={concurrency}, tope={rate} req/s, duración={duration}s",
        flush=True,
    )

    counts: Counter = Counter()
    counts_lock = threading.Lock()
    stop_event = threading.Event()
    rate_limiter = RateLimiter(rate)
    start = time.monotonic()
    deadline = start + duration

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(worker, url, deadline, stop_event, rate_limiter, counts, counts_lock)
            for _ in range(concurrency)
        ]
        try:
            while time.monotonic() < deadline:
                time.sleep(min(REPORT_INTERVAL_SECONDS, deadline - time.monotonic()))
                if not quiet:
                    report(target, snapshot(counts, counts_lock), time.monotonic() - start)
        except KeyboardInterrupt:
            print("\nDeteniendo workers tras Ctrl-C...", flush=True)
        finally:
            stop_event.set()
        for future in futures:
            future.result()

    counts = snapshot(counts, counts_lock)
    report(target, counts, time.monotonic() - start, final=True)
    return counts


def main() -> None:
    args = parse_args()
    run_test(args.host, args.port, args.path, args.concurrency,
             args.rate, args.duration, args.quiet)
    print("\n=== Prueba finalizada; solo se contactó el objetivo indicado. ===")


if __name__ == "__main__":
    main()
