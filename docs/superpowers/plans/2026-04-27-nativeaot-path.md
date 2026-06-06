# NativeAOT Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `--aot` opt-in mode for the C# codegen that produces a single self-contained native binary for one reference service (`product_service` in-memory), with the dependency surface verified AOT-clean via a separate probe project.

**Architecture:** `transpiler.py` gains a single `aot: bool` flag threaded into `generate_csharp()`. Under `aot=True` the emitted `.csproj` adds `PublishAot`, `InvariantGlobalization`, and `TreatWarningsAsErrors`; a `partial class <Service>JsonContext : JsonSerializerContext` is emitted listing every DTO + response type with `[JsonSerializable]`; the context is wired via `ConfigureHttpJsonOptions`; `ReadFromJsonAsync<T>()` call sites switch to the context-aware overload; and the one anonymous-type response (`/auth/token`) becomes a named record. Other services and the Go target are untouched. The Go target rejects `--aot` at the CLI. A standalone `bytecode/probes/aot-probe/` project verifies dependency-level AOT-cleanness independently of codegen. A bash smoke script regenerates → publishes → runs → curls → asserts → kills.

**Tech Stack:** Python 3 (transpiler), .NET 10 LTS, ASP.NET Minimal APIs, NativeAOT publish, System.Text.Json source generation, Polly 8 (resilience deps via probe only — pilot service does not use them), Npgsql 10 (probe only), JwtBearer 10.

**Spec reference:** `docs/superpowers/specs/2026-04-27-cs-primary-go-frozen-design.md` (commit `1fb1fb5`). §4 describes the codegen requirements; §6 item 1 the scope; §8 the success criteria.

---

## File Structure

**Created (new files):**
- `bytecode/probes/aot-probe/AotProbe.csproj` — probe project, `<PublishAot>true</PublishAot>`, imports every dependency we emit.
- `bytecode/probes/aot-probe/Program.cs` — exercises Polly, JwtBearer, Npgsql, Minimal APIs, source-gen JSON; build target is publish-clean.
- `bytecode/probes/aot-probe/AotProbeContext.cs` — source-gen JSON context for the probe's DTOs.
- `bytecode/probes/aot-probe/README.md` — one-paragraph instructions for re-running the probe.
- `bytecode/scripts/aot-smoke-product-service.sh` — end-to-end smoke: regenerate → publish → run → curl → assert → kill.

**Modified:**
- `transpiler/transpiler.py`:
  - `main()` (~line 7209): parse `--aot`; reject `--target go --aot`; pass `aot` to `generate_csharp`.
  - `generate_csharp()` (line 1794): accept `aot: bool` and thread; csproj emit (line 2272) branches on `aot`; emit `<Service>JsonContext` class; wire `ConfigureHttpJsonOptions`; replace anonymous `/auth/token` response with a concrete record.
  - JSON deserialization site (line 705 `ReadFromJsonAsync<{schema}Dto>()`): branch on `aot` to context-aware overload.
- `CLAUDE.md`: apply spec §5 edits (close runtime/memory/interop pending items; add Targets section; replace roadmap item #3).
- `README.md`: brief note on `--aot` usage.

**Out of scope (not touched):**
- Other five generated services (`order/pricing/payment/dashboard/product_service_pg`) — they keep emitting today's csproj/code.
- Go codegen — frozen per spec §3.
- `transpiler.py` JSON sites used only by EVENT_HANDLER (line 2488) and STREAM_RESPOND (line 1564) — pilot service does not use either; future spec for a service that does.
- AST/parser — no changes.

---

## Task 1: Probe project verifies the dependency surface is AOT-clean

Reason this is first: if Polly or Npgsql emits AOT warnings under .NET 10, we need to know before changing the transpiler. The probe is throwaway-ish but lives in the repo so future agents can re-run it.

**Files:**
- Create: `bytecode/probes/aot-probe/AotProbe.csproj`
- Create: `bytecode/probes/aot-probe/Program.cs`
- Create: `bytecode/probes/aot-probe/AotProbeContext.cs`
- Create: `bytecode/probes/aot-probe/README.md`

- [ ] **Step 1.1: Create the probe csproj**

Create `bytecode/probes/aot-probe/AotProbe.csproj`:

```xml
<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <PublishAot>true</PublishAot>
    <InvariantGlobalization>true</InvariantGlobalization>
    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
    <TrimmerSingleWarn>false</TrimmerSingleWarn>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Microsoft.AspNetCore.Authentication.JwtBearer" Version="10.0.0" />
    <PackageReference Include="System.IdentityModel.Tokens.Jwt" Version="8.2.1" />
    <PackageReference Include="Npgsql" Version="10.0.0" />
    <PackageReference Include="Polly" Version="8.4.2" />
    <PackageReference Include="Microsoft.Extensions.Http.Resilience" Version="9.0.0" />
  </ItemGroup>
</Project>
```

- [ ] **Step 1.2: Create the source-gen JSON context for the probe**

Create `bytecode/probes/aot-probe/AotProbeContext.cs`:

```csharp
using System.Text.Json.Serialization;

namespace AotProbe;

public record ProbeDto(string Name, int Count);

[JsonSourceGenerationOptions(WriteIndented = false)]
[JsonSerializable(typeof(ProbeDto))]
internal partial class AotProbeJsonContext : JsonSerializerContext;
```

- [ ] **Step 1.3: Create the probe Program.cs exercising each dependency**

Create `bytecode/probes/aot-probe/Program.cs`:

```csharp
using System.IdentityModel.Tokens.Jwt;
using System.Security.Claims;
using System.Text;
using AotProbe;
using Microsoft.IdentityModel.Tokens;
using Npgsql;
using Polly;
using Polly.Retry;

// Polly: build a retry pipeline (does not need to execute).
var pipeline = new ResiliencePipelineBuilder()
    .AddRetry(new RetryStrategyOptions { MaxRetryAttempts = 1 })
    .Build();

// JwtBearer + System.IdentityModel.Tokens.Jwt: issue a token.
var key = new SymmetricSecurityKey(Encoding.UTF8.GetBytes("dev-secret-key-min-32-characters!!"));
var creds = new SigningCredentials(key, SecurityAlgorithms.HmacSha256);
var token = new JwtSecurityToken(
    claims: [new Claim("sub", "probe")],
    expires: System.DateTime.UtcNow.AddMinutes(1),
    signingCredentials: creds);
_ = new JwtSecurityTokenHandler().WriteToken(token);

// Npgsql: construct a connection string + builder (do NOT open a real connection).
var nb = new NpgsqlConnectionStringBuilder
{
    Host = "127.0.0.1", Port = 5432, Username = "probe", Password = "probe", Database = "probe"
};
_ = nb.ToString();

// Minimal APIs + source-gen JSON.
var builder = WebApplication.CreateSlimBuilder(args);
builder.Services.ConfigureHttpJsonOptions(o =>
    o.SerializerOptions.TypeInfoResolverChain.Insert(0, AotProbeJsonContext.Default));
var app = builder.Build();
app.MapGet("/probe", () => new ProbeDto("probe", 1));

// Print and exit immediately — we want publish-clean, not a long-running probe.
System.Console.WriteLine("[AOT-PROBE] dependencies wired");
System.Console.WriteLine($"[AOT-PROBE] pipeline={pipeline.GetType().Name}");
```

- [ ] **Step 1.4: Create the probe README**

Create `bytecode/probes/aot-probe/README.md`:

```markdown
# AOT Probe

Standalone .NET 10 project that imports every dependency the AILang C# codegen emits and is configured with `<PublishAot>true</PublishAot>` + `<TreatWarningsAsErrors>true</TreatWarningsAsErrors>`. If any dependency surfaces an AOT analyzer warning, `dotnet publish` fails — which is what we want.

## Run

```
cd bytecode/probes/aot-probe
dotnet publish -c Release -r linux-x64   # or win-x64 / osx-arm64
```

A successful publish prints `Build succeeded` with zero warnings, and emits `bin/Release/net10.0/<rid>/publish/AotProbe(.exe)`. Run the binary; it should print `[AOT-PROBE] dependencies wired` and exit.

Re-run this probe whenever we bump a package version listed in `AotProbe.csproj` so AOT-warning regressions are caught before they reach the transpiler.
```

- [ ] **Step 1.5: Verify the probe publishes clean**

Run from repo root (target your local OS RID — `linux-x64` on WSL, `win-x64` on Windows, `osx-arm64` on Mac):

```
cd bytecode/probes/aot-probe
dotnet publish -c Release -r win-x64
```

Expected: `Build succeeded` with `0 Warning(s), 0 Error(s)` and a published binary under `bin/Release/net10.0/win-x64/publish/`.

If warnings appear, do not proceed to Task 2. Diagnose: read the warning, identify the offending package, search Context7 for "<package-name> NativeAOT compatibility .NET 10", and update the probe (or pin a different version) until clean. Document any package version bumps that became necessary.

- [ ] **Step 1.6: Run the published binary to confirm it executes**

```
./bin/Release/net10.0/win-x64/publish/AotProbe.exe
```

Expected stdout (exact strings):
```
[AOT-PROBE] dependencies wired
[AOT-PROBE] pipeline=ResiliencePipeline
```

If the binary throws or crashes, the dependency surface is not actually AOT-safe at runtime even though publish was clean. Stop and investigate.

- [ ] **Step 1.7: Commit**

```
git add bytecode/probes/aot-probe/
git commit -m "feat(aot): standalone probe project verifies dependency surface is AOT-clean"
```

---

## Task 2: CLI plumbing — accept `--aot`, reject `--target go --aot`

**Files:**
- Modify: `transpiler/transpiler.py:7209-7270` (`main()`)
- Modify: `transpiler/transpiler.py:1794` (`generate_csharp` signature)

- [ ] **Step 2.1: Add `--aot` parsing and the go-guard to `main()`**

In `transpiler/transpiler.py`, locate the existing `main()` block:

```python
def main():
    if len(sys.argv) < 2:
        print("AILang Transpiler v0.1")
        print("Usage: python transpiler.py <bytecode.json> [--target csharp|go|all]")
        sys.exit(1)

    filepath = sys.argv[1]
    target = "all"

    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
```

Replace with:

```python
def main():
    if len(sys.argv) < 2:
        print("AILang Transpiler v0.2")
        print("Usage: python transpiler.py <bytecode.json> [--target csharp|go|all] [--aot]")
        print()
        print("  --aot    Emit C# project with <PublishAot>true</PublishAot> and a")
        print("           source-generated JSON context. Implies single-binary publish.")
        print("           Rejected with --target go (Go does not support NativeAOT in this transpiler).")
        sys.exit(1)

    filepath = sys.argv[1]
    target = "all"
    aot = "--aot" in sys.argv

    if "--target" in sys.argv:
        idx = sys.argv.index("--target")
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]

    if aot and target in ("go", "all"):
        print(f"ERROR: --aot is only supported with --target csharp (got --target {target}).", file=sys.stderr)
        print("       NativeAOT is a .NET concept; the Go codegen does not produce AOT output.", file=sys.stderr)
        sys.exit(2)
```

- [ ] **Step 2.2: Thread `aot` into the `generate_csharp` call**

Still in `main()`, find:

```python
    if target in ('csharp', 'all'):
        output_dir = os.path.join(base_dir, f"generated-csharp-{source_stem}")
        generate_csharp(bc, output_dir)
        generate_csharp_tests(bc, output_dir)
```

Replace with:

```python
    if target in ('csharp', 'all'):
        output_dir = os.path.join(base_dir, f"generated-csharp-{source_stem}")
        generate_csharp(bc, output_dir, aot=aot)
        generate_csharp_tests(bc, output_dir)
```

(Test project stays non-AOT — that's intentional. xUnit's discovery uses reflection.)

- [ ] **Step 2.3: Update the `generate_csharp` signature**

Locate at `transpiler/transpiler.py:1794`:

```python
def generate_csharp(bc, output_dir: str):
```

Replace with:

```python
def generate_csharp(bc, output_dir: str, aot: bool = False):
```

- [ ] **Step 2.4: Verify CLI behavior with all three rejection paths**

Run from repo root:

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target go --aot
```

Expected: exits with code 2, stderr contains "ERROR: --aot is only supported with --target csharp".

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target all --aot
```

Expected: same — exit 2, same error message.

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
```

Expected: exits 0, prints normal "Done!" line. (Codegen still produces the *same* output as today; `aot=True` is plumbed but unused. The csproj/source-gen changes come in Tasks 3–6.)

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
```

Expected: exits 0, normal behavior, no AOT effect.

- [ ] **Step 2.5: Commit**

```
git add transpiler/transpiler.py
git commit -m "feat(aot): --aot CLI flag plumbed to generate_csharp; rejected with --target go"
```

---

## Task 3: csproj branches on `aot` — add AOT properties

**Files:**
- Modify: `transpiler/transpiler.py:2272-2289` (csproj emit inside `generate_csharp`)

- [ ] **Step 3.1: Branch the PropertyGroup based on `aot`**

Locate the existing csproj emission block at approximately line 2272:

```python
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <!-- Tests/ is its own project; ASP.NET would otherwise auto-include it. -->\n'
        '    <Compile Remove="Tests/**" />\n'
        '    <Content Remove="Tests/**" />\n'
        '    <None Remove="Tests/**" />\n'
        '  </ItemGroup>\n'
        '  <ItemGroup>\n'
        + '\n'.join(pkg_refs) + '\n'
        '  </ItemGroup>\n'
        '</Project>'
    )
```

Replace with:

```python
    aot_props = (
        '    <PublishAot>true</PublishAot>\n'
        '    <InvariantGlobalization>true</InvariantGlobalization>\n'
        '    <TreatWarningsAsErrors>true</TreatWarningsAsErrors>\n'
        '    <TrimmerSingleWarn>false</TrimmerSingleWarn>\n'
    ) if aot else ''
    csproj = (
        '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
        '  <PropertyGroup>\n'
        '    <TargetFramework>net10.0</TargetFramework>\n'
        '    <Nullable>enable</Nullable>\n'
        '    <ImplicitUsings>enable</ImplicitUsings>\n'
        + aot_props +
        '  </PropertyGroup>\n'
        '  <ItemGroup>\n'
        '    <!-- Tests/ is its own project; ASP.NET would otherwise auto-include it. -->\n'
        '    <Compile Remove="Tests/**" />\n'
        '    <Content Remove="Tests/**" />\n'
        '    <None Remove="Tests/**" />\n'
        '  </ItemGroup>\n'
        '  <ItemGroup>\n'
        + '\n'.join(pkg_refs) + '\n'
        '  </ItemGroup>\n'
        '</Project>'
    )
```

- [ ] **Step 3.2: Regenerate the pilot with `--aot` and eyeball the csproj**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
```

Then read `bytecode/generated-csharp-product_service/ProductService.csproj` and verify the four AOT properties are present and the rest of the file is unchanged.

- [ ] **Step 3.3: Regenerate without `--aot` and verify diff is zero**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
git diff bytecode/generated-csharp-product_service/ProductService.csproj
```

Expected: no diff vs the pre-AOT version. (The non-AOT path is bit-for-bit identical to today's output.)

- [ ] **Step 3.4: Commit**

```
git add transpiler/transpiler.py bytecode/generated-csharp-product_service/ProductService.csproj
git commit -m "feat(aot): csproj emits PublishAot+InvariantGlobalization+TreatWarningsAsErrors under --aot"
```

(Note: regenerate without `--aot` first so the committed `ProductService.csproj` in the repo stays the non-AOT version — the AOT csproj is produced on demand by Task 7's smoke script.)

---

## Task 4: Source-gen `JsonContext` class + `ConfigureHttpJsonOptions` wiring

This task emits the JsonContext class but does NOT yet switch `ReadFromJsonAsync<T>()` to the context-aware overload. After this task, `dotnet build` is clean (the context is unused but valid); AOT publish still fails because Minimal APIs auto-serialization of responses now goes through the configured options but the Read sites still take the reflection path. Task 5 closes that.

**Files:**
- Modify: `transpiler/transpiler.py:1794` (`generate_csharp`): emit the JsonContext class after schemas; add `ConfigureHttpJsonOptions` to the host builder.

- [ ] **Step 4.1: Collect the set of types to register in JsonContext**

Inside `generate_csharp`, after the existing schema/DTO emission, compute the set of types. The minimum for the pilot is the union of:

- Every `<Schema>` record (e.g. `Product`).
- Every `<Schema>Dto` record (e.g. `ProductDto`).
- `AiLangError` and `ErrorDetail` (always emitted).
- `List<T>` of every Schema that is returned from a handler (Minimal APIs response serialization).
- A new `TokenResponse` record (introduced in Task 6) — defer reference until Task 6 lands.

Add a helper in `generate_csharp` near where schemas are walked:

```python
def _aot_json_types(bc) -> list[str]:
    schema_names = sorted(bc.schemas.keys())  # e.g. ['Product']
    types = []
    for s in schema_names:
        types.append(s)
        types.append(f"{s}Dto")
        types.append(f"List<{s}>")
    types += ["AiLangError", "ErrorDetail"]
    return types
```

Call site (inside `generate_csharp`, only when `aot=True`):

```python
    if aot:
        json_types = _aot_json_types(bc)
        service_pascal = pascal_case(bc.service['name'])
        ctx_lines = [
            '',
            '// ── AOT: source-generated JSON context ──',
            f'[System.Text.Json.Serialization.JsonSourceGenerationOptions(WriteIndented = false)]',
        ]
        for t in json_types:
            ctx_lines.append(f'[System.Text.Json.Serialization.JsonSerializable(typeof({t}))]')
        ctx_lines.append(f'internal partial class {service_pascal}JsonContext : System.Text.Json.Serialization.JsonSerializerContext;')
        # Insert these after the last DTO/error-model emission. The exact insertion
        # point depends on the current Program.cs layout — append to the end of
        # program_lines BEFORE the `public partial class Program {}` line.
        # See existing `program_lines.append(...)` calls; insert just above the
        # final `public partial class Program {}`.
        program_lines.extend(ctx_lines)
```

(The exact `program_lines` variable name is the one the function already uses to accumulate Program.cs content. Search for `public partial class Program {` in `generate_csharp` to find the insertion point.)

- [ ] **Step 4.2: Wire `ConfigureHttpJsonOptions` into the host builder**

Find the existing line in the codegen that produces:

```
builder.Services.AddAuthorization();
```

Immediately after it, when `aot=True`, append:

```python
    if aot:
        service_pascal = pascal_case(bc.service['name'])
        program_lines.append('')
        program_lines.append('// ── AOT: register source-gen JSON context ──')
        program_lines.append('builder.Services.ConfigureHttpJsonOptions(o =>')
        program_lines.append(f'    o.SerializerOptions.TypeInfoResolverChain.Insert(0, {service_pascal}JsonContext.Default));')
```

- [ ] **Step 4.3: Regenerate the pilot with `--aot` and inspect**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
```

Read `bytecode/generated-csharp-product_service/Program.cs`. Verify:
1. `ConfigureHttpJsonOptions` is present after `AddAuthorization`.
2. `internal partial class ProductServiceJsonContext : JsonSerializerContext;` is present near the end of the file, after the `AiLangError`/`ErrorDetail` records.
3. The list of `[JsonSerializable(...)]` attributes contains: `Product`, `ProductDto`, `List<Product>`, `AiLangError`, `ErrorDetail`.

- [ ] **Step 4.4: Build to confirm the file compiles**

```
cd bytecode/generated-csharp-product_service
dotnet build
```

Expected: `Build succeeded` with 0 errors. Warnings about unused JsonContext are OK at this point — they will go away once Task 5 switches the Read sites.

- [ ] **Step 4.5: Verify non-AOT output is unchanged**

```
cd ../..
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
git diff --stat bytecode/generated-csharp-product_service/Program.cs
```

Expected: zero diff. The non-AOT path emits today's Program.cs byte-for-byte.

- [ ] **Step 4.6: Commit**

```
git add transpiler/transpiler.py
git commit -m "feat(aot): emit JsonSerializerContext + ConfigureHttpJsonOptions under --aot"
```

---

## Task 5: Switch `ReadFromJsonAsync<T>()` to the context-aware overload

After this task the AOT publish will finally succeed (assuming Task 6's anonymous-type fix is also done — bundle them or order Task 6 first if you prefer to bisect).

**Files:**
- Modify: `transpiler/transpiler.py:705` (the `ReadFromJsonAsync` emission site)

- [ ] **Step 5.1: Find the emission site**

Locate at approximately `transpiler/transpiler.py:705`:

```python
        lines.append(f"{pad}try {{ {assign} = await ctx.Request.ReadFromJsonAsync<{schema}Dto>(); }}")
```

Note the surrounding function — search backwards from this line for the enclosing `def ` to find which function emits it. Likely `emit_read_body_csharp` or similar. Whatever its name, the function needs access to a flag indicating AOT mode.

- [ ] **Step 5.2: Thread `aot` to the call site**

If the function is purely a helper without context, the cleanest fix is to add `aot: bool = False` as a parameter and propagate from `generate_csharp`. If it already receives a context dict, add `aot` to that dict instead. Either way, end up with `aot` accessible at line 705.

(Implementer's choice — whatever needs the fewest call-site changes. Search for callers of the emit function to estimate. If there's just one caller, parameter; if many, ctx dict.)

- [ ] **Step 5.3: Branch the emission**

Replace line 705 with:

```python
        if aot:
            service_pascal = pascal_case(service_name)  # service_name must also be in scope; if not, thread it too
            lines.append(f"{pad}try {{ {assign} = await ctx.Request.ReadFromJsonAsync({service_pascal}JsonContext.Default.{schema}Dto); }}")
        else:
            lines.append(f"{pad}try {{ {assign} = await ctx.Request.ReadFromJsonAsync<{schema}Dto>(); }}")
```

(If `service_name` is not in scope here, thread it the same way you thread `aot`.)

- [ ] **Step 5.4: Regenerate and inspect**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
grep -n ReadFromJsonAsync bytecode/generated-csharp-product_service/Program.cs
```

Expected output: two lines, both reading
```
try { body = await ctx.Request.ReadFromJsonAsync(ProductServiceJsonContext.Default.ProductDto); }
```

- [ ] **Step 5.5: Build to confirm compile-clean**

```
cd bytecode/generated-csharp-product_service
dotnet build
```

Expected: `Build succeeded`, 0 errors, 0 warnings.

- [ ] **Step 5.6: Confirm non-AOT regeneration is unchanged**

```
cd ../..
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
git diff bytecode/generated-csharp-product_service/Program.cs | grep -c ReadFromJsonAsync
```

Expected output: `2` (both deserialize sites use the generic overload, exactly as today).

- [ ] **Step 5.7: Commit**

```
git add transpiler/transpiler.py
git commit -m "feat(aot): ReadFromJsonAsync uses source-gen context overload under --aot"
```

---

## Task 6: Replace the anonymous `/auth/token` response with a concrete record

`Results.Ok(new { token = ... })` is the only anonymous-type response in the pilot. Under AOT, Minimal APIs may not be able to resolve a JSON type-info for it via the source-gen context. Replace with a named record and add it to the context.

**Files:**
- Modify: `transpiler/transpiler.py` — the block that emits the `/auth/token` route (search for `/auth/token` in the file). Likely around the bottom of `generate_csharp` near where the development token endpoint is emitted.
- Modify: `transpiler/transpiler.py:_aot_json_types` (added in Task 4) — add `TokenResponse` to the list when `aot=True`.

- [ ] **Step 6.1: Find and replace the `/auth/token` emit**

Locate the codegen line that produces:

```csharp
return Results.Ok(new { token = new System.IdentityModel.Tokens.Jwt.JwtSecurityTokenHandler().WriteToken(token) });
```

Replace the emission with code that produces:

```csharp
return Results.Ok(new TokenResponse(new System.IdentityModel.Tokens.Jwt.JwtSecurityTokenHandler().WriteToken(token)));
```

Then, in the same function, ensure that a `TokenResponse` record is always emitted (regardless of `aot`, for consistency):

```csharp
public record TokenResponse([property: System.Text.Json.Serialization.JsonPropertyName("token")] string Token);
```

(Always-emit is simpler than `aot`-conditional emit. It changes the non-AOT output too — but only the dev-only token endpoint — which is acceptable; the wire shape `{"token":"..."}` is identical.)

- [ ] **Step 6.2: Add `TokenResponse` to the AOT context type list**

In the `_aot_json_types` helper from Task 4:

```python
def _aot_json_types(bc) -> list[str]:
    schema_names = sorted(bc.schemas.keys())
    types = []
    for s in schema_names:
        types.append(s)
        types.append(f"{s}Dto")
        types.append(f"List<{s}>")
    types += ["AiLangError", "ErrorDetail", "TokenResponse"]
    return types
```

- [ ] **Step 6.3: Regenerate (both AOT and non-AOT) and inspect**

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp --aot
grep -n "TokenResponse\|auth/token" bytecode/generated-csharp-product_service/Program.cs
```

Expected:
- `auth/token` route now does `Results.Ok(new TokenResponse(...))`.
- A `public record TokenResponse(...)` definition exists.
- `[JsonSerializable(typeof(TokenResponse))]` is present in the JsonContext attributes.

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
grep -n "TokenResponse" bytecode/generated-csharp-product_service/Program.cs
```

Expected: `TokenResponse` record + `Results.Ok(new TokenResponse(...))` present. (No JsonContext block in non-AOT output.)

- [ ] **Step 6.4: Run the existing tests to verify wire format unchanged**

```
cd bytecode/generated-csharp-product_service/Tests
dotnet test
```

Expected: 6/6 pass. The `/auth/token` response wire format `{"token":"..."}` is identical to before — the test (which fetches a token and uses it as a bearer) sees no behavioral difference.

- [ ] **Step 6.5: Commit**

```
cd ../../..
git add transpiler/transpiler.py bytecode/generated-csharp-product_service/Program.cs
git commit -m "feat(aot): replace anonymous /auth/token response with concrete TokenResponse record"
```

---

## Task 7: Smoke script publishes the pilot as a native binary and asserts behavior

End-to-end verification: regenerate with `--aot`, `dotnet publish` to a single binary, run it, curl it, kill it. This is THE success-criterion proof from spec §8.

**Files:**
- Create: `bytecode/scripts/aot-smoke-product-service.sh`

- [ ] **Step 7.1: Write the smoke script**

Create `bytecode/scripts/aot-smoke-product-service.sh`:

```bash
#!/usr/bin/env bash
# AOT smoke check for product_service.
# Verifies that `--aot` produces a native binary that boots, responds, and exits cleanly.
# Exits non-zero on any failure. Safe to re-run.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SVC_DIR="$REPO_ROOT/bytecode/generated-csharp-product_service"

# Detect RID
case "$(uname -s)" in
  Linux*)   RID=linux-x64 ;;
  Darwin*)  RID=osx-arm64 ;;
  MINGW*|MSYS*|CYGWIN*) RID=win-x64 ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

echo "[smoke] RID=$RID"

echo "[smoke] regenerating with --aot"
python "$REPO_ROOT/transpiler/transpiler.py" \
    "$REPO_ROOT/bytecode/product_service.sexpr" \
    --target csharp --aot

cd "$SVC_DIR"

echo "[smoke] dotnet publish (AOT)"
dotnet publish -c Release -r "$RID" --nologo --verbosity quiet

PUB_DIR="$SVC_DIR/bin/Release/net10.0/$RID/publish"
case "$RID" in
  win-x64) BIN="$PUB_DIR/ProductService.exe" ;;
  *) BIN="$PUB_DIR/ProductService" ;;
esac

if [ ! -x "$BIN" ]; then
  echo "[smoke] FAIL: published binary not found at $BIN" >&2
  exit 1
fi

BIN_SIZE_MB=$(du -m "$BIN" | cut -f1)
echo "[smoke] binary: $BIN (${BIN_SIZE_MB} MB)"

echo "[smoke] starting binary on :8080"
"$BIN" >/tmp/aot-smoke.log 2>&1 &
PID=$!
trap "kill $PID 2>/dev/null || true; rm -f /tmp/aot-smoke.log" EXIT

# Wait up to 10s for [READY]
for i in $(seq 1 20); do
  if grep -q "\[READY\]" /tmp/aot-smoke.log 2>/dev/null; then
    break
  fi
  sleep 0.5
done
if ! grep -q "\[READY\]" /tmp/aot-smoke.log 2>/dev/null; then
  echo "[smoke] FAIL: binary did not reach [READY] in 10s"
  cat /tmp/aot-smoke.log
  exit 1
fi
echo "[smoke] binary is ready"

# Public GET — list returns []
LIST=$(curl -fsS http://127.0.0.1:8080/products/)
if [ "$LIST" != "[]" ]; then
  echo "[smoke] FAIL: expected GET / → '[]', got '$LIST'"
  exit 1
fi
echo "[smoke] GET /products/ → [] ✓"

# Public GET on missing UUID — 404 with AiLangError shape
HTTP=$(curl -s -o /tmp/aot-smoke-resp -w "%{http_code}" \
    http://127.0.0.1:8080/products/00000000-0000-0000-0000-000000000000)
if [ "$HTTP" != "404" ]; then
  echo "[smoke] FAIL: expected 404 on missing GUID, got $HTTP"
  cat /tmp/aot-smoke-resp; exit 1
fi
grep -q '"type":"NOT_FOUND"' /tmp/aot-smoke-resp \
  || { echo "[smoke] FAIL: 404 body missing NOT_FOUND"; cat /tmp/aot-smoke-resp; exit 1; }
echo "[smoke] GET /products/<missing> → 404 NOT_FOUND ✓"

# Get a token and POST a product
TOKEN=$(curl -fsS -X POST http://127.0.0.1:8080/auth/token | grep -o '"token":"[^"]*"' | cut -d'"' -f4)
if [ -z "$TOKEN" ]; then
  echo "[smoke] FAIL: /auth/token did not return a token"
  exit 1
fi
echo "[smoke] /auth/token → token (len=${#TOKEN}) ✓"

POSTED=$(curl -fsS -X POST http://127.0.0.1:8080/products/ \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name":"smoke widget","price":1.5,"stock":2}')
echo "$POSTED" | grep -q '"name":"smoke widget"' \
  || { echo "[smoke] FAIL: POST response missing name"; echo "$POSTED"; exit 1; }
echo "[smoke] POST /products/ → product created ✓"

echo "[smoke] PASS"
```

- [ ] **Step 7.2: Make it executable**

```
chmod +x bytecode/scripts/aot-smoke-product-service.sh
```

- [ ] **Step 7.3: Run it**

```
bytecode/scripts/aot-smoke-product-service.sh
```

Expected final line: `[smoke] PASS`. Binary size will print; ~5–15 MB is expected.

If publish fails with AOT warnings, the codegen still emits something non-AOT-clean — diagnose by re-reading the warning and fixing in the transpiler. If runtime curl assertions fail, the binary boots but behavior diverged — most likely a JSON serialization edge case; check whether all response types are in the JsonContext.

- [ ] **Step 7.4: Re-confirm the test project still passes (test path is non-AOT, must not regress)**

```
cd bytecode/generated-csharp-product_service/Tests
dotnet test
```

Expected: 6/6 pass. The Tests project does not set `PublishAot`; xUnit's reflection-based discovery is fine.

- [ ] **Step 7.5: Restore the committed non-AOT csproj/Program.cs**

The smoke script regenerated the pilot with `--aot`, so the working tree now has AOT artifacts. Restore the default committed (non-AOT) version:

```
python transpiler/transpiler.py bytecode/product_service.sexpr --target csharp
```

Then confirm the working tree matches HEAD for those files:

```
git diff bytecode/generated-csharp-product_service/Program.cs bytecode/generated-csharp-product_service/ProductService.csproj
```

Expected: empty diff (the TokenResponse changes from Task 6 are already committed).

- [ ] **Step 7.6: Commit the smoke script**

```
git add bytecode/scripts/aot-smoke-product-service.sh
git commit -m "feat(aot): smoke script publishes product_service as native binary and asserts behavior"
```

---

## Task 8: Update CLAUDE.md per spec §5

Apply the exact edits described in §5 of the design spec.

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 8.1: Insert the new "Targets" section before "Próximos Pasos"**

Locate the line:

```
## Próximos Pasos
```

Immediately above it, insert:

```markdown
## Targets

- **C# (.NET 10 LTS)** — primary target. Todas las features nuevas del lenguaje aterrizan acá primero.
- **Go (1.25)** — frozen, en mantenimiento. Bug fixes sí, features nuevas no se backportean. PRs externos bienvenidos.

---

```

- [ ] **Step 8.2: Replace roadmap item #3**

In `## Próximos Pasos`, replace:

```
3. **Compilador real** — de S-expressions a binario nativo (probablemente vía LLVM)
```

with:

```
3. **NativeAOT como modo opt-in del C# codegen** — flag `--aot`, JSON source-generation, single-binary publish. Verificado AOT-compat con Polly / Npgsql / Minimal APIs via `bytecode/probes/aot-probe/`. Reemplaza el item "runtime nativo propio".
```

- [ ] **Step 8.3: Trim the closed items from "Decisiones Pendientes"**

In `## Decisiones Pendientes`, remove these three lines:

```
- Runtime: ¿nativo propio? ¿LLVM? ¿BEAM?
- Modelo de memoria: ¿GC? ¿Ownership? ¿Híbrido?
- Interop: ¿FFI? ¿WASM?
```

Leave the remaining four bullets untouched:

```
- Transaccionalidad cross-service: ¿Sagas + dead letter queue?
- GUARD/VALIDATE_CHAIN: instrucción para evitar BRANCH anidados
- Hot-reload de config (v2)
- Linter con colores para S-expressions (solo necesita colores e indentación, no reglas de estilo)
```

- [ ] **Step 8.4: Verify the edits visually**

```
git diff CLAUDE.md
```

Expected diff shape:
- Added: "Targets" section before "Próximos Pasos".
- Modified: roadmap item #3.
- Removed: three lines from "Decisiones Pendientes".
- No other changes.

- [ ] **Step 8.5: Commit**

```
git add CLAUDE.md
git commit -m "docs(claude): C# primary, Go frozen; roadmap #3 swapped to NativeAOT path"
```

---

## Task 9: Add `--aot` to README

**Files:**
- Modify: `README.md`

- [ ] **Step 9.1: Find the existing usage section and add an `--aot` mention**

Locate the existing transpiler usage block (search for `--target csharp` in `README.md`). Append, in the same code block where targets are documented:

```
# NativeAOT opt-in: emit csproj with <PublishAot>true</PublishAot>
# and a source-generated JSON context. Only valid with --target csharp.
python transpiler.py product_service.sexpr --target csharp --aot
```

Below that block, add a short paragraph:

```markdown
The `--aot` flag is opt-in. Without it, the C# codegen emits the same project layout as before — classic JIT, reflection-based JSON, standard publish. With `--aot`, the emitted csproj enables `<PublishAot>true</PublishAot>` and a source-generated `JsonSerializerContext`; `dotnet publish -c Release -r <rid>` produces a single self-contained native binary (~5–15 MB) that boots in milliseconds.
```

- [ ] **Step 9.2: Verify**

```
git diff README.md
```

Expected: only the additions above; no other changes.

- [ ] **Step 9.3: Commit**

```
git add README.md
git commit -m "docs(readme): document --aot opt-in for C# codegen"
```

---

## Task 10: Final regression sweep — both targets still green

This is the spec §8 success-criterion verification.

- [ ] **Step 10.1: Re-run the C# in-memory suite**

```
for svc in order_service pricing_service payment_service dashboard_service product_service; do
  pushd "bytecode/generated-csharp-$svc/Tests" >/dev/null
  dotnet test --nologo --verbosity quiet
  popd >/dev/null
done
```

Expected: 11 + 6 + 7 + 6 + 6 = 36 tests, all pass.

- [ ] **Step 10.2: Re-run the Go in-memory suite**

```
for svc in order_service pricing_service payment_service dashboard_service product_service; do
  pushd "bytecode/generated-go-$svc" >/dev/null
  go test ./...
  popd >/dev/null
done
```

Expected: 11 + 6 + 7 + 6 + 6 = 36 tests, all pass.

- [ ] **Step 10.3: Re-run the PG suite (if Docker is available)**

```
docker info >/dev/null 2>&1 && {
  pushd bytecode/generated-csharp-product_service_pg/Tests >/dev/null
  dotnet test --nologo --verbosity quiet
  popd >/dev/null
  pushd bytecode/generated-go-product_service_pg >/dev/null
  go test ./...
  popd >/dev/null
} || echo "Docker not available — PG suite skipped (acceptable)."
```

Expected when Docker is available: 14 C# + 14 Go = 28 PG tests pass.

- [ ] **Step 10.4: Re-run the AOT smoke**

```
bash bytecode/scripts/aot-smoke-product-service.sh
```

Expected final line: `[smoke] PASS`.

- [ ] **Step 10.5: Update the spec's status header**

Edit `docs/superpowers/specs/2026-04-27-cs-primary-go-frozen-design.md` line 5:

Change:

```
**Status:** Approved by user; spec review passed (round 2)
```

to:

```
**Status:** Approved by user; implemented in plan `docs/superpowers/plans/2026-04-27-nativeaot-path.md`
```

- [ ] **Step 10.6: Final commit**

```
git add docs/superpowers/specs/2026-04-27-cs-primary-go-frozen-design.md
git commit -m "docs(spec): mark NativeAOT spec as implemented"
```

---

## Self-Review (post-write checklist for the plan author)

**Spec coverage (§ of the spec ↔ task that implements it):**

- §3 "C# primary" — implicit, no work needed (already primary).
- §3 "Go frozen" — implicit, no work needed (no Go changes in this plan).
- §3 "`--target go --aot` rejected" — Task 2.1.
- §3 "NativeAOT path opt-in via `--aot`" — Tasks 2, 3.
- §4 item 1 "JSON source generation" — Tasks 4, 5.
- §4 item 2 "dependency AOT-compatibility verified during implementation" — Task 1 (probe project).
- §4 item 3 "no reflection in emitted handler code" — Task 6 (anonymous-type fix), Task 5 (Read sites).
- §4 item 4 "csproj additions when --aot" — Task 3.
- §4 item 5 "single self-contained native binary via publish" — Task 7.
- §5 "CLAUDE.md edits" — Task 8.
- §6 item 1 "NativeAOT path (1–2 weeks)" — entire plan.
- §6 item 1 "one reference service published end-to-end and added to a smoke check" — Task 7.
- §8 "regression suite remains green on both targets" — Task 10.
- §8 "implementation plan exists and is approved" — this document.

No gaps.

**Placeholder scan:** no "TBD", no "implement later", no "add appropriate error handling" without specifics. Tasks 5.2 and 5.3 acknowledge implementer choice ("parameter or ctx dict") rather than dictating — that's a deliberate judgment call, not a placeholder.

**Type/name consistency:** `ProductServiceJsonContext` appears in Tasks 4, 5, 7 and matches `pascal_case(bc.service['name'])` → `ProductService`. `TokenResponse` is added to `_aot_json_types` (Task 6.2) and emitted in both AOT and non-AOT (Task 6.1). `_aot_json_types` is defined in Task 4.1 and extended in Task 6.2. The `aot` parameter on `generate_csharp` is added in Task 2.3 and read in Tasks 3 and 4.
