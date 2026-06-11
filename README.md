# AILang MVP — Proof of Concept

> Transpilación del bytecode AILang a C# y Go funcional.

## Estructura

```
ailang/
├── bytecode/
│   └── product_service.json    ← Bytecode AILang (la "fuente de verdad")
├── output-csharp/
│   ├── Program.cs              ← Servicio transpilado a C# (.NET 8)
│   ├── ProductService.csproj
│   └── Tests/
│       └── ProductServiceTests.cs  ← Tests funcionales (TestServer)
├── output-go/
│   ├── main.go                 ← Servicio transpilado a Go
│   ├── main_test.go            ← Tests funcionales (httptest)
│   └── go.mod
└── README.md
```

## Transpilar

```bash
# Transpilar a C# y Go
python transpiler.py product_service.sexpr --target all

# Solo C#
python transpiler.py product_service.sexpr --target csharp

# Solo Go
python transpiler.py product_service.sexpr --target go

# NativeAOT opt-in: emit csproj with <PublishAot>true</PublishAot>
# and a source-generated JSON context. Only valid with --target csharp.
python transpiler.py product_service.sexpr --target csharp --aot
```

The `--aot` flag is opt-in. Without it, the C# codegen emits the same project layout as before — classic JIT, reflection-based JSON, standard publish. With `--aot`, the emitted csproj enables `<PublishAot>true</PublishAot>` and a source-generated `JsonSerializerContext`; `dotnet publish -c Release -r <rid>` produces a single self-contained native binary (~5–20 MB) that boots in milliseconds.

**Error responses**: AILang's generated C# services emit RFC 7807 `application/problem+json` responses for every error path. The wire shape uses URIs like `https://ailang.dev/errors/not-found` and includes a `traceId` extension for correlation with structured logs. Validation errors are collected by `Validator.TryValidateObject` (wrapped in an AOT-safe helper) and returned as `ValidationProblemDetails` — all violations in one response, keyed by field name.

## Cómo correr

### C# (.NET 8+)

```bash
cd output-csharp

# Restaurar paquetes y correr
dotnet restore
dotnet run

# El servicio arranca en http://localhost:8080
# Tests:
dotnet test
```

### Go

```bash
cd output-go

# Descargar dependencias y correr
go mod tidy
go run main.go

# El servicio arranca en http://localhost:8080
# Tests:
go test -v
# Con race detector:
go test -race -v
```

## Probar manualmente

```bash
# 1. Obtener token (C#)
curl -X POST http://localhost:8080/auth/token
# Copiar el token devuelto

# 2. Crear producto
curl -X POST http://localhost:8080/products \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d '{"name": "Widget", "price": 9.99, "stock": 10}'

# 3. Listar productos
curl http://localhost:8080/products

# 4. Obtener por ID
curl http://localhost:8080/products/<ID>

# 5. Intentar eliminar con stock (debe fallar con 422)
curl -X DELETE http://localhost:8080/products/<ID> \
  -H "Authorization: Bearer <TOKEN>"

# 6. Crear sin auth (debe fallar con 401)
curl -X POST http://localhost:8080/products \
  -H "Content-Type: application/json" \
  -d '{"name": "NoAuth", "price": 1, "stock": 0}'
```

## Qué demuestra este POC

1. **El bytecode es la fuente de verdad**: `product_service.json` define todo el servicio
2. **Transpilación funcional**: el mismo bytecode genera C# y Go equivalentes
3. **Modelo de errores nativo**: errores tipados con mapeo automático a HTTP status
4. **Auth declarativo**: PUBLIC vs REQUIRES auth definido por handler
5. **Tests generados desde el bytecode**: los mismos tests se transpilan a cada lenguaje
6. **CORS y OPTIONS auto-generados**: declarados una vez, aplicados a todo
7. **Tracing nativo**: cada request tiene trace_id automático
8. **DI nativa**: el store se inyecta sin configuración manual
9. **Validación desde schema**: constraints del schema se convierten en validación

## Lo que falta para un transpilador real

- [ ] Parser real del bytecode (hoy es transpilación manual conceptual)
- [ ] Generación automática de código desde el JSON
- [ ] Soporte para ITERATE / ITERATE_PARALLEL
- [ ] Eventos nativos (hoy son console.log)
- [ ] Supervisión de fibers (Erlang-style)
- [ ] Compilación a binario nativo (LLVM backend)
