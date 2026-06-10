# AiLangError → ProblemDetails Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace AILang's in-house `AiLangError` envelope with RFC 7807 ProblemDetails responses for all 6 C# reference services, swap the hand-rolled DTO `Validate()` method for .NET 10's `AddValidation()` with DataAnnotations + custom strict-bounds attributes, implement cross-field validation via `IValidatableObject`, and update the 17 wire-shape assertions in the `.sexpr` bytecode files.

**Architecture:** Edits live in `transpiler/transpiler.py` (8 distinct regions) plus the 6 bytecode `.sexpr` files (assertion paths) and the AOT smoke script (substring grep checks). The migration changes the wire shape from `{error:{type:"NOT_FOUND",...}}` to `{type:"https://ailang.dev/errors/not-found", status:404, traceId, ...extension fields}`. Hand-rolled `Validate()` is removed; field-level rules become DataAnnotations attributes (`[Required]`, `[StringLength]`, custom `[GreaterThan]` etc.) interpreted by .NET 10's source-generated validation; cross-field rules become an `IValidatableObject.Validate(ValidationContext)` body. The `AiLangError` and `ErrorDetail` records are deleted; a static `AiLangProblems` helper provides factories that return `IResult` via `Results.Problem(...)`. Tests continue using the existing `JsonDocument.Parse(...).RootElement.GetProperty(...)` walker; only the assertion *paths* in `.sexpr` files change.

**Tech Stack:** Python 3 (transpiler), .NET 10 LTS, ASP.NET Minimal APIs (`Results.Problem`, `AddProblemDetails`, `AddValidation`), `System.ComponentModel.DataAnnotations`, xUnit + `Microsoft.AspNetCore.Mvc.Testing` (existing). No new package references.

**Spec reference:** `docs/superpowers/specs/2026-06-10-problemdetails-migration-design.md` (commit `5b63561`).

---

## File Structure

**Modified:**
- `transpiler/transpiler.py` (8 regions):
  - csproj emit (line ~2272): add `<InterceptorsNamespaces>` property.
  - Program.cs service registration (line ~1900, after `AddAuthorization()`): emit `AddValidation()` + `AddProblemDetails()`.
  - `generate_error_model_csharp` (line 3142): REPLACE entire function body with `AiLangProblems` static class + custom validation attributes emission.
  - DTO emission inside `generate_schema_csharp` (line ~2396, specifically lines 2440-2488): replace `[JsonPropertyName]` + `required` with DataAnnotations attributes alongside; DELETE the `Validate()` method (lines 2449-2487).
  - READ_BODY emit (line ~715): delete the `var _verr = body.Validate(); if (_verr != null)…` block.
  - 9 `Results.Json(AiLangError.X(...), ..., statusCode: N)` call sites (lines ~447, ~450, ~457, ~460, ~466, ~663, ~667, ~711, ~713, ~757, ~760, ~824, ~826, ~1612-1622, ~2208): replace with `AiLangProblems.X(...)`.
  - `_aot_json_types` (line 1806): remove `AiLangError` and `ErrorDetail` entries.
  - New helper function `_cross_field_validations_csharp(schema_def)` near `generate_schema_csharp` for emitting `IValidatableObject.Validate(...)` bodies.

- 6 bytecode `.sexpr` files: `bytecode/{order,pricing,payment,dashboard,product}_service.sexpr`, `bytecode/product_service_pg.sexpr`. Update 17 `(ASSERT body.error.X ...)` assertion paths to the new ProblemDetails shape.

- `bytecode/scripts/aot-smoke-product-service.sh`: 2 substring grep checks updated.

- `CLAUDE.md`: update `### Errores` (line 54) and `### Validación` (line 114).

- `README.md`: add a paragraph about ProblemDetails wire shape.

**Created:**
- `bytecode/scratch_cross_field.sexpr`: synthetic service with one DTO per Nivel 2 operator (CONDITIONAL_REQUIRED, MUTUAL_EXCLUSIVE, AT_LEAST_ONE, SUM_MAX, COMPARE, NOT_EQUAL). Exercises the new `IValidatableObject` codegen path with a single `(test)` block per operator. Lives in `bytecode/` alongside the other scratch_phaseN files.

**Not touched:**
- Any `generate_go*` codegen function — Go target stays frozen per spec.
- The AILang bytecode schema vocabulary (`required`, `min`, `max`, `gt`, `gte`, cross-field operators). Only the C# translation changes.
- The test csproj emit (lines 6774-6792 of `transpiler.py`): no new package references.

---

## Task 1: csproj emits `<InterceptorsNamespaces>` for AddValidation source generator

`AddValidation()` is source-generator-driven. Without the namespace declaration the interceptors can't fire and validation silently does nothing.

**Files:**
- Modify: `transpiler/transpiler.py:~2272` (the `csproj = (...)` block inside `generate_csharp`).

- [ ] **Step 1.1: Add the property to the emitted csproj**

Locate the existing csproj block (search for `<TargetFramework>net10.0</TargetFramework>` inside `generate_csharp`). Inside `<PropertyGroup>`, after the `aot_props` insertion, add unconditionally (not gated on `aot`):

```python
    # Always required: AddValidation()'s source generator emits its interceptors
    # into Microsoft.AspNetCore.Http.Validation.Generated.
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        '    <InterceptorsNamespaces>$(InterceptorsNamespaces);Microsoft.AspNetCore.Http.Validation.Generated</InterceptorsNamespaces>\n'
        + aot_props +
        '  </PropertyGroup>\n'
        ...
```

- [ ] **Step 1.2: Regenerate the pilot and eyeball the csproj**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n InterceptorsNamespaces bytecode/generated-csharp-product_service/ProductService.csproj
```

Expected: one line containing `<InterceptorsNamespaces>$(InterceptorsNamespaces);Microsoft.AspNetCore.Http.Validation.Generated</InterceptorsNamespaces>`.

- [ ] **Step 1.3: Build the pilot to confirm csproj is well-formed**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: build succeeds. The property is inert until `AddValidation()` is called in code, but the csproj must still parse.

- [ ] **Step 1.4: Commit**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): csproj emits InterceptorsNamespaces for AddValidation source generator"
```

---

## Task 2: Program.cs registers AddValidation + AddProblemDetails with traceId customizer

`AddValidation()` enables source-generated DataAnnotations validation. `AddProblemDetails()` registers the ProblemDetails service so `Results.Problem(...)` returns a well-formed ProblemDetails response. The customizer injects the `trace_id` set by the existing tracing middleware (`HttpContext.Items["trace_id"]`) into every ProblemDetails response.

**Files:**
- Modify: `transpiler/transpiler.py:~1900` (the line `lines.append('builder.Services.AddAuthorization();')` inside `generate_csharp`).

- [ ] **Step 2.1: Locate the AddAuthorization emission**

Search for `'builder.Services.AddAuthorization();'` in `transpiler.py`. It appears once in `generate_csharp`. Confirm the surrounding context emits service registration before `var app = builder.Build();`.

- [ ] **Step 2.2: Emit AddValidation + AddProblemDetails after AddAuthorization**

Replace the single `AddAuthorization()` line with this block:

```python
    lines.append('builder.Services.AddAuthorization();')
    lines.append('')
    lines.append('// ── Validation: source-generated by AddValidation() ──')
    lines.append('builder.Services.AddValidation();')
    lines.append('')
    lines.append('// ── ProblemDetails: RFC 7807 error responses ──')
    lines.append('// The customizer injects the trace_id from the tracing middleware into')
    lines.append('// every ProblemDetails response so clients can correlate errors with logs.')
    lines.append('builder.Services.AddProblemDetails(opts =>')
    lines.append('{')
    lines.append('    opts.CustomizeProblemDetails = ctx =>')
    lines.append('    {')
    lines.append('        if (ctx.HttpContext.Items.TryGetValue("trace_id", out var tid) && tid is not null)')
    lines.append('            ctx.ProblemDetails.Extensions["traceId"] = tid.ToString();')
    lines.append('    };')
    lines.append('});')
```

- [ ] **Step 2.3: Regenerate and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -nA 1 "AddValidation\|AddProblemDetails" bytecode/generated-csharp-product_service/Program.cs
```

Expected: lines containing `builder.Services.AddValidation();`, `builder.Services.AddProblemDetails(opts =>`, and the `CustomizeProblemDetails` lambda.

- [ ] **Step 2.4: Build to confirm it compiles**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. Existing 6 tests will still pass (the new services are registered but no error path uses them yet — error responses still emit `AiLangError`).

- [ ] **Step 2.5: Run the test project to confirm no regression**

```
cd Tests
dotnet test --nologo --verbosity quiet
```

Expected: 6/6 pass.

- [ ] **Step 2.6: Commit**

```
cd ../../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): register AddValidation + AddProblemDetails with traceId customizer"
```

---

## Task 3: Replace generate_error_model_csharp body — emit AiLangProblems helper + custom validation attributes

The function `generate_error_model_csharp(custom_errors)` at line 3142 emits `AiLangError` + `ErrorDetail` records. Replace its body to emit `AiLangProblems` static factories + 4 custom validation attributes (`GreaterThan`, `LessThan`, `GreaterThanOrEqual`, `LessThanOrEqual`).

`AiLangError` is NOT removed yet — it stays alongside `AiLangProblems` so the 9 existing call sites still compile. Task 7 deletes `AiLangError` after Task 4 switches all call sites.

**Files:**
- Modify: `transpiler/transpiler.py:3142-3201` (`generate_error_model_csharp`).

- [ ] **Step 3.1: Add the AiLangProblems emission**

Locate `def generate_error_model_csharp(custom_errors: dict) -> list[str]:` at line 3142. Right after the existing function body (just before the existing `return lines`), append the new emissions:

```python
def generate_error_model_csharp(custom_errors: dict) -> list[str]:
    """Generate AiLangError + AiLangProblems + custom validation attributes for C#.

    Both AiLangError (legacy) and AiLangProblems (new ProblemDetails factories) are
    emitted during the migration. Task 7 will delete the AiLangError emission once
    all call sites use AiLangProblems.
    """
    # ── existing AiLangError + ErrorDetail emission (unchanged) ──
    lines = []
    lines.append("// ERROR MODEL: AILang native errors (legacy — being migrated)")
    # … [existing AiLangError + ErrorDetail block, lines 3145-3201 unchanged] …

    # ── NEW: AiLangProblems static factories + custom validation attributes ──
    lines.append("")
    lines.append("// ── ProblemDetails factories (RFC 7807) ──")
    lines.append("public static class AiLangProblems")
    lines.append("{")
    lines.append('    public static IResult NotFound(string entity, string id) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/not-found",')
    lines.append('        title: "Resource not found",')
    lines.append('        statusCode: 404,')
    lines.append('        detail: $"{entity} with id {id} not found",')
    lines.append('        extensions: new Dictionary<string, object?> { ["entity"] = entity, ["entityId"] = id });')
    lines.append("")
    lines.append('    public static IResult BusinessRule(string rule, string detail) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/business-rule",')
    lines.append('        title: "Business rule violation",')
    lines.append('        statusCode: 422,')
    lines.append('        detail: detail,')
    lines.append('        extensions: new Dictionary<string, object?> { ["rule"] = rule });')
    lines.append("")
    lines.append('    public static IResult Unauthorized() => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/unauthorized",')
    lines.append('        title: "Unauthorized",')
    lines.append('        statusCode: 401);')
    lines.append("")
    lines.append('    public static IResult Timeout(string service) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/timeout",')
    lines.append('        title: "Upstream service timed out",')
    lines.append('        statusCode: 504,')
    lines.append('        detail: $"service {service} timed out",')
    lines.append('        extensions: new Dictionary<string, object?> { ["service"] = service });')
    lines.append("")
    lines.append('    public static IResult Unavailable(string service) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/unavailable",')
    lines.append('        title: "Upstream service unavailable",')
    lines.append('        statusCode: 503,')
    lines.append('        detail: $"service {service} is unavailable",')
    lines.append('        extensions: new Dictionary<string, object?> { ["service"] = service });')
    lines.append("")
    lines.append('    public static IResult Internal(string detail) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/internal",')
    lines.append('        title: "Internal server error",')
    lines.append('        statusCode: 500,')
    lines.append('        detail: detail);')
    lines.append("")
    lines.append('    public static IResult InvalidParam(string field, string expected) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/invalid-param",')
    lines.append('        title: "Invalid parameter",')
    lines.append('        statusCode: 400,')
    lines.append('        detail: $"parameter {field} must be {expected}",')
    lines.append('        extensions: new Dictionary<string, object?> { ["field"] = field });')
    lines.append("")
    lines.append('    public static IResult InvalidBody(string schema, string detail) => Results.Problem(')
    lines.append('        type: "https://ailang.dev/errors/invalid-body",')
    lines.append('        title: "Invalid request body",')
    lines.append('        statusCode: 400,')
    lines.append('        detail: $"body does not match schema {schema}: {detail}",')
    lines.append('        extensions: new Dictionary<string, object?> { ["schema"] = schema });')
    lines.append("")
    # Custom errors (from bytecode) get their own factories.
    for err_type, entry in (custom_errors or {}).items():
        factory = err_name_to_factory(err_type)
        tag = err_type_tag(err_type)
        declared = entry.get('params', []) or []
        template = entry.get('message_template', tag)
        param_camel = {p: camel_case(p) for p in declared}
        sig_params = ", ".join(f"string {param_camel[p]}" for p in declared)
        msg_body = template_to_csharp_interpolated(template, param_camel)
        slug = tag.lower().replace('_', '-')
        lines.append(f'    public static IResult {factory}({sig_params}) => Results.Problem(')
        lines.append(f'        type: "https://ailang.dev/errors/{slug}",')
        lines.append(f'        title: "{tag}",')
        lines.append(f'        statusCode: 400,')
        lines.append(f'        detail: $"{msg_body}");')
        lines.append("")
    lines.append("}")
    lines.append("")
    # Custom validation attributes — covered DataAnnotations [Range] is inclusive only.
    lines.append("// ── Custom validation attributes (for AILang's strict bounds: gt, lt) ──")
    for attr_name, op, msg in [
        ("GreaterThan", ">", "must be > "),
        ("GreaterThanOrEqual", ">=", "must be >= "),
        ("LessThan", "<", "must be < "),
        ("LessThanOrEqual", "<=", "must be <= "),
    ]:
        lines.append(f"[AttributeUsage(AttributeTargets.Property)]")
        lines.append(f"public sealed class {attr_name}Attribute(double value) : System.ComponentModel.DataAnnotations.ValidationAttribute")
        lines.append("{")
        lines.append(f"    public double Value {{ get; }} = value;")
        lines.append(f"    public override bool IsValid(object? v) => v switch")
        lines.append(f"    {{")
        lines.append(f"        null => true,")
        lines.append(f"        IConvertible c => System.Convert.ToDouble(c) {op} Value,")
        lines.append(f"        _ => false")
        lines.append(f"    }};")
        lines.append(f'    public override string FormatErrorMessage(string name) => $"{msg}{{Value}}";')
        lines.append("}")
        lines.append("")
    return lines
```

- [ ] **Step 3.2: Regenerate the pilot and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "class AiLangProblems\|class GreaterThanAttribute\|class LessThanAttribute\|class GreaterThanOrEqualAttribute\|class LessThanOrEqualAttribute" bytecode/generated-csharp-product_service/Program.cs
```

Expected: 5 grep hits (one class each).

- [ ] **Step 3.3: Build to confirm both AiLangError and AiLangProblems coexist**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. Both the old `public record AiLangError` and the new `public static class AiLangProblems` are present.

- [ ] **Step 3.4: Run tests to confirm no regression**

```
cd Tests
dotnet test --nologo --verbosity quiet
```

Expected: 6/6 pass.

- [ ] **Step 3.5: Commit**

```
cd ../../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): emit AiLangProblems static helper + custom validation attributes alongside legacy AiLangError"
```

---

## Task 4: Switch all 9 Results.Json(AiLangError.X(...), ...) call sites to AiLangProblems.X(...)

Every site that returns an error via `Results.Json(AiLangError.X(), Context.Default.AiLangError, statusCode: N)` is replaced with `AiLangProblems.X(...)`. Likewise `Results.NotFound(AiLangError.X(...))` and `Results.BadRequest(AiLangError.X(...))`. These all live in `transpiler.py`.

This is the wire-shape switch. After this task, regenerating any service produces ProblemDetails responses. Tests in the existing 6 services WILL FAIL because the `.sexpr` assertion paths still expect the old `body.error.type` shape. Task 10 updates the `.sexpr` files. Until then, the build is clean but test runs fail — that is intentional and resolved by Task 10.

**Files:**
- Modify: `transpiler/transpiler.py` — the 9 emission sites enumerated below.

The transpiler has both `Results.Json(AiLangError.X(...), ..., statusCode: N)` (from CALL_SERVICE catches, RESPOND_STATUS, auth gates) and `Results.NotFound(AiLangError.X(...))` / `Results.BadRequest(AiLangError.X(...))` (from RESPOND emit). After this task, all of them return `AiLangProblems.X(...)` instead.

- [ ] **Step 4.1: List every call site by grep**

```
cd e:/02-lab/ai-lang
grep -n "AiLangError\." transpiler/transpiler.py | grep -v "generate_error_model_csharp\|def err_name_to_factory\|def err_type_tag\|AiLangError\\?\\? Validate\|AiLangError Validate\|public AiLangError\|public record AiLangError\|public static AiLangError\|AiLangProblems" | head -25
```

Expected: ~14-16 emission sites. Each `lines.append(...)` that contains `AiLangError.` is a candidate. Use the list to plan the per-site edits.

- [ ] **Step 4.2: Convert each Results.Json(AiLangError.X(...), Context.Default.AiLangError, statusCode: N) site**

For each site, replace:

```python
lines.append(f'{pad}return Results.Json(AiLangError.X(...){type_info_arg}, statusCode: N);')
```

with:

```python
lines.append(f'{pad}return AiLangProblems.X(...);')
```

(Remove the `type_info_arg` — `Results.Problem` is resolved via the framework's source-gen context, not the service's own.)

Specifically:
- Line ~757 (ERR_CUSTOM): `Results.Json(AiLangError.{factory}(...){type_info_arg}, statusCode: {status})` → `AiLangProblems.{factory}(...)`.
- Line ~760 (ERR_CUSTOM fallback): `Results.Json(AiLangError.BusinessRule(...){type_info_arg}, statusCode: 422)` → `AiLangProblems.BusinessRule(...)`.
- Line ~824 (CALL_SERVICE TimeoutRejectedException): `Results.Json(AiLangError.Timeout(...){type_info_arg}, statusCode: 504)` → `AiLangProblems.Timeout(...)`.
- Line ~826 (CALL_SERVICE BrokenCircuitException): `Results.Json(AiLangError.Unavailable(...){type_info_arg}, statusCode: 503)` → `AiLangProblems.Unavailable(...)`.
- Line ~1616 (ERR_BUSINESS_RULE in compile_error_csharp): `Results.Json(AiLangError.BusinessRule(...){type_info_arg}, statusCode: 422)` → `AiLangProblems.BusinessRule(...)`.
- Line ~1620 (ERR_UNAUTHORIZED in compile_error_csharp): `Results.Json(AiLangError.Unauthorized(){type_info_arg}, statusCode: 401)` → `AiLangProblems.Unauthorized()`.
- Line ~1622 (ERR_UNAVAILABLE in compile_error_csharp): `Results.Json(AiLangError.Unavailable(...){type_info_arg}, statusCode: 503)` → `AiLangProblems.Unavailable(...)`.
- Line ~2208 (handler auth-gate): `Results.Json(AiLangError.Unauthorized(), Context.Default.AiLangError, statusCode: 401)` → `AiLangProblems.Unauthorized()` (AOT branch) AND its non-AOT companion two lines below.

- [ ] **Step 4.3: Convert each Results.NotFound(AiLangError.X(...)) site**

- Line ~1612 (ERR_NOT_FOUND in compile_error_csharp): `Results.NotFound(AiLangError.NotFound(...))` → `AiLangProblems.NotFound(...)`.

- [ ] **Step 4.4: Convert each Results.BadRequest(AiLangError.X(...)) site**

These emit 400-status errors. All become `AiLangProblems.X(...)` (or `AiLangProblems.InvalidBody(...)` / `AiLangProblems.InvalidParam(...)` as appropriate):

- Lines 447, 450, 457, 460, 466 (DTO `Validate()` method's inline check returns — but these are inside the **legacy** `Validate()` method which is deleted in Task 5; do NOT touch in this task).
- Line ~663 (path/query param UUID parse failure): `Results.BadRequest(AiLangError.InvalidParam(...))` → `AiLangProblems.InvalidParam(...)`.
- Line ~667 (path/query param int parse failure): `Results.BadRequest(AiLangError.InvalidParam(...))` → `AiLangProblems.InvalidParam(...)`.
- Line ~711 (READ_BODY JsonException): `Results.BadRequest(AiLangError.InvalidBody(...))` → `AiLangProblems.InvalidBody(...)`.
- Line ~713 (READ_BODY null body): `Results.BadRequest(AiLangError.InvalidBody(...))` → `AiLangProblems.InvalidBody(...)`.
- Line ~1614 (ERR_VALIDATION in compile_error_csharp): the codegen here emits a per-field validation error. Replace with the equivalent extension dict:

```python
# Before:
lines.append(f'{pad}return Results.BadRequest(AiLangError.Validation("{inst["field"]}", "{inst["reason"]}"));')
# After:
lines.append(f'{pad}return Results.Problem(')
lines.append(f'{pad}    type: "https://ailang.dev/errors/validation",')
lines.append(f'{pad}    title: "Validation failed",')
lines.append(f'{pad}    statusCode: 400,')
lines.append(f'{pad}    extensions: new Dictionary<string, object?> {{ ["errors"] = new Dictionary<string, string[]> {{ ["{inst["field"]}"] = new[] {{ "{inst["reason"]}" }} }} }});')
```

This emits a `ValidationProblemDetails`-shaped error from the handler-side `ERR_VALIDATION` bytecode op. (`AddValidation`'s automatic-validation path uses `ValidationProblemDetails` directly; here we mimic it for the rare handler that explicitly issues a validation error mid-handler.)

- Line ~1618 (ERR_INVALID_BODY in compile_error_csharp): `Results.BadRequest(AiLangError.InvalidBody(...))` → `AiLangProblems.InvalidBody(...)`.

- [ ] **Step 4.5: Remove all `type_info_arg = f", {pascal_case(ctx['service_name'])}JsonContext.Default.AiLangError" if ctx.get('aot') else ""` definitions**

The `type_info_arg` is only used for `Results.Json` overloads requiring an explicit JsonTypeInfo. `Results.Problem` does not need it. Search for `type_info_arg` and remove every line that defines or interpolates it. These appear at the same line numbers as the call sites above.

- [ ] **Step 4.6: Regenerate the pilot and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "AiLangError\.\|AiLangProblems\." bytecode/generated-csharp-product_service/Program.cs | head -20
```

Expected:
- All response emissions (the `return Results...` lines for errors) reference `AiLangProblems.X(...)`.
- The only remaining `AiLangError.X` references are inside the legacy `Validate()` method on each DTO (deleted in Task 5).

- [ ] **Step 4.7: Build to confirm**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. Tests will fail because the wire shape changed but `.sexpr` assertions haven't yet. That's OK — Task 10 fixes them.

- [ ] **Step 4.8: Commit**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): switch all error-response sites to AiLangProblems factories (wire shape changes)"
```

---

## Task 5: Replace DTO emission — DataAnnotations attributes + delete the hand-rolled Validate() method

The DTO record gets DataAnnotations attributes that `AddValidation()` reads at request time. The hand-rolled `Validate()` method is deleted entirely — `AddValidation()` runs before the handler executes, so the manual call site in READ_BODY is also unnecessary (Task 6 removes it).

**Files:**
- Modify: `transpiler/transpiler.py:~2440-2488` (the DTO Validate() emission block inside `generate_schema_csharp`).

- [ ] **Step 5.1: Locate the DTO Validate() emission**

Open `transpiler/transpiler.py` and find lines 2440-2488 — search for `# Validate() — mirrors Go's (d XDto) Validate() *AiLangError`. The block emits `[JsonPropertyName]`, the `required` modifier, and then the `Validate()` method body.

- [ ] **Step 5.2: Add DataAnnotations attribute emission alongside [JsonPropertyName]**

In the loop that emits each property (around lines 2434-2447), add DataAnnotation attribute lines before each `lines.append(f"    public {modifier}{csharp_type}{nullable} {prop_name} {{ get; init; }}")`:

```python
        for field_name, field_def in dto_fields.items():
            ftype = field_def.get('type')
            required = field_def.get('required', False)
            csharp_type = ailang_type_to_csharp(field_def)
            prop_name = pascal_case(field_name)
            if required:
                modifier = "required "
                nullable = ""
            else:
                modifier = ""
                nullable = "?" if ftype in ('string', 'list', 'object') else ""
            lines.append(f'    [JsonPropertyName("{field_name}")]')

            # NEW: emit DataAnnotations attributes derived from the schema
            data_annotations = []
            if required:
                data_annotations.append("Required")
            if ftype == 'string':
                min_len = field_def.get('min', 0)
                max_len = field_def.get('max', 0)
                if min_len > 0 and max_len > 0:
                    data_annotations.append(f"StringLength({max_len}, MinimumLength = {min_len})")
                elif max_len > 0:
                    data_annotations.append(f"StringLength({max_len})")
                elif min_len > 0:
                    data_annotations.append(f"MinLength({min_len})")
            elif ftype in ('int', 'decimal', 'long', 'double'):
                gt = field_def.get('gt')
                gte = field_def.get('gte')
                lt = field_def.get('lt')
                lte = field_def.get('lte')
                if gt is not None:
                    data_annotations.append(f"GreaterThan({gt})")
                if gte is not None:
                    data_annotations.append(f"GreaterThanOrEqual({gte})")
                if lt is not None:
                    data_annotations.append(f"LessThan({lt})")
                if lte is not None:
                    data_annotations.append(f"LessThanOrEqual({lte})")
            elif ftype == 'list':
                min_items = field_def.get('min_items')
                if min_items:
                    data_annotations.append(f"MinLength({min_items})")
            if data_annotations:
                lines.append(f"    [{', '.join(data_annotations)}]")

            lines.append(f"    public {modifier}{csharp_type}{nullable} {prop_name} {{ get; init; }}")
```

- [ ] **Step 5.3: Delete the entire Validate() method emission block**

Remove the lines that emit `public AiLangError? Validate()` and its body — the block from `lines.append("")` through `lines.append("    }")` plus the closing `lines.append("}")`. Keep the final `lines.append("}")` that closes the record definition.

The simplest approach: locate the comment `# Validate() — mirrors Go's (d XDto) Validate() *AiLangError. Returns null` and delete from that comment line through `lines.append("    }")`. Verify the surrounding context: the record's closing `}` is still emitted.

- [ ] **Step 5.4: Add `using System.ComponentModel.DataAnnotations;` to the Program.cs preamble**

Search for the line that emits `using System.Text.Json;` in `generate_csharp` and add a sibling:

```python
    lines.append("using System.ComponentModel.DataAnnotations;")
```

- [ ] **Step 5.5: Regenerate and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -nA 1 "public required.*Name\|public required.*Price\|public required.*Stock" bytecode/generated-csharp-product_service/Program.cs
```

Expected: each property declaration is preceded by a `[Required, StringLength(...)]` or `[GreaterThan(...)]` etc. attribute line. The `public AiLangError? Validate()` method is gone.

- [ ] **Step 5.6: Build**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. (The READ_BODY block at line ~715 still calls `body.Validate()` — see Task 6.)

Wait — `body.Validate()` no longer exists, so the build will fail with `CS1061`. Task 6 removes that call site. Either do Tasks 5+6 together, or accept the build failure here and only build after Task 6.

To keep the plan task-by-task clean: complete Step 5.7 and 5.8 below WITHOUT building (skip the build check), then immediately proceed to Task 6 which will fix the build.

- [ ] **Step 5.7: Commit (build intentionally broken until Task 6)**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): emit DataAnnotations attributes on DTO properties; delete hand-rolled Validate() method"
```

---

## Task 6: Remove the `body.Validate()` call from READ_BODY emit

`AddValidation()` runs before the handler executes. The manual `body.Validate()` check is redundant and now references a deleted method.

**Files:**
- Modify: `transpiler/transpiler.py:~715` (the READ_BODY emit, search for `body.Validate()`).

- [ ] **Step 6.1: Locate the READ_BODY emission**

Search `transpiler.py` for `body.Validate()`. Should appear once in `generate_csharp` near line 715, inside the READ_BODY emit.

- [ ] **Step 6.2: Delete the validation call block**

The block typically looks like:

```python
    lines.append(f"{pad}    var _verr = {assign}.Validate();")
    lines.append(f"{pad}    if (_verr != null) return Results.BadRequest(_verr);")
```

Delete both lines. The surrounding code (the try/catch on `JsonException` and the `if (body is null)` check) remains.

- [ ] **Step 6.3: Regenerate and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "body\.Validate\|_verr" bytecode/generated-csharp-product_service/Program.cs
```

Expected: NO matches. The validation call site is gone.

- [ ] **Step 6.4: Build the pilot**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. AddValidation now does the validation work.

- [ ] **Step 6.5: Commit**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): remove manual body.Validate() call from READ_BODY (AddValidation now runs before handler)"
```

---

## Task 7: Delete AiLangError + ErrorDetail emission from generate_error_model_csharp

Now that no call site references `AiLangError.X(...)` and DTOs no longer have a `Validate()` returning `AiLangError`, the legacy types can be removed.

**Files:**
- Modify: `transpiler/transpiler.py:3142-3201` (`generate_error_model_csharp`).

- [ ] **Step 7.1: Remove the AiLangError + ErrorDetail block**

In `generate_error_model_csharp`, delete every line from `lines.append("// ERROR MODEL: AILang native errors (legacy — being migrated)")` through `lines.append("}")` of `ErrorDetail` (lines ~3145-3201, but precise line numbers shift after previous tasks; the block to delete is the one that emits `public record AiLangError {...}` and `public record ErrorDetail {...}`).

Keep:
- The function signature.
- The `lines = []` initialization.
- The new `AiLangProblems` static class emission (added in Task 3).
- The custom validation attributes emission (added in Task 3).
- The custom errors loop (modified in Task 3).
- The final `return lines`.

Update the docstring to reflect the legacy types are gone:

```python
def generate_error_model_csharp(custom_errors: dict) -> list[str]:
    """Emit AiLangProblems factories + custom validation attributes for C#.

    Returns ProblemDetails-based error responses (RFC 7807) via Results.Problem(...).
    The legacy AiLangError record was removed as part of the 2026-06-10 migration.
    """
```

- [ ] **Step 7.2: Regenerate and inspect**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "public record AiLangError\|public record ErrorDetail\|AiLangError\." bytecode/generated-csharp-product_service/Program.cs
```

Expected: NO matches. AiLangError and ErrorDetail are gone from the generated output.

- [ ] **Step 7.3: Build**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. Tests will still fail because `.sexpr` assertions haven't been updated yet.

- [ ] **Step 7.4: Commit**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): delete legacy AiLangError + ErrorDetail emission"
```

---

## Task 8: Remove AiLangError + ErrorDetail from `_aot_json_types` source-gen registration list

The AOT JsonContext registered `AiLangError` and `ErrorDetail`. Now that those types no longer exist, the registrations would fail to compile. Remove them. `ProblemDetails` and `ValidationProblemDetails` are framework types with their own source-gen context shipped by ASP.NET 10 — no replacement registration needed unless AOT publish surfaces a warning.

**Files:**
- Modify: `transpiler/transpiler.py:1806` (`_aot_json_types`).

- [ ] **Step 8.1: Edit the helper**

Locate `_aot_json_types(bc)` in `transpiler.py`. The current `types += [...]` line:

```python
    types += ["AiLangError", "ErrorDetail", "TokenResponse"]
```

Replace with:

```python
    types += ["TokenResponse"]
```

- [ ] **Step 8.2: Regenerate the pilot under --aot**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
grep -nA 1 "JsonSerializerContext\|JsonSerializable" bytecode/generated-csharp-product_service/Program.cs | head -20
```

Expected: `[JsonSerializable(typeof(AiLangError))]` and `[JsonSerializable(typeof(ErrorDetail))]` are gone. `[JsonSerializable(typeof(TokenResponse))]` remains.

- [ ] **Step 8.3: Build the AOT csproj**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors, 0 warnings.

- [ ] **Step 8.4: AOT publish to verify the new wire shape under AOT**

```
dotnet publish -c Release -r win-x64 --nologo --verbosity quiet
```

Expected: 0 warnings, 0 errors. If the customizer (`opts.CustomizeProblemDetails = ctx => ...`) surfaces IL2026/IL3050 on the `object?` Extensions assignment, REPORT — that is Risk 1 from §11 of the spec. Mitigation: type the customizer's value explicitly as string (`ctx.ProblemDetails.Extensions["traceId"] = tid?.ToString();`).

- [ ] **Step 8.5: Restore non-AOT regen**

```
cd ../..
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
```

- [ ] **Step 8.6: Commit**

```
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): drop AiLangError + ErrorDetail from AOT source-gen JsonContext registration"
```

---

## Task 9: Update the 6 .sexpr bytecode files' wire-shape assertions

The `(ASSERT body.error.X ...)` assertion paths in the `.sexpr` files reflect the old AiLangError shape. They need to point at the new ProblemDetails paths.

Mapping:

| Before (`.sexpr`) | After (`.sexpr`) |
|---|---|
| `(ASSERT body.error.type "NOT_FOUND")` | `(ASSERT body.type "https://ailang.dev/errors/not-found")` |
| `(ASSERT body.error.type "BUSINESS_RULE")` | `(ASSERT body.type "https://ailang.dev/errors/business-rule")` |
| `(ASSERT body.error.type "VALIDATION")` | `(ASSERT body.type "https://ailang.dev/errors/validation")` |
| `(ASSERT body.error.type "UNAUTHORIZED")` | `(ASSERT body.type "https://ailang.dev/errors/unauthorized")` |
| `(ASSERT body.error.type "TIMEOUT")` | `(ASSERT body.type "https://ailang.dev/errors/timeout")` |
| `(ASSERT body.error.type "UNAVAILABLE")` | `(ASSERT body.type "https://ailang.dev/errors/unavailable")` |
| `(ASSERT body.error.type "INTERNAL")` | `(ASSERT body.type "https://ailang.dev/errors/internal")` |
| `(ASSERT body.error.type "INVALID_PARAM")` | `(ASSERT body.type "https://ailang.dev/errors/invalid-param")` |
| `(ASSERT body.error.type "INVALID_BODY")` | `(ASSERT body.type "https://ailang.dev/errors/invalid-body")` |
| `(ASSERT body.error.entity "product")` | `(ASSERT body.entity "product")` (extension field, no nesting) |
| `(ASSERT body.error.field "id")` | `(ASSERT body.field "id")` |
| `(ASSERT body.error.rule "X")` | `(ASSERT body.rule "X")` |

**Files:**
- Modify: `bytecode/order_service.sexpr`, `bytecode/pricing_service.sexpr`, `bytecode/payment_service.sexpr`, `bytecode/dashboard_service.sexpr`, `bytecode/product_service.sexpr`, `bytecode/product_service_pg.sexpr`.

- [ ] **Step 9.1: List every assertion site across the 6 files**

```
cd e:/02-lab/ai-lang
grep -n "body\.error" bytecode/order_service.sexpr bytecode/pricing_service.sexpr bytecode/payment_service.sexpr bytecode/dashboard_service.sexpr bytecode/product_service.sexpr bytecode/product_service_pg.sexpr
```

Expected: ~17 lines listed. Each one is an `(ASSERT body.error.X ...)` form.

- [ ] **Step 9.2: For each file, apply the mapping**

Edit each `.sexpr` file. For each match, transform per the table above. The pattern is:
- `body.error.type` → `body.type` AND the literal string changes to the URI.
- `body.error.entity` / `field` / `rule` / `id` / `service` → drop the `error.` prefix, keep the rest.

Example (`bytecode/product_service.sexpr` line 160):

Before:
```
(ASSERT body.error.type "NOT_FOUND")
```

After:
```
(ASSERT body.type "https://ailang.dev/errors/not-found")
```

Apply to all 17 sites.

- [ ] **Step 9.3: Verify no body.error references remain**

```
grep -n "body\.error" bytecode/*.sexpr
```

Expected: no output for the 6 reference services. (Scratch_phaseN files may still match — leave those for a future cleanup; they are not part of the regression suite.)

- [ ] **Step 9.4: Regenerate all 6 services**

```
for svc in order_service pricing_service payment_service dashboard_service product_service; do
  python transpiler/transpiler.py "bytecode/${svc}.sexpr" --target csharp
done
python transpiler/transpiler.py bytecode/product_service_pg.sexpr --target csharp
```

Expected: each prints `Done!`.

- [ ] **Step 9.5: Run all 5 in-memory test suites**

```
for svc in order_service pricing_service payment_service dashboard_service product_service; do
  (cd "bytecode/generated-csharp-$svc/Tests" && dotnet test --nologo --verbosity quiet)
done
```

Expected: 11/11 + 6/6 + 7/7 + 6/6 + 6/6 = 36/36 pass.

If a service fails, the diagnostic is in the test output. Most common failure: an assertion path missed in Step 9.2. Re-check the `.sexpr` and regenerate.

- [ ] **Step 9.6: Run PG tests (if Docker available)**

```
docker info >/dev/null 2>&1 && (cd bytecode/generated-csharp-product_service_pg/Tests && dotnet test --nologo --verbosity quiet) || echo "Docker unavailable — PG tests skipped"
```

Expected if Docker: 14/14. If skipped, acceptable per spec §10.

- [ ] **Step 9.7: Commit**

```
git add bytecode/order_service.sexpr bytecode/pricing_service.sexpr bytecode/payment_service.sexpr bytecode/dashboard_service.sexpr bytecode/product_service.sexpr bytecode/product_service_pg.sexpr
git commit -m "fix(bytecode): wire-shape assertions point at ProblemDetails paths instead of body.error.*"
```

---

## Task 10: Update the AOT smoke script substring grep checks

The smoke script asserts on the raw JSON body via `grep`. Two checks expect `"type":"NOT_FOUND"`; the new shape is `"type":"https://ailang.dev/errors/not-found"`.

**Files:**
- Modify: `bytecode/scripts/aot-smoke-product-service.sh`.

- [ ] **Step 10.1: Update the 404 grep**

In the smoke script, locate:

```bash
grep -q '"type":"NOT_FOUND"' /tmp/aot-smoke-resp \
  || { echo "[smoke] FAIL: 404 body missing NOT_FOUND"; cat /tmp/aot-smoke-resp; exit 1; }
echo "[smoke] GET /products/<missing> → 404 NOT_FOUND ✓"
```

Replace with:

```bash
grep -q '"type":"https://ailang.dev/errors/not-found"' /tmp/aot-smoke-resp \
  || { echo "[smoke] FAIL: 404 body missing not-found ProblemDetails type"; cat /tmp/aot-smoke-resp; exit 1; }
echo "[smoke] GET /products/<missing> → 404 not-found ProblemDetails ✓"
```

- [ ] **Step 10.2: Run the smoke script**

```
cd e:/02-lab/ai-lang
bash bytecode/scripts/aot-smoke-product-service.sh
```

Expected: final line `[smoke] PASS`. The four scenarios (`GET /products/ → []`, `GET <missing> → 404 ProblemDetails`, `/auth/token → JWT`, authenticated POST) all succeed.

- [ ] **Step 10.3: Commit**

```
git add bytecode/scripts/aot-smoke-product-service.sh
git commit -m "fix(smoke): assert on ProblemDetails not-found URI instead of legacy NOT_FOUND tag"
```

---

## Task 11: Emit `IValidatableObject` codegen for cross-field validators (no service uses it yet)

Implementation of the cross-field codegen path. The AILang Nivel 2 operators (`COMPARE`, `NOT_EQUAL`, `CONDITIONAL_REQUIRED`, `MUTUAL_EXCLUSIVE`, `AT_LEAST_ONE`, `SUM_MAX`) become `yield return new ValidationResult(...)` statements in an `IValidatableObject.Validate(ValidationContext)` body. The DTO record is declared `: IValidatableObject`.

No current service has these in its schema, so this task does not change any committed test output. Task 12 adds a synthetic test service that exercises the codegen.

**Files:**
- Modify: `transpiler/transpiler.py:~2396` (`generate_schema_csharp` and a new helper).

- [ ] **Step 11.1: Add a helper that converts a cross-field validator block to a yield-return statement**

Place this new function near `generate_schema_csharp` in `transpiler.py`:

```python
def _cross_field_validations_csharp(schema_def: dict) -> list[str]:
    """Emit IValidatableObject.Validate(...) body statements for each Nivel 2 operator.

    Returns a list of code lines (not including the method signature or braces).
    Returns an empty list if the schema has no cross-field validators.
    """
    validators = schema_def.get('cross_field_validators', []) or []
    if not validators:
        return []
    lines = []
    for v in validators:
        op = v.get('op')
        if op == 'CONDITIONAL_REQUIRED':
            # CONDITIONAL_REQUIRED: when {when_field} == {when_value}, {required_field} must be non-empty.
            when_field = pascal_case(v['when_field'])
            when_value = v['when_value']
            required_field = pascal_case(v['required_field'])
            lines.append(f"        if ({when_field} == \"{when_value}\" && string.IsNullOrEmpty({required_field}))")
            lines.append(f"            yield return new ValidationResult(\"required when {v['when_field']}={when_value}\", new[] {{ nameof({required_field}) }});")
        elif op == 'MUTUAL_EXCLUSIVE':
            # MUTUAL_EXCLUSIVE: at most one of the listed fields may be non-empty.
            fields = [pascal_case(f) for f in v['fields']]
            cond = " && ".join(f"!string.IsNullOrEmpty({f})" for f in fields)
            names = ", ".join(f"nameof({f})" for f in fields)
            lines.append(f"        if ({cond})")
            lines.append(f"            yield return new ValidationResult(\"{', '.join(v['fields'])} are mutually exclusive\", new[] {{ {names} }});")
        elif op == 'AT_LEAST_ONE':
            # AT_LEAST_ONE: at least one of the listed fields must be non-empty.
            fields = [pascal_case(f) for f in v['fields']]
            cond = " && ".join(f"string.IsNullOrEmpty({f})" for f in fields)
            names = ", ".join(f"nameof({f})" for f in fields)
            lines.append(f"        if ({cond})")
            lines.append(f"            yield return new ValidationResult(\"at least one of {', '.join(v['fields'])} must be provided\", new[] {{ {names} }});")
        elif op == 'COMPARE':
            # COMPARE: {left} {comparison} {right}. Comparison is one of <, <=, ==, !=, >=, >.
            left = pascal_case(v['left'])
            right = pascal_case(v['right'])
            cmp = v.get('comparison', '==')
            # Negate the comparison to detect violation.
            neg = {'<': '>=', '<=': '>', '==': '!=', '!=': '==', '>=': '<', '>': '<='}[cmp]
            lines.append(f"        if ({left} {neg} {right})")
            lines.append(f"            yield return new ValidationResult(\"{v['left']} must be {cmp} {v['right']}\", new[] {{ nameof({left}), nameof({right}) }});")
        elif op == 'NOT_EQUAL':
            # NOT_EQUAL: {left} != {right}.
            left = pascal_case(v['left'])
            right = pascal_case(v['right'])
            lines.append(f"        if ({left} == {right})")
            lines.append(f"            yield return new ValidationResult(\"{v['left']} and {v['right']} must differ\", new[] {{ nameof({left}), nameof({right}) }});")
        elif op == 'SUM_MAX':
            # SUM_MAX: sum of {fields} must be <= {max}.
            fields = [pascal_case(f) for f in v['fields']]
            max_v = v['max']
            sum_expr = " + ".join(f"(decimal){f}" for f in fields)
            names = ", ".join(f"nameof({f})" for f in fields)
            lines.append(f"        if ({sum_expr} > {max_v}m)")
            lines.append(f"            yield return new ValidationResult(\"sum of {', '.join(v['fields'])} must be <= {max_v}\", new[] {{ {names} }});")
        else:
            lines.append(f"        // UNKNOWN CROSS-FIELD OPERATOR: {op}")
    return lines
```

- [ ] **Step 11.2: Call the helper from generate_schema_csharp**

In `generate_schema_csharp`, after the DTO property emissions (before the closing `lines.append("}")`), add:

```python
        # Cross-field validation via IValidatableObject (Nivel 2 operators).
        cross_field_lines = _cross_field_validations_csharp(schema_def)
        if cross_field_lines:
            lines.append("")
            lines.append("    public IEnumerable<ValidationResult> Validate(ValidationContext _)")
            lines.append("    {")
            lines.extend(cross_field_lines)
            lines.append("    }")
```

Also update the record declaration to add `: IValidatableObject` when `cross_field_lines` is non-empty. Locate the line that emits `public record {name}Dto` (around line 2403) and change to:

```python
        iface = " : IValidatableObject" if schema_def.get('cross_field_validators') else ""
        lines.append(f"public record {name}Dto{iface}")
```

- [ ] **Step 11.3: Regenerate the pilot to confirm no regression for schemas WITHOUT cross-field**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "IValidatableObject\|Validate(ValidationContext" bytecode/generated-csharp-product_service/Program.cs
```

Expected: NO matches. `product_service` has no cross-field validators, so the codegen path is dormant.

- [ ] **Step 11.4: Build**

```
cd bytecode/generated-csharp-product_service
dotnet build --nologo
```

Expected: 0 errors. 6/6 tests pass.

- [ ] **Step 11.5: Commit**

```
cd ../..
git add transpiler/transpiler.py
git commit -m "feat(problemdetails): cross-field validation via IValidatableObject codegen (no current service uses it)"
```

---

## Task 12: Synthetic cross-field test — bytecode/scratch_cross_field.sexpr

A minimal AILang service whose only purpose is to exercise the new `IValidatableObject` codegen path. One handler accepts a DTO, and the DTO declares one cross-field validator. The generated test asserts that posting invalid data produces a `ValidationProblemDetails` with the cross-field violation.

**Files:**
- Create: `bytecode/scratch_cross_field.sexpr`.

- [ ] **Step 12.1: Write the scratch service**

Create `bytecode/scratch_cross_field.sexpr` with:

```
(service ScratchCrossField
  (port 8080))

(schemas
  (schema Order
    (fields
      (field delivery_type     string  (required))
      (field shipping_address  string)
      (field pickup_location   string))
    (cross_field_validators
      (CONDITIONAL_REQUIRED
        (when_field delivery_type)
        (when_value "ship")
        (required_field shipping_address))
      (MUTUAL_EXCLUSIVE
        (fields shipping_address pickup_location)))))

(auth
  (PUBLIC))

(routes
  (route POST "/orders" handler_create))

(handlers
  (handler handler_create
    (READ_BODY Order order)
    (RESPOND order 201)))

(tests
  (test "missing shipping_address when delivery_type=ship returns 400"
    (setup)
    (run
      (POST "/orders"
        (body {"delivery_type": "ship"})
        (expect status 400)
        (ASSERT body.type "https://ailang.dev/errors/validation")
        (ASSERT body.status 400)))
    (clean))
  (test "MUTUAL_EXCLUSIVE: both shipping and pickup returns 400"
    (setup)
    (run
      (POST "/orders"
        (body {"delivery_type": "pickup", "shipping_address": "123 St", "pickup_location": "Branch A"})
        (expect status 400)
        (ASSERT body.type "https://ailang.dev/errors/validation"))
    (clean))))
```

(Note: The exact `.sexpr` syntax for cross-field validator blocks may need adjustment to match the parser's grammar in `sexpr_bytecode_parser.py`. If the parser rejects the file, inspect the parser's expectations around `cross_field_validators` and adjust the syntax. If the parser doesn't yet support this grammar, that is a precondition discovery — REPORT it and update `sexpr_bytecode_parser.py` to accept the form before continuing.)

- [ ] **Step 12.2: Regenerate**

```
cd e:/02-lab/ai-lang
python transpiler/transpiler.py bytecode/scratch_cross_field.sexpr --target csharp
```

Expected: `Done!`. If the parser rejects the file, escalate.

- [ ] **Step 12.3: Inspect the generated IValidatableObject body**

```
grep -nA 6 "Validate(ValidationContext" bytecode/generated-csharp-scratch_cross_field/Program.cs
```

Expected: 2 cross-field checks emitted as `if (...) yield return new ValidationResult(...)` statements.

- [ ] **Step 12.4: Build and test**

```
cd bytecode/generated-csharp-scratch_cross_field
dotnet build --nologo
cd Tests
dotnet test --nologo --verbosity quiet
```

Expected: 2/2 tests pass. Both cross-field violations produce 400 ValidationProblemDetails responses.

- [ ] **Step 12.5: Commit**

```
cd ../../..
git add bytecode/scratch_cross_field.sexpr
git commit -m "test(problemdetails): synthetic scratch service exercises cross-field IValidatableObject codegen"
```

---

## Task 13: CLAUDE.md updates — §Errores and §Validación

Reflect the new error model and validation pipeline in the project's reference doc.

**Files:**
- Modify: `CLAUDE.md` — `### Errores` (line 54) and `### Validación` (line 114).

- [ ] **Step 13.1: Replace the `### Errores` section**

Locate `### Errores` at line 54. The section currently describes the AiLangError catalog and HTTP status mapping. Replace its body with:

```markdown
### Errores
- Wire format estándar: **RFC 7807 ProblemDetails** (`application/problem+json` Content-Type).
- Cada response de error contiene `type` (URI identificador), `title`, `status`, `traceId` (extension, inyectado por middleware) y `detail` opcional.
- Mapping completo de tipos AILang a URIs ProblemDetails:
  - `NOT_FOUND` (404) → `https://ailang.dev/errors/not-found` + ext `entity`, `entityId`
  - `VALIDATION` (400) → `https://ailang.dev/errors/validation` + ext `errors` (Dict<string, string[]>)
  - `BUSINESS_RULE` (422) → `https://ailang.dev/errors/business-rule` + ext `rule`
  - `UNAUTHORIZED` (401) → `https://ailang.dev/errors/unauthorized`
  - `TIMEOUT` (504) → `https://ailang.dev/errors/timeout` + ext `service`
  - `UNAVAILABLE` (503) → `https://ailang.dev/errors/unavailable` + ext `service`
  - `INTERNAL` (500) → `https://ailang.dev/errors/internal`
  - `INVALID_PARAM` (400) → `https://ailang.dev/errors/invalid-param` + ext `field`
  - `INVALID_BODY` (400) → `https://ailang.dev/errors/invalid-body` + ext `schema`
- RESULT types obligatorios siguen igual (Rust-inspired): si no hacés MATCH, no compila.
- Supervisión "let it crash" (Erlang/OTP) — fiber muere, supervisor reinicia.
- Errores custom extensibles con `define_error`; cada uno produce su propia URI bajo `https://ailang.dev/errors/{slug}`.
- Propagación de errores entre servicios preserva el `traceId` para correlación cross-service.
```

- [ ] **Step 13.2: Replace the `### Validación` section**

Locate `### Validación` at line 114. Replace its body with:

```markdown
### Validación
- Nivel 1 (field-level): schema → DataAnnotations attributes en el DTO (`[Required]`, `[StringLength]`, custom `[GreaterThan]`/`[LessThan]` para strict bounds). Validado por `AddValidation()` de .NET 10 (source-generator, AOT-friendly).
- Nivel 2 (cross-field): schema → `IValidatableObject.Validate(ValidationContext)` body. Operators: `COMPARE`, `NOT_EQUAL`, `CONDITIONAL_REQUIRED`, `MUTUAL_EXCLUSIVE`, `AT_LEAST_ONE`, `SUM_MAX`. Codegen emit cubierto; ningún servicio actual lo usa todavía.
- Wire response: `ValidationProblemDetails` con TODOS los errores (no first-fail-and-bail). `application/problem+json` Content-Type automático.
- READ_BODY no llama `Validate()` manualmente — `AddValidation` corre antes del handler.
```

- [ ] **Step 13.3: Verify the diff**

```
cd e:/02-lab/ai-lang
git diff CLAUDE.md
```

Expected: only the two sections changed; no drift elsewhere.

- [ ] **Step 13.4: Commit**

```
git add CLAUDE.md
git commit -m "docs(claude): update §Errores and §Validación for ProblemDetails + AddValidation"
```

---

## Task 14: README — note about ProblemDetails wire shape

Brief mention that AILang generates RFC 7807-compliant error responses.

**Files:**
- Modify: `README.md`.

- [ ] **Step 14.1: Add a paragraph after the existing transpile section**

Locate the `## Transpilar` section in `README.md` (added during the NativeAOT path implementation). Below the existing `--aot` paragraph, add:

```markdown
**Error responses**: AILang's generated C# services emit RFC 7807 `application/problem+json` responses for every error path. The wire shape uses URIs like `https://ailang.dev/errors/not-found` and includes a `traceId` extension for correlation with structured logs. Validation errors are collected by `AddValidation()` and returned as `ValidationProblemDetails` (all violations in one response).
```

- [ ] **Step 14.2: Verify**

```
cd e:/02-lab/ai-lang
git diff README.md
```

Expected: only the new paragraph added; no other changes.

- [ ] **Step 14.3: Commit**

```
git add README.md
git commit -m "docs(readme): mention ProblemDetails wire shape for generated error responses"
```

---

## Task 15: Final regression sweep + AOT smoke + spec status flip

Verify the entire migration:

- [ ] **Step 15.1: C# in-memory regression**

```
cd e:/02-lab/ai-lang
for svc in order_service pricing_service payment_service dashboard_service product_service; do
  pushd "bytecode/generated-csharp-$svc/Tests" >/dev/null
  dotnet test --nologo --verbosity quiet
  popd >/dev/null
done
```

Expected: 11 + 6 + 7 + 6 + 6 = 36/36 pass.

- [ ] **Step 15.2: PG suite (if Docker available)**

```
docker info >/dev/null 2>&1 && (cd bytecode/generated-csharp-product_service_pg/Tests && dotnet test --nologo --verbosity quiet) || echo "Docker unavailable — PG suite skipped (acceptable per spec §10)"
```

Expected: 14/14 if Docker present.

- [ ] **Step 15.3: AOT smoke**

```
bash bytecode/scripts/aot-smoke-product-service.sh
```

Expected: `[smoke] PASS`. Binary size in the ~20 MB band (no significant regression from the NativeAOT baseline of 19.6 MB).

- [ ] **Step 15.4: Restore canonical non-AOT regen**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
```

- [ ] **Step 15.5: Flip the spec status header**

Edit `docs/superpowers/specs/2026-06-10-problemdetails-migration-design.md` line 5:

Change:
```
**Status:** Approved by user; pending spec review
```

To:
```
**Status:** Approved by user; implemented in plan `docs/superpowers/plans/2026-06-10-problemdetails-migration.md`
```

- [ ] **Step 15.6: Commit the spec flip**

```
git add docs/superpowers/specs/2026-06-10-problemdetails-migration-design.md
git commit -m "docs(spec): mark ProblemDetails migration spec as implemented"
```

---

## Self-Review

**Spec coverage:**

- §2 "Decision" — adopt RFC 7807 → Tasks 1, 2, 3, 4, 9, 10. ✓
- §2 "Field-level validation via AddValidation + DataAnnotations" → Tasks 1, 2, 5. ✓
- §2 "Cross-field via IValidatableObject" → Tasks 11, 12. ✓
- §2 "All 6 services + smoke script in one pass" → Tasks 9, 10, 15. ✓
- §2 "Go target not migrated" → not touched, by omission. ✓
- §3 In-scope: csproj + service registration + DTO emit + IValidatableObject + AiLangProblems helper + custom attrs + remove AiLangError + READ_BODY simplification → Tasks 1, 2, 3, 4, 5, 6, 7, 11. ✓
- §3 In-scope: `_aot_json_types` update → Task 8. ✓
- §3 In-scope: bytecode `.sexpr` updates → Task 9. ✓
- §3 In-scope: smoke script → Task 10. ✓
- §3 In-scope: CLAUDE.md + README → Tasks 13, 14. ✓
- §4 Wire shape table → encoded in Tasks 3 (AiLangProblems factories) and 9 (assertion paths). ✓
- §5 Codegen architecture (5 sub-changes) → Tasks 1-8, 11. ✓
- §6 Test strategy (typed deserialization) → assertions already use `_body.GetProperty(...).GetString()` (`_cs_json_walk`); only the paths change. Task 9 handles this. ✓
- §10 Success criteria → Task 15 verifies. ✓

**Placeholder scan:**

- Task 12 Step 12.1 has the conditional "if the parser rejects the file, escalate" which is an escape hatch but the rest of the step is concrete. Acceptable.
- All other tasks have concrete code blocks and exact commands.
- No "TBD", "implement later", "handle edge cases", or "Similar to Task N" patterns.

**Type consistency:**

- `AiLangProblems` is the helper class name throughout (Tasks 3, 4, 7).
- `GreaterThanAttribute`, `LessThanAttribute`, `GreaterThanOrEqualAttribute`, `LessThanOrEqualAttribute` are the custom attribute names throughout (Tasks 3, 5, 13).
- `_cross_field_validations_csharp` is the helper function name (Tasks 11, 12).
- The URI scheme `https://ailang.dev/errors/{slug}` is consistent across Tasks 3, 9, 10, 13, 14.
- `IValidatableObject.Validate(ValidationContext _)` signature is consistent in Tasks 11, 12, 13.
