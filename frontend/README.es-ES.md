# Servicio Frontend

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Interfaz de chat personalizada para el sistema RAG Agente de BedtimeNews. Una
aplicación de página única estática (HTML/CSS/JS) servida por una pequeña
aplicación FastAPI que también hace proxy del flujo de chat al backend agente
interno.

Consulta el [README principal](../README.es-ES.md) para la configuración
completa de la pila.

## Diseño

- **Tema:** paleta de escritorio de lectura en tinta cálida / papel — neutros
  casi negros cálidos, un acento primario bronce champán, y un acento verde
  marino suave para señales en progreso. Independiente de los colores del logo
  del programa. Claro y oscuro siguen el `prefers-color-scheme` del SO por
  defecto; el interruptor del encabezado escribe una anulación de sesión en
  `sessionStorage` (sobrevive al recargar, se reinicia en una pestaña nueva).
- **Tokens de color** son semánticos y tematizables (`--bg`, `--surface`,
  `--line`, `--text`, `--text-dim`, `--muted`, `--accent`, `--accent-2`),
  definidos para el tema oscuro en `:root` y sobrescritos bajo
  `[data-theme="light"]`.
- **Tipografía:** una sola pila sans CJK del sistema (PingFang SC /
  Microsoft YaHei / Noto Sans SC) para marca, canales y lectura; la jerarquía
  viene del peso y el tamaño. La monoespaciada se reserva para pocas etiquetas
  legibles por máquina (p. ej. espacios de nombres URI). Fuentes solo del
  sistema por diseño — sin CDN de webfonts, así la página carga de forma fiable
  desde China continental.
- **Registro de adquisición de señales:** las etapas aplicables del pipeline
  RAG (condense → route → rewrite → retrieve → grade → generate) se renderizan
  como un registro en vivo que se bloquea cuando comienza la respuesta y luego
  se colapsa. Condense aparece solo cuando el historial de conversación
  resuelve una pregunta de seguimiento.

## Características

- Chat anónimo (sin autenticación)
- Tema claro/oscuro consciente del sistema con un interruptor manual
  persistente
- Preguntas de muestra agrupadas por categoría (la pregunta completa es el
  texto clicable)
- Streaming SSE en tiempo real con pasos del pipeline visibles
- Respuestas en Markdown renderizadas con
  [markdown-it](https://github.com/markdown-it/markdown-it) (incluido
  localmente; `html:false` por seguridad XSS) más chips de cita específicos
  de la aplicación
- Conversación efímera, dentro de la página (se borra al refrescar)
- Adaptable a móviles; accesible por teclado; respeta `prefers-reduced-motion`

## Arquitectura

![Arquitectura de peticiones del frontend](../docs/diagrams/frontend-architecture.svg)

El frontend:

- Se ejecuta en un contenedor Docker que sirve HTTP puro en el puerto 8080
  (sin TLS — la exposición pública y la terminación TLS se gestionan fuera de
  este repositorio)
- Es el único servicio publicado al host (`FRONTEND_PORT`, por defecto 8080)
- Hace proxy de `/chat` al agente a través de la red interna de Docker; el
  agente nunca se expone al host

## Componentes

- **server.py** — aplicación FastAPI: sirve `static/`, expone
  `/api/starters`, y hace proxy del SSE de `/chat` al agente
- **starters.py** — datos de preguntas de muestra (categorías + preguntas);
  datos planos, sin dependencia de framework de UI
- **static/index.html** — marcado de la página, script de arranque de tema, y
  plantillas de turnos
- **static/styles.css** — sistema de diseño tematizable (`:root` +
  `[data-theme="light"]`)
- **static/app.js** — lista de preguntas de muestra, compositor, interruptor
  de tema, análisis SSE, renderizado Markdown
- **static/markdown-it.min.js** — renderizador Markdown incluido localmente
  (MIT), cargado bajo demanda en lugar de con la página
- **static/bedtimenews.webp** — favicon / logo de marca
- **pyproject.toml** — metadatos de dependencias (`fastapi`, `uvicorn`,
  `httpx`)

## Endpoints

| Método | Ruta           | Propósito                                        |
| ------ | -------------- | ------------------------------------------------ |
| GET    | `/`            | Sirve la SPA (`static/index.html`)               |
| GET    | `/api/starters`| JSON de preguntas de muestra (`categories`)      |
| POST   | `/chat`        | Hace proxy del flujo SSE del agente al navegador |
| GET    | `/healthz`     | Comprobación de vitalidad                        |

## Flujo de Desarrollo

El contenedor ejecuta `uvicorn server:app`. Tras cambiar archivos Python o
estáticos, reconstruye y reinicia:

```bash
# El frontend se publica en el host (FRONTEND_PORT, por defecto 8080)
docker compose build web
docker compose up -d web
open http://localhost:8080
```

> Usa `--no-cache` si una reconstrucción parece servir código obsoleto.

### Ejecutar sin Docker

```bash
cd frontend
pip install .
# Apunta a un backend agente alcanzable:
AGENT_BACKEND_HOST=localhost AGENT_BACKEND_PORT=8000 \
  uvicorn server:app --reload --port 8080
```

### Personalización

- **Preguntas de inicio / categorías:** edita `starters.py` (`CATEGORIES`).
- **Estilos:** edita `static/styles.css` (los tokens de diseño viven en
  `:root`).
- **Textos / disposición:** edita `static/index.html`.
- **Logo / favicon:** reemplaza `static/bedtimenews.webp`. Se renderiza a
  2.1rem, así que mantenlo pequeño — 128px cuadrados bastan para hi-DPI, y el
  archivo queda cacheado una semana por `CachedStaticFiles`.

## Configuración

| Variable             | Por defecto | Propósito                                       |
| -------------------- | ----------- | ----------------------------------------------- |
| `AGENT_BACKEND_HOST` | `agent`     | Nombre del servicio agente en la red Docker     |
| `AGENT_BACKEND_PORT` | `8000`      | Puerto del agente                               |
| `FRONTEND_PORT`      | `8080`      | Puerto del host donde se publica el frontend    |

## Depuración

```bash
# Logs
docker compose logs -f web

# Conectividad del backend desde dentro del contenedor (la imagen slim no
# tiene ping/curl; usa el Python + httpx incluidos en su lugar)
docker compose exec web python -c "import httpx; print(httpx.post(
    'http://agent:8000/chat', json={'question': '测试'}, timeout=120).text)"
```

## Contrato de API

El frontend hace proxy del endpoint `/chat` del agente.

### Solicitud

```json
{
  "question": "string (required)",
  "history": [{"question": "…", "answer": "…", "grounded": true}],
  "stream": true
}
```

`history` es opcional; el navegador envía como máximo sus tres turnos más
recientes.

### Respuesta en streaming (SSE)

```json
{"type": "step", "step": "condense|route|rewrite|retrieve|grade|generate", "content": "…"}
{"type": "citations", "urls": {"ShuiQianXiaoXi/0501-0600/0588.md": {"title": "睡前消息588", "url": "/transcripts/ShuiQianXiaoXi/0501-0600/0588.md"}}}
{"type": "answer_chunk", "content": "…"}
{"type": "answer_final", "content": "…", "grounded": true}
{"type": "answer_meta", "grounded": true}
{"type": "followups", "items": ["…"]}
{"type": "error", "content": "…"}
```

El servidor puede emitir comentarios SSE `: ping` entre eventos y termina cada
flujo con `data: [DONE]`. Un turno exitoso envía exactamente uno de
`answer_final` o `answer_meta`.

## Limitaciones (MVP)

- **Sin autenticación** — solo anónimo
- **Sin persistencia** — la conversación se borra al refrescar
- **Sesión por pestaña** — sin historial entre pestañas ni del lado del
  servidor

## Solución de Problemas

**Puerto 8080 en uso:** establece `FRONTEND_PORT` en `.env` a otro puerto del
host y recrea el servicio (`docker compose up -d web`).

**No se puede conectar al backend:**

- `docker compose ps agent` y `docker compose logs agent`
- Comprobación de conectividad desde dentro del contenedor (consulta
  [Depuración](#depuración))

**Los cambios no aparecen:** reconstruye (`--no-cache`) y recarga forzadamente
el navegador (Cmd/Ctrl+Shift+R).
