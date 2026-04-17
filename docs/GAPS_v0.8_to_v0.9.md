# Transpiler Gaps — v0.8 → v0.9

Catálogo de instrucciones y secciones que los 4 servicios convertidos
(`order`, `pricing`, `payment`, `dashboard`) usan y que el transpilador actual
(v0.8, al 2026-04-16) NO soporta todavía. Los `.sexpr` parsean sintácticamente
pero no transpilan a código compilable en C# .NET 10 / Go 1.25.

## Estado por servicio

| Servicio | Formato previo | S-expr | Transpila? |
|---|---|---|---|
| `product_service` | .sexpr v0.7 | ✅ | ✅ C# + Go (in-memory) |
| `product_service_pg` | .sexpr v0.8 | ✅ | ✅ C# + Go (Npgsql / pgx) |
| `order_service` | .json v0.6 | ✅ (convertido) | ❌ |
| `pricing_service` | .json v0.6 | ✅ (convertido) | ❌ |
| `payment_service` | .json v0.6 | ✅ (convertido) | ❌ |
| `dashboard_service` | .json v0.6 | ✅ (convertido) | ❌ |

---

## 1. Ops de ejecución — falta en el instruction compiler

### Variables locales y expresiones
- **`DEFINE_VAR`** — declarar variable local con tipo y valor inicial.
  Ejemplo: `(DEFINE_VAR total decimal 0)`.
- **`SET_VAR`** — asignar a variable existente.
- **`COMPUTE`** — aritmética binaria: `(COMPUTE left op right → assign)`.
  Operadores: `+ - * / %`. Debe validar tipos (`decimal * int → decimal`).
- **`COMPUTE_FN`** — aplicar función built-in: `round/floor/ceil/min/max/abs`.
- **`TERNARY`** — `(TERNARY cond then else → assign)`.
- **`GET_CURRENT`** — leer contexto actual (auth user, request headers).

### Colecciones
- **`APPEND`** — agregar ítem a lista local.
- **`ITERATE`** / **`ITERATE_PARALLEL`** — loop sobre colección, con body
  anidado. Parallel usa fibers por ítem.

### Concurrencia
- **`PARALLEL`** — ejecutar fibers nombrados en paralelo, agregar resultado.
  Forma: `(PARALLEL → assign (fiber name instructions...) ...)`.
- **`AWAIT_ALL`** — esperar resultado de PARALLEL con timeout.
  Forma: `(AWAIT_ALL source (timeout_ms N))`.
- **`IS_OK`** — chequear si un resultado es OK (no error/timeout).

### Objetos
- **`CONSTRUCT`** — construir instancia de schema a partir de datos.
  Forma: `(CONSTRUCT Schema (field val) ... → assign)`.

### Inter-servicio / Errores
- **`CALL_SERVICE`** — llamada HTTP a otro servicio con resilience-policy aplicada.
  Forma: `(CALL_SERVICE name (method X) (path "/..") (idempotent bool) (body ...) → assign)`.
  El `idempotent` flag determina si el retry aplica.
- **`ERR_CUSTOM`** con `params` dinámicos — usar `custom_errors[].message_template`
  con sustitución de placeholders `{param}`.
- **`SAGA_COMPENSATE`** — invocar compensación definida en `(sagas ...)`.

### Comparadores como ops
- `==`, `!=`, `<`, `>`, `<=`, `>=`, `IN`, `NOT_IN` aparecen como top-level en
  algunos handlers (principalmente dentro de BRANCH). Hoy BRANCH ya los soporta
  vía `condition.op`, pero el converter también los deja como ops estructurados.

---

## 2. Secciones top-level — falta parsear + usar

### `dependencies` (grafo de dependencias)
- Tipos: `STORE`, `SERVICE_REF`, `CLOCK`.
- Para `SERVICE_REF`: `target`, `base_url_ref` (env var), `default`,
  `resilience` (apuntando a un policy por nombre).
- **Impacto C#**: registrar `HttpClient` typed + Polly policy por servicio.
- **Impacto Go**: registrar `http.Client` + wrapper con `sony/gobreaker`+`cenkalti/backoff` o equivalente.

### `resilience.policies`
- `timeout` con `duration_ms` + `on_timeout`.
- `circuit_breaker`: `failure_threshold`, `success_threshold`,
  `half_open_max_calls`, `reset_timeout_ms`, eventos `on_open/on_half_open/on_close`.
- `retry`: `max_attempts`, `backoff` (exponential/linear/fixed),
  `retryable_errors`, `non_retryable_errors`.
- **C# .NET 10**: usar `Polly.Core` con `ResiliencePipelineBuilder`.
- **Go 1.25**: armar wrapper manual o usar `github.com/sony/gobreaker` +
  `github.com/cenkalti/backoff`.

### `resilience.events`
- Definición de eventos que emite el circuit breaker:
  `circuit_open`, `circuit_half_open`, `circuit_closed` con schema de payload.

### `events.definitions` + `events.subscriptions`
- Eventos tipados con schema + handlers suscritos (`EVENT_HANDLER`).
- **MVP in-process**: `ConcurrentQueue` + worker pool en C#, channels en Go.
- **Post-MVP**: bridge a Kafka / RabbitMQ / NATS.

### `sagas`
- Multi-step con `action` + `compensate` por step.
- Si un step falla, compensaciones corren en orden inverso.
- **C#**: orquestador simple con try/catch por step + lista de compensaciones.
- **Go**: mismo patrón con goroutine de orquestación.

### `custom_errors`
- `http_status` + `params` + `message_template`.
- Necesita engine de sustitución `{param}` en el template.
- **Integración**: `ERR_CUSTOM` lo consume.

### `discount_rules` (pricing-specific)
- Reglas con condiciones + acción de descuento.
- Puede ser dominio-específico — considerar si generalizable o ad-hoc.

### `expressions` (compile-time spec)
- Spec del mini-lenguaje para `COMPUTE`: operadores soportados, funciones,
  reglas de tipos.
- **No es ejecutable** — es input para el validador/type-checker.

### Handlers tipo `EVENT_HANDLER`
- `(handler X (type EVENT_HANDLER) (event name) (do ...))`.
- No son route-handlers — el runtime los invoca cuando se emite el evento.
- Requieren acceso a `event.data` (payload del evento).

### `tests` (legacy format)
- **Phase 8 (2026-04-16):** Generator implementado para el formato v0.7 (`(test "name" (setup) (run ...) (clean ...))`).
  - C#: `{Service}/Tests/ApiTests.cs` con `WebApplicationFactory<Program>` + xUnit + un `[Fact]` por test. Factory fresh por test para aislamiento total.
  - Go: `{Service}/main_test.go` con `httptest.NewServer(buildHandler())`.
  - Seams: C# expone `public partial class Program { }`; Go expone `func buildHandler() http.Handler`. Los stores in-memory exponen `Reset()` para `RESET_STORE` en `(clean ...)`.
  - Cubre REQUEST (con auth/body), ASSERT (status/nested fields/`exists`/`length >= N`), CAPTURE con interpolación de path (`{var}`).
  - Bonus: fix al contrato de `READ_BODY` — ahora llama `body.Validate()` automáticamente tanto en C# como Go (antes solo existía el método en Go y jamás se invocaba).
- **Pendiente:** formato legacy `(tests (cases (item (name X) (setup (mock_services ...)) (run (item (action REQUEST) ...)))))` que usan order/pricing/payment/dashboard — requiere parser separado en `_parse_tests`.
- PostgreSQL-backed services quedan fuera del MVP (necesitan testcontainers/DB real).

---

## CALL_SERVICE: representación del resultado

**Decisión v0.9 (2026-04-16):** Phase 3 usa `JsonNode?` en C# (no `dynamic`).

| Opción | Compile-safe | AOT-safe | Overhead | Cuándo |
|---|---|---|---|---|
| ~~`dynamic`~~ | ❌ | ❌ (.NET 8+ AOT bloquea) | Alto (DLR) | Nunca en 2026 |
| **`JsonNode?`** | ✅ indexer | ✅ | Bajo | **Default** para CALL_SERVICE con shape desconocida |
| `JsonElement?` | ✅ | ✅ | Bajo | Read-only; requiere `.HasValue` |
| DTO tipado | ✅✅ | ✅ | Mínimo | Futuro: cuando CALL_SERVICE declare schema en bytecode |

**Phase 3b (2026-04-16 — ✅ implementado)**: el codegen ahora trackea vars CALL_SERVICE-asignadas como `JsonNode`/`map[string]any` en `ctx['locals']`. Los call sites de COMPUTE / BRANCH / CONSTRUCT / SET_VAR / MATCH delegan en `_cs_expr` / `_go_expr`, que detectan raíces JsonNode-typed y emiten indexer access (`root["k"]!.GetValue<T>()` en C#, `root["k"].(T)` en Go). El tipo T se infiere del operando escalar (en comparaciones), de la constante numérica por default en aritmética (`decimal`), o del tipo de campo del schema en CONSTRUCT. Validado con `scratch_phase3b.sexpr` — mock HTTP server devuelve `{"id","price","active"}`, el handler computa `price * quantity`, y la respuesta tipada sale con los valores correctos (smoke test en Go).

**Patrón de acceso post-OK** (ejemplo real):
```csharp
JsonNode? product_result = await CallServiceAsync(...);
if (product_result is null) return ERR;
decimal price = product_result["price"]!.GetValue<decimal>();
string name  = product_result["name"]?.GetValue<string>() ?? "";
// o deserializar a DTO conocido:
var dto = product_result.Deserialize<ProductDto>();
```

En Go se mantiene `map[string]any` (idiom estándar, no hay alternativa cleaner).

---

## 3. Pendientes conocidos post-Phase 3b

### dashboard_service (parse-only, no builda)
El parse-time está desbloqueado (commit dc1a4e0), pero construir el service requiere varios gaps de instrucción aparte de Phase 3b:
- **PARALLEL result plumbing**: los locals dentro de un fiber (p.ej. `products_response`) no fluyen al scope externo tras `AWAIT_ALL`. El acceso correcto es `parallel_results.fetch_products` — requiere reescribir los usos siguientes.
- **ITERATE sobre JsonNode/map**: `(ITERATE products_response p (COMPUTE p.price * p.stock → …))` necesita que el iterador `p` herede el tipo JsonNode del collection (ahora queda como `object`), y que el wrapper emita `.AsArray()!` en C# / `.([]any)` en Go.
- **SET_VAR con object literal**: `(SET_VAR products_section "{'total_count': …}")` emite el Python dict repr como string literal — hace falta soportar `(SET_VAR name (object (k v) …))` o similar.
- **DEFINE_VAR con default object**: mismo problema — el default viene como string.
- **MATCH con arms enteros**: `(MATCH services_up (3 …) (2 …) …)` no genera un switch; cae en el camino RESULT-style.
- **COMPUTE widening int↔decimal**: `decimal_field * int_field` falla en Go (`mismatched types float64 and int`). C# lo tolera gracias a conversiones implícitas.
- **GET_CURRENT dispatch**: hoy `(GET_CURRENT auth user → now)` + `(CONSTRUCT … (generated_at now) …)` emite `now = ctx.User.FindFirst("sub")?.Value`, aunque la intención era `CLOCK.NOW`. Hay que mapear el par `(from field)` a las operaciones correctas.

### pricing_service, payment_service, order_service
Gaps propios documentados en memoria (topic_key `legacy/per-service-codegen-gaps`); ninguno bloqueado solamente por Phase 3b.


## 3. Orden de prioridad recomendado para v0.9

1. **Variables y expresiones** (DEFINE_VAR, SET_VAR, COMPUTE, CONSTRUCT, APPEND, ITERATE, GET_CURRENT, TERNARY, IS_OK). Base para todo lo demás. Complejidad: media.
2. **`custom_errors` + `ERR_CUSTOM`** con template sustitución. Complejidad: baja. Desbloquea errores de negocio.
3. **`dependencies` + `CALL_SERVICE` sin resilience** — bridge HTTP simple. Complejidad: baja-media.
4. **`resilience.policies` + wrap CALL_SERVICE** con Polly/gobreaker. Complejidad: alta. Es el diferencial de AILang.
5. **`events` + `EVENT_HANDLER`** in-process. Complejidad: media.
6. **`sagas`** con compensación. Complejidad: media-alta.
7. **`PARALLEL`/`AWAIT_ALL`/`ITERATE_PARALLEL`** con fibers. Complejidad: alta (pero .NET/Go lo hacen fácil).
8. **Generador de tests** (xUnit + Go test) desde `(tests ...)`. Complejidad: media.

Implementando 1-3 se desbloquea `pricing_service` (el más simple).
Implementando 1-5 se desbloquea `order_service`.
Implementando 1-6 se desbloquea `payment_service`.
Implementando 1-7 se desbloquea `dashboard_service`.
