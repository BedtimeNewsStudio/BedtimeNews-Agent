<h1 align="center">BedtimeNews Knowledge Base</h1>

<p align="center">
  <a href="https://github.com/BedtimeNewsStudio/BedtimeNews-Agent/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/BedtimeNewsStudio/BedtimeNews-Agent/ci.yml?branch=main&amp;label=CI&amp;logo=githubactions&amp;logoColor=white&amp;style=for-the-badge&amp;labelColor=1f2328"></a>
  <a href="https://github.com/BedtimeNewsStudio/BedtimeNews-Agent/actions/workflows/release.yml"><img alt="Release" src="https://img.shields.io/github/actions/workflow/status/BedtimeNewsStudio/BedtimeNews-Agent/release.yml?label=release&amp;logo=githubactions&amp;logoColor=white&amp;style=for-the-badge&amp;labelColor=1f2328"></a>
  <a href="https://github.com/orgs/BedtimeNewsStudio/packages?repo_name=BedtimeNews-Agent"><img alt="ghcr.io" src="https://img.shields.io/github/v/tag/BedtimeNewsStudio/BedtimeNews-Agent?sort=semver&amp;label=ghcr.io&amp;logo=docker&amp;logoColor=white&amp;color=2563eb&amp;style=for-the-badge&amp;labelColor=1f2328"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/github/license/BedtimeNewsStudio/BedtimeNews-Agent?color=6e7781&amp;style=for-the-badge&amp;labelColor=1f2328"></a>
</p>

<p align="center">
  <a href="README.md">中文</a> |
  <a href="README.en.md">English</a> |
  <a href="README.es-ES.md">Español</a>
</p>

El sitio web de la base de conocimiento de BedtimeNews (睡前消息): pregunta al
agente y navega o lee el archivo completo de transcripciones en el mismo lugar.
El Q&A funciona con un sistema RAG agéntico (Retrieval-Augmented Generation) —
enrutamiento automático, búsqueda semántica, contexto de transcripciones
recuperadas y citas de episodios.

[![Video de demostración de la base de conocimiento de Bedtime News (YouTube)](https://img.youtube.com/vi/9_SlMaqBvcU/maxresdefault.jpg)](https://www.youtube.com/watch?v=9_SlMaqBvcU)

> Video en chino. ¿No se reproduce? Prueba el sitio directamente en [bedtime.blog](https://bedtime.blog).

## Descripción General

Los textos de las transcripciones y el sistema inteligente viven en dos repos
separados: las transcripciones fuente se mantienen en
[BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts),
mientras que este repositorio (BedtimeNews-Agent) las indexa y opera un sitio
web que sirve tanto Q&A impulsado por LLM como lectura de transcripciones dentro
de la aplicación — las transcripciones indexadas se ofrecen como contenido del
propio sitio, y las citas de las respuestas saltan directamente al lector
integrado. Construido con LangGraph, endpoints de chat/embedding compatibles con OpenAI
(DeepSeek para chat y los embeddings Qwen3 de SiliconFlow por defecto), y
PostgreSQL + pgvector.

**Características Principales:**

- Enrutamiento automático de consultas (recuperación de archivo vs manejo directo restringido)
- Optimización de consultas y búsqueda semántica
- Calificación basada en LLM de documentos
- Transcripciones recuperadas proporcionadas como contexto de respuesta, con citas en formato markdown y reparación de citas
- Indexación automatizada de documentos con actualizaciones incrementales,
  publicada como snapshots inmutables: atómica, reversible y adoptada por el
  agente sin reiniciar
- Interfaz web: chat de preguntas y respuestas más navegación y lectura de
  transcripciones dentro del sitio (las citas saltan directamente al lector
  integrado)

## Arquitectura

![Arquitectura del sistema BedtimeNews](docs/diagrams/system-architecture.svg)

**Componentes:**

- **[Frontend](frontend/README.es-ES.md)**: Interfaz de chat personalizada (HTML/CSS/JS estático servido por una pequeña aplicación FastAPI)
- **[Agente](agent/README.es-ES.md)**: Servicio RAG agente basado en LangGraph
- **[Indexador](indexer/README.es-ES.md)**: Pipeline automatizado de incrustación de documentos
- **Base de Datos**: PostgreSQL con extensión pgvector como base de datos
  vectorial. El indexador es el único que escribe y publica la base de
  conocimiento como snapshots inmutables y versionados (un esquema `rag_s<id>`
  por construcción, registrado en `rag_meta`); el agente lee el snapshot
  compatible más reciente mediante el rol de solo lectura `rag_agent` y cambia a
  uno nuevo en 15 segundos

La pila sirve HTTP puro en el puerto 8080 — sin TLS. La exposición pública y la
terminación TLS se gestionan fuera de este repositorio.

## Inicio Rápido

### Requisitos Previos

- Docker con Docker Compose 2.20 o posterior (los archivos compose usan `include`)
- Claves API para los endpoints de generación y embeddings — se rellenan en
  `config.yml` (cualquier proveedor compatible con OpenAI; la plantilla por
  defecto usa DeepSeek para chat y Qwen3 embeddings de SiliconFlow como ejemplo)

### Configuración

1. **Clonar el repositorio**

   ```bash
   git clone https://github.com/BedtimeNewsStudio/BedtimeNews-Agent.git
   cd BedtimeNews-Agent
   ```

2. **Configurar el entorno**

   Copia [`config.example.yml`](config.example.yml) a `config.yml` y configura:

   ```bash
   cp config.example.yml config.yml
   # Edita config.yml — rellena los grupos generation / embedding
   # (api_key + base_url + model; cualquier proveedor compatible con OpenAI)
   ```

   También copia `.env.example` a `.env` (cableado de despliegue: puertos,
   tag de imagen, credenciales de postgres, `EMBEDDING_DIM` — solo esto sigue
   yendo por variables de entorno):

   ```bash
   cp .env.example .env
   ```

   - `EMBEDDING_DIM` es la dimensión de salida del modelo de embeddings (`2560`
     para el modelo por defecto `Qwen/Qwen3-Embedding-4B`). Junto con el modelo
     define el espacio vectorial en el que el indexador construye los snapshots
     y que lee el agente; ya no dimensiona una columna de la base de datos, y
     cambiarlo después hace que el indexador construya un snapshot nuevo en
     lugar de requerir una migración.
   - `POSTGRES_AGENT_PASSWORD` (opcional, recomendado) permite al agente
     conectarse con el rol de solo lectura `rag_agent`, sin poder modificar
     datos. Sin definir, el agente usa el superusuario y registra un aviso.

   > **Prioridad: variables de entorno > `config.yml`** (claves anidadas con
   > doble guion bajo, p.ej. `GENERATION__API_KEY`). Las claves y la config de
   > aplicación viven en `config.yml`; ya no hace falta exportar claves.

3. **Iniciar servicios**

   ```bash
   docker compose up -d
   ```

4. **Acceder a la interfaz**

   Abre `http://localhost:8080` (HTTP puro; cambia el puerto del host con
   `FRONTEND_PORT` en `.env`).

   Esto ejecuta las imágenes publicadas. Si has editado el código, añade `--build` — consulta [Imagen publicada vs tu checkout](#imagen-publicada-vs-tu-checkout).

   Con una base de datos nueva, el agente no está listo (`/health` devuelve
   `503`) hasta que se publica la primera construcción del indexador; después
   pasa a estar listo por sí solo.

### Verificar Instalación

```bash
# Verificar estado de servicios
docker compose ps

# Ver logs
docker compose logs -f

# Snapshots publicados por el indexador y disponibilidad del agente
docker compose exec indexer python -m src.snapshots list
docker compose exec agent curl -s http://localhost:8000/health

# Disponibilidad de toda la pila a través del servicio web (200 cuando se sirve
# un snapshot, 503 antes; el cuerpo indica el snapshot)
curl -s localhost:8080/readyz
```

### Pruebas y Cobertura

El comando de prueba raíz ejecuta agente, indexador y frontend en procesos aislados:

```bash
uv run pytest
uv run pytest --cov
```

Las opciones se reenvían a cada componente. Para ejecutar solo un componente, invócalo desde ese directorio:

```bash
cd agent  # o indexer / frontend
uv run pytest --cov
```

Las pruebas de construcción/publicación y selección de snapshots necesitan
PostgreSQL con pgvector y se omiten si no está disponible. Apúntalas a un
servidor donde el usuario pueda crear bases de datos (cada prueba usa su propia
base de datos temporal):

```bash
PGTEST_HOST=localhost PGTEST_PORT=5432 PGTEST_USER=postgres PGTEST_PASSWORD=postgres uv run pytest
```

## Versiones

Las versiones etiquetadas publican imágenes multi-arquitectura preconstruidas (amd64 + arm64) en GHCR mediante [release.yml](.github/workflows/release.yml):

- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-agent`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-indexer`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-frontend`

Para desplegar una versión publicada, fija una versión con `IMAGE_TAG` en `.env` (por defecto `latest`) y descarga. `IMAGE_TAG` fija el indexador y, por defecto, la aplicación (`agent` y `web`); `APP_IMAGE_TAG` sustituye solo la versión de la aplicación, que es como un despliegue blue-green ejecuta dos versiones a la vez (ver [Actualizaciones sin interrupción](#actualizaciones-sin-interrupción)):

```bash
# en .env: IMAGE_TAG=0.1.0
docker compose pull
docker compose up -d
```

### Imagen publicada vs tu checkout

`docker compose up` **nunca construye por sí mismo**, incluso desde un checkout de código fuente con ediciones locales. La clave `image:` decide qué se ejecuta:

| Situación                                | Lo que hace `docker compose up`               |
| ---------------------------------------- | --------------------------------------------- |
| Imagen etiquetada ya presente localmente | La reutiliza — sin descarga, sin construcción |
| Imagen etiquetada no presente localmente | **Descarga** la imagen publicada de GHCR      |
| `docker compose up --build`              | Construye desde el checkout                   |

Así que después de editar código, reconstruye explícitamente o seguirás ejecutando la imagen antigua:

```bash
docker compose up -d --build agent web
```

Ten en cuenta que una imagen construida localmente y una versión publicada comparten la misma etiqueta, por lo que la última creada gana. `docker compose pull` sobrescribe una construcción local, y `--build` sobrescribe una versión publicada. Por eso una instancia blue-green construida desde el código usa una etiqueta propia (p. ej. `APP_IMAGE_TAG=0.4.0-<sha>`) y pasa `--build` en el mismo `up`; si no, compose intenta descargar una etiqueta que no existe.

Para lanzar una versión, empuja una etiqueta `v*` (las etiquetas de imagen omiten el `v` inicial):

```bash
git tag v0.1.0 && git push origin v0.1.0
```

> Las notas de versión deben señalar cambios operativos: variables de entorno nuevas o renombradas, montajes y si se ejecutará una construcción completa. El indexador crea y adopta la estructura de la base de datos automáticamente; ya no hacen falta migraciones manuales. Un cambio en las tablas que lee el agente es un nuevo `format_version` de snapshot, construido junto al anterior.

### Actualización a snapshots RAG

El indexador con snapshots adopta automáticamente una base de datos existente en su primer arranque, sin pasos manuales y con el agente y el indexador actualizables en cualquier orden:

- El esquema `rag` existente se registra como snapshot `legacy`; el registro de auditoría pasa a `rag_state` y `rag.indexing_history` pasa a ser `rag.index_state`.
- La primera construcción es completa y reutiliza todos los vectores existentes (normalmente sin llamadas de embedding, unos minutos), publicando el primer snapshot normal.
- `legacy` se conserva 7 días, así que mientras tanto el agente antiguo sigue funcionando y los datos pueden revertirse con `python -m src.snapshots retire <id>`.
- Tras la adopción no se admite volver a una versión del **indexador** anterior a los snapshots; el agente puede volver atrás libremente hasta que `legacy` se recolecte.
- El indexador incorpora un montaje de solo lectura de `POSTGRES_DATA_DIR` (comprobación de disco) y `POSTGRES_AGENT_PASSWORD` es opcional; ambos están en los archivos compose (`compose.data.yml`, `compose.app.yml`).

Detalles: [indexer/README.es-ES.md](indexer/README.es-ES.md#actualización-desde-el-esquema-anterior-a-los-snapshots) y el [documento de diseño](docs/designs/20261007_rag-snapshot-architecture.md). `docker-compose.sample.yml` ofrece el subconjunto local determinista y requiere rutas aisladas en `POSTGRES_DATA_DIR` / `INDEXER_DATA_DIR`.

### Actualizaciones sin interrupción

La pila tiene dos capas ([diseño](docs/designs/20261008_blue-green-deployment.md)):

| Capa       | Archivo compose    | Servicios              | Actualización                                 |
| ---------- | ------------------ | ---------------------- | --------------------------------------------- |
| Datos      | `compose.data.yml` | `postgres`, indexador  | En el sitio                                   |
| Aplicación | `compose.app.yml`  | `agent`, `web`         | Blue-green: un proyecto compose por instancia |

`docker-compose.yml` incluye ambas y es lo que ejecuta `docker compose up -d` al autoalojar; ahí no cambia nada. Una actualización blue-green arranca la nueva versión como un segundo proyecto de aplicación en otro puerto, junto a la que está en marcha y con la misma base de datos, espera a su `/readyz`, cambia el proxy de borde, deja que la instancia antigua se vacíe y la retira con `down -v`. Como los datos servidos son snapshots inmutables que cada agente selecciona por sí mismo, dos versiones pueden funcionar a la vez sin reglas de compatibilidad de esquema.

Lo que el proyecto ofrece a una herramienta de despliegue: entradas por instancia (`APP_IMAGE_TAG`, `APP_PORT`, `APP_INSTANCE`, `BEDTIMENEWS_NET_NAME`, `BEDTIMENEWS_NET_EXTERNAL` y, opcionalmente, `RAG_SNAPSHOT`, `EMBEDDING_DIM`, `APP_CONFIG_FILE`), pasadas en la línea de comandos y nunca escritas en `.env`; `GET /healthz` (vitalidad de `web`, con versión e instancia) y `GET /readyz` (disponibilidad, con el snapshot); y una parada ordenada que deja terminar los streams de `/chat` abiertos. Lo que requiere: un proxy de borde que cambie de upstream de forma atómica, deje completar los streams en curso e informe de las peticiones en curso por upstream (Caddy hace las tres cosas), y una herramienta de despliegue que siga los pasos de la sección 3 del diseño. En producción, el `.env` de la VM define `COMPOSE_FILE=compose.data.yml` para que un `docker compose ...` sin `-f` solo actúe sobre la capa de datos.

Quien autoaloja y simplemente reinicia en el sitio no pierde nada, pero detener o recrear `agent` o `web` ahora espera hasta 5 minutos mientras haya un stream de chat abierto (cada `/chat` tiene un límite de 240 s; uvicorn espera 270 s; compose, 300 s).

## Documentación Específica de Servicios

- **[Frontend](frontend/README.es-ES.md)**: Personalización de UI
- **[Agente](agent/README.es-ES.md)**: Puntos finales API, implementación RAG agente
- **[Indexador](indexer/README.es-ES.md)**: Procesamiento de documentos

## Persistencia de Datos

Los datos se persisten entre reinicios:

- **Datos de PostgreSQL** (snapshots RAG, registro, registro de auditoría): montados en enlace a `./storage/postgres/volume`
- **Logs de servicios**: volúmenes nombrados de Docker `bedtimenews_indexer_logs` (capa de datos) y `bedtimenews_agent_logs` (uno por proyecto de aplicación, así dos instancias nunca comparten un archivo de log)

## Estructura del Proyecto

```plaintext
BedtimeNews-Agent/
├── agent/              # Servicio RAG agente LangGraph
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── frontend/           # Interfaz web personalizada (estático + FastAPI)
│   ├── server.py       # FastAPI: sirve UI estática + proxy de /chat SSE y las APIs de transcripciones
│   ├── starters.py     # Datos de preguntas de muestra
│   ├── static/         # index.html, styles.css, app.js, logo
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── indexer/            # Pipeline de incrustación de documentos
│   ├── src/
│   ├── Dockerfile
│   ├── README.md
│   ├── README.en.md
│   └── README.es-ES.md
├── docs/
│   ├── designs/        # Documentos de diseño
│   └── diagrams/       # Diagramas SVG de arquitectura y flujo de trabajo
├── storage/
│   └── postgres/       # init.sh (habilita pgvector); migrations/ es solo histórico
├── docker-compose.yml  # Paraguas: incluye ambas capas (autoalojamiento, desarrollo)
├── compose.data.yml    # Capa de datos: postgres + indexador (actualización en el sitio)
├── compose.app.yml     # Capa de aplicación: agent + web (un proyecto por instancia)
├── docker-compose.sample.yml  # Overlay de muestra solo local
├── config.yml          # Config de aplicación y claves (no en git, copiada del ejemplo)
├── config.example.yml  # Plantilla de config de aplicación
├── .env                # Cableado de despliegue (no en git, copiado de .env.example)
├── .env.example        # Plantilla de cableado
├── THIRD_PARTY_NOTICES.md  # Licencias de componentes de terceros
├── README.md           # README predeterminado (中文)
├── README.en.md        # README en inglés
└── README.es-ES.md     # README en español (este archivo)
```

## Licencia

Licencia MIT — consulta el archivo [LICENSE](LICENSE).

Este proyecto incluye componentes de terceros bajo sus propias licencias — consulta [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) para más detalles.
