# Dashboard Música — Script de prueba de carga / DDoS (repo 4 de 4)

Herramienta de **prueba de carga HTTP acotada** para medir cuánta carga soportan
los servicios del lab (frontend, backend y API LDAP) **ya protegidos con
Fail2Ban**, y para encontrar el valor de carga que **no** tumba cada servicio.

> ⚠️ **Uso exclusivo del lab.** Este script solo debe usarse contra tus propios
> servicios en el entorno aislado de la clase. Mantiene topes de seguridad
> (concurrencia, tasa y duración máximas) para que sea una prueba controlada.

Vive **fuera** de las 3 imágenes de Docker de los servicios: es su propio repo y
su propia imagen, tal como pide la actividad.

---

## 1. Qué se modificó respecto al script original de la práctica

El `flood.py` de la práctica tenía los objetivos **fijos** en el código:

```python
TARGETS = ("victim-protected", "victim-unprotected")   # objetivos hardcodeados
```
y leía los parámetros desde variables de entorno (`DURATION`, `CONCURRENCY`,
`REQUESTS_PER_SECOND`).

La versión de este repo recibe el **host y el puerto como argumentos** de línea
de comandos (más la ruta, concurrencia, tasa y duración como opciones), así el
mismo script sirve para cualquiera de los tres servicios sin tocar el código:

```
python flood.py <HOST> <PUERTO> [--path /] [--concurrency N] [--rate R] [--duration S]
```

Resumen de cambios:

| Antes (práctica) | Ahora (este repo) |
|---|---|
| `TARGETS` fijos en el código | `host` y `port` como argumentos posicionales |
| Config por variables de entorno | Config por flags `--concurrency --rate --duration --path` |
| Golpea 2 objetivos fijos en secuencia | Golpea el objetivo que tú indiques |
| — | `sweep.py`: barrido automático que halla el máximo sostenible |

---

## 2. Cómo ejecutarlo

### Opción A — con Python directo (desde el host)
Apunta al **puerto publicado** de cada servicio en `localhost`:

```bash
python flood.py 127.0.0.1 8001                 # backend
python flood.py 127.0.0.1 3000 --path /login   # frontend
python flood.py 127.0.0.1 8000                 # API LDAP (flujo JWT)
```

### Opción B — como contenedor en la MISMA red Docker (recomendado)
Correrlo dentro de la red del lab hace que Nginx vea la **IP real del atacante**
(la IP del contenedor), y así Fail2Ban banea esa IP y no el gateway de Docker:

```bash
# 1) Crear/compartir una red común (si usas 'external: true' en los compose)
docker network create labnet

# 2) Construir la imagen del atacante
docker build -t ddos-tester .

# 3) Lanzar contra un servicio por su nombre de contenedor y puerto interno
docker run --rm --network labnet ddos-tester \
    flood.py dashboard-backend-protected 8001 --rate 150 --concurrency 150 --duration 45
```

> **Por qué importa la red:** si golpeas `localhost:puerto` desde el host, el NAT
> de Docker puede hacer que el contenedor vea como origen la IP del gateway
> (p. ej. `172.x.0.1`) en vez de la tuya, y Fail2Ban banearía al gateway. En la
> misma red de usuario, el origen es la IP real del contenedor atacante, igual
> que en la práctica (el atacante `172.29.0.20`).

---

## 3. Metodología para hallar “la carga que NO tumba el servicio”

Cada servicio está protegido con un jail `http-flood` de Fail2Ban:

```
maxretry = 60      # nº de peticiones...
findtime = 10s     # ...dentro de esta ventana  -> ~6 req/s sostenidas antes del ban
bantime  = 10m     # (con escalado para reincidentes)
```

Por encima de ese umbral, Fail2Ban banea la IP con `iptables-allports` y las
peticiones siguientes empiezan a fallar en la conexión (status `000`): el
servicio “se cae” **para esa IP**. El objetivo del ejercicio es encontrar
empíricamente el valor de `--rate` justo por debajo de ese punto.

### Criterio de “aguanta”
Un nivel de carga se considera soportado si:
- el **% de respuestas NO-200** se mantiene bajo (≤ 5% por defecto), **y**
- **no** aparecen fallos de conexión `000` (que indican ban o saturación).

### Procedimiento manual
1. Levanta el servicio protegido (ver su repo: `docker compose up --build`).
2. Lanza `flood.py` con una tasa baja y ve subiendo `--rate`:
   ```bash
   python flood.py 127.0.0.1 8001 --rate 20  --duration 15
   python flood.py 127.0.0.1 8001 --rate 60  --duration 15
   python flood.py 127.0.0.1 8001 --rate 120 --duration 15
   ```
3. Observa el `RESULTADO`: cuando empiecen a aparecer `no200` masivos y
   `fallos de conexión (000)`, ese nivel **tumba/banea** el servicio. El nivel
   inmediatamente anterior es el máximo sostenible.
4. (Opcional) En el contenedor del servicio verifica el ban:
   ```bash
   docker compose exec backend fail2ban-client status http-flood
   docker compose exec backend iptables -S | grep f2b
   ```

### Procedimiento automático (recomendado)
`sweep.py` hace ese barrido solo y te da el número directamente. Tres detalles
clave para que la medición sea válida:

1. **Pégale a una ruta que responda 200** (`--path /health`). Si usas `/` y el
   servicio no tiene esa ruta, devuelve 404 y el barrido lo marca como "cae"
   aunque esté sano.
2. **Barre tasas alrededor del umbral del jail** (~6 req/s para
   `maxretry=60/findtime=10s`), no muy por encima.
3. **Desbanea entre niveles** con `--reset-cmd`: el ban dura 10 min, así que sin
   esto el primer ban contamina todos los niveles siguientes.

```bash
python sweep.py 127.0.0.1 8001 --path /health \
    --rates 2,4,5,6,8,10 --duration 15 \
    --reset-cmd "docker exec dashboard-backend-protected fail2ban-client unban --all" \
    --out resultados-backend.txt
```

Imprime una tabla y la línea final:

```
VALOR MAXIMO QUE NO TUMBA EL SERVICIO: <N> req/s
```

> `--duration` ≥ `findtime` (10 s) para que el ban alcance a dispararse dentro de
> cada nivel. El contenedor de cada servicio se llama `dashboard-backend-protected`,
> `dashboard-frontend-protected` o `ldap-oauth-api` (ajusta el `--reset-cmd`).
>
> **Nota sobre la IP:** al pegarle desde el host a `localhost:puerto`, el NAT de
> Docker hace que Nginx vea como origen el gateway de la red Docker, así que
> Fail2Ban banea esa IP (y con ella todo el tráfico del host). Para que la prueba
> sea por-IP real, corre el atacante como contenedor en la misma red (Opción B).

---

## 4. Resultados obtenidos

> Rellena esta tabla corriendo `sweep.py` contra cada servicio en **tu** máquina
> (los números dependen del hardware y de la config del jail). Guarda las salidas
> `resultados-*.txt` como evidencia.

| Servicio | Puerto | `maxretry`/`findtime` | Máx. que aguanta | Nivel que lo tumba/banea |
|---|---|---|---|---|
| Frontend (Next.js) | 3000 | 60 / 10s | _p. ej._ ___ req/s | ___ req/s |
| Backend (FastAPI)  | 8001 | 60 / 10s | ___ req/s | ___ req/s |
| API LDAP (JWT)     | 8000 | 60 / 10s | ___ req/s | ___ req/s |

Comando usado para cada fila (ruta `/health` que responde 200, tasas alrededor
del umbral, y desbaneo entre niveles):
```bash
python sweep.py 127.0.0.1 8001 --path /health --rates 2,4,5,6,8,10 --duration 15 \
    --reset-cmd "docker exec dashboard-backend-protected fail2ban-client unban --all"  --out resultados-backend.txt
python sweep.py 127.0.0.1 3000 --path /health --rates 2,4,5,6,8,10 --duration 15 \
    --reset-cmd "docker exec dashboard-frontend-protected fail2ban-client unban --all" --out resultados-frontend.txt
python sweep.py 127.0.0.1 8000 --path /health --rates 2,4,5,6,8,10 --duration 15 \
    --reset-cmd "docker exec ldap-oauth-api fail2ban-client unban --all"               --out resultados-ldap.txt
```

**Interpretación esperada:** como el jail banea con ~6 req/s sostenidas durante
10 s, el máximo sostenible ronda ese umbral. Si subes `maxretry`/`findtime`, el
servicio tolera más carga antes del ban; si los bajas, se banea antes. Esa
relación directa entre la config de Fail2Ban y el valor medido es el punto del
ejercicio.

---

## 5. Archivos

| Archivo | Qué hace |
|---|---|
| `flood.py` | Prueba de carga contra `host puerto` (argumentos). |
| `sweep.py` | Barrido de tasas; imprime el máximo que no tumba el servicio. |
| `Dockerfile` | Imagen del atacante para correr en la red del lab. |
