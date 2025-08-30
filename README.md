# Lab3 – Simulador Redis Pub/Sub (Dijkstra + LSR)

Este simulador:

* Carga una **topología** (formato `N1-N2:20, ...`).
* **Dijkstra:** construye **tablas de next-hop** estáticas.
* **LSR (Link State Routing):** inunda **LSPs** (Link-State Packets), mantiene una **LSDB**, y recalcula **SPF (Dijkstra)** dinámicamente en cada nodo.
* Levanta un **router** por cada nodo (uno por canal de Redis).
* Envía un **HELLO** y un **MESSAGE** de prueba de `--source` a `--dest`.

> `--proto` acepta `dijkstra|lsr|flooding|dvr`.
> Implementados: **dijkstra** (estático) y **lsr** (dinámico).
> `flooding`/`dvr`: placeholders (por ahora caen en bootstrap Dijkstra).

---

## Requisitos

* Python 3.10+
* Docker + Docker Compose:

  * **Windows:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  * **Linux:** Docker Engine y plugin `docker compose`
* Dependencias Python (dentro de un venv):

  ```bash
  pip install -r requirements.txt
  ```

---

## Estructura

```
.
├─ docker-compose.yml
├─ topology.txt
└─ src/
   ├─ simulator.py     # Dijkstra + LSR (LSP flood + LSDB + SPF)
   ├─ topology.py      # parser de topología
   └─ dijkstra.py      # dijkstra, build_next_hop, all_pairs_next_hops
```

---

## Levantar Redis local

### Linux / macOS

```bash
docker compose up -d
# parar rápido:
docker rm -f lab03-redes-redis-1
```

### Windows (PowerShell)

```powershell
docker compose up -d
# parar rápido:
docker rm -f lab03-redes-redis-1
```

El `docker-compose.yml` expone Redis en `localhost:6379` con password `testpass`.

---

## Entornos Python

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## Ejecución rápida

### A) Dijkstra (estático)

```bash
python3 src/simulator.py \
  --topology topology.txt \
  --source N3 \
  --dest N11 \
  --text "Hola desde Dijkstra 👋" \
  --proto dijkstra \
  --redis-host localhost \
  --redis-port 6379 \
  --redis-pwd testpass \
  --group grupo3 \
  --prefix "sec30.grupo3"
```

### B) LSR (dinámico)

```bash
python3 src/simulator.py \
  --topology topology.txt \
  --source N3 \
  --dest N11 \
  --text "Hola desde LSR 👋" \
  --proto lsr \
  --redis-host localhost \
  --redis-port 6379 \
  --redis-pwd testpass \
  --group grupo3 \
  --prefix "sec30.grupo3"
```

> Con **LSR**, cada nodo:
>
> * Inunda LSPs periódicamente (2s).
> * Actualiza la **LSDB** al recibir LSPs nuevos (con control de `seq`).
> * Reconstruye un grafo SPF a partir de la LSDB y corre **Dijkstra** local.
> * Tus mensajes `message` usan la **tabla de next-hop** actualizada dinámicamente.

---

## Flags (referencia)

| Flag           | Default        | Descripción                                                               |
| -------------- | -------------- | ------------------------------------------------------------------------- |
| `--topology`   | `topology.txt` | Archivo de topología (texto).                                             |
| `--source`     | `N3`           | Nodo origen lógico (`N*`).                                                |
| `--dest`       | `N11`          | Nodo destino lógico (`N*`).                                               |
| `--text`       | `"Hola..."`    | Payload del mensaje de usuario.                                           |
| `--ttl`        | `20`           | TTL del mensaje.                                                          |
| `--runtime`    | `8.0`          | Segundos a mantener corriendo la simulación.                              |
| `--proto`      | `dijkstra`     | `dijkstra`/`lsr`/`flooding`/`dvr` (implementados: dijkstra, lsr).         |
| `--redis-host` | `localhost`    | Host Redis.                                                               |
| `--redis-port` | `6379`         | Puerto Redis.                                                             |
| `--redis-pwd`  | `testpass`     | Password Redis.                                                           |
| `--group`      | `sim`          | Grupo para direcciones `sec30.<group>.nodoX` (routers **escuchan** aquí). |
| `--prefix`     | `""`           | Prefijo alterno para **publicar** `sec30.<prefix>.nodoX`.                 |

### Variables de entorno equivalentes (opcional)

**Linux/macOS:**

```bash
export REDIS_HOST=lab3.redesuvg.cloud
export REDIS_PWD=UVGRedis2025
export GROUP=grupo3
export CHAN_PREFIX=grupo3
python3 src/simulator.py --source N3 --dest N11 --proto lsr
```

**Windows (PowerShell):**

```powershell
$env:REDIS_HOST="lab3.redesuvg.cloud"
$env:REDIS_PWD="UVGRedis2025"
$env:GROUP="grupo3"
$env:CHAN_PREFIX="grupo3"
python src/simulator.py --source N3 --dest N11 --proto lsr
```

---

## Topología (ejemplo)

```
N1-N2:20, N1-N3:14, N1-N5:17, N10-N5:8, N10-N6:7, N10-N7:7, N11-N2:1, N11-N4:10,
N11-N6:20, N2-N7:4, N3-N4:14, N3-N9:2, N4-N6:4, N4-N8:19, N4-N9:14, N5-N6:5, N5-
N9:20, N6-N7:3, N6-N9:1, N8-N9:4
```

El parser crea un grafo **no dirigido** con pesos.

* **Dijkstra:** calcula **next-hops** estáticos.
* **LSR:** reconstruye la topología vía LSPs y **recalcula** next-hops dinámicamente.
