# BedtimeNews Knowledge Base

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

El sitio web de la base de conocimiento de BedtimeNews (睡前消息): pregunta al
agente y navega o lee el archivo completo de transcripciones en el mismo lugar.
El Q&A funciona con un sistema RAG agéntico (Retrieval-Augmented Generation) —
enrutamiento automático, búsqueda semántica, contexto de transcripciones
recuperadas y citas de episodios.

> **¡Pruébalo:** [bedtime.blog](https://bedtime.blog)

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
- Indexación automatizada de documentos con actualizaciones incrementales
- Interfaz web: chat de preguntas y respuestas más navegación y lectura de
  transcripciones dentro del sitio (las citas saltan directamente al lector
  integrado)

## Arquitectura

![Arquitectura del sistema BedtimeNews](docs/diagrams/system-architecture.svg)

**Componentes:**

- **[Frontend](frontend/README.es-ES.md)**: Interfaz de chat personalizada (HTML/CSS/JS estático servido por una pequeña aplicación FastAPI)
- **[Agente](agent/README.es-ES.md)**: Servicio RAG agente basado en LangGraph
- **[Indexador](indexer/README.es-ES.md)**: Pipeline automatizado de incrustación de documentos
- **Base de Datos**: PostgreSQL con extensión pgvector como base de datos vectorial

La pila sirve HTTP puro en el puerto 8080 — sin TLS. La exposición pública y la
terminación TLS se gestionan fuera de este repositorio.

## Inicio Rápido

### Requisitos Previos

- Docker
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

### Verificar Instalación

```bash
# Verificar estado de servicios
docker compose ps

# Ver logs
docker compose logs -f
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

## Versiones

Las versiones etiquetadas publican imágenes multi-arquitectura preconstruidas (amd64 + arm64) en GHCR mediante [release.yml](.github/workflows/release.yml):

- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-agent`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-indexer`
- `ghcr.io/bedtimenewsstudio/bedtimenews-agent-frontend`

Para desplegar una versión publicada, fija una versión con `IMAGE_TAG` en `.env` (por defecto `latest`) y descarga:

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

Ten en cuenta que una imagen construida localmente y una versión publicada comparten la misma etiqueta, por lo que la última creada gana. `docker compose pull` sobrescribe una construcción local, y `--build` sobrescribe una versión publicada.

Para lanzar una versión, empuja una etiqueta `v*` (las etiquetas de imagen omiten el `v` inicial):

```bash
git tag v0.1.0 && git push origin v0.1.0
```

> Las notas de versión deben señalar cambios operativos: nuevas variables de entorno renombradas, cambios de esquema (ej. `EMBEDDING_DIM` — consulta el manual en [indexer/README.es-ES.md](indexer/README.es-ES.md)), y si se requiere reindexación. `storage/postgres/init.sh` solo se ejecuta en un volumen de datos nuevo, por lo que los cambios de esquema nunca se aplican automáticamente a despliegues existentes.

### Actualización del esquema de hash del cuerpo

El indexador ahora invalida vectores con el SHA-256 del texto normalizado exacto de `## 正文` y conserva otra huella de la fuente completa. Los volúmenes existentes deben aplicar `storage/postgres/migrations/001_body_hashes.sql` antes del indexador nuevo; consulta [indexer/README.es-ES.md](indexer/README.es-ES.md). `docker-compose.sample.yml` ofrece el subconjunto local determinista y requiere rutas aisladas en `POSTGRES_DATA_DIR` / `INDEXER_DATA_DIR`.

## Documentación Específica de Servicios

- **[Frontend](frontend/README.es-ES.md)**: Personalización de UI
- **[Agente](agent/README.es-ES.md)**: Puntos finales API, implementación RAG agente
- **[Indexador](indexer/README.es-ES.md)**: Procesamiento de documentos

## Persistencia de Datos

Los datos se persisten entre reinicios:

- **Datos de PostgreSQL** (chunks + embeddings): montados en enlace a `./storage/postgres/volume`
- **Logs de servicios**: volúmenes nombrados de Docker `bedtimenews_indexer_logs` y `bedtimenews_agent_logs`

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
├── docs/diagrams/      # Diagramas SVG de arquitectura y flujo de trabajo
├── storage/            # Scripts de inicialización de base de datos
│   └── postgres/
├── docker-compose.yml  # Orquestación de servicios
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
