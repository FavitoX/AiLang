# AILang — Contexto Completo para Claude Code

> Este archivo contiene todo el contexto del proyecto AILang para que Claude Code
> pueda continuar el trabajo sin perder información.
> Colocar en la raíz del repo como CLAUDE.md o en docs/CONTEXT.md

---

## Sobre el Usuario

- Prefiere C# .NET (actualmente .NET 10 LTS) y también trabaja con Go
- Fuerte disgusto por la falta de DI y resiliencia built-in en Go
- Ama el TestServer de C#, IOptions/appsettings con soporte de filesystem secrets para K8s/Swarm, y la integración de Polly
- Cree que resiliencia, auth, observabilidad y error handling deben ser mandatorios/nativos, no librerías opcionales
- Experiencia en arquitectura de software enterprise, microservicios y sistemas de pagos
- Trabaja con archivos TOML actualmente y los encuentra malos comparados con appsettings de C#
- Filosofía: "quiero escribir negocio, no escribir un circuit breaker"

---

## ¿Qué es AILang?

Un lenguaje de programación diseñado para ser **escrito por IA**, **compilado a código nativo**, y **auditado por humanos con herramientas**.

### Arquitectura de tres capas

```
CAPA HUMANA (Spec declarativa) → lo que escribe el humano
    ↓
CAPA IA (Bytecode en S-expressions) → lo que genera la IA
    ↓
CAPA COMPILADOR (Binario nativo) → lo que ejecuta la máquina
```

### Formato: S-expressions (no JSON)

Migrado de JSON a S-expressions en v0.7 por:
- 55% menos tokens para la IA
- Parser de ~50 líneas vs ~500 para JSON
- Más legible para auditoría con linter con colores
- Sin ruido sintáctico (sin llaves, comillas en keys, comas)
- La IA está bien entrenada con S-expressions (Lisp/Scheme/Clojure)

---

## Decisiones de Diseño Clave (todas tomadas y confirmadas)

### Concurrencia
- Todo es async por defecto (no hay código sync)
- Fibers modelo M:N (millones de fibers sobre N threads del SO)
- PARALLEL + AWAIT_ALL para ejecución paralela
- Background workloads explícitos (no implícitos)

### Errores
- RESULT types obligatorios (inspirado en Rust) — si no hacés MATCH, no compila
- Supervisión "let it crash" (inspirado en Erlang/OTP) — fiber muere, supervisor reinicia
- Catálogo nativo de errores con mapeo automático a HTTP status codes
- Errores custom extensibles con `define_error`
- Formato de error estandarizado (todos los servicios, mismo formato)
- Propagación de errores entre servicios con contexto

### Seguridad
- Auth es OBLIGATORIO en cada handler — PUBLIC o REQUIRES, si no está no compila
- AuthN declarativo (JWT, API keys, OAuth)
- AuthZ por handler (roles, permisos)

### Resiliencia
- OBLIGATORIA para toda comunicación entre servicios — sin policy no compila
- Timeout + Circuit Breaker + Retry en cada policy
- Flag `idempotent` OBLIGATORIO en CALL_SERVICE (true = se puede reintentar, false = NUNCA)
- Circuit breaker emite eventos nativos (open/half-open/close) — otros servicios pueden suscribirse
- Inspirado en C# Polly y Spring Boot 4 @Retryable, pero obligatorio en vez de opcional

### Configuración
- Tipada, con 4 niveles de prioridad:
  1. ENV vars (mayor prioridad, inmutables en runtime)
  2. Filesystem secrets (/run/secrets/ para K8s/Swarm — pueden rotar sin reiniciar)
  3. Config file por ambiente (config.production.json)
  4. Defaults en bytecode
- Secrets NUNCA tienen default — si faltan, el servicio no arranca
- v1: inmutable en runtime. v2: hot-reload (estilo IOptionsMonitor<T>)
- Feature flags → provider OpenFeature/Flagd (post-MVP)

### Fechas y Tiempo
- Tipos nativos: TIMESTAMP, DATE, TIME, DURATION
- CLOCK mockeable como dependencia inyectable (inspirado en C# TimeProvider)
- MOCK_CLOCK en tests para controlar el tiempo
- Formatos predefinidos (ISO8601, UNIX_EPOCH, etc.)
- Aritmética de fechas nativa (COMPUTE_DATE)

### Testing
- TDD nativo
- Tests INDEPENDIENTES: setup → run → clean (NUNCA depends_on entre tests)
- Pueden ejecutarse en paralelo
- Preferencia por tests funcionales (servicio levantado, estilo TestServer de C#)
- La IA genera tests automáticos para CRUDs
- El humano agrega tests de negocio en la capa declarativa
- MOCK_CLOCK, CONFIG_OVERRIDE, mock_services, seed_data en setup
- SQL_SEED para tests con base de datos

### Eventos
- Nativos y tipados — EMIT_EVENT + EVENT_HANDLER
- Desacoplamiento nativo (emisor no conoce suscriptores)
- El runtime los distribuye internamente
- Bridge a Kafka/RabbitMQ/NATS post-MVP

### Observabilidad
- OpenTelemetry automático (tracing en cada handler y CALL_SERVICE)
- Structured logging automático (errores con trace_id, fiber_id, request context)
- Audit log automático (intención + resultado)
- Health checks auto-generados
- Impacto en throughput controlado (sampling, buffer async)

### Validación
- Nivel 1: campo individual desde schema (required, min, max, gt, gte)
- Nivel 2: cross-field (COMPARE, NOT_EQUAL, CONDITIONAL_REQUIRED, MUTUAL_EXCLUSIVE, AT_LEAST_ONE, SUM_MAX)
- READ_BODY ejecuta TODAS las validaciones automáticamente

### Rate Limiting
- Local por instancia (core) — cada instancia protege su capacidad
- Distribuido con Redis (post-MVP)

### Scheduling
- NO es nativo del lenguaje
- El servicio debe ser rápido para iniciar → ejecutar → finalizar
- Encaja con Lambda/Azure Functions/K8s CronJob
- Un handler HTTP que un scheduler externo invoca

### Sagas
- Declarativas con compensación
- Cada step tiene action + compensate
- SAGA_COMPENSATE ejecuta compensación en orden inverso
- Compilador valida que todo step tenga compensación

### DI
- Nativa, auto-wiring del grafo de dependencias
- No más contenedores manuales ni main.go de 200 líneas

---

## Provider PostgreSQL (definido en v0.8)

### Operaciones STORE (CRUD simple, genera SQL parametrizado)

| Instrucción | Retorna | Uso |
|---|---|---|
| STORE_FIND | RESULT(T, NONE) | Por PK |
| STORE_FIND_BY | RESULT(T, NONE) | Por campo(s), uno o ninguno |
| STORE_FILTER | Paginated<T> | Por filtro, N resultados paginados |
| STORE_LIST_ALL | Paginated<T> | Todos, paginados |
| STORE_INSERT | T | Inserta con RETURNING * |
| STORE_UPDATE | T | Reemplaza completo |
| STORE_UPDATE_FIELDS | T | Actualiza campos específicos |
| STORE_DELETE | bool | Elimina por PK |

### Paginación nativa
- Respuesta: { data: [...], page, limit, total, total_pages }
- Parámetros: page, limit, max_limit
- Viene gratis con STORE_FILTER y STORE_LIST_ALL

### SQL_QUERY — para queries complejas
- JOINs, CTEs, window functions, aggregations
- SIEMPRE prepared statements ($1, $2...) — no concatenación de SQL
- Parámetros tipados con (param nombre tipo variable)

### SQL_STREAM — para datasets grandes
- Cursor/reader que trae filas de a batches
- No carga todo en memoria
- Ideal para exports, ETL, batch processing

### SQL_EXEC — operaciones sin retorno de datos
- Bulk updates, DDL

### SQL_TRANSACTION — atomicidad
- Todo dentro es atómico
- Si falla → rollback automático
- Retorna RESULT

### Connection Pool
```
(pool
  (min 5)
  (max 50)
  (idle_timeout (duration 30 seconds)))
```

### Mapping (schema → tabla)
```
(mapping
  (field id → column "id" type "UUID" default "gen_random_uuid()")
  (field name → column "name" type "VARCHAR(200)" not_null))
```

---

## Catálogo de Instrucciones (69 + nuevas de PostgreSQL)

### Por categoría:
- Variables y estado (6): DEFINE_VAR, SET_VAR, COMPUTE, COMPUTE_FN, CONSTRUCT, GET_CURRENT
- Control de flujo (6): BRANCH, MATCH, ITERATE, ITERATE_PARALLEL, LOOP, COMMENT
- Colecciones (1): APPEND
- HTTP (7): READ_BODY, READ_PARAM, READ_QUERY, READ_HEADER, RESPOND, RESPOND_STATUS, SET_HEADER
- Store (8): LIST_ALL, FIND, FIND_BY, FILTER, INSERT, UPDATE, UPDATE_FIELDS, DELETE
- SQL (4): SQL_QUERY, SQL_EXEC, SQL_STREAM, SQL_TRANSACTION
- Streaming (1): STREAM_RESPOND
- Errores (21): catálogo completo
- Concurrencia (5): SPAWN_FIBER, PARALLEL, AWAIT_ALL, CHANNEL, ATOMIC_READ
- Inter-servicio (2): CALL_SERVICE, SAGA_COMPENSATE
- Eventos (1): EMIT_EVENT
- Observabilidad (1): LOG
- Lógica inline (1): TERNARY
- Configuración (3): CONFIG.{key}, SECRET_REF, CONFIG_OVERRIDE
- Fecha/Tiempo (6): CLOCK.NOW, CLOCK.TODAY, CLOCK.DAY_OF_WEEK, COMPUTE_DATE, PARSE_DATE, FORMAT_DATE
- Testing (2): MOCK_CLOCK, SQL_SEED
- Infraestructura (2): RATE_LIMIT, RESET_STORE/RESET_CIRCUIT

### Total: ~77 instrucciones

---

## Servicios de Prueba (validaron el formato)

| Servicio | Archivo | Complejidad | Qué estresa |
|---|---|---|---|
| product_service | product_service.sexpr | Baja | CRUD, auth, DI, tests |
| product_service_pg | product_service_pg.sexpr | Media | PostgreSQL, FIND_BY, FILTER, paginación, SQL_QUERY, SQL_STREAM |
| order_service | order_service.json | Media | Inter-service, eventos, resiliencia |
| pricing_service | pricing_service.json | Media-Alta | Lógica condicional compleja, COMPUTE |
| payment_service | payment_service.json | Alta | Sagas, idempotent, no-retry |
| dashboard_service | dashboard_service.json | Alta | PARALLEL, graceful degradation |

---

## Transpilador v0.2

- **transpiler.py** — generadores de C# y Go (~900 líneas)
- **sexpr_parser.py** — parser genérico de S-expressions (~150 líneas)
- **sexpr_bytecode_parser.py** — semántica AILang sobre S-expressions (~280 líneas)
- Detecta automáticamente si el input es .sexpr o .json
- Genera proyectos C# (.NET 8+) y Go compilables
- Incluye auth JWT, CORS, tracing, DI, error model, validación

### Uso:
```bash
python transpiler.py <archivo.sexpr> --target all
python transpiler.py <archivo.sexpr> --target csharp
python transpiler.py <archivo.sexpr> --target go
```

---

## Análisis de Ecosistemas (completado)

Comparamos C# .NET 10, Java Spring Boot 4, Go, Node/NestJS, Rust, Erlang/Elixir.

### Conclusión principal:
La industria converge hacia hacer nativas las cosas que antes eran librerías (C# integró Polly, Spring integró Resilience4j). AILang da el siguiente paso: **hacer obligatorio lo que hoy es opcional**.

### 10 innovaciones que ningún lenguaje tiene:
1. Auth que no compila sin definir
2. Errores que no se pueden ignorar (RESULT obligatorio)
3. Resiliencia obligatoria para inter-service calls
4. Flag idempotent — conocimiento tribal formalizado
5. Circuit breaker que emite eventos consumibles
6. Sagas declarativas nativas
7. Tests con estructura enforceada (setup→run→clean)
8. Graceful degradation como patrón nativo
9. Mapeo automático error → HTTP status
10. Eventos tipados nativos

---

## Providers Planificados (post-MVP)

| Categoría | Providers |
|---|---|
| Datos SQL | PostgreSQL (en progreso), MySQL |
| Datos NoSQL | MongoDB, CosmosDB, DynamoDB |
| Cache | Redis |
| Búsqueda | Elasticsearch |
| Storage | AWS S3, Azure Blob |
| Mensajería | Kafka, RabbitMQ, NATS |
| Feature Flags | OpenFeature/Flagd |

---

## Targets

- **C# (.NET 10 LTS)** — primary target. Todas las features nuevas del lenguaje aterrizan acá primero.
- **Go (1.25)** — frozen, en mantenimiento. Bug fixes sí, features nuevas no se backportean. PRs externos bienvenidos.

---

## Próximos Pasos

1. **Transpilador para PostgreSQL** — generar C# con Npgsql y Go con pgx desde product_service_pg.sexpr
2. **Convertir order/pricing/payment/dashboard a S-expressions** 
3. **NativeAOT como modo opt-in del C# codegen** — flag `--aot`, JSON source-generation, single-binary publish. Verificado AOT-compat con Polly / Npgsql / Minimal APIs via `bytecode/probes/aot-probe/`. Reemplaza el item "runtime nativo propio".

---

## Decisiones Pendientes

- Transaccionalidad cross-service: ¿Sagas + dead letter queue?
- GUARD/VALIDATE_CHAIN: instrucción para evitar BRANCH anidados
- Hot-reload de config (v2)
- Linter con colores para S-expressions (solo necesita colores e indentación, no reglas de estilo)
