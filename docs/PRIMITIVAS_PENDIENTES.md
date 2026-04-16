# AILang — Primitivas Pendientes: Deep Dive

> Complementario al Manifiesto v0.6 y al Análisis de Ecosistemas

---

## 1. Configuración

### Principio: Config por ambiente, sin archivos mágicos, sin YAML hell

Todo servicio enterprise necesita configuración que cambie entre ambientes (dev, staging, prod). En C# tenés `appsettings.json` + `appsettings.Production.json` + env vars + user secrets. En Spring tenés `application.yml` + profiles. En Go... tenés que construir todo a mano.

### Modelo AILang

```
CONFIG service_name:

  ── Valores con defaults ──
  port: INT = 8080
  log_level: ENUM(ERROR, WARNING, INFO, DEBUG) = INFO
  max_connections: INT = 100

  ── Secrets (nunca hardcodeados, siempre referencia) ──
  jwt_secret: SECRET_REF("JWT_SECRET")
  db_connection: SECRET_REF("DB_CONNECTION_STRING")

  ── Feature flags ──
  enable_new_pricing: BOOL = false

  ── Config de resiliencia (overrideable por ambiente) ──
  product_service_timeout_ms: INT = 3000
  product_service_cb_threshold: INT = 5
```

### Resolución por orden de prioridad (mayor a menor)

1. **Variables de entorno** — siempre ganan (estándar en containers/k8s)
2. **Config file por ambiente** — `config.{environment}.json`
3. **Defaults en el bytecode** — lo que está en CONFIG

### Reglas

- **Secrets NUNCA tienen default** — si no hay env var, el servicio no arranca y dice claramente qué falta
- **Config es tipada** — INT, STRING, BOOL, ENUM, LIST, DECIMAL. Tipo incorrecto = error al arrancar, no en runtime
- **Config es inmutable en runtime** — se lee al iniciar, no cambia. Si querés feature flags dinámicos, es otro mecanismo (post-MVP)
- **El compilador valida** que toda referencia a config en el bytecode tenga una entrada en CONFIG

### Acceso desde handlers

```
HANDLER get_products() → RESULT(List<Product>, Error):
  max = CONFIG.max_connections     ← acceso tipado, validado en compilación
  BRANCH CONFIG.enable_new_pricing
    → TRUE:  ...pricing v2...
    → FALSE: ...pricing v1...
```

### En tests

```
TEST_FUNCTIONAL product_service:
  CONFIG_OVERRIDE:
    enable_new_pricing: true       ← override de config para este test
    product_service_timeout_ms: 100
```

### Inspiración

| Ecosistema | Qué tomar | Qué NO tomar |
|------------|-----------|--------------|
| C# .NET | `IConfiguration` con override por ambiente, `IOptions<T>` tipado | `appsettings.json` anidado que se vuelve inmanejable |
| Spring Boot | Profiles, `@Value` con defaults | YAML con 200 líneas de config de Spring internals |
| Go | Env vars como ciudadanos de primera clase | Tener que usar Viper/Cobra porque no hay nada nativo |
| 12-Factor App | Env vars para config, secrets separados | — |

---

## 2. Fechas y Tiempo

### Principio: Las fechas son difíciles. El lenguaje debe hacerlas fáciles y testeables.

### Lo que cada lenguaje hace

| Lenguaje | Modelo | Problema |
|----------|--------|----------|
| C# | `DateTime`, `DateTimeOffset`, `TimeSpan`, `DateOnly`, `TimeOnly` | Bien diseñado. `DateTimeOffset` para UTC es excelente. Desde .NET 8: `TimeProvider` abstracto para testing |
| Go | `time.Time`, `time.Duration`, `time.Parse("2006-01-02", ...)` | La fecha mágica "2006-01-02 15:04:05" como template de formato es críptica |
| Java | `java.time` (Instant, LocalDate, ZonedDateTime, Duration) | Bien desde Java 8. Antes era un desastre (java.util.Date) |
| JS/Node | `Date` | Horrible. Mutable, confuso, sin timezone serio. Moment.js deprecated, luxon/dayjs como reemplazo |
| Rust | `chrono` crate | Bueno pero externo, no nativo |

### Modelo AILang

#### Tipos nativos de fecha/tiempo

```
TIMESTAMP    → fecha + hora + timezone (siempre UTC internamente, display según config)
DATE         → solo fecha (sin hora)
TIME         → solo hora (sin fecha)
DURATION     → diferencia de tiempo (ej: 30m, 2h, 7d)
```

#### Operaciones nativas

```
GET_CURRENT timestamp → now          ← obtiene timestamp actual
GET_CURRENT date → today             ← obtiene fecha actual
GET_CURRENT day_of_week → dow        ← lunes, martes, etc.

── Aritmética de fechas ──
COMPUTE_DATE now + DURATION(30, "days") → future_date
COMPUTE_DATE now - DURATION(2, "hours") → past_time
COMPUTE_DATE diff(date_a, date_b) → DURATION

── Comparaciones ──
BRANCH { left: "expiry_date", op: "<", right: "now" }
  → TRUE: ... expirado ...

── Parsing (sin fechas mágicas como Go) ──
PARSE_DATE "2026-04-15" FORMAT "YYYY-MM-DD" → date
PARSE_TIMESTAMP "2026-04-15T10:30:00Z" → timestamp

── Formateo ──
FORMAT_DATE date PATTERN "DD/MM/YYYY" → string
FORMAT_TIMESTAMP ts PATTERN "ISO8601" → string
```

#### Formatos de fecha predefinidos (sin inventar)

```
ISO8601      → "2026-04-15T10:30:00Z"       (default para APIs)
DATE_ONLY    → "2026-04-15"
TIME_ONLY    → "10:30:00"
HUMAN_SHORT  → "15 Apr 2026"
HUMAN_LONG   → "Tuesday, April 15, 2026"
UNIX_EPOCH   → 1776326400                   (segundos desde epoch)
UNIX_MS      → 1776326400000                (milisegundos)
```

#### CLOCK mockeable para testing

Esto es **fundamental**. Inspirado en `TimeProvider` de C# .NET 8+ y `Clock` de Java:

```
── En el servicio ──
SERVICE coupon_service
  DEPENDS_ON coupon_store, CLOCK    ← CLOCK es una dependencia inyectable

  HANDLER validate_coupon(code) → RESULT(Coupon, Error):
    now = CLOCK.NOW                  ← usa CLOCK, no GET_CURRENT directo
    coupon = coupon_store.FIND(code)
    BRANCH { left: "now", op: ">", right: "coupon.valid_until" }
      → TRUE: ERR_COUPON_EXPIRED(code)
      → FALSE: RESPOND coupon

── En tests ──
TEST_FUNCTIONAL coupon_service:
  SETUP:
    MOCK_CLOCK: "2026-12-31T23:59:59Z"     ← control total del tiempo
    seed_data:
      coupon_store: [
        { code: "NEWYEAR", valid_until: "2027-01-01T00:00:00Z", ... }
      ]

  CASE "coupon still valid":
    REQUEST GET "/coupons/NEWYEAR"
    ASSERT status = 200

  CASE "coupon expired":
    MOCK_CLOCK: "2027-01-02T00:00:00Z"     ← adelantamos el reloj
    REQUEST GET "/coupons/NEWYEAR"
    ASSERT status = 422
    ASSERT body.error.type = "COUPON_EXPIRED"
```

**El CLOCK nunca se accede directo en producción** — siempre usa el reloj real. Pero en tests, el MOCK_CLOCK te permite:
- Simular expiración de tokens/cupones
- Testear lógica que depende del día de la semana
- Probar year boundaries, leap years, timezone edge cases
- Reproducir bugs que solo pasan en ciertos horarios

---

## 3. Validación de Request

### Principio: Validación completa desde schema, incluyendo cross-field

### Nivel 1 — Validación de campo individual (ya existe)

Esto ya lo tenemos en el schema:

```
"name": { "type": "string", "required": true, "min": 1, "max": 200 }
"price": { "type": "decimal", "required": true, "gt": 0 }
"stock": { "type": "int", "required": true, "gte": 0 }
```

### Nivel 2 — Validación cross-field (nuevo)

Reglas que involucran más de un campo:

```
"schemas": {
  "DateRange": {
    "fields": {
      "start_date": { "type": "date", "required": true },
      "end_date": { "type": "date", "required": true }
    },
    "validations": [
      {
        "rule": "COMPARE",
        "left": "end_date",
        "op": ">",
        "right": "start_date",
        "error": "end_date must be after start_date"
      }
    ]
  },

  "Transfer": {
    "fields": {
      "from_account": { "type": "uuid", "required": true },
      "to_account": { "type": "uuid", "required": true },
      "amount": { "type": "decimal", "required": true, "gt": 0 }
    },
    "validations": [
      {
        "rule": "NOT_EQUAL",
        "left": "from_account",
        "right": "to_account",
        "error": "cannot transfer to the same account"
      }
    ]
  },

  "Discount": {
    "fields": {
      "type": { "type": "enum", "values": ["percentage", "fixed"], "required": true },
      "percentage": { "type": "decimal", "required": false },
      "fixed_amount": { "type": "decimal", "required": false }
    },
    "validations": [
      {
        "rule": "CONDITIONAL_REQUIRED",
        "condition": { "field": "type", "equals": "percentage" },
        "required_field": "percentage",
        "error": "percentage is required when type is percentage"
      },
      {
        "rule": "CONDITIONAL_REQUIRED",
        "condition": { "field": "type", "equals": "fixed" },
        "required_field": "fixed_amount",
        "error": "fixed_amount is required when type is fixed"
      }
    ]
  }
}
```

### Reglas de validación cross-field

| Regla | Descripción | Ejemplo |
|-------|-------------|---------|
| `COMPARE` | Compara dos campos | end_date > start_date |
| `NOT_EQUAL` | Dos campos deben ser distintos | from != to |
| `CONDITIONAL_REQUIRED` | Campo requerido si otro tiene cierto valor | percentage requerido si type == "percentage" |
| `MUTUAL_EXCLUSIVE` | Solo uno de N campos puede tener valor | phone XOR email |
| `AT_LEAST_ONE` | Al menos uno de N campos debe tener valor | phone OR email |
| `SUM_MAX` | Suma de campos no puede exceder valor | discount_a + discount_b <= 50 |

### En el bytecode

El `READ_BODY` automáticamente ejecuta TODAS las validaciones (campo + cross-field). Si alguna falla, retorna `ERR_VALIDATION` con el detalle. La IA no tiene que escribir ifs manuales.

---

## 4. Rate Limiting

### Principio: Cada instancia protege su capacidad. Rate limiting distribuido es otra capa.

### El problema que planteás

Es real: si tenés 3 instancias y cada una soporta 200 rps, el rate limiting local protege cada instancia, pero un load balancer podría enviar 600 rps en total. El rate limiting distribuido (Redis-based, por ejemplo) es un problema distinto.

### Modelo AILang: Dos niveles

#### Nivel 1 — Rate limiting local (por instancia) — CORE

Protege la instancia individual de sobrecarga:

```
SERVICE product_service
  RATE_LIMIT:
    max_rps: 200                    ← requests por segundo por instancia
    burst: 50                       ← permite burst temporal
    on_limit: ERR_RATE_LIMITED      ← error nativo con status 429
    queue_size: 0                   ← 0 = no queue, rechaza inmediatamente
```

Esto es **obligatorio** si el servicio recibe tráfico HTTP. Si no lo definís, el compilador advierte (warning, no error — porque un servicio interno puede no necesitarlo).

#### Nivel 2 — Rate limiting distribuido — POST-MVP (provider)

Cuando necesitás limitar globalmente (ej: "este API key puede hacer 1000 requests/min en total"):

```
SERVICE product_service
  RATE_LIMIT:
    max_rps: 200                     ← local
    distributed:                     ← post-MVP
      provider: REDIS
      key: "request.auth.api_key"
      max_rpm: 1000
      window: SLIDING
```

Esto requiere un provider (Redis) y es parte de la segunda fase.

---

## 5. Scheduling

### Principio: No es nativo del servicio. Es un patrón de invocación externa.

Coincido 100% con tu visión: scheduling es **externo**. El servicio debe ser:

```
RÁPIDO PARA INICIAR → EJECUTAR → FINALIZAR
```

Esto encaja perfectamente con:
- **Azure Functions / AWS Lambda** — invocación por timer trigger
- **Kubernetes CronJob** — invocación periódica
- **Cualquier scheduler externo** — invoca un endpoint HTTP

### Lo que AILang sí necesita para soportar esto

```
── Endpoint que un scheduler externo puede invocar ──
ROUTE POST "/jobs/cleanup" → handler_cleanup_job

HANDLER handler_cleanup_job:
  AUTH: { requires: "service_account" }    ← auth específico para jobs
  
  ← lógica del job
  
  RESPOND 200 { "processed": count, "duration_ms": elapsed }
```

Lo importante es que el handler sea igual que cualquier otro — con auth, tracing, error handling, todo nativo. No es un "job especial", es un endpoint más que alguien invoca.

### Lo que AILang NO necesita

- No necesita un scheduler interno (no es responsabilidad del lenguaje)
- No necesita cron syntax nativa
- No necesita un concepto de "background job queue" nativo (eso es infraestructura)

---

## 6. Providers: La Arquitectura de Extensibilidad

### Principio: Core vs Providers

El bytecode tiene primitivas core que siempre están (HTTP, errores, fibers, etc.). Pero la conexión con el mundo externo se hace mediante **providers**. Un provider es una abstracción tipada sobre un servicio externo.

### Visión a futuro de providers

```
── Providers de datos ──
PROVIDER PostgreSQL    → STORE operations (SQL)
PROVIDER MongoDB       → STORE operations (document)
PROVIDER Redis         → CACHE operations + rate limiting distribuido
PROVIDER MySQL         → STORE operations (SQL)

── Providers de storage ──
PROVIDER AWS_S3        → BLOB operations (upload, download, presigned URL)
PROVIDER Azure_Blob    → BLOB operations

── Providers de búsqueda ──
PROVIDER Elasticsearch → SEARCH operations (full-text, aggregations)

── Providers cloud-native ──
PROVIDER CosmosDB      → STORE operations (multi-model)
PROVIDER DynamoDB      → STORE operations (key-value)

── Providers de mensajería (post-MVP) ──
PROVIDER Kafka         → EVENT bridge (emit/subscribe a topics externos)
PROVIDER RabbitMQ      → EVENT bridge
PROVIDER NATS          → EVENT bridge
```

### Cómo se usaría un provider en el bytecode

```
SERVICE order_service
  DEPENDS_ON order_store, cache, product_service

  STORES:
    order_store:
      PROVIDER: PostgreSQL
      CONNECTION: CONFIG.db_connection
      SCHEMA: Order
      TABLE: "orders"

    cache:
      PROVIDER: Redis
      CONNECTION: CONFIG.redis_connection
      TTL: DURATION(5, "minutes")

  HANDLER get_order(id) → RESULT(Order, Error):
    ── Primero intenta cache ──
    cached = cache.GET("order:{id}")
    MATCH cached:
      OK(order) → RESPOND order
      NONE:
        ── Si no está en cache, va a la DB ──
        order = order_store.FIND(id)
        MATCH order:
          OK(o):
            cache.SET("order:{id}", o)    ← cachea para la próxima
            RESPOND o
          NONE → ERR_NOT_FOUND("order", id)
```

### Interfaz de un Provider

Todos los providers de datos implementan la misma interfaz de STORE:

```
PROVIDER_INTERFACE Store<T>:
  LIST_ALL(filters?) → RESULT(List<T>, Error)
  FIND(key) → RESULT(T?, Error)
  INSERT(data) → RESULT(T, Error)
  UPDATE(key, data) → RESULT(T, Error)
  UPDATE_FIELDS(key, fields) → RESULT(T, Error)
  DELETE(key) → RESULT(bool, Error)
  QUERY(filter, sort?, limit?) → RESULT(List<T>, Error)
```

Esto significa que **podés cambiar de PostgreSQL a MongoDB sin cambiar el bytecode del handler**. Solo cambiás la declaración del provider en STORES.

### Provider-specific operations

Algunos providers tienen operaciones que otros no:

```
── Solo PostgreSQL/MySQL ──
STORE.TRANSACTION([
  order_store.INSERT(order),
  inventory_store.UPDATE_FIELDS(product_id, { stock: new_stock })
])

── Solo Redis ──
cache.SET_WITH_TTL("key", value, DURATION(5, "minutes"))
cache.INCREMENT("counter:daily")
cache.EXPIRE("session:{id}", DURATION(30, "minutes"))

── Solo Elasticsearch ──
search_store.SEARCH("laptop gaming", filters: { price_range: [500, 2000] })
search_store.AGGREGATE("products", group_by: "category", metric: "avg_price")

── Solo S3/Blob ──
blob.UPLOAD(file, path: "uploads/{id}.pdf")
blob.DOWNLOAD(path) → RESULT(Blob, Error)
blob.PRESIGNED_URL(path, expires: DURATION(1, "hour")) → string
```

### Para el MVP

Solo IN_MEMORY como store. Los providers son post-MVP pero la **interfaz STORE ya está definida**, lo cual significa que cuando se agreguen providers, el bytecode de los handlers no cambia.

---

## 7. Resumen: Instrucciones Nuevas

| Instrucción | Categoría | Descripción |
|-------------|-----------|-------------|
| `CONFIG.{key}` | Configuración | Acceso tipado a config |
| `SECRET_REF` | Configuración | Referencia a secret (env var) |
| `CONFIG_OVERRIDE` | Testing | Override de config en tests |
| `CLOCK.NOW` | Fecha/Tiempo | Timestamp actual (mockeable) |
| `CLOCK.TODAY` | Fecha/Tiempo | Fecha actual (mockeable) |
| `CLOCK.DAY_OF_WEEK` | Fecha/Tiempo | Día de la semana (mockeable) |
| `MOCK_CLOCK` | Testing | Fijar el reloj en tests |
| `COMPUTE_DATE` | Fecha/Tiempo | Aritmética de fechas |
| `PARSE_DATE` | Fecha/Tiempo | Parsing de string a fecha |
| `FORMAT_DATE` | Fecha/Tiempo | Formateo de fecha a string |
| `RATE_LIMIT` | Infraestructura | Rate limiting por instancia |
| Cross-field validations | Validación | COMPARE, NOT_EQUAL, CONDITIONAL_REQUIRED, etc. |

**Conteo actualizado**: 57 instrucciones previas + 12 nuevas = **69 instrucciones**

---

## 8. Instrucciones Totales por Categoría (actualizado)

| Categoría | Cantidad | Instrucciones |
|-----------|----------|---------------|
| Variables y estado | 6 | DEFINE_VAR, SET_VAR, COMPUTE, COMPUTE_FN, CONSTRUCT, GET_CURRENT |
| Control de flujo | 6 | BRANCH, MATCH, ITERATE, ITERATE_PARALLEL, LOOP, COMMENT |
| Colecciones | 1 | APPEND |
| HTTP | 7 | READ_BODY, READ_PARAM, READ_QUERY, READ_HEADER, RESPOND, RESPOND_STATUS, SET_HEADER |
| Store | 6 | LIST_ALL, FIND, INSERT, UPDATE, UPDATE_FIELDS, DELETE |
| Errores | 21 | Catálogo completo (NOT_FOUND, VALIDATION, etc.) |
| Concurrencia | 5 | SPAWN_FIBER, PARALLEL, AWAIT_ALL, CHANNEL, ATOMIC_READ |
| Inter-servicio | 2 | CALL_SERVICE, SAGA_COMPENSATE |
| Eventos | 1 | EMIT_EVENT |
| Observabilidad | 1 | LOG |
| Lógica inline | 1 | TERNARY |
| **Configuración** | **3** | **CONFIG.{key}, SECRET_REF, CONFIG_OVERRIDE** |
| **Fecha/Tiempo** | **6** | **CLOCK.NOW, CLOCK.TODAY, CLOCK.DAY_OF_WEEK, COMPUTE_DATE, PARSE_DATE, FORMAT_DATE** |
| **Testing** | **1** | **MOCK_CLOCK** |
| **Infraestructura** | **2** | **RATE_LIMIT, (MOCK_CLOCK ya contado)** |
| **Total** | **69** | |

---

*Documento de primitivas pendientes v1.0 — Abril 2026*
*Complementario al Manifiesto AILang v0.6*
