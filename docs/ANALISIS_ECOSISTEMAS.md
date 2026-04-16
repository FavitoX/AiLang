# AILang — Análisis de Ecosistemas: ¿Qué ya no tiene sentido reinventar?

> "Quiero escribir negocio, no escribir un circuit breaker."

## Objetivo

Analizar qué resuelven C# .NET, Java/Spring, Go, Node/NestJS, Rust y Erlang/Elixir para determinar qué debería ser **core** en AILang (no opcional, no librería, no framework).

---

## 1. Matriz Comparativa: Built-in vs Librería vs Manual

Clasificación:
- 🟢 **Nativo**: viene con el lenguaje/framework, no hay que instalar nada extra
- 🟡 **Librería oficial/estándar**: paquete externo pero del ecosistema oficial
- 🟠 **Librería comunidad**: paquete de terceros, calidad variable
- 🔴 **Manual**: lo tenés que construir vos

### HTTP Server + Routing

| Feature | C# .NET 10 | Java Spring Boot 4 | Go stdlib | Node/NestJS | Rust (Axum) | Erlang/Elixir (Phoenix) | **AILang** |
|---------|-----------|-------------------|-----------|-------------|-------------|----------------------|------------|
| HTTP Server | 🟢 Kestrel | 🟢 Embedded Tomcat | 🟢 net/http | 🟢 Express/Fastify | 🟡 Axum/Actix | 🟢 Cowboy | 🟢 Nativo |
| Routing | 🟢 Minimal APIs | 🟢 @RequestMapping | 🔴 Manual o 🟠 Chi/Mux | 🟢 Decorators | 🟡 Router | 🟢 Router | 🟢 Nativo |
| CORS | 🟢 builder.Services | 🟢 @CrossOrigin | 🔴 Manual | 🟡 cors package | 🟡 tower-http | 🟢 Plug | 🟢 Nativo + auto-OPTIONS |
| Content negotiation | 🟢 Automático | 🟢 Automático | 🔴 Manual | 🟠 Varía | 🟠 Manual | 🟢 Automático | 🟢 Nativo |
| Request validation | 🟢 DataAnnotations | 🟢 Bean Validation | 🔴 Manual | 🟡 class-validator | 🟠 Manual | 🟡 Changeset | 🟢 Desde schema |

### Dependency Injection

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node/NestJS | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|-------------|------|---------------|------------|
| DI Container | 🟢 Nativo | 🟢 Spring IoC | 🔴 Manual (wire/dig) | 🟢 Nativo en Nest | 🔴 No existe | 🟡 Supervision trees | 🟢 Nativo |
| Auto-wiring | 🟢 Sí | 🟢 @Autowired | 🔴 No | 🟢 Sí | 🔴 No | 🟡 Parcial | 🟢 Automático |

### Concurrencia

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|------|------|---------------|------------|
| Async nativo | 🟢 async/await | 🟢 Virtual Threads | 🟢 goroutines | 🟢 Event loop | 🟢 async/await | 🟢 Procesos BEAM | 🟢 Todo es async |
| Paralelismo real | 🟢 Task.WhenAll | 🟢 CompletableFuture | 🟢 goroutines | 🔴 Workers | 🟢 tokio::spawn | 🟢 Procesos | 🟢 PARALLEL+AWAIT_ALL |
| Fibers/Green threads | 🟡 Tasks | 🟢 Virtual Threads (21+) | 🟢 Goroutines | 🔴 No | 🟡 tokio tasks | 🟢 Procesos (~300B) | 🟢 Fibers nativas |
| Channels | 🟢 Channel<T> | 🟡 BlockingQueue | 🟢 chan | 🔴 No nativo | 🟢 mpsc/broadcast | 🟢 Mailbox | 🟢 CHANNEL |
| Race detection | 🔴 No nativo | 🔴 No nativo | 🟢 go -race | 🔴 No | 🟢 En compilación | 🟢 Por diseño | 🟢 En compilador |

### Resiliencia

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node/NestJS | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|-------------|------|---------------|------------|
| Retry | 🟢 Microsoft.Extensions.Resilience | 🟢 @Retryable nativo | 🔴 Manual | 🟠 Librerías | 🟠 Librerías | 🟢 Supervisor restart | 🟢 Nativo + obligatorio |
| Circuit breaker | 🟢 Polly integrado | 🟢 Nativo (SB4) | 🔴 Manual | 🟠 opossum | 🟠 Librerías | 🟢 Por diseño | 🟢 Nativo + obligatorio |
| Timeout | 🟢 Nativo | 🟢 Nativo | 🟢 context.WithTimeout | 🟢 AbortController | 🟢 tokio::timeout | 🟢 Nativo | 🟢 Nativo + obligatorio |
| Fallback | 🟢 Polly | 🟢 @Retryable fallback | 🔴 Manual | 🟠 Varía | 🔴 Manual | 🟢 Supervisor | 🟢 Nativo |
| Idempotency control | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🟢 Flag idempotent |

### Manejo de Errores

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|------|------|---------------|------------|
| Modelo de errores | 🟡 Excepciones | 🟡 Excepciones | 🟢 error como valor | 🟡 Excepciones | 🟢 Result<T,E> | 🟢 {:ok}/{:error} | 🟢 RESULT obligatorio |
| Error no ignorable | 🔴 Se puede ignorar | 🔴 Se puede ignorar | 🔴 Se puede ignorar | 🔴 Se puede ignorar | 🟢 Compiler enforce | 🟡 Advertencia | 🟢 No compila |
| Supervisión/recovery | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🟢 Supervision trees | 🟢 Nativo (Erlang-style) |
| Error → HTTP status | 🟠 Manual/middleware | 🟡 @ExceptionHandler | 🔴 Manual | 🟠 Filters | 🟠 Manual | 🟡 Plug | 🟢 Mapeo automático |

### Testing

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node/NestJS | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|-------------|------|---------------|------------|
| Test runner | 🟢 dotnet test | 🟢 JUnit/Maven | 🟢 go test | 🟡 Jest/Vitest | 🟢 cargo test | 🟢 ExUnit | 🟢 Nativo |
| Test funcional (server up) | 🟢 TestServer/WebAppFactory | 🟢 @SpringBootTest | 🟢 httptest | 🟡 supertest | 🟠 Varía | 🟢 ConnCase | 🟢 Nativo |
| Mocking de servicios | 🟡 Moq/NSubstitute | 🟡 Mockito | 🔴 Manual/interfaces | 🟡 Jest mocks | 🟠 mockall | 🟡 Mox | 🟢 Declarativo en bytecode |
| Race detector en tests | 🔴 No | 🔴 No | 🟢 go test -race | 🔴 No | 🟢 Compile-time | 🟢 Por diseño | 🟢 En compilador |
| Test independencia | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🔴 Manual | 🟢 setup→run→clean nativo |

### Observabilidad

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|------|------|---------------|------------|
| Tracing (OpenTelemetry) | 🟡 Paquetes OTel | 🟢 @Observed nativo | 🟠 OTel SDK | 🟠 OTel SDK | 🟠 OTel SDK | 🟡 telemetry lib | 🟢 Automático |
| Structured logging | 🟡 Serilog | 🟢 Nativo (SB 3.4+) | 🟡 zap/zerolog | 🟡 pino/winston | 🟡 tracing crate | 🟢 Logger nativo | 🟢 Automático |
| Métricas | 🟡 Prometheus client | 🟢 Micrometer/Actuator | 🟠 Librerías | 🟠 Librerías | 🟠 Librerías | 🟡 telemetry | 🟢 Automático |
| Health checks | 🟢 Nativo | 🟢 Actuator /health | 🔴 Manual | 🟠 Terminus | 🔴 Manual | 🟡 Plug | 🟢 Automático |

### Seguridad

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node/NestJS | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|-------------|------|---------------|------------|
| Auth (JWT/OAuth) | 🟢 Nativo | 🟢 Spring Security | 🟠 Librerías | 🟡 Passport | 🟠 Librerías | 🟡 Guardian | 🟢 Declarativo |
| Authorization (RBAC) | 🟢 [Authorize] | 🟢 @PreAuthorize | 🔴 Manual | 🟡 Guards | 🔴 Manual | 🟡 Plugs | 🟢 REQUIRES nativo |
| Auth obligatorio | 🔴 Se puede olvidar | 🔴 Se puede olvidar | 🔴 Se puede olvidar | 🔴 Se puede olvidar | 🔴 Se puede olvidar | 🔴 Se puede olvidar | 🟢 No compila sin auth |

### Event-Driven

| Feature | C# .NET 10 | Java Spring Boot 4 | Go | Node | Rust | Erlang/Elixir | **AILang** |
|---------|-----------|-------------------|------|------|------|---------------|------------|
| Eventos in-process | 🟡 MediatR | 🟢 ApplicationEvent | 🔴 Manual/channels | 🟢 EventEmitter | 🔴 Manual | 🟢 GenServer/PubSub | 🟢 EMIT_EVENT nativo |
| Suscripciones | 🟡 MediatR handlers | 🟢 @EventListener | 🔴 Manual | 🟢 .on() | 🔴 Manual | 🟢 handle_info | 🟢 EVENT_HANDLER nativo |
| Eventos tipados | 🟡 Depende | 🟢 Tipados | 🔴 No | 🔴 No | 🟡 Depende | 🟡 Parcial | 🟢 Schema nativo |

---

## 2. Conclusiones: ¿Qué debería ser Core en AILang?

### Categoría A: "Ya todos lo tienen, solo hay que hacerlo bien"

Cosas que la mayoría de los ecosistemas enterprise ya resolvieron. AILang debe tenerlas nativas, sin discusión:

| Feature | Quién lo hace bien | Qué tomar |
|---------|-------------------|-----------|
| HTTP Server + Routing | Todos | Routing declarativo, verbos HTTP, path params tipados |
| DI | C# .NET, Spring, NestJS | Auto-wiring del grafo de dependencias |
| Async/Concurrencia | Go, Erlang, C# | Todo async by default, fibers modelo M:N |
| Testing funcional | C# TestServer, Go httptest | Server up para tests, sin frameworks externos |
| JWT/Auth | C# .NET, Spring Security | Declarativo, un solo lugar de configuración |
| CORS | Todos | Declarativo, OPTIONS auto-generado |
| JSON serialization | Todos | Desde schema, automático |

### Categoría B: "La industria converge, pero aún hay fricción"

Cosas que los ecosistemas están incorporando como nativas ahora (2024-2026), pero que hace 2-3 años eran "librerías opcionales". AILang debe tenerlas nativas desde el día 1:

| Feature | Tendencia 2024-2026 | Evidencia |
|---------|---------------------|-----------|
| Resiliencia (retry, CB, timeout) | C# integró Polly en .NET 8. Spring Boot 4 lo hizo nativo con @Retryable. | La industria decidió: esto no es opcional |
| OpenTelemetry / Tracing | Spring Boot 4: @Observed nativo. C# .NET: OTel packages. | Observabilidad es infraestructura, no feature |
| Structured Logging | Spring Boot 3.4 lo hizo nativo. C# con Serilog semi-estándar. | Logs no estructurados ya no sirven |
| Health checks | C# y Spring los tienen nativos | Kubernetes lo espera, no es opcional |

### Categoría C: "AILang innova donde nadie lo tiene"

Cosas que **ningún ecosistema** resuelve bien y donde AILang puede diferenciarse:

| Feature | Problema actual | Solución AILang |
|---------|-----------------|-----------------|
| Auth obligatorio por handler | En TODOS los lenguajes podés olvidar poner auth. Es un bug de seguridad silencioso. | No compila sin PUBLIC o REQUIRES |
| Errores no ignorables | Solo Rust lo enforce. Go, C#, Java, Node permiten ignorar errores. | RESULT sin MATCH = no compila |
| Mapeo error → HTTP automático | En todos es manual (middleware, exception handler, etc.) | Catálogo nativo con mapeo built-in |
| Flag idempotent en calls | NADIE tiene esto. Es conocimiento tribal que vive en la cabeza del dev. | Obligatorio declarar en CALL_SERVICE |
| Resiliencia obligatoria | Incluso C# y Spring la tienen *disponible* pero *opcional*. Podés no usarla. | No compila sin resilience policy |
| Eventos tipados nativos | Spring tiene @EventListener pero es interno. Kafka/RabbitMQ son externos. | EMIT_EVENT + EVENT_HANDLER nativos |
| Tests con setup→run→clean | Todos dicen "hacelo así" pero ninguno lo enforce. | Estructura obligatoria en el bytecode |
| Circuit breaker emite eventos | Polly/Resilience4j loguean. No emiten eventos que otros servicios consuman. | Eventos nativos de CB: open/half-open/close |
| Sagas declarativas | No existe en ningún lenguaje. Se construyen a mano o con Temporal/MassTransit. | SAGA con compensación en el bytecode |
| Graceful degradation pattern | Siempre manual. "Si service X falla, mostrá datos parciales." | PARALLEL + MATCH ERR → degraded nativo |

---

## 3. Lo que Go hace bien (y mal)

**Bien:**
- `go test` es rápido y directo — no necesitás JUnit, xUnit, ni nada
- `go test -race` detecta race conditions en runtime — nadie más tiene esto tan fácil
- Goroutines son brillantes — concurrencia accesible para cualquier dev
- Channels son primitivas del lenguaje
- Compila a un solo binario sin dependencias
- `context.WithTimeout` es elegante para timeouts

**Mal (lo que AILang no debe repetir):**
- DI no existe — terminás con `main.go` de 200 líneas construyendo y pasando objetos
- Resiliencia: todo manual, librería por librería (go-retryablehttp, gobreaker, etc.)
- Error handling: `if err != nil` infinito, y podés ignorar errores
- No hay framework web estándar — Chi, Mux, Gin, Echo, Fiber... cada proyecto elige uno distinto
- No hay eventos nativos
- No hay auth estándar
- Observabilidad: OTel SDK pero todo manual

---

## 4. Lo que C# .NET hace bien (y mal)

**Bien:**
- DI nativa de primera clase
- `async/await` maduro y bien integrado
- Resiliencia ahora integrada (Microsoft.Extensions.Resilience sobre Polly)
- TestServer/WebApplicationFactory para tests funcionales — excelente
- Minimal APIs simplifican muchísimo
- BackgroundService para workloads async
- Structured logging con Serilog como semi-estándar

**Mal (lo que AILang no debe repetir):**
- Excepciones como modelo de error — podés ignorar errores, catch vacíos, etc.
- Auth se puede olvidar — [Authorize] es un atributo que podés no poner
- Resiliencia es *disponible* pero *opcional* — no hay enforcement
- Boilerplate en controllers, DTOs, mappings — aunque Minimal APIs mejoraron mucho
- Ecosystem pesado: NuGet packages, MSBuild, etc.

---

## 5. Lo que Java/Spring hace bien (y mal)

**Bien:**
- Spring Boot 4 con resiliencia nativa (@Retryable) — llegó al mismo lugar que C#
- @Observed para observabilidad — un annotation y tenés tracing
- Spring Security es muy completo (quizás demasiado)
- @EventListener para eventos in-process
- Actuator para health checks y métricas
- Virtual Threads (Java 21+) — el equivalente a goroutines
- Ecosistema maduro y documentado

**Mal (lo que AILang no debe repetir):**
- Demasiada magia con annotations — difícil de debuggear
- Startup lento (aunque GraalVM native mejora esto)
- Boilerplate aún significativo (aunque menos que antes)
- Excepciones como modelo de error
- Config XML/YAML complejo
- Auth se puede olvidar igual que en C#

---

## 6. Lo que Erlang/Elixir hace bien (y mal)

**Bien:**
- Supervision trees — el modelo de "let it crash" que AILang adopta
- Procesos ultra-livianos (~300 bytes) — lo más cercano a lo que queremos con fibers
- Pattern matching nativo — {:ok, result} / {:error, reason}
- Hot code reload — actualizar código sin bajar el servicio
- Diseñado para 99.999% uptime (telecom)
- Concurrencia es el default, no un add-on

**Mal:**
- Curva de aprendizaje alta
- Ecosistema más chico
- Performance en CPU-bound (no es su fuerte)
- Tipado dinámico (Elixir con Dialyzer ayuda pero no es compile-time)

---

## 7. Tabla Resumen: Primitivas Core de AILang

Basado en el análisis, estas son las primitivas que deben ser **core e inamovibles**:

| # | Primitiva | Justificación | Inspiración |
|---|-----------|---------------|-------------|
| 1 | HTTP Server + Routing declarativo | Todos lo tienen, es tabla stakes | C# Minimal APIs |
| 2 | DI nativa con auto-wiring | C#, Spring, NestJS lo demostraron | C# .NET |
| 3 | Todo async + Fibers M:N | Go y Erlang lo probaron | Go goroutines + Erlang BEAM |
| 4 | RESULT type obligatorio (no ignorable) | Solo Rust lo tiene. El resto permite ignorar errores | Rust Result<T,E> |
| 5 | Supervisión "let it crash" | Solo Erlang lo tiene nativo. Los demás mueren | Erlang/OTP |
| 6 | Resiliencia obligatoria (timeout+CB+retry) | C# y Spring la agregaron como nativa en 2024-2025. AILang la hace obligatoria | C# Polly + Spring @Retryable |
| 7 | Flag idempotent en calls | NADIE lo tiene. Es conocimiento tribal | Concepto nuevo |
| 8 | Mapeo error → HTTP automático | Todos lo hacen manual | Concepto nuevo |
| 9 | Auth obligatorio (no compila sin) | NADIE lo enforcea en compilación | Concepto nuevo |
| 10 | Eventos tipados nativos | Spring tiene parcial. El resto es externo | Spring @EventListener (mejorado) |
| 11 | Testing funcional con server up | C# TestServer, Go httptest | C# WebApplicationFactory |
| 12 | Tests independientes (setup→run→clean) | Nadie lo enforce | Concepto nuevo |
| 13 | OpenTelemetry automático | Spring Boot 4 con @Observed. Pero sigue siendo opt-in | Spring @Observed (pero obligatorio) |
| 14 | Structured logging automático | Spring Boot 3.4+ nativo | Spring Boot structured logging |
| 15 | Circuit breaker con eventos | Nadie emite eventos consumibles por otros servicios | Concepto nuevo |
| 16 | Sagas declarativas | No existe nativo en ningún lenguaje | Concepto nuevo |
| 17 | Validación desde schema | Spring Bean Validation, C# DataAnnotations. Pero siempre manual | Ambos (mejorado) |
| 18 | CORS declarativo + OPTIONS auto | Todos lo tienen pero requiere config | Simplificación |
| 19 | Graceful degradation nativa | Nadie. Siempre manual | Concepto nuevo |
| 20 | Race detection en compilador | Go lo tiene en runtime. Rust en compile. AILang en compilador | Go -race + Rust borrow checker |

---

## 8. La conclusión fundamental

Mirando la tabla, hay un patrón claro: **la industria está convergiendo hacia hacer nativas las cosas que antes eran librerías**. C# integró Polly. Spring Boot 4 integró Resilience4j. Ambos agregaron OTel. Ambos mejoraron structured logging.

Pero ninguno dio el paso que AILang propone: **hacer obligatorio lo que hoy es opcional**.

- En C# *podés* usar resiliencia. En AILang *tenés que* usarla.
- En Spring *podés* poner @Observed. En AILang el tracing es automático.
- En todos *podés* olvidar el auth. En AILang no compila sin él.

Esa es la diferencia fundamental: **AILang no te deja construir software frágil**. No por restricción artificial, sino porque las cosas que hoy son "buenas prácticas opcionales" son en realidad **requisitos de cualquier sistema enterprise serio**.

Nadie en producción dice "no necesito circuit breaker" o "no necesito tracing". Simplemente a veces se olvidan de implementarlo. AILang elimina esa categoría de olvidos.

---

*Documento de análisis v1.0 — Abril 2026*
*Complementario al Manifiesto AILang v0.6*
