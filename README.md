# Lab3 – Simulador Redis Pub/Sub (Dijkstra)

Este simulador:

* Carga una **topología** (formato `N1-N2:20, ...`).
* Construye **tablas de next-hop** con **Dijkstra**.
* Levanta un **router** por cada nodo (uno por canal de Redis).
* Envía un **HELLO** y un **MESSAGE** de prueba de `--source` a `--dest`.

> Por ahora, la flag `--proto` acepta `dijkstra|flooding|lsr|dvr`, pero **solo Dijkstra** está implementado (las otras se aceptan para dejar listo el CLI).

---

## Requisitos

* Python 3.10+
* Docker + Docker Compose:

  * **Windows:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  * **Linux:** Docker Engine y plugin `docker compose` (en distros nuevas viene incluido)
* Dependencias Python (dentro de un venv):

  ```bash
  pip install -r requirements.txt
  ```

---

## Estructura sugerida

```
.
├─ docker-compose.yml
├─ topology.txt
└─ src/
   ├─ simulator.py
   ├─ topology.py
   └─ dijkstra.py
```

* `topology.txt` contiene la topología en texto (ejemplo al final).
* `src/topology.py` tiene el parser.
* `src/dijkstra.py` resuelve next-hops.
* `src/simulator.py` corre los routers y publica mensajes.

---

## Levantar Redis local

### Linux / macOS

Para inicializar: 
```bash
docker compose up -d
```

Para detener:
```bash
docker rm -f lab03-redes-redis-1
```

### Windows (PowerShell)

Para inicializar: 
```powershell
docker compose up -d
```

Para detener:
```powershell
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

## Ejecución rápida (Dijkstra)

### A) Contra Redis local (Docker)

```bash
python3 src/simulator.py \
  --topology topology.txt \
  --source N3 \
  --dest N11 \
  --text "Hola desde N3" \
  --proto dijkstra \
  --redis-host localhost \
  --redis-port 6379 \
  --redis-pwd testpass \
  --group grupo3 \
  --prefix "sec30.grupo3"
```

```powershell
python.exe src/simulator.py --topology topology.txt --source N3 --dest N11 --text "Hola desde N3" --proto dijkstra --redis-host localhost --redis-port 6379 --redis-pwd testpass --group grupo3 --prefix "sec30.grupo3"
```

### B) Contra Redis Real

```bash
python3 src/simulator.py \
  --topology topology.txt \
  --source N3 \
  --dest N11 \
  --text "Test grupo3" \
  --proto dijkstra \
  --redis-host lab3.redesuvg.cloud \
  --redis-port 6379 \
  --redis-pwd UVGRedis2025 \
  --group grupo3 \
  --prefix "sec30.grupo3"
```

```powershell
python.exe src/simulator.py --topology topology.txt --source N3 --dest N11 --text "Test grupo3" --proto dijkstra --redis-host lab3.redesuvg.cloud --redis-port 6379 --redis-pwd UVGRedis2025 --group grupo3 --prefix "sec30.grupo3"
```

> ⚠️ **Importante**:
>
> * `--group grupo3` hace que **cada router escuche** en canales `sec30.grupo3.nodoX`.
> * `--prefix "sec30.grupo3"` hace que **publique** a `sec30.grupo3.nodoX`.
> * Para que el simulador funcione internamente, **group y prefix deben coincidir** (a menos que tengas un motivo para separarlos y lo controles conscientemente).

---

## Flags (referencia)

| Flag           | Valor por defecto        | Descripción                                                                                |          |     |                                         |
| -------------- | ------------------------ | ------------------------------------------------------------------------------------------ | -------- | --- | --------------------------------------- |
| `--topology`   | `topology.txt`           | Ruta al archivo de topología (texto).                                                      |          |     |                                         |
| `--source`     | `N3`                     | Nodo origen lógico (formato `N*`).                                                         |          |     |                                         |
| `--dest`       | `N11`                    | Nodo destino lógico (formato `N*`).                                                        |          |     |                                         |
| `--text`       | `Hola desde Dijkstra` | Payload del mensaje de usuario.                                                            |          |     |                                         |
| `--ttl`        | `20`                     | TTL del mensaje.                                                                           |          |     |                                         |
| `--runtime`    | `6.0`                    | Segundos que se mantiene corriendo la simulación (para ver hops).                          |          |     |                                         |
| `--proto`      | `dijkstra`               | dijkstra/flooding/lsr/dvr (por ahora siempre usa Dijkstra). |
| `--redis-host` | `localhost`              | Host de Redis.                                                                             |          |     |                                         |
| `--redis-port` | `6379`                   | Puerto de Redis.                                                                           |          |     |                                         |
| `--redis-pwd`  | `testpass`               | Password de Redis (en el Docker local).                                                    |          |     |                                         |
| `--group`      | `sim`                    | Grupo para direcciones `sec30.<group>.nodoX` (routers **escuchan** aquí).                  |          |     |                                         |
| `--prefix`     | `grupo3`                 | Prefijo alterno para **publicar** `sec30.<prefix>.nodoX`. Deja `grupo3` para sec30.grupo3. |          |     |                                         |

### Variables de entorno equivalentes (opcional)

**Linux/macOS:**

```bash
export REDIS_HOST=lab3.redesuvg.cloud
export REDIS_PWD=UVGRedis2025
export GROUP=grupo3
export CHAN_PREFIX=grupo3
python3 src/simulator.py --source N3 --dest N11
```

**Windows (PowerShell):**

```powershell
$env:REDIS_HOST="lab3.redesuvg.cloud"
$env:REDIS_PWD="UVGRedis2025"
$env:GROUP="grupo3"
$env:CHAN_PREFIX="grupo3"
python src/simulator.py --source N3 --dest N11
```

---

## Canales y direcciones

* Los campos `from` y `to` del mensaje usan la forma:
  `sec30.<group>.nodo<NUM>` (ej. `sec30.grupo3.nodo3`).
* Internamente, el simulador conoce el **id lógico** del nodo (`N3`) y lo convierte a/desde dirección (helpers `address_of` / `node_of`).
* Los **headers** se envían como **lista de objetos** `[{k:v},{k2:v2}]`, y el simulador los enriquece con:

  * `via` (el hop actual), `path` (lista de nodos recorridos), `cost` (costo acumulado).

---

## Formato de topología (`topology.txt`)

```
N1-N2:20, N1-N3:14, N1-N5:17, N10-N5:8, N10-N6:7, N10-N7:7, N11-N2:1, N11-N4:10,
N11-N6:20, N2-N7:4, N3-N4:14, N3-N9:2, N4-N6:4, N4-N8:19, N4-N9:14, N5-N6:5, N5-
N9:20, N6-N7:3, N6-N9:1, N8-N9:4
```

El parser crea un grafo **no dirigido** con pesos. Dijkstra calcula **next-hops** para cada nodo hacia todos los destinos.

