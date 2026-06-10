# AiLangError → RFC 7807 ProblemDetails migration; .NET 10 AddValidation for field-level + IValidatableObject for cross-field

**Date:** 2026-06-10
**Author:** Brainstormed with Claude (AILang project)
**Status:** Approved by user; pending spec review

---

## 1. Context

The 2026-04-27 spec (`docs/superpowers/specs/2026-04-27-cs-primary-go-frozen-design.md`) committed C# to be the primary AILang target and put NativeAOT on the implementation path. §6 item 2 of that spec named the next investigation: **".NET 10 Minimal API enhanced validation — evaluate whether built-in validation can replace per-DTO `Validate()` emission."**

The investigation widened during brainstorming. The framing changed from *"can we drop the hand-rolled Validate() method?"* to *"should AILang adopt the industry-standard error response shape (RFC 7807 ProblemDetails) instead of maintaining its own `AiLangError` envelope?"* — because AILang's stated philosophy in CLAUDE.md is "bake in production patterns, don't reinvent libraries that already exist".

`AiLangError` is exactly the kind of in-house reinvention the philosophy says to avoid:
- Every modern HTTP client library (HttpClient, Refit, RestSharp, axios with adapters) parses ProblemDetails natively.
- OpenAPI generators describe ProblemDetails responses out-of-the-box.
- ASP.NET Core's `AddValidation()` (.NET 10) and `AddProblemDetails()` produce ProblemDetails by default — staying with `AiLangError` means fighting the framework via `IProblemDetailsService` overrides.

This spec migrates AILang's C# codegen from `AiLangError` to ProblemDetails for the **entire** error catalog, and replaces the hand-rolled `Validate()` method with .NET 10's source-generated `AddValidation()` + DataAnnotations attributes. Cross-field validation (Nivel 2 in CLAUDE.md) is implemented as part of this spec via `IValidatableObject` so the codegen handles all current AILang validators.

## 2. Decision

**Adopt RFC 7807 ProblemDetails as the wire shape for every error response from C# generated services.** Replace the `AiLangError`/`ErrorDetail` records with a static `AiLangProblems` helper that returns `IResult` instances via `Results.Problem(...)` / `Results.ValidationProblem(...)`.

**Adopt .NET 10's `AddValidation()` for field-level validation.** DTOs are decorated with `System.ComponentModel.DataAnnotations` attributes (`[Required]`, `[StringLength]`, `[MinLength]`, etc.). Strict bounds that DataAnnotations doesn't ship natively (`gt`, `lt`) are covered by custom attributes emitted once per service. The hand-rolled `Validate()` method on each DTO is removed.

**Implement cross-field validation (CLAUDE.md Nivel 2) via `IValidatableObject`** so the AILang validator catalog (`COMPARE`, `NOT_EQUAL`, `CONDITIONAL_REQUIRED`, `MUTUAL_EXCLUSIVE`, `AT_LEAST_ONE`, `SUM_MAX`) is finally usable in generated services. Today's bytecode shows zero services using these, but the codegen path exists so the next service that needs them works.

**Migrate all 6 reference services + smoke script in one pass.** No backwards-compat flag, no `--legacy-errors`. The breaking change is bundled.

**Go target is not migrated.** Per the 2026-04-27 spec it is frozen — Go services keep emitting `AiLangError`. Cross-target wire divergence is accepted.

### Why now

- The NativeAOT path (merged at `22eef1d`) is the prerequisite — `AddValidation()` is source-generator-driven and AOT-friendly. The wiring (`ConfigureHttpJsonOptions`, source-gen JsonContext) is already in place.
- The error model is touched by 9 codegen sites in `transpiler.py` (covered during Task 5 of the NativeAOT path). Doing this migration on top of that work is cheap; doing it later would mean revisiting all 9 sites again.
- Postponing keeps the in-house `AiLangError` shape as a soft commitment that gets harder to walk back the more services depend on it.

## 3. Scope

### In

- `transpiler.py`'s C# codegen:
  - DTO emission: DataAnnotations attributes for field-level rules + `IValidatableObject` when cross-field rules are present.
  - Removal of the hand-rolled `Validate()` method per DTO.
  - `AddValidation()` + `AddProblemDetails(opts => { CustomizeProblemDetails = ... traceId injection ... })` service registration.
  - csproj: `<InterceptorsNamespaces>` property required by `AddValidation()`.
  - Replace `AiLangError` / `ErrorDetail` records with a static `AiLangProblems` helper class and a custom-attributes file (`GreaterThanAttribute`, `LessThanAttribute`, `GreaterThanOrEqualAttribute`, `LessThanOrEqualAttribute`).
  - Cross-field codegen via `IValidatableObject.Validate(ValidationContext)` for the 6 AILang Nivel 2 operators.
- Update `generate_csharp_tests` to use `ReadFromJsonAsync<ProblemDetails>()` + typed `Assert.*` instead of substring matching on response bodies.
- Regenerate all 6 services (order, pricing, payment, dashboard, product in-memory, product PG) with the new codegen.
- Update `bytecode/scripts/aot-smoke-product-service.sh`: the two `"type":"NOT_FOUND"` substring checks become URI substring checks against `"errors/not-found"`.
- Update `_aot_json_types(bc)` to remove `AiLangError` / `ErrorDetail` and add whatever framework types (if any) need explicit registration under AOT.
- Update CLAUDE.md §Validación and README to reflect the new error model.

### Out

- **Go target.** Frozen per the 2026-04-27 spec. Go services keep emitting `AiLangError`. Inter-target wire divergence is accepted; today no service does cross-target CALL_SERVICE.
- **Backwards-compat flag** (`--legacy-errors` or similar). Breaking change is bundled cleanly.
- **i18n / multi-language error messages.** Out of scope.
- **Hot-reload of validation rules.** Out of scope.
- **Changes to the AILang bytecode.** The schema vocabulary (`required`, `min`, `max`, `gt`, `gte`, cross-field operators) does not change. Only how the C# codegen translates it.
- **Publishing a real `https://ailang.dev/errors/*` site.** URIs are identifiers, not resolvable URLs. Whether to host a docs page at those URIs is a follow-up post-MVP.
- **Other ASP.NET output formats** (XML, Protobuf, etc.). JSON-only.

## 4. Wire shape — ProblemDetails per error type

**Content-Type**: `application/problem+json` (RFC 7807). ASP.NET sets this automatically for `Results.Problem(...)`.

**URI scheme**: `https://ailang.dev/errors/{slug}` where `slug` is the lower-kebab form of the AILang error type. URIs are identifiers; they are not resolvable today.

**Common fields on every error response**:
- `type` (URI)
- `title` (short generic label)
- `status` (HTTP status code)
- `traceId` (extension) — populated from `HttpContext.Items["trace_id"]` injected by the tracing middleware. Always present on every response.
- `detail` (optional, per-instance string)

### Mapping

| AILang type | ProblemDetails `type` URI | Status | Extension fields | Notes |
|---|---|---|---|---|
| `NOT_FOUND` | `.../not-found` | 404 | `entity`, `entityId` | |
| `VALIDATION` | `.../validation` | 400 | `errors` (Dict<string, string[]>) | Uses `ValidationProblemDetails`; collects all violations |
| `BUSINESS_RULE` | `.../business-rule` | 422 | `rule` | |
| `UNAUTHORIZED` | `.../unauthorized` | 401 | — | |
| `TIMEOUT` | `.../timeout` | 504 | `service` | CALL_SERVICE Polly timeout |
| `UNAVAILABLE` | `.../unavailable` | 503 | `service` | Circuit breaker open / upstream down |
| `INTERNAL` | `.../internal` | 500 | — | |
| `INVALID_PARAM` | `.../invalid-param` | 400 | `field` | Path/query param malformed (e.g. invalid UUID) |
| `INVALID_BODY` | `.../invalid-body` | 400 | `schema` | JSON body doesn't parse or doesn't match schema (caught before AddValidation runs) |

### Examples

**NOT_FOUND** — `GET /products/abc-123` on a missing record:

```json
{
  "type": "https://ailang.dev/errors/not-found",
  "title": "Resource not found",
  "status": 404,
  "detail": "product with id abc-123 not found",
  "traceId": "a1b2c3d4e5f60718",
  "entity": "product",
  "entityId": "abc-123"
}
```

**VALIDATION** — `POST /products/` with `{"name":"","price":-1,"stock":0}`:

```json
{
  "type": "https://ailang.dev/errors/validation",
  "title": "One or more validation errors occurred.",
  "status": 400,
  "traceId": "a1b2c3d4e5f60718",
  "errors": {
    "name": ["required, min length 1"],
    "price": ["must be > 0"]
  }
}
```

**BUSINESS_RULE** — `DELETE /products/{id}` when stock > 0:

```json
{
  "type": "https://ailang.dev/errors/business-rule",
  "title": "Business rule violation",
  "status": 422,
  "detail": "product has stock > 0",
  "traceId": "a1b2c3d4e5f60718",
  "rule": "cannot_delete_with_stock"
}
```

## 5. Codegen architecture

Five regions of `transpiler.py` change.

### 5.1 Service registration

After `builder.Services.AddAuthorization();`, emit:

```csharp
builder.Services.AddValidation();
builder.Services.AddProblemDetails(opts =>
{
    opts.CustomizeProblemDetails = ctx =>
    {
        if (ctx.HttpContext.Items.TryGetValue("trace_id", out var tid) && tid is not null)
            ctx.ProblemDetails.Extensions["traceId"] = tid.ToString();
    };
});
```

The `traceId` injection ensures every ProblemDetails response carries the trace id without each call site having to thread it through.

### 5.2 csproj

Add property:

```xml
<InterceptorsNamespaces>$(InterceptorsNamespaces);Microsoft.AspNetCore.Http.Validation.Generated</InterceptorsNamespaces>
```

No new package references — `AddValidation` and `AddProblemDetails` live in the `Microsoft.AspNetCore.App` shared framework already referenced via `Sdk="Microsoft.NET.Sdk.Web"`.

### 5.3 DTO emission (`generate_schema_csharp`)

DTO records replace the hand-rolled `Validate()` method with DataAnnotations attributes on properties:

```csharp
public record ProductDto
{
    [JsonPropertyName("name")]
    [Required, StringLength(200, MinimumLength = 1)]
    public required string Name { get; init; }

    [JsonPropertyName("price")]
    [GreaterThan(0)]
    public required decimal Price { get; init; }

    [JsonPropertyName("stock")]
    [GreaterThanOrEqual(0)]
    public required int Stock { get; init; }
}
```

**Mapping** (AILang schema vocab → DataAnnotations):

| AILang | DataAnnotation | Notes |
|---|---|---|
| `required` (field) | `[Required]` | Coexists with C# `required` keyword (compile-time vs runtime) |
| `min` (string) | `[StringLength(max, MinimumLength=min)]` or `[MinLength(min)]` | Combined with `max` when both present |
| `max` (string) | `[StringLength(max)]` or `[MaxLength(max)]` | |
| `gt` (numeric) | `[GreaterThan(n)]` | Custom attribute (see 5.5) |
| `gte` (numeric) | `[GreaterThanOrEqual(n)]` | Custom attribute (see 5.5) |
| `lt` (numeric) | `[LessThan(n)]` | Custom attribute (see 5.5) |
| `lte` (numeric) | `[LessThanOrEqual(n)]` | Custom attribute (see 5.5) |
| `min_items` (list) | `[MinLength(n)]` | |
| Cross-field operators | `IValidatableObject.Validate()` body (see 5.4) | |

### 5.4 Cross-field via `IValidatableObject`

When a DTO's schema includes any of the 6 Nivel 2 operators (`COMPARE`, `NOT_EQUAL`, `CONDITIONAL_REQUIRED`, `MUTUAL_EXCLUSIVE`, `AT_LEAST_ONE`, `SUM_MAX`), the record implements `IValidatableObject`:

```csharp
public record OrderDto : IValidatableObject
{
    [JsonPropertyName("delivery_type")] public required string DeliveryType { get; init; }
    [JsonPropertyName("shipping_address")] public string? ShippingAddress { get; init; }
    [JsonPropertyName("pickup_location")] public string? PickupLocation { get; init; }

    public IEnumerable<ValidationResult> Validate(ValidationContext _)
    {
        // CONDITIONAL_REQUIRED: shipping_address required when delivery_type == "ship"
        if (DeliveryType == "ship" && string.IsNullOrEmpty(ShippingAddress))
            yield return new ValidationResult(
                "required when delivery_type=ship",
                new[] { nameof(ShippingAddress) });

        // MUTUAL_EXCLUSIVE: shipping_address and pickup_location can't both be set
        if (!string.IsNullOrEmpty(ShippingAddress) && !string.IsNullOrEmpty(PickupLocation))
            yield return new ValidationResult(
                "shipping_address and pickup_location are mutually exclusive",
                new[] { nameof(ShippingAddress), nameof(PickupLocation) });

        // ... one yield per cross-field operator declared in the bytecode
    }
}
```

`AddValidation` calls `Validate(ValidationContext)` after field-level validators; results are merged into the same `ValidationProblemDetails.errors` dictionary so the client sees a single response with all violations.

Today's bytecode has zero services using these operators. The codegen emit is exercised by a Python-level test (a synthetic schema with one operator per type, regenerated, compiled, runtime-verified). Real services adopt cross-field as needed.

### 5.5 Helper class + custom attributes

Emitted once per service at the end of `Program.cs`:

```csharp
// ── Custom validation attributes ──
[AttributeUsage(AttributeTargets.Property)]
public sealed class GreaterThanAttribute(double value) : ValidationAttribute
{
    public double Value { get; } = value;
    public override bool IsValid(object? v) => v switch
    {
        null => true,
        IConvertible c => Convert.ToDouble(c) > Value,
        _ => false
    };
    public override string FormatErrorMessage(string name) => $"must be > {Value}";
}
// ... plus LessThanAttribute, GreaterThanOrEqualAttribute, LessThanOrEqualAttribute,
// each ~12 lines.

// ── ProblemDetails factories ──
public static class AiLangProblems
{
    public static IResult NotFound(string entity, string id) => Results.Problem(
        type: "https://ailang.dev/errors/not-found",
        title: "Resource not found",
        statusCode: 404,
        detail: $"{entity} with id {id} not found",
        extensions: new Dictionary<string, object?> { ["entity"] = entity, ["entityId"] = id });

    public static IResult BusinessRule(string rule, string detail) => Results.Problem(
        type: "https://ailang.dev/errors/business-rule",
        title: "Business rule violation",
        statusCode: 422,
        detail: detail,
        extensions: new Dictionary<string, object?> { ["rule"] = rule });

    public static IResult Unauthorized() => Results.Problem(
        type: "https://ailang.dev/errors/unauthorized",
        title: "Unauthorized",
        statusCode: 401);

    public static IResult Timeout(string service) => Results.Problem(
        type: "https://ailang.dev/errors/timeout",
        title: "Upstream service timed out",
        statusCode: 504,
        detail: $"service {service} timed out",
        extensions: new Dictionary<string, object?> { ["service"] = service });

    public static IResult Unavailable(string service) => Results.Problem(
        type: "https://ailang.dev/errors/unavailable",
        title: "Upstream service unavailable",
        statusCode: 503,
        detail: $"service {service} is unavailable",
        extensions: new Dictionary<string, object?> { ["service"] = service });

    public static IResult Internal(string detail) => Results.Problem(
        type: "https://ailang.dev/errors/internal",
        title: "Internal server error",
        statusCode: 500,
        detail: detail);

    public static IResult InvalidParam(string field, string expected) => Results.Problem(
        type: "https://ailang.dev/errors/invalid-param",
        title: "Invalid parameter",
        statusCode: 400,
        detail: $"parameter {field} must be {expected}",
        extensions: new Dictionary<string, object?> { ["field"] = field });

    public static IResult InvalidBody(string schema, string detail) => Results.Problem(
        type: "https://ailang.dev/errors/invalid-body",
        title: "Invalid request body",
        statusCode: 400,
        detail: $"body does not match schema {schema}: {detail}",
        extensions: new Dictionary<string, object?> { ["schema"] = schema });
}
```

The helper keeps call sites short: `return AiLangProblems.NotFound("product", id);` replaces today's `return Results.NotFound(AiLangError.NotFound("product", id));`. The `Validation` factory is not in the helper because `AddValidation` produces `ValidationProblemDetails` directly before any handler runs.

### 5.6 Removal of `AiLangError` and `ErrorDetail`

Deleted from codegen:
- `public record AiLangError` and its factory methods.
- `public record ErrorDetail` and its property bag.
- The `[JsonSerializable(typeof(AiLangError))]` and `[JsonSerializable(typeof(ErrorDetail))]` entries in the AOT source-gen context (Task 4 of the NativeAOT path).

What's added to the AOT context, if anything, is determined during implementation. ASP.NET 10 likely ships its own source-gen JsonContext for `ProblemDetails` / `ValidationProblemDetails`; if so we add nothing. If not, the implementation plan adds them.

### 5.7 Call-site simplifications

The 9 sites that Task 5 of the NativeAOT path converted to `Results.Json(AiLangError.X(), Context.Default.AiLangError, statusCode: N)` change to `AiLangProblems.X(...)`. The AOT context arg is no longer needed at the call site because `Results.Problem` is framework-resolved.

The 2 `ReadFromJsonAsync<TDto>(Context.Default.TDto)` sites keep their source-gen context arg — they deserialize DTOs, not error responses.

### 5.8 READ_BODY simplification

Today:

```csharp
try { body = await ctx.Request.ReadFromJsonAsync<ProductDto>(Context.Default.ProductDto); }
catch (JsonException _je) { return Results.BadRequest(AiLangError.InvalidBody("Product", _je.Message)); }
if (body is null) return Results.BadRequest(AiLangError.InvalidBody("Product", "body is null"));
var _verr = body.Validate();
if (_verr != null) return Results.BadRequest(_verr);
```

After:

```csharp
try { body = await ctx.Request.ReadFromJsonAsync<ProductDto>(Context.Default.ProductDto); }
catch (JsonException _je) { return AiLangProblems.InvalidBody("Product", _je.Message); }
if (body is null) return AiLangProblems.InvalidBody("Product", "body is null");
// AddValidation runs validators before this point; if invalid, handler doesn't execute.
```

Two lines removed per handler. `AddValidation` handles the validation gate at the framework layer.

## 6. Test strategy

The test codegen (`generate_csharp_tests`) changes its assertion style from substring matching on the raw response body to typed deserialization to `ProblemDetails` + `Assert.*`:

**Before** (fragile to whitespace, key order, escape variations):

```csharp
var body = await response.Content.ReadAsStringAsync();
Assert.Contains("\"type\":\"NOT_FOUND\"", body);
Assert.Contains("\"entity\":\"product\"", body);
```

**After** (typed, structural):

```csharp
var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();
Assert.NotNull(problem);
Assert.Equal("https://ailang.dev/errors/not-found", problem!.Type);
Assert.Equal(404, problem.Status);
Assert.Equal("product", problem.Extensions["entity"]?.ToString());
Assert.NotNull(problem.Extensions["traceId"]);
```

No new package references in the test csproj. `ProblemDetails` is in `Microsoft.AspNetCore.Mvc` which the test project pulls in transitively via `Microsoft.AspNetCore.Mvc.Testing`.

Estimated assertion sites that change: ~15-20 across 5 in-memory services + ~4-5 in the PG service. The work is in the codegen template, not per-test — once `generate_csharp_tests` is updated, regenerating all 6 services produces a consistent test suite.

Happy-path assertions (status codes, response bodies for `Results.Ok`-returning handlers) keep their current `Assert.*` style.

## 7. Smoke script changes

`bytecode/scripts/aot-smoke-product-service.sh` has two substring checks that update:

```bash
# Before
grep -q '"type":"NOT_FOUND"' /tmp/aot-smoke-resp

# After
grep -q '"type":"https://ailang.dev/errors/not-found"' /tmp/aot-smoke-resp
```

Trivial. The smoke's other 3 assertions (`GET / → []`, `/auth/token` → JWT, `POST /products/` with bearer) don't touch error shapes.

## 8. CLAUDE.md changes

The "Validación" subsection of "Decisiones de Diseño Clave" updates to reflect the new path:

**Before** (current state, ~lines 100-105):

> ### Validación
> - Nivel 1: campo individual desde schema (required, min, max, gt, gte)
> - Nivel 2: cross-field (COMPARE, NOT_EQUAL, CONDITIONAL_REQUIRED, MUTUAL_EXCLUSIVE, AT_LEAST_ONE, SUM_MAX)
> - READ_BODY ejecuta TODAS las validaciones automáticamente

**After**:

> ### Validación
> - Nivel 1 (field-level): schema → DataAnnotations attributes en el DTO (`[Required]`, `[StringLength]`, custom `[GreaterThan]`/`[LessThan]` para strict bounds). Validado por `AddValidation()` de .NET 10 (source-generator, AOT-friendly).
> - Nivel 2 (cross-field): schema → `IValidatableObject.Validate(ValidationContext)` body. Operators: `COMPARE`, `NOT_EQUAL`, `CONDITIONAL_REQUIRED`, `MUTUAL_EXCLUSIVE`, `AT_LEAST_ONE`, `SUM_MAX`.
> - Wire response: RFC 7807 `ValidationProblemDetails` con TODOS los errores (no first-fail-and-bail). `application/problem+json` Content-Type automático.
> - READ_BODY no necesita llamar `Validate()` manualmente — `AddValidation` corre antes del handler.

The existing `### Errores` subsection (CLAUDE.md line 54) is updated to reflect that AILang errors are emitted as RFC 7807 ProblemDetails responses with extension fields (instead of the in-house `AiLangError` envelope). The catalog table from §4 of this spec replaces the prior catalog description.

## 9. Roadmap impact

This spec consumes "Item #2 — Investigar Minimal API enhanced validation" from CLAUDE.md "Próximos Pasos". After landing, the roadmap becomes:

1. ~~NativeAOT path~~ — done (`22eef1d`).
2. ~~Minimal API enhanced validation~~ — this spec.
3. GUARD / VALIDATE_CHAIN instruction (still pending).
4. Hot-reload de config (v2) (still pending).
5. Linter con colores para S-expressions (still pending).

## 10. Success criteria

This spec is considered delivered when:

- `transpiler.py` emits DTOs with DataAnnotations attributes + `IValidatableObject` for cross-field, with no `Validate()` method.
- `AiLangError` / `ErrorDetail` records are removed from codegen; replaced by `AiLangProblems` helper and custom attributes file.
- `AddValidation()` + `AddProblemDetails()` are registered in every generated `Program.cs`.
- csproj includes `<InterceptorsNamespaces>`.
- `generate_csharp_tests` emits typed `ProblemDetails` deserialization in error assertions.
- The full regression suite remains green across all 6 services:
  - C# in-memory: 36/36 (across 5 services).
  - C# PG: 14/14 (Docker dependent).
  - AOT smoke: PASS, binary size in the same band (~20 MB).
- A Python-level codegen test exercises the cross-field `IValidatableObject` emission against a synthetic schema with one operator per Nivel 2 type, regenerated, compiled, runtime-verified.
- `CLAUDE.md` and `README.md` updated.
- AOT publish stays clean (verified during implementation; risk surfaced in §11).

## 11. Open questions / risks for implementation

- **AOT compat of `CustomizeProblemDetails`**: the customizer assigns into `ProblemDetails.Extensions` which is `IDictionary<string, object?>`. Under AOT this may trigger trim/AOT warnings if the serializer can't resolve `object` runtime types. Mitigation if warnings appear: type the customizer's added values explicitly (always `string` for `traceId`) and verify the existing `ProblemDetails` source-gen context covers our extension key set; if not, add a thin `JsonSerializable` registration.
- **`Results.Problem` overload selection under AOT**: `Results.Problem(string? type, ...)` vs `Results.Problem(ProblemDetails)` may have different AOT analyzer profiles. Verify the chosen overload is AOT-clean during implementation; if not, switch to the object overload and pre-construct the `ProblemDetails`.
- **`required` keyword + `[Required]` interaction**: today's DTOs use both `required string Name` (C# 11 keyword) and the new `[Required]` attribute. They don't conflict (compile-time vs runtime) but the redundancy is worth a comment in the codegen output so future readers understand both are intentional.
- **Cross-field codegen without a real service to exercise**: the spec includes Python-level codegen testing against a synthetic schema, but no end-to-end service exercises the operators. If a future service uses cross-field and the codegen has a bug, the test surface is thin. Accepted; future cross-field-using services will validate the path naturally.
- **EF Core stayed off the dependency list, no concern.** Mentioned only because `ProblemDetails` + EF Core has known AOT issues; not applicable here.
- **Documentation URI host**: `https://ailang.dev/errors/*` URIs are identifiers, not resolvable today. Standing up the host is a post-MVP follow-up; including it in the wire shape now keeps the migration coherent if/when we ship documentation.
