# Servicio Indexador

[中文](README.md) | [English](README.en.md) | [Español](README.es-ES.md)

Pipeline automatizado de incrustación de documentos para el archivo de BedtimeNews. Clona el repositorio de transcripciones, divide e incrusta las transcripciones y publica el resultado en PostgreSQL + pgvector como **instantáneas (snapshots) inmutables y versionadas** que lee el agente. El indexador es el único que escribe en la base de datos.

Consulta el [README principal](../README.es-ES.md) para las instrucciones de configuración y el [documento de diseño](../docs/designs/20261007_rag-snapshot-architecture.md) para la justificación completa.

## Características

- **Sincronización automática**: clona/actualiza desde [BedtimeNews-Transcripts](https://github.com/BedtimeNewsStudio/BedtimeNews-Transcripts); cada construcción queda fijada al commit tomado al inicio
- **Publicación por snapshots**: cada construcción produce un esquema completo y coherente `rag_s<id>` que nunca se modifica después. La publicación es atómica (una transacción), reversible (`retire`) y queda registrada; el agente cambia al nuevo snapshot en 15 segundos, sin reiniciar
- **Construcciones incrementales**: las transcripciones se comparan con el `index_state` del último snapshot, con huellas SHA-256 separadas para el Markdown completo y para el texto normalizado exacto de `## 正文` que se envía a los embeddings. Las transcripciones sin cambios se copian del snapshot base; las ediciones que solo afectan al título, la fecha o el apéndice no hacen ninguna llamada de embedding
- **Reutilización de vectores por texto del fragmento**: los vectores se reutilizan por `sha256(texto del fragmento)` desde el último snapshot del mismo espacio vectorial, de modo que las construcciones completas (tras un cambio de fragmentación o normalización, una actualización de formato o la adopción) solo incrustan el texto que realmente cambió
- **Autocomprobación antes de publicar**: completitud, una consulta HNSW de muestra y una reincrustación de muestra (coseno ≥ 0,99) protegen frente a construcciones defectuosas
- **Ejecución programada**: planificador en proceso con expresión cron configurable (por defecto: cada hora); las ejecuciones que se exceden de su franja no se recuperan, y un bloqueo de ejecución impide ejecuciones simultáneas
- **Indexación solo del cuerpo**: solo se extrae el tramo entre `## 正文` y `## 附录`; se descartan la línea del título, la línea de metadatos `**发布日期**` y las notas de corrección del apéndice
- **URI como doc_id**: la ruta de la transcripción relativa a `contents/` (con `.md`) es su identificador
- **Títulos estandarizados**: se analiza el `URI映射.md` del repositorio de origen y la correspondencia URI → título se escribe en la tabla `documents` del snapshot
- **Fragmentación inteligente**: fragmentación semántica consciente de Markdown, una sección por subtítulo dentro de `## 正文` (el texto anterior al primer subtítulo es su propia sección); el solapamiento solo se mantiene dentro de una sección, nunca atraviesa un título, y los fragmentos de menos de 50 palabras se descartan
- **Agente de solo lectura**: el indexador mantiene el rol `rag_agent`, que solo puede leer snapshots publicados
- **Monitoreo**: estado de ejecución en `rag_meta.indexer_status` (expuesto por `/health` del agente), comandos de operación y un depurador

## Fases del Pipeline

![Pipeline del indexador](../docs/diagrams/indexer-pipeline.svg)

Una ejecución:

1. **Bloqueo de ejecución**: un `flock` no bloqueante sobre `/data/.indexer.lock` (el montaje de `INDEXER_DATA_DIR`). Si otra ejecución lo tiene (la programada, un `build` manual o un segundo contenedor), esta registra `skipped_busy` y termina sin tocar el repositorio git ni la base de datos.
2. **Arranque** (idempotente): crea `rag_meta`, `rag_state` y el rol `rag_agent`; adopta un esquema `rag` anterior a los snapshots como snapshot `legacy` (ver [Actualización](#actualización-desde-el-esquema-anterior-a-los-snapshots)).
3. **Sincronización git** y fijación del commit.
4. **Plan**: la base incremental es el último snapshot publicado del linaje actual (misma huella de pipeline y mismo espacio vectorial). Se comparan las transcripciones con su `index_state`: añadidas, con cuerpo modificado, solo con cambios de origen (título/fecha/apéndice) y eliminadas, además de las proyecciones de lectura y los títulos a refrescar. Sin cambios → `no_change`. Sin base (primera construcción, un cambio de versión, un nuevo espacio vectorial) o `build --full` → construcción completa.
5. **GC y después comprobación de disco**: no se construye salvo que el sistema de archivos de datos de Postgres tenga libre al menos max(2 GB, 3 × el tamaño del snapshot base).
6. **Fase A** (sin transacción): fragmenta las transcripciones a (re)indexar; reutiliza vectores por hash del texto del fragmento y llama a la API de embeddings solo para los fallos; renderiza las proyecciones de lectura. Los vectores nuevos solo viven en memoria.
7. **Fase B** (una transacción con el bloqueo consultivo de la base de datos): crea `rag_s<id>` → copia las cuatro tablas del base (incremental) → borra las transcripciones modificadas y eliminadas → inserta los fragmentos (los vectores reutilizados se unen en el servidor) → escribe `index_state`, títulos y proyecciones de lectura → `ANALYZE` → construye el índice HNSW → autocomprobación → vuelve a comprobar que el base sigue publicado y sigue siendo el más reciente de su linaje → lo registra como `published` → concede lectura a `rag_agent` → `COMMIT`.
8. **GC de nuevo** y actualización de `rag_meta.indexer_status`.

Cualquier fallo, un proceso terminado o una conexión caída revierte toda la transacción de la fase B: nunca queda un esquema a medio construir ni una fila de registro, y los snapshots publicados no se ven afectados. Con `SIGTERM` el indexador cancela la sentencia en curso y termina (compose concede 30 s).

Medido con el corpus completo (1.902 transcripciones, 13.434 fragmentos, 2560 dimensiones; máquina local de 4 núcleos): una construcción incremental con una transcripción modificada tarda ~19 s (fase B ~14 s, sobre todo el índice HNSW); una construcción completa reutilizando todos los vectores ~36 s; un snapshot ocupa ~240 MB y una construcción escribe ~200 MB de WAL.

## Configuración

### Programación Cron

En `config.yml`:

```yaml
indexer_cron_schedule: "0 * * * *"      # Cada hora (por defecto)
# indexer_cron_schedule: "*/30 * * * *" # Cada 30 minutos
# indexer_cron_schedule: "0 2 * * *"    # Diariamente a las 2 AM
```

La siguiente ejecución siempre se calcula desde el momento actual: una ejecución más larga que el intervalo omite las franjas perdidas en lugar de lanzarlas una tras otra. Una ejecución perdida nunca pierde cambios, porque cada construcción se compara con el snapshot publicado.

### Modo de muestra local

`index_config.sample.yml` selecciona ocho transcripciones deterministas. Solo se admite con `INDEXER_SCOPE=sample`, `INDEX_CONFIG_FILE=/app/index_config.sample.yml`, almacenamiento aislado y un `POSTGRES_DB` terminado en `_local`; el indexador rechaza cualquier otro destino. `docker-compose.sample.yml` aporta las sobreescrituras de servicios.

### Filtros de Documentos

Edita `index_config.yml`:

```yaml
# Patrones de inclusión (se procesan primero)
# Se comparan con el URI de la transcripción: su ruta relativa a contents/,
# incluido el sufijo .md.
include:
  # 睡前消息
  - "ShuiQianXiaoXi/*/*.md"

  # 参考信息
  - "CanKaoXinXi/*/*.md"

  # 高见
  - "GaoJian/*/*.md"

  # 讲点黑话
  - "JiangDianHeiHua/*/*.md"

  # 产经破壁机 (nombrado por fecha desde 2026-09; archivos en la raíz de la sección)
  - "ChanJingPoBiJi/*.md"
  # 产经破壁机 (números antiguos y especiales misc/, rutas de dos niveles)
  - "ChanJingPoBiJi/*/*.md"

# Patrones de exclusión (se procesan después de la inclusión)
exclude:
  # Páginas de navegación de cada canal, no son transcripciones
  - "*/INDEX.md"

# Reglas de validación de archivos
validation:
  # Tamaño mínimo en bytes (omitir archivos vacíos o diminutos)
  min_file_size: 100

  # Tamaño máximo en bytes (omitir archivos extremadamente grandes)
  max_file_size: 10485760 # 10 MB
```

## Operación de Snapshots

Dentro del contenedor del indexador:

| Comando                                           | Efecto                                                                                                   |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `python -m src.snapshots list`                    | Lista los snapshots: estado, fijado, linaje (actual/otro), fecha de publicación, tamaño, espacio vectorial |
| `python -m src.snapshots status`                  | Muestra `rag_meta.indexer_status` (última ejecución, resultado, error, fallos consecutivos)               |
| `python -m src.snapshots retire <id>`             | Retira un snapshot (reversión de datos); los agentes vuelven al snapshot legible anterior en 15 s        |
| `python -m src.snapshots unretire <id>`           | Deshace una retirada (los snapshots retirados se conservan 24 horas)                                     |
| `python -m src.snapshots pin <id>` / `unpin <id>` | Fija o desfija; los snapshots fijados nunca se recolectan                                                |
| `python -m src.snapshots build [--full]`          | Construye ahora, protegido por el mismo bloqueo de ejecución y bloqueo consultivo que la ejecución programada |

```bash
docker compose exec indexer python -m src.snapshots list
docker compose exec indexer python -m src.snapshots retire s20261007t091512z_a1b2c3d
```

## Utilidades de Depuración

`stats`, `history` e `inspect` leen el snapshot actual (el último publicado del linaje actual del indexador); `history` lee su `index_state`. `recent` lee el registro de auditoría `rag_state.file_actions`.

### Probar Conexión

```bash
docker compose exec indexer python -m src.debugger test
```

### Ver Estadísticas

```bash
# Estadísticas del snapshot actual
docker compose exec indexer python -m src.debugger stats

# Acciones de archivo recientes
docker compose exec indexer python -m src.debugger recent --limit 20

# Historial de indexación de todos los archivos
docker compose exec indexer python -m src.debugger history

# Historial de un archivo concreto
docker compose exec indexer python -m src.debugger history ShuiQianXiaoXi/0901-1000/0960.md
```

### Inspeccionar Documentos

```bash
# Ver los fragmentos de un documento
docker compose exec indexer python -m src.debugger inspect ShuiQianXiaoXi/0901-1000/0960.md
```

### Ver Logs

```bash
# Logs recientes de ejecuciones programadas
docker compose exec indexer python -m src.debugger logs

# Últimas 100 líneas
docker compose exec indexer python -m src.debugger logs --lines 100

# Todos los logs
docker compose exec indexer python -m src.debugger logs --all
```

### Ejecución Manual

```bash
# Construir una vez ahora (incremental)
docker compose exec indexer python -m src.snapshots build

# Reconstruir todo (los vectores se siguen reutilizando por texto del fragmento)
docker compose exec indexer python -m src.snapshots build --full
```

Ya no existe un comando para "borrar datos": reconstruye con `build --full` y revierte datos erróneos con `retire`.

## Esquema de Base de Datos

El indexador es el único que escribe. Crea todo lo siguiente al arrancar y durante las construcciones; `storage/postgres/init.sh` solo habilita la extensión `vector`.

```plaintext
rag_meta    registro de snapshots y estado del indexador (solo lo escribe el indexador)
rag_state   privado del indexador: registro de auditoría
rag_s<id>   snapshots de solo lectura: document_chunks / documents / transcripts / index_state
```

### `rag_s<id>`: un snapshot

`<id>` es `s` + la hora UTC de construcción + los 7 primeros caracteres del commit de las transcripciones, p. ej. `s20261007t091512z_a1b2c3d`. Una vez publicado, un snapshot nunca se modifica. El DDL de las tablas está en `src/snapshot_schema.py`, por `format_version`.

**`document_chunks`**: fragmentos con embeddings

- `chunk_id`: identificador único, `{slug}_chunk_{índice:03d}`, donde el slug es el URI sin `.md` y con `/` sustituido por `_` (p. ej. `ShuiQianXiaoXi_0501-0600_0588_chunk_000`)
- `doc_id`: el URI de la transcripción, **incluido `.md`**, p. ej. `ShuiQianXiaoXi/0501-0600/0588.md`: byte a byte la clave del `URI映射.md` de origen
- `chunk_index`: índice basado en 0 dentro del documento
- `heading`: título de la sección (si lo hay)
- `text`: contenido del fragmento
- `word_count`: número de palabras
- `embedding`: `halfvec(N)`, siendo `N` la dimensión del espacio vectorial del snapshot. El **tipo** de columna es fijo a propósito: `halfvec` admite cualquier modelo de hasta 4000 dimensiones con la mitad de almacenamiento y una pérdida de recall despreciable. Un índice HNSW (`halfvec_cosine_ops`, valores por defecto de pgvector `m=16`, `ef_construction=64`) atiende la búsqueda de vecinos del agente
- `created_at`: marca temporal
- `id`: clave `SERIAL` regenerada en cada snapshot; nada puede depender de ella

**`documents`**: URI → 标准化标题 (título estandarizado)

- `doc_id`: URI de la transcripción (clave primaria, incluye `.md`)
- `title`: título estandarizado, p. ej. `睡前消息588`
- `updated_at`: marca temporal

Los títulos provienen del `URI映射.md` de origen. Una regla general (`{canal}/{tramo}/{número}.md` → `{nombre chino del canal}{número sin ceros}`) cubre la gran mayoría, pero 29 transcripciones —los especiales `misc/`, números de episodio duplicados oficialmente, los números negativos de 产经破壁机— no pueden derivarse con ninguna regla, así que ese archivo es la referencia. El agente hace LEFT JOIN con esta tabla al recuperar para mostrar las citas con el título y no con el URI. Cada construcción refresca todos los títulos, porque el origen puede corregir un título sin tocar la transcripción.

**`transcripts`**: transcripciones renderizadas para el lector de la aplicación

- `doc_id`: URI de la transcripción (clave primaria)
- `canonical_title` / `source_title`: título estandarizado y el título `# ` propio de la transcripción
- `channel`, `publication_date`: nombre del canal y el valor de `**发布日期**`
- `body_html`: la página renderizada que sirve la API `/transcripts` del agente
- `source_hash`, `projection_version`: disparadores de re-renderizado (ediciones del origen o un cambio del renderizador)

**`index_state`**: procedencia de la construcción, base de la siguiente construcción incremental (el agente no la lee)

- `file_path`: URI de la transcripción
- `source_hash`: SHA-256 del Markdown original completo
- `body_hash`: SHA-256 del cuerpo normalizado exacto que representan los fragmentos y vectores
- `body_normalization_version`: la versión del normalizador que lo produjo
- `indexed_at`, `source_observed_at`: última indexación del cuerpo y último cambio de origen aceptado

### `rag_meta`

**`snapshots`**: el registro. Una fila por snapshot confirmado (`status` `published` o `retired`; las construcciones fallidas nunca aparecen): `snapshot_id`, `schema_name`, `format_version`, `pipeline_fingerprint`, `embedding_space`, `embedding_model`, `embedding_dim`, `normalization_version`, `chunker_version`, `source_commit`, `base_snapshot_id` (construcciones incrementales), `builder_version`, `pinned`, `published_at`, `retired_at`, `index_params` (parámetros HNSW) y `stats` (recuentos, embeddings nuevos frente a reutilizados, tiempos de cada paso).

**`indexer_status`**: exactamente una fila: `last_run_at`, `last_result` (`published` / `no_change` / `skipped_busy` / `failed`), `last_error`, `last_published_at`, `consecutive_failures`.

### `rag_state`

**`file_actions`**: registro de auditoría solo de inserción, una fila por cambio de transcripción publicado: `action_type` (`ADD`, `MODIFY`, `SOURCE_ONLY`, `DELETE`), `source_hash` / `body_hash` (`NULL` para `DELETE`), `run_timestamp` / `processed_at`. No forma parte del servicio y no necesita copia de seguridad.

### Dos dimensiones de compatibilidad

| Dimensión                              | Composición                                                                                                                   | Función                                                                                       |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| **Huella del pipeline**                | Hash de (`format_version`, `normalization_version` = `BODY_NORMALIZATION_VERSION`, `chunker_version` = `CHUNKER_VERSION`)     | Solo hay construcción incremental entre snapshots con la misma huella; si no, completa         |
| **Espacio vectorial** `embedding_space`| `<modelo>@<dimensión>`, p. ej. `Qwen/Qwen3-Embedding-4B@2560`; sustituible con `embedding.space_id`                            | Los vectores se reutilizan, y un agente lee snapshots, solo dentro de un mismo espacio        |

- Incrementa `CHUNKER_VERSION` (`src/chunker.py`) siempre que cambien los parámetros o la lógica de fragmentación, y `BODY_NORMALIZATION_VERSION` (`src/document_loader.py`) siempre que cambie la normalización. Olvidarlo mezcla fragmentación antigua y nueva en un mismo snapshot; la revisión de código debe comprobarlo.
- Incrementa `FORMAT_VERSION` (`src/snapshot_schema.py`) cuando cambie la estructura o el significado de una tabla o columna que lea el agente, y añade el nuevo formato a `SUPPORTED_FORMATS` del agente.
- Un "linaje" es el conjunto de snapshots que comparten huella y espacio vectorial.

### El rol `rag_agent`

El agente se conecta como `rag_agent`, que solo tiene `USAGE` sobre `rag_meta` con `SELECT` en sus dos tablas, más `USAGE` + `SELECT` sobre los snapshots publicados, concedidos dentro de la transacción de publicación. No tiene permisos de escritura en ningún sitio. El indexador garantiza el rol en cada arranque: con `POSTGRES_AGENT_PASSWORD` definido recibe `LOGIN` y esa contraseña; sin ella el rol es `NOLOGIN` y el agente recurre al superusuario (con un aviso). El indexador sigue usando el superusuario.

### Recolección de basura

La GC se ejecuta antes y después de cada construcción; cada borrado va en su propia transacción con el bloqueo consultivo (`lock_timeout` 5 s; un snapshot que aún se está leyendo se reintenta en la siguiente ejecución):

| Regla                                                                                                    | Justificación                                                  |
| -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| El linaje actual conserva sus 2 últimos snapshots publicados                                             | El actual más un destino de reversión inmediata                |
| Cualquier otro linaje (incluido `legacy`) conserva solo el último, durante 7 días tras ser sustituido por el linaje actual | La versión de código anterior aún puede volver a usarse |
| Los snapshots `pinned` nunca se borran                                                                   | Fijación explícita a largo plazo                               |
| Los snapshots `retired` se conservan 24 horas                                                            | Una retirada por error puede deshacerse                        |
| Nada se borra en los 10 minutos siguientes a ser sustituido o retirado                                   | Protege las peticiones en curso (el agente cambia en 15 s)     |

## Actualización desde el esquema anterior a los snapshots

Los despliegues existentes se adoptan automáticamente en el primer arranque del indexador con snapshots, sin pasos manuales ni orden de actualización obligatorio:

1. El esquema `rag` existente se registra como snapshot `legacy` (formato 1, espacio vectorial a partir de la dimensión real de la columna y del modelo configurado); `rag.file_actions` pasa a `rag_state`, `rag.indexing_history` se renombra a `rag.index_state` y `rag_agent` recibe lectura sobre `rag`.
2. La primera construcción es completa, en un nuevo linaje, reutilizando todos los vectores de `legacy` (normalmente sin llamadas de embedding, unos minutos).
3. `legacy` se conserva 7 días más, así que el agente antiguo sigue funcionando y los datos pueden revertirse con `retire`.

Tras la adopción **no se admite** volver a una versión del **indexador** anterior a los snapshots (escribiría en las tablas renombradas y modificaría `legacy` en el sitio). El agente puede volver atrás libremente hasta que `legacy` se recolecte. Las bases de datos anteriores a v0.3 deben aplicar primero `storage/postgres/migrations/` (histórico).

## Cambiar el Modelo de Embedding

Los vectores de modelos distintos no son comparables, aunque tengan la misma dimensión, y cada modelo emite una dimensión fija (p. ej. `Qwen/Qwen3-Embedding-4B` = 2560, `text-embedding-3-small` = 1536, `text-embedding-3-large` = 3072). Un cambio de modelo es, por tanto, un nuevo espacio vectorial, construido como un nuevo snapshot mientras el anterior sigue sirviendo: sin detener servicios, sin `ALTER TABLE` y sin estado que borrar.

### Manual de Procedimiento

```bash
# 1. Edita config.yml: el grupo embedding (model / base_url / api_key).
#    Pon EMBEDDING_DIM en .env a la dimensión de salida del nuevo modelo.

# 2. Reinicia solo el indexador. La siguiente construcción ve un espacio
#    vectorial nuevo, no encuentra base incremental y hace una construcción
#    completa incrustando todo el corpus (unos 25 minutos). El snapshot
#    anterior sigue sirviendo al agente actual.
docker compose up -d indexer
docker compose exec indexer python -m src.snapshots build   # o espera a la programación

# 3. Comprueba que el nuevo snapshot está publicado en el nuevo espacio.
docker compose exec indexer python -m src.snapshots list

# 4. Reinicia el agente con la misma configuración: selecciona el snapshot de
#    su (nuevo) espacio vectorial. Con una sola instancia hay una breve interrupción.
docker compose up -d agent
docker compose exec agent curl -s localhost:8000/health
```

El snapshot anterior sigue siendo legible durante 7 días, así que revertir es restaurar la configuración anterior y reiniciar el agente. Para mantener el mismo modelo pero dejar de mezclar vectores de otro proveedor, define `embedding.space_id` (igual en indexador y agente).

**Con despliegues blue-green** (producción; ver el [diseño blue-green](../docs/designs/20261008_blue-green-deployment.md), sección 5), el paso 4 no interrumpe el servicio: en el paso 2 recrea **solo** el indexador (`docker compose -p bedtimenews-agent -f compose.data.yml up -d indexer`); la instancia de aplicación en marcha (blue) sigue sirviendo el espacio antiguo, porque su agente cargó la configuración al arrancar. Cuando se publique el nuevo snapshot, despliega una nueva instancia (green), que arranca con la nueva configuración y selecciona el nuevo snapshot, y cambia el proxy a ella. Los datos de blue quedan congelados desde el paso 2 hasta el cambio (el linaje antiguo no recibe construcciones nuevas), unos treinta minutos.

### Producción: la capa de datos

En producción la pila tiene dos capas: `compose.data.yml` (postgres + indexador, proyecto `bedtimenews-agent`, actualizado en el sitio) y un proyecto `compose.app.yml` por instancia de aplicación. Allí los comandos sobre el indexador o postgres necesitan `-p bedtimenews-agent -f compose.data.yml`, o confiar en la protección `COMPOSE_FILE=compose.data.yml` del `.env` de la VM, que hace que un `docker compose ...` sin `-f` actúe solo sobre la capa de datos. Nunca ejecutes `down -v` sobre `bedtimenews-agent`, ni `docker compose down` allí en nombre de una instancia de aplicación: las instancias se retiran con `docker compose -p <instancia> -f compose.app.yml down -v`. Quien autoaloja con el `docker-compose.yml` paraguas ejecuta todos los comandos tal como están escritos.

## Copia de Seguridad y Restauración de Datos

### Los snapshots no son copias de seguridad

Todos los snapshots están en la misma instancia de Postgres y el mismo disco; un fallo de disco o el borrado accidental del directorio de datos los pierde todos.

- **El peor caso puede reconstruirse**: todos los datos provienen del repositorio git de transcripciones; una construcción completa sobre una base vacía los restaura (una pasada completa de embeddings, unos 25 minutos).
- **Exportación opcional fuera del host** del último snapshot (unos cientos de MB):

  ```bash
  docker compose exec postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
    -n rag_s<id> -n rag_meta > rag-<id>.dump
  ```

  Para restaurarlo, aplica `pg_restore` y asegúrate de que la fila del snapshot en `rag_meta.snapshots` tenga `status = 'published'`.
- El registro de auditoría `rag_state.file_actions` no necesita copia de seguridad.

### Copia del volumen completo

El volumen de datos de PostgreSQL se guarda en un directorio ignorado por git dentro del repositorio. Puedes respaldar y restaurar toda la base de datos con archivos tar estándar.

**Requisitos**: detén todos los servicios para garantizar la consistencia:

```bash
docker compose down
# Producción (dos capas): detén primero cada instancia de aplicación y después
# la capa de datos, sin -v:
#   docker compose -p <instancia> -f compose.app.yml stop
#   docker compose -p bedtimenews-agent -f compose.data.yml down
```

**(Opcional) Comprobar el tamaño del volumen (sin comprimir)**:

```bash
du -h -d 0 storage/postgres/volume
```

**Crear copia**:

```bash
# Crear un archivo de copia con marca temporal
tar czf /path/to/backup/postgres-volume-$(date +%F).tar.gz storage/postgres/volume/

# Ejemplo: /path/to/backup/postgres-volume-2025-11-27.tar.gz
```

La copia incluye todos los snapshots, el registro, el registro de auditoría y la configuración de la base de datos.

**Restaurar desde la copia**:

```bash
docker compose down

# Eliminar los datos existentes (si los hay)
rm -rf storage/postgres/volume

# Extraer la copia en el directorio de datos de postgres (desde la raíz del proyecto)
tar xzf /path/to/backup/postgres-volume-2025-11-27.tar.gz -C .
# Comprueba que se extrajo como ./storage/postgres/volume/18/docker/...

# Iniciar servicios
docker compose up -d
```

**Verificar la restauración**:

```bash
docker compose exec indexer python -m src.snapshots list
docker compose exec indexer python -m src.debugger stats
```

### Notas

- **Solo local**: el directorio de datos de postgres está en `.gitignore` y no se versiona
- **Migración**: las copias son portables y pueden restaurarse en otras máquinas
- **Espacio en disco**: en estado estable hay dos snapshots (~500 MB); una construcción necesita temporalmente un snapshot más y el WAL

## Estructura del Proyecto

```plaintext
indexer/src/
├── entrypoint.py        # Punto de entrada del contenedor (construcción inmediata + planificador)
├── pipeline.py          # Una ejecución: bloqueo, arranque, plan, construcción, GC, estado
├── builder.py           # Constructor de snapshots: plan, comprobación de disco, fases A y B, autocomprobación
├── catalog.py           # rag_meta/rag_state, rol rag_agent, adopción, registro, GC
├── snapshot_schema.py   # Ids de snapshot, huella, espacio vectorial, DDL por formato
├── snapshots.py         # Comandos de operación (list, status, retire, pin, build, ...)
├── run_lock.py          # Bloqueo de archivo de toda la ejecución
├── db.py                # Conexiones, bloqueo consultivo, cancelación por SIGTERM
├── scheduler.py         # Planificador cron (sin recuperación de franjas perdidas)
├── git_sync.py          # Sincronización del repositorio y commit fijado
├── file_scanner.py      # Escaneo del sistema de archivos
├── document_loader.py   # Procesamiento de Markdown (BODY_NORMALIZATION_VERSION)
├── change_detector.py   # Comparación de hashes de origen/cuerpo con el index_state de un snapshot
├── chunker.py           # Fragmentación semántica (CHUNKER_VERSION)
├── embeddings.py        # Generación de embeddings (cliente compatible con OpenAI)
├── vector_db.py         # Consultas de solo lectura para el depurador
├── debugger.py          # Utilidades de depuración
├── transcript_export.py # Proyección de lectura (Markdown -> HTML)
├── uri_mapping.py       # Analizador de la tabla de títulos URI映射.md
├── models.py            # Modelos de datos
├── paths.py             # Gestión de rutas
└── settings.py          # Configuración
```

## Monitoreo

### Comprobar Estado del Servicio

```bash
# Ver logs
docker compose logs -f indexer

# Resultado de la última ejecución y fallos consecutivos
docker compose exec indexer python -m src.snapshots status

# Snapshots y sus tamaños
docker compose exec indexer python -m src.snapshots list

# Comprobar el proceso planificador sin privilegios
docker compose top indexer
```

`GET /health` del agente también informa del snapshot que sirve, la antigüedad de sus datos y `indexer_status`; alerta, por ejemplo, tras 3 fallos consecutivos o más de 3 horas sin ejecución.

### Salida Esperada

Tras la primera ejecución deberías ver:

- El repositorio clonado en `indexer/data/BedtimeNews-Transcripts/`
- Un snapshot publicado en `python -m src.snapshots list`, con sus fragmentos en `rag_s<id>.document_chunks`
- Acciones de archivo registradas en `rag_state.file_actions`
- `last_result` = `published` en `python -m src.snapshots status`

Cada construcción publicada registra sus estadísticas (también guardadas en `rag_meta.snapshots.stats`): número de transcripciones y fragmentos, embeddings nuevos frente a reutilizados, el origen de la reutilización, los recuentos de cambios y los tiempos de cada paso.

## Solución de Problemas

**No se publica ningún snapshot:**

```bash
# Último resultado y error
docker compose exec indexer python -m src.snapshots status

# Buscar errores en los logs
docker compose logs indexer | grep -iE "error|failed"

# Construir manualmente
docker compose exec indexer python -m src.snapshots build

# Verificar que el clon git se hizo
docker compose exec indexer ls -la /data/BedtimeNews-Transcripts/
```

**`skipped_busy`:** otra ejecución tiene el bloqueo de ejecución (la programada, un `build` manual o un segundo contenedor del indexador que comparte el directorio de datos) o el bloqueo consultivo de la base de datos. Espera a que termine; no borres `/data/.indexer.lock`.

**Falla la comprobación de disco:** el sistema de archivos de datos de Postgres tiene menos de max(2 GB, 3 × el snapshot base) libres. Libera espacio (la GC ya se ejecutó antes de la comprobación). Comprueba que `${POSTGRES_DATA_DIR}` esté montado en solo lectura en `/pgdata` (sin el montaje la comprobación se omite con un aviso).

**Falla la autocomprobación:** no se publica nada y el snapshot anterior sigue sirviendo. El error indica la comprobación:

- *snapshot is empty / transcripts have no chunks / doc_id sets differ*: un problema de origen o de fragmentación; revisa las transcripciones indicadas.
- *re-embedded sample disagrees with stored vector*: el servicio de embeddings devuelve vectores distintos a los de antes, o `embedding.model` no coincide con el modelo que realmente sirve el endpoint.
- *sampled top-k query returned … rows*: el índice HNSW no es utilizable; revisa los logs de Postgres.

**Se publicaron datos erróneos:** reviértelos con `python -m src.snapshots retire <id>`; los agentes vuelven al snapshot anterior en 15 s. Una construcción en curso sobre el snapshot retirado se abandona.

**Errores de la API de embeddings:**

- Comprueba `embedding.api_key` en `config.yml`
- Verifica que no se superan los límites de tasa
- Revisa el uso de la API en el panel del proveedor
- `Embedding service returned N dimensions, expected M`: `EMBEDDING_DIM` no coincide con la salida del modelo; ver [Cambiar el Modelo de Embedding](#cambiar-el-modelo-de-embedding)

**Falla la conexión a la base de datos:**

- Asegúrate de que postgres está en marcha: `docker compose ps postgres`
- Revisa las credenciales `POSTGRES_*` en `.env`
- Prueba la conexión: `docker compose exec indexer python -m src.debugger test`

**El planificador no se ejecuta:**

```bash
# Comprobar el proceso planificador
docker compose top indexer

# Ver los logs de ejecuciones programadas
docker compose exec indexer python -m src.debugger logs

# Reiniciar el servicio
docker compose restart indexer
```
