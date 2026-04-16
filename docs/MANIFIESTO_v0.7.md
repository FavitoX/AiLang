# AILang — Manifiesto v0.7

> Un lenguaje de programación diseñado para ser escrito por IA, compilado a código máquina, y auditado por humanos mediante herramientas.
> Bytecode en formato S-expression.

---

## 1. Visión

Los lenguajes de programación actuales fueron diseñados para que **humanos** expresen instrucciones de forma legible. Pero en la era de la IA generadora de código, ese puente entre pensamiento humano y máquina ya no es necesario como capa de escritura.

**AILang** propone una arquitectura de tres capas:

```
┌─────────────────────────────────────────────┐
│  CAPA HUMANA (Especificación Declarativa)   │
│  El humano describe QUÉ quiere, con qué     │
│  restricciones (rendimiento, seguridad,      │
│  plataforma). Similar a product specs.       │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  CAPA IA (Bytecode Semántico — AILang)      │
│  La IA genera código en formato S-expression │
│  optimizado para generación por IA.          │
│  No legible directamente por humanos.        │
│  Auditable con herramientas + linter.        │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────┐
│  CAPA COMPILADOR (Binario)                  │
│  Compila el bytecode semántico a código      │
│  máquina optimizado para el hardware         │
│  target (x86, ARM, GPU, NPU, etc.)          │
└─────────────────────────────────────────────┘
```

---

## 2. Problema que Resuelve

Cuando una IA escribe código en lenguajes humanos (Python, Go, C#), enfrenta problemas innecesarios:

- **Desperdicia tokens en sintaxis decorativa**: llaves, paréntesis, indentación, imports, boilerplate — todo existe para legibilidad humana.
- **Nombra cosas para humanos**: `getUserData()` existe para que un humano lo entienda; la IA no necesita nombres descriptivos.
- **Se equivoca en formato, no en lógica**: errores de indentación, `;` olvidados, brackets mal cerrados — son errores del formato humano.
- **No puede expresar intención de concurrencia de forma directa**: tiene que usar abstracciones humanas como `async/await`, `go func()`, `Task<T>`.

---

## 3. Formato: S-expressions

### ¿Por qué S-expressions y no JSON?

El bytecode AILang v0.1–v0.6 usó JSON. En v0.7 migramos a S-expressions por:

| Criterio | JSON | S-expression |
|----------|------|-------------|
| Ruido sintáctico | Alto (llaves, comillas, comas, "op":) | Bajo (solo paréntesis) |
| Bytes por servicio | 7,512 (product_service) | 6,955 (-7.4%) |
| Tokens para IA | ~18 por instrucción | ~8 por instrucción (-55%) |
| Velocidad de parseo | Parser complejo (~500 LOC) | Parser simple (~50 LOC) |
| Anidación legible | Difícil de seguir con {} anidados | Natural con indentación |
| Soporte IA | Bueno | Excelente (entrenada con Lisp/Scheme/Clojure) |
| Herramientas | Universales | Linter con colores = excelente |

### Estructura general de un archivo .sexpr

```
;; Comentarios con doble punto y coma

(service nombre
  (version "0.7")
  (port 8080)
  ...)

(schema NombreEntidad
  (field nombre tipo (constraint) (constraint))
  ...)

(store nombre_store
  (type IN_MEMORY)
  (schema NombreEntidad)
  ...)

(config
  (key tipo default)
  ...)

(route VERBO "/path" handler_name)

(handler handler_name
  (auth PUBLIC | (requires authenticated) | (requires (role "admin")))
  (returns (RESULT TipoRetorno Error))
  (do
    (INSTRUCCION args → resultado)
    ...))

(tests nombre_servicio
  (test "nombre_test"
    (setup ...)
    (run ...)
    (clean ...)))
```

### Ejemplo: handler completo

```
(handler handler_delete
  (auth (requires authenticated))
  (params (id uuid))
  (returns (RESULT Void Error))
  (do
    (READ_PARAM id uuid → id)
    (STORE_FIND product_store id → product)
    (MATCH product
      (NONE (ERR_NOT_FOUND "product" id))
      (OK
        (BRANCH (> product.stock 0)
          (TRUE
            (ERR_BUSINESS_RULE "cannot_delete_with_stock"
              "product has stock > 0"))
          (FALSE
            (STORE_DELETE product_store id)
            (EMIT_EVENT product_deleted id)
            (RESPOND 204)))))))
```

---

## 4. Concurrencia y Paralelismo

### Principio: Todo es async por defecto

No hay distinción entre código sync y async. Todo es async. Se eliminan categorías enteras de bugs.

### Primitivas de concurrencia

```
(SPAWN_FIBER (do ...))             ;; fire-and-forget, runtime distribuye entre cores
(PARALLEL result
  (fiber "name_a" (do ...))
  (fiber "name_b" (do ...)))       ;; ejecución paralela nombrada
(AWAIT_ALL result timeout_ms)      ;; espera a todas las fibers
(CHANNEL name tipo)                ;; comunicación entre fibers sin locks
(ATOMIC_READ source → target)      ;; lectura atómica de memoria compartida
```

### Background workloads explícitos

```
(background cleanup_job
  (every (duration 30 minutes))
  (do
    (QUERY db_pool DELETE expired)))
```

### Fibers: Modelo M:N

| Unidad | Peso | Quién la maneja | Escala |
|--------|------|-----------------|--------|
| Proceso del SO | ~MB | Kernel | Miles |
| Thread del SO | ~1-2MB | Kernel | Miles |
| **Fiber (AILang)** | **~2-8KB** | **Runtime** | **Millones** |

Detección de race conditions es responsabilidad del compilador (inspirado en `go -race` y Rust borrow checker).

---

## 5. Dependency Injection Nativa

DI es parte del lenguaje. Las dependencias se declaran y el runtime resuelve el grafo automáticamente:

```
(service user_service
  (depends_on db_pool cache logger CLOCK))
```

No más contenedores de DI manuales, ni `main.go` de 200 líneas, ni XML/YAML de wiring.

---

## 6. Configuración

### Principio: Tipada, por ambiente, sin YAML hell

Inspirado en C# `appsettings.json` con override por ambiente + `IOptions<T>` tipado + 12-Factor App.

### Definición en bytecode

```
(config
  (port          int     8080)
  (log_level     enum    (ERROR WARNING INFO DEBUG) INFO)
  (max_connections int   100)
  (enable_new_pricing bool false)
  (jwt_secret    secret  "JWT_SECRET")
  (db_connection secret  "DB_CONNECTION_STRING"))
```

### Resolución por prioridad (mayor a menor)

1. **Variables de entorno** — inmutables en runtime, máxima prioridad
2. **Filesystem secrets** — pueden rotar sin reiniciar (`/run/secrets/` para K8s/Swarm)
3. **Config file por ambiente** — `config.production.json`, `config.staging.json`
4. **Defaults en bytecode** — lo definido en `(config ...)`

### Reglas

- **Secrets NUNCA tienen default** — si no hay env var ni archivo, el servicio no arranca y dice qué falta
- **Config es tipada** — tipo incorrecto = error al arrancar, no en runtime
- **v1: inmutable en runtime** — se lee al iniciar
- **v2: hot-reload** para secciones específicas (estilo `IOptionsMonitor<T>`)
- **Feature flags** → provider OpenFeature/Flagd (post-MVP)

### Acceso desde handlers

```
(handler get_products
  (do
    (BRANCH CONFIG.enable_new_pricing
      (TRUE  ...)
      (FALSE ...))))
```

### Override en tests

```
(test "test_with_custom_config"
  (setup
    (CONFIG_OVERRIDE
      (enable_new_pricing true)
      (max_connections 10)))
  (run ...)
  (clean ...))
```

---

## 7. Fechas y Tiempo

### Principio: Fechas fáciles + CLOCK mockeable para testing

Inspirado en C# `DateTime`/`DateTimeOffset`/`TimeProvider`.

### Tipos nativos

```
TIMESTAMP    ;; fecha + hora + timezone (siempre UTC internamente)
DATE         ;; solo fecha
TIME         ;; solo hora
DURATION     ;; diferencia de tiempo (30m, 2h, 7d)
```

### CLOCK: dependencia inyectable y mockeable

```
(service coupon_service
  (depends_on coupon_store CLOCK)

  (handler validate_coupon
    (do
      (CLOCK.NOW → now)
      (STORE_FIND coupon_store code → coupon)
      (BRANCH (> now coupon.valid_until)
        (TRUE  (ERR_CUSTOM ERR_COUPON_EXPIRED code))
        (FALSE (RESPOND 200 coupon))))))
```

En tests:

```
(test "coupon_expired"
  (setup
    (MOCK_CLOCK "2027-01-02T00:00:00Z")
    (seed_data coupon_store
      ((code "NEWYEAR") (valid_until "2027-01-01T00:00:00Z") ...)))
  (run
    (REQUEST GET "/coupons/NEWYEAR")
    (ASSERT status 422)
    (ASSERT body.error.type "COUPON_EXPIRED"))
  (clean (RESET_STORE coupon_store)))
```

### Operaciones

```
(CLOCK.NOW → now)                              ;; timestamp actual
(CLOCK.TODAY → today)                           ;; fecha actual
(CLOCK.DAY_OF_WEEK → dow)                       ;; lunes, martes, etc.
(COMPUTE_DATE now + (duration 30 days) → future) ;; aritmética
(COMPUTE_DATE diff date_a date_b → diff)        ;; diferencia
(PARSE_DATE "2026-04-15" "YYYY-MM-DD" → date)   ;; parsing
(FORMAT_DATE date "DD/MM/YYYY" → string)         ;; formateo
```

### Formatos predefinidos

```
ISO8601      ;; "2026-04-15T10:30:00Z" (default para APIs)
DATE_ONLY    ;; "2026-04-15"
UNIX_EPOCH   ;; 1776326400 (segundos)
UNIX_MS      ;; 1776326400000 (milisegundos)
```

---

## 8. Modelo de Errores: Dos Niveles + Catálogo Nativo

### Nivel 1 — Errores esperados: RESULT obligatorio

Inspirado en Rust `Result<T,E>`, pero sin la ceremonia. Si no hacés MATCH sobre un RESULT, **no compila**.

```
(STORE_FIND product_store id → product)
(MATCH product
  (OK   (RESPOND 200 product))
  (NONE (ERR_NOT_FOUND "product" id)))
```

### Nivel 2 — Errores inesperados: Supervisión (Erlang-style)

```
(service user_service
  (on_fiber_crash RESTART_FIBER)
  (on_service_crash NOTIFY_RESTART))
```

La fiber muere, el supervisor la reinicia, el servicio sigue vivo.

### Catálogo nativo con mapeo HTTP automático

```
ERR_NOT_FOUND          → 404     ERR_UNAUTHORIZED     → 401
ERR_ALREADY_EXISTS     → 409     ERR_FORBIDDEN        → 403
ERR_VALIDATION         → 400     ERR_TOKEN_EXPIRED    → 401
ERR_BUSINESS_RULE      → 422     ERR_TIMEOUT          → 504
ERR_INVALID_BODY       → 400     ERR_UNAVAILABLE      → 503
ERR_INVALID_PARAM      → 400     ERR_RATE_LIMITED     → 429
ERR_CONFLICT           → 409     ERR_CIRCUIT_OPEN     → 503
```

### Formato de error estandarizado (todos los servicios, mismo formato)

```
(error
  (type "NOT_FOUND")
  (entity "product")
  (id "123")
  (message "product with id 123 not found")
  (trace_id "abc-123-def")
  (timestamp "2026-04-15T..."))
```

### Errores custom

```
(define_error ERR_INSUFFICIENT_STOCK 422
  (params product_id requested available)
  (message "product {product_id} has {available} units but {requested} requested"))
```

### Propagación entre servicios

```
(PROPAGATE error (context "create_order" item.product_id))
```

---

## 9. Validación

### Nivel 1 — Campo individual (desde schema)

```
(schema Product
  (field name  string (required) (min 1) (max 200))
  (field price decimal (required) (gt 0)))
```

### Nivel 2 — Cross-field

```
(schema DateRange
  (field start_date date (required))
  (field end_date   date (required))
  (validate (COMPARE end_date > start_date)
    "end_date must be after start_date"))

(schema Transfer
  (field from_account uuid (required))
  (field to_account   uuid (required))
  (validate (NOT_EQUAL from_account to_account)
    "cannot transfer to the same account"))

(schema Discount
  (field type         enum (percentage fixed) (required))
  (field percentage   decimal)
  (field fixed_amount decimal)
  (validate (CONDITIONAL_REQUIRED (= type "percentage") percentage)
    "percentage required when type is percentage"))
```

### Reglas de validación cross-field

| Regla | Descripción |
|-------|-------------|
| `COMPARE` | Compara dos campos (end > start) |
| `NOT_EQUAL` | Dos campos deben ser distintos |
| `CONDITIONAL_REQUIRED` | Campo requerido si otro tiene cierto valor |
| `MUTUAL_EXCLUSIVE` | Solo uno de N campos puede tener valor |
| `AT_LEAST_ONE` | Al menos uno de N campos debe tener valor |
| `SUM_MAX` | Suma de campos no puede exceder valor |

El `READ_BODY` ejecuta TODAS las validaciones automáticamente.

---

## 10. Testing

### Principio: TDD nativo. Tests independientes: setup → run → clean.

### Quién escribe los tests

1. **El humano** — en la capa declarativa (contratos de negocio)
2. **La IA** — genera tests automáticos (CRUDs completos, negocio como base)

### Estilo: funcionales sobre unitarios

Preferencia por tests funcionales con servicio levantado (inspirado en C# `TestServer` / Go `httptest`).

### Estructura obligatoria

```
(test "nombre_descriptivo"
  (setup
    (seed_data store_name ((...)))           ;; datos iniciales
    (mock_services service_name ((...)))     ;; mocks de servicios externos
    (MOCK_CLOCK "2026-12-31T23:59:59Z")     ;; control de tiempo
    (CONFIG_OVERRIDE (key value)))           ;; override de config
  (run
    (REQUEST VERB "/path" (auth bool) (body ...))
    (ASSERT campo operador valor)
    (CAPTURE campo → variable))
  (clean
    (RESET_STORE store_name)
    (RESET_CIRCUIT policy_name)))
```

Cada test es un universo aislado. No hay `depends_on` entre tests. Pueden ejecutarse en paralelo.

---

## 11. Resiliencia Nativa: Obligatoria y Configurable

### Principio: No podés llamar a otro servicio sin definir qué pasa cuando falla

Resiliencia es **obligatoria** para toda comunicación entre servicios. Sin policy definida, **no compila**.

```
(resilience product_service_policy
  (timeout
    (duration_ms 3000)
    (on_timeout ERR_TIMEOUT))
  (circuit_breaker
    (failure_threshold 5)
    (success_threshold 3)
    (reset_timeout_ms 30000)
    (on_open    (emit circuit_open)    (fallback ERR_UNAVAILABLE))
    (on_half_open (emit circuit_half_open))
    (on_close   (emit circuit_closed)))
  (retry
    (max_attempts 3)
    (backoff exponential (initial_ms 100) (max_ms 2000))
    (retryable ERR_TIMEOUT ERR_CONNECTION)
    (non_retryable ERR_NOT_FOUND ERR_VALIDATION ERR_BUSINESS_RULE)))
```

### Flag `idempotent` en CALL_SERVICE

```
(CALL_SERVICE product_service GET "/products/{id}"
  (idempotent true) → product_result)    ;; se puede reintentar

(CALL_SERVICE payment_gateway POST "/charge"
  (idempotent false) → charge_result)    ;; NUNCA se reintenta
```

Es **obligatorio** declarar `idempotent`. Sin él, no compila.

### Circuit breaker emite eventos

Otros servicios pueden suscribirse y actuar (graceful degradation):

```
(on_event circuit_open
  (do
    (LOG WARNING "service {event.service} circuit open")))
```

---

## 12. Sagas: Flujos Multi-Step con Compensación

```
(saga process_payment
  (step create_payment_record
    (action     (STORE_INSERT payment_store))
    (compensate (STORE_UPDATE_FIELDS payment_store (status "failed"))))
  (step fraud_check
    (action     (CALL_SERVICE fraud_service POST "/check" (idempotent true)))
    (compensate (LOG INFO "fraud check compensated")))
  (step charge_gateway
    (action     (CALL_SERVICE gateway POST "/charge" (idempotent false)))
    (compensate (CALL_SERVICE gateway POST "/refund" (idempotent false))))
  (step confirm_order
    (action     (CALL_SERVICE order_service PUT "/confirm" (idempotent true)))
    (compensate (CALL_SERVICE order_service PUT "/cancel" (idempotent true)))))
```

Compensación en orden inverso. El compilador valida que todo step tenga compensación.

---

## 13. Eventos Nativos

```
(events
  (define order_created (schema Order))
  (define order_cancelled (schema Order))
  (subscribe order_created on_order_created_handler)
  (subscribe order_cancelled on_order_cancelled_handler))

;; En un handler:
(EMIT_EVENT product_created product)

;; Event handler:
(handler on_order_created_handler
  (type EVENT_HANDLER)
  (event order_created)
  (do ...))
```

Tipados, desacoplados, el runtime los distribuye.

---

## 14. Seguridad Nativa: AuthN + AuthZ

```
(auth
  (provider jwt)
  (secret_ref jwt_secret))

(handler get_user
  (auth PUBLIC))                              ;; sin auth

(handler delete_user
  (auth (requires (role "admin"))))           ;; requiere role admin

(handler create_user
  (auth (requires authenticated)))            ;; requiere estar autenticado
```

**Si no ponés auth en un handler, no compila.** Seguridad by default.

---

## 15. Observabilidad Nativa

### Automática (sin instrucciones explícitas)

- **Tracing**: OpenTelemetry automático en cada handler y CALL_SERVICE
- **Structured logging**: errores con trace_id, fiber_id, request context
- **Audit log**: intención + resultado de cada operación
- **Health checks**: endpoint `/health` auto-generado

### Explícita

```
(LOG INFO "user created: {user.id}")
(LOG WARNING "rate limit near threshold")
(LOG ERROR "failed to update stock: {error}")
```

### Impacto en throughput controlado

- Sampling configurable por ambiente
- Buffer async (nunca bloquea la fiber)
- Producción: solo ERROR + traces muestreados

---

## 16. Rate Limiting

### Local (por instancia) — CORE

```
(rate_limit
  (max_rps 200)
  (burst 50)
  (on_limit ERR_RATE_LIMITED))
```

### Distribuido (Redis) — POST-MVP

```
(rate_limit
  (max_rps 200)
  (distributed
    (provider REDIS)
    (key request.auth.api_key)
    (max_rpm 1000)
    (window SLIDING)))
```

---

## 17. Scheduling

**No es nativo.** Un servicio AILang debe ser rápido para iniciar → ejecutar → finalizar. Encaja con Lambda/Azure Functions/K8s CronJob.

Lo que AILang necesita: un handler que un scheduler externo invoca:

```
(route POST "/jobs/cleanup" handler_cleanup_job)

(handler handler_cleanup_job
  (auth (requires service_account))
  (do
    ;; lógica del job
    (RESPOND 200 (processed count) (duration_ms elapsed))))
```

---

## 18. Arquitectura de Providers

### Core vs Providers

El bytecode tiene primitivas core que siempre están. La conexión con el mundo externo usa **providers** — abstracciones tipadas sobre servicios externos.

### Interfaz STORE (común a todos los providers de datos)

```
STORE<T>:
  LIST_ALL(filters?)          → RESULT(List<T>, Error)
  FIND(key)                   → RESULT(T?, Error)
  INSERT(data)                → RESULT(T, Error)
  UPDATE(key, data)           → RESULT(T, Error)
  UPDATE_FIELDS(key, fields)  → RESULT(T, Error)
  DELETE(key)                 → RESULT(bool, Error)
  QUERY(filter, sort?, limit?) → RESULT(List<T>, Error)
```

Cambiar de PostgreSQL a MongoDB no cambia el bytecode del handler.

### Providers planificados

| Categoría | Providers | Prioridad |
|-----------|-----------|-----------|
| Datos SQL | PostgreSQL, MySQL | Alta |
| Datos NoSQL | MongoDB, CosmosDB, DynamoDB | Alta |
| Cache | Redis | Alta |
| Búsqueda | Elasticsearch | Media |
| Storage | AWS S3, Azure Blob | Media |
| Mensajería | Kafka, RabbitMQ, NATS | Post-MVP |
| Feature Flags | OpenFeature/Flagd | Post-MVP |

### Uso en bytecode

```
(store order_store
  (provider PostgreSQL)
  (connection CONFIG.db_connection)
  (schema Order)
  (table "orders"))

(store cache
  (provider Redis)
  (connection CONFIG.redis_connection)
  (ttl (duration 5 minutes)))
```

---

## 19. Catálogo de Instrucciones (69 instrucciones)

### Variables y Estado (6)

| Instrucción | Descripción |
|-------------|-------------|
| `DEFINE_VAR` | Declara variable con tipo y valor inicial |
| `SET_VAR` | Sobrescribe valor de variable existente |
| `COMPUTE` | Operación aritmética binaria |
| `COMPUTE_FN` | Función built-in (min, max, round, length, etc.) |
| `CONSTRUCT` | Construye objeto tipado desde schema |
| `GET_CURRENT` | Obtiene valores del contexto (legacy, usar CLOCK) |

### Control de Flujo (6)

| Instrucción | Descripción |
|-------------|-------------|
| `BRANCH` | Evaluación condicional (if/else) |
| `MATCH` | Pattern matching (switch) |
| `ITERATE` | Recorrer colección secuencialmente |
| `ITERATE_PARALLEL` | Recorrer colección en paralelo |
| `LOOP` | Loop condicional (while) |
| `COMMENT` | Comentario semántico |

### Colecciones (1)

| `APPEND` | Agrega elemento al final de una lista |

### HTTP (7)

| Instrucción | Descripción |
|-------------|-------------|
| `READ_BODY` | Lee y valida body contra schema → RESULT |
| `READ_PARAM` | Lee path parameter tipado |
| `READ_QUERY` | Lee query parameter tipado |
| `READ_HEADER` | Lee header del request |
| `RESPOND` | Envía response (serializa a JSON) |
| `RESPOND_STATUS` | Envía response con status específico |
| `SET_HEADER` | Setea header en response |

### Store (6)

| Instrucción | Descripción |
|-------------|-------------|
| `STORE_LIST_ALL` | Lista todos los registros |
| `STORE_FIND` | Busca por key → RESULT(T, NONE) |
| `STORE_INSERT` | Inserta registro nuevo |
| `STORE_UPDATE` | Reemplaza registro completo |
| `STORE_UPDATE_FIELDS` | Actualiza campos específicos |
| `STORE_DELETE` | Elimina registro por key |

### Errores (21)

Catálogo completo: NOT_FOUND, ALREADY_EXISTS, VALIDATION, BUSINESS_RULE, PRECONDITION, CONFLICT, UNAUTHORIZED, FORBIDDEN, TOKEN_EXPIRED, TOKEN_INVALID, INVALID_BODY, INVALID_PARAM, MISSING_HEADER, UNSUPPORTED_MEDIA, TIMEOUT, CONNECTION, UNAVAILABLE, RATE_LIMITED, CIRCUIT_OPEN, CUSTOM, PROPAGATE.

### Concurrencia (5)

SPAWN_FIBER, PARALLEL, AWAIT_ALL, CHANNEL, ATOMIC_READ.

### Inter-servicio (2)

CALL_SERVICE (con flag idempotent obligatorio), SAGA_COMPENSATE.

### Eventos (1)

EMIT_EVENT.

### Observabilidad (1)

LOG (con levels ERROR, WARNING, INFO, DEBUG).

### Lógica Inline (1)

TERNARY.

### Configuración (3)

CONFIG.{key}, SECRET_REF, CONFIG_OVERRIDE (testing).

### Fecha/Tiempo (6)

CLOCK.NOW, CLOCK.TODAY, CLOCK.DAY_OF_WEEK, COMPUTE_DATE, PARSE_DATE, FORMAT_DATE.

### Testing (1)

MOCK_CLOCK.

### Infraestructura (2)

RATE_LIMIT, RESET_STORE (testing), RESET_CIRCUIT (testing).

### Resumen

| Categoría | Cantidad |
|-----------|----------|
| Variables y estado | 6 |
| Control de flujo | 6 |
| Colecciones | 1 |
| HTTP | 7 |
| Store | 6 |
| Errores | 21 |
| Concurrencia | 5 |
| Inter-servicio | 2 |
| Eventos | 1 |
| Observabilidad | 1 |
| Lógica inline | 1 |
| Configuración | 3 |
| Fecha/Tiempo | 6 |
| Testing | 1 |
| Infraestructura | 2 |
| **Total** | **69** |

---

## 20. Lo Mejor de Cada Ecosistema

| Feature | Inspiración | Origen |
|---------|-------------|--------|
| Todo async por defecto | async/await | C# .NET 10 |
| Tests funcionales (server up) | TestServer/WebApplicationFactory | C# .NET |
| Tests independientes (setup→run→clean) | Concepto nuevo | — |
| Race detection | go test -race + borrow checker | Go + Rust |
| RESULT obligatorio (no ignorable) | Result<T,E> | Rust |
| Supervisión "let it crash" | Supervision trees | Erlang/OTP |
| Fibers M:N | Goroutines + BEAM processes | Go + Erlang |
| PARALLEL + AWAIT_ALL | Task.WhenAll + WaitGroup | C# + Go |
| Graceful degradation | Concepto nuevo | — |
| DI nativa | Built-in DI | C# .NET |
| Resiliencia obligatoria | Polly + @Retryable | C# .NET 10 + Spring Boot 4 |
| Flag idempotent | Concepto nuevo | — |
| CB con eventos | Concepto nuevo | — |
| Sagas declarativas | Concepto nuevo | — |
| Auth no compila sin definir | Concepto nuevo | — |
| Eventos tipados nativos | @EventListener mejorado | Spring Boot |
| OTel automático | @Observed | Spring Boot 4 |
| Structured logging | Nativo desde SB 3.4 | Spring Boot |
| Config tipada con prioridades | IOptions<T> + appsettings | C# .NET |
| Filesystem secrets | /run/secrets/ | K8s + Docker Swarm |
| CLOCK mockeable | TimeProvider | C# .NET 8+ |
| Validación cross-field | Bean Validation + DataAnnotations | Java + C# (mejorado) |
| Formato S-expression | Lisp/Scheme/Clojure | — |

---

## 21. Decisiones de Diseño Pendientes

- [ ] **Runtime**: ¿nativo propio? ¿LLVM? ¿BEAM? Decisión futura
- [ ] **Modelo de memoria**: ¿GC? ¿Ownership? ¿Híbrido?
- [ ] **Interop**: ¿FFI? ¿WASM?
- [ ] **Transaccionalidad cross-service**: ¿2PC? ¿Sagas + dead letter queue?
- [ ] **GUARD / VALIDATE_CHAIN**: instrucción para evitar BRANCH anidados
- [ ] **Dead letter queue**: para compensaciones de saga que fallan
- [ ] **Hot-reload de config**: estilo IOptionsMonitor<T> (v2)
- [ ] **Feature flags**: provider OpenFeature/Flagd (post-MVP)
- [ ] **Providers de datos**: PostgreSQL, MongoDB, Redis, etc. (post-MVP)
- [ ] **Providers de storage**: S3, Azure Blob (post-MVP)
- [ ] **Providers de mensajería**: Kafka, RabbitMQ, NATS (post-MVP)
- [ ] **Herramientas de auditoría**: decompiler visual, inspector, diff tool
- [ ] **Linter con colores para S-expressions**: herramienta de auditoría

---

## 22. Servicios de Prueba (Stress Test del Formato)

| Servicio | Complejidad | Qué estresa |
|----------|-------------|-------------|
| `product_service` | Baja | CRUD, validación, auth, DI, tests funcionales |
| `order_service` | Media | Inter-service, ITERATE, eventos, custom errors, resiliencia |
| `pricing_service` | Media-Alta | Lógica condicional compleja, MATCH anidado, COMPUTE, reglas de negocio |
| `payment_service` | Alta | Sagas, compensación, idempotent, multi-step async, no-retry |
| `dashboard_service` | Alta | PARALLEL + AWAIT_ALL, graceful degradation, agregación |

---

## 23. MVP

### Alcance

Request HTTP → Procesamiento en fibers → Response HTTP (sin DB, in-memory).

### Estrategia de bootstrapping

La IA construye AILang con la spec de AILang. Dogfooding.

### Transpilador v0.1 existente

Lee bytecode → genera C# y Go funcional. Próximo paso: migrar a S-expressions como input.

---

## 24. Nombre de trabajo

**AILang** (temporal — sujeto a cambio)

---

*Manifiesto v0.7 — Abril 2026*
*Formato: S-expressions*
*69 instrucciones catalogadas*
*Validado con 5 servicios de prueba*
*Análisis comparativo con C# .NET 10, Java Spring Boot 4, Go, Node/NestJS, Rust, Erlang/Elixir*
