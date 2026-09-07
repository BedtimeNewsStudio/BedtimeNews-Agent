# BedtimeNews Agent

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Sistema agente RAG (Retrieval-Augmented Generation) para la base de conocimiento de 睡前消息 (BedtimeNews). Proporciona Q&A con enrutamiento automático, búsqueda semántica, contexto de transcripciones recuperadas y citas de episodios.

> **¡Pruébalo:** [chat.bedtime.blog](https://chat.bedtime.blog)

## Descripción General

Este sistema indexa transcripciones de videos del [archivo de BedtimeNews](https://archive.bedtime.news/) y permite búsqueda semántica con Q&A impulsado por LLM. Construido con LangGraph, proveedores de LLM/embedding conectables (DeepSeek para chat y los embeddings Qwen3 de SiliconFlow por defecto), y PostgreSQL + pgvector.

**Características Principales:**

- Enrutamiento automático de consultas (recuperación de archivo vs manejo directo restringido)
- Optimización de consultas y búsqueda semántica
- Calificación basada en LLM de documentos
- Transcripciones recuperadas proporcionadas como contexto de respuesta, con citas en formato markdown y reparación de citas
- Indexación automatizada de documentos con actualizaciones incrementales
- Interfaz de chat basada en web

## Cobertura de Contenido

El sistema indexa transcripciones de videos de [bedtimenews-archive-contents](https://github.com/bedtimenews/bedtimenews-archive-contents) cubriendo diversos temas a través de múltiples programas:

**Catálogo de Programas:**

| Catálogo      | Nombre     | Descripción                                                   |
| ------------- | ---------- | ------------------------------------------------------------- |
| `main/`       | 睡前消息   | Cobertura integral a través de todos los temas                |
| `reference/`  | 参考信息   | Agregación diaria de noticias                                 |
| `business/`   | 产经破壁机 | Economía, industria, negocios, tecnología                     |
| `commercial/` | 讲点黑话   | Relaciones internacionales, geopolítica                       |
| `opinion/`    | 高见       | Análisis técnico, infraestructura, ingeniería                 |
| `daily/`      | 每日新闻   | Actualizaciones diarias de noticias                           |
| `others/`     | 其它文稿   | Sesiones de preguntas en vivo y otros contenidos relacionados |

**Categorías de Temas:**

1. **Economía e Industria Doméstica** - Política económica, desarrollo industrial, bienes raíces, deuda de gobiernos locales, desarrollo urbano
2. **Tecnología e Innovación** - IA, chips, semiconductores, vehículos autónomos, aeroespacial, ingeniería
3. **Comercio Electrónico Transfronterizo y Expansión Global** - SHEIN, TikTok, ventajas de la manufactura china, mercados globales
4. **Gobernanza Corporativa y Regulación** - Escándalos corporativos, auditoría, supervisión financiera, seguridad alimentaria, regulación fiscal
5. **Relaciones Internacionales y Geopolítica** - Relaciones EE.UU.-China, conflicto Rusia-Ucrania, Oriente Medio, Península Coreana, Indo-Pacífico
6. **Problemas Sociales y Vida Civil** - Educación, salud, demografía, bienestar social, gobernanza urbana
7. **Criptomonedas y Finanzas Tecnológicas** - Bitcoin, blockchain, finanzas descentralizadas, activos digitales
8. **Población y Políticas Sociales** - Crisis poblacional, cuidado infantil socializado, sistema educativo, reforma de bienestar social
9. **Infraestructura e Ingeniería** - Construcción ferroviaria, infraestructura energética, desarrollo urbano, servicios públicos
10. **Derecho y Asuntos Judiciales** - Disputas corporativas, justicia penal, protección del consumidor, marcos regulatorios

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
- Claves API para tus proveedores elegidos (por defecto: `DEEPSEEK_API_KEY` para chat y `SILICONFLOW_API_KEY` para embeddings)

### Configuración

1. **Clonar el repositorio**

   ```bash
   git clone https://github.com/BedtimeNewsStudio/BedtimeNews-Agent.git
   cd BedtimeNews-Agent
   ```

2. **Configurar el entorno**

   Copia [`.env.example`](.env.example) a `.env` y configura:

   ```bash
   cp .env.example .env
   # Editar .env 
   ```

   > **Las claves API se leen del entorno del shell, no desde `.env`.** `.env` contiene configuración no secreta (selección de proveedor/modelo, puertos, configuración de BD); exporta tus secretos en el shell en su lugar, e.g.:
   >
   > ```bash
   > export DEEPSEEK_API_KEY=...      # proveedor de chat
   > export SILICONFLOW_API_KEY=...   # proveedor de embeddings
   > ```

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
IMAGE_TAG=0.1.0   # en .env, o dejar como latest
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
│   ├── server.py       # FastAPI: sirve UI estática + proxy /chat SSE
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
├── .env                # Configuración del entorno (no en git)
├── .env.example        # Plantilla de configuración del entorno
├── THIRD_PARTY_NOTICES.md  # Licencias de componentes de terceros
├── README.md           # README predeterminado (中文)
├── README.en.md        # README en inglés
└── README.es-ES.md     # README en español (este archivo)
```

## Licencia

Licencia MIT — consulta el archivo [LICENSE](LICENSE).

Este proyecto incluye componentes de terceros bajo sus propias licencias — consulta [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) para más detalles.
