# C# as primary target, Go in maintenance mode; NativeAOT replaces native runtime roadmap

**Date:** 2026-04-27
**Author:** Brainstormed with Claude (AILang project)
**Status:** Approved by user; spec review passed (round 2)

---

## 1. Context

AILang has reached a stable inflection point:

- 50/50 tests green on both targets (C# and Go): 30/30 legacy services, 6/6 in-memory product service, 14/14 PG via Testcontainers.
- The language surface has been exercised by six reference services covering CRUD, PARALLEL, sagas, events, resilience policies, and PostgreSQL.
- Two-target parity has been informative — it proved abstractions translate across distinct concurrency and type models — but at significant cost in iteration speed. Several rounds of work in 2026-04-17 → 2026-04-23 were "now backport this to Go", and Go always trailed.

The roadmap entry "#3 — native runtime (LLVM / BEAM / propio)" was open: a 12-24 month implementation effort with three coupled sub-decisions (memory model, IR, scheduler ABI) and no chosen path.

This spec resolves the strategic question that gates everything downstream: **what is AILang's compilation target?**

## 2. Decision

**Adopted: option B (from the brainstorming session)** — C# as primary, Go in maintenance, NativeAOT replaces native runtime.

Two coupled choices:

**(B1) Targets**
- **C# (.NET 10 LTS) becomes the primary target.** All new language features land in the C# codegen first. C# releases do not block on Go parity.
- **Go (1.25) enters maintenance mode.** Existing services keep running and are kept in the regression suite. Bug fixes in Go codegen are accepted. New language features are *not* backported. External PRs to Go codegen are welcome.

**(B2) Native runtime**
- The roadmap item "native runtime (LLVM/BEAM/propio)" is **closed**, replaced by NativeAOT as an opt-in mode of the C# codegen.
- The three coupled sub-decisions (memory model, IR design, scheduler ABI) are likewise closed: GC (.NET server GC), AST-driven codegen (no separate IR), and the .NET ThreadPool / `Task` scheduler.

### Why

- **Ecosystem alignment.** AILang's value proposition is to *bake in* what other ecosystems treat as optional: auth, resilience, validation, observability, errors-that-can't-be-ignored. .NET already ships production-grade equivalents for all of these (Polly, Serilog, FluentValidation, ASP.NET Identity, `Results<T>`-style return shapes, IOptions). The C# codegen *wires* mature libraries. The Go codegen *reinvents* them in emitted code (we saw this concretely with gobreaker lacking a `ManualControl` analog to Polly's, and Go having no `TimeProvider`-equivalent).
- **Code readability.** AILang's generated code is meant to be auditable by humans. C# 12 records, pattern matching, expression-bodied members, `async/await`, LINQ, and Minimal APIs produce output that reads as pseudocode. Go's verbosity (explicit `if err != nil`, no sum types, late generics) makes generated code denser and harder to audit.
- **Audience.** The stated goal is "proyecto en GitHub para que lo use quien quiera". The primary target must be where generated code is idiomatic and pleasant for the reader, not where the transpiler author finds it easier to emit. .NET has a larger pool of enterprise backend developers comfortable with the AILang feature set.
- **Iteration velocity.** Cutting backport-to-Go from the critical path roughly doubles language-feature throughput.
- **NativeAOT covers 90% of "native runtime" intent.** Single binary, ms-scale startup, no shipped runtime, native machine code — all without writing a compiler. The remaining 10% (ownership/region memory, custom scheduler, exotic targets like microcontrollers) is not a current need.

### Why *not* drop Go entirely (option A)

- Three days of work landed Go at 50/50 green; deleting that is irreversible and premature.
- Polyglot codegen still provides design feedback — when an abstraction is awkward in Go but clean in C#, that is signal about the abstraction.
- Keeping Go around at zero ongoing cost (no new features, bugs only) preserves optionality.

## 3. Scope: what "primary" and "frozen" mean

### C# primary

- All new language features (instructions, syntax, semantics) are designed against the C# codegen first.
- C# releases do not wait on Go parity.
- C# is the documented target in README / CLAUDE.md / examples.
- The first generator written for any new feature is the C# one.

### Go frozen (maintenance)

- The generated Go services in the regression suite (`generated-go-*`) stay green. If a regression appears, it is fixed.
- Existing Go codegen functions in `transpiler.py` are not removed.
- New language features may simply not be implemented in Go. The CLI does not need a special "Go support" flag — the generator either supports the feature or emits a clear warning.
- External contributors who want to backport features to Go are welcome via PR. Maintainer review applies the same quality bar as any PR.
- After 6-12 months, if no one engages with the Go target, a future spec may move it to A (drop).

### NativeAOT path

- New flag: `python transpiler.py X.sexpr --target csharp --aot`.
- Default (no `--aot`) emits a classic .NET project — exactly what we emit today, untouched.
- With `--aot`, the emitted .csproj sets `<PublishAot>true</PublishAot>` and additional codegen is required (see §4).
- Existing services do not change. AOT is elected per regeneration, per service.
- `--target go --aot` is rejected with a clear error message — AOT is a C#-only concept here.

## 4. NativeAOT path — codegen requirements

Anchored to .NET 10 LTS (Context7 confirms 10.0.8 May 2026, EOL 2028-11-14; .NET 11 channel not yet active in release metadata).

**Required codegen changes when `--aot` is set:**

1. **JSON source generation.** `JsonSerializer.Deserialize<T>` without a generated context is not AOT-safe (relies on reflection). For each service emit one `partial class XJsonContext : JsonSerializerContext` with a `[JsonSerializable(typeof(T))]` attribute per DTO and per response type, and route all serialization through it.

2. **Dependency AOT-compatibility — to be verified during implementation, not assumed.**
   This spec does *not* certify any vendor as AOT-clean. Vendor stances at time of writing:
   - Polly 8.x: vendor docs claim AOT support; verify with `-warnaserror PublishTrimAnalysisWarnings;PublishAotAnalysisWarnings` on a probe project.
   - Npgsql 10.x: vendor docs document AOT support; verify by ensuring our emitted code avoids `dynamic` (it already does) and re-running the probe project.
   - ASP.NET Minimal APIs: officially AOT-ready since .NET 8; .NET 10 expands coverage.
   - xUnit: AOT support is partial (test discovery uses reflection); the AOT path is the *published binary*, not the test process. Tests can stay non-AOT.
   - EF Core: known AOT-flaky. Not in our dependency surface (we use Npgsql). No change.
   Implementation plan owns the verification: spin up a minimal AOT probe project that imports each dependency, build with AOT warnings as errors, document failures or workarounds.

3. **No reflection in emitted handler code.** Today's codegen does not use reflection at runtime (only in Python during emission). Maintain.

4. **csproj additions when `--aot`:**
   ```xml
   <PublishAot>true</PublishAot>
   <InvariantGlobalization>true</InvariantGlobalization>
   ```

5. **Build / publish:** developer runs `dotnet publish -c Release -r linux-x64` (or `win-x64`, `osx-arm64`). The result is a single self-contained native binary of ~5–10 MB. Startup is sub-100ms.

**What NativeAOT does *not* give us (explicit non-goals):**
- Ownership / region-based memory (Rust-style). We keep .NET server GC.
- Sub-millisecond GC pauses under arbitrary load (server GC is good but not realtime).
- Custom thread scheduler with explicit cooperative preemption.
- Microcontroller / baremetal targets.

If any of these becomes a real requirement in the future, this spec is re-opened.

## 5. CLAUDE.md changes

The following edits apply atomically when this spec lands:

### Section "Próximos Pasos"

- Item **#3** ("Compilador real — de S-expressions a binario nativo (probablemente vía LLVM)") is replaced by:
  > **NativeAOT como modo opt-in del C# codegen** — flag `--aot`, JSON source-generation, single-binary publish. Path verificado AOT-compat con Polly / Npgsql / Minimal APIs. Reemplaza el item "runtime nativo propio".

### Section "Decisiones Pendientes"

- **Closed and removed**: "Runtime: ¿nativo propio? ¿LLVM? ¿BEAM?" — resolved to NativeAOT.
- **Closed and removed**: "Modelo de memoria: ¿GC? ¿Ownership? ¿Híbrido?" — resolved to .NET server GC.
- **Closed and removed**: "Interop: ¿FFI? ¿WASM?" — resolved to P/Invoke (built-in) for FFI; WASM via wasi-experimental if a future spec needs it.
- **Kept open (no change in this spec)**:
  - "Transaccionalidad cross-service: ¿Sagas + dead letter queue?" — orthogonal to target choice.
  - "GUARD/VALIDATE_CHAIN: instrucción para evitar BRANCH anidados".
  - "Hot-reload de config (v2)".
  - "Linter con colores para S-expressions".

### New section "Targets"

Inserted before "Próximos Pasos":

> ### Targets
>
> - **C# (.NET 10 LTS)** — primary target. Todas las features nuevas del lenguaje aterrizan acá primero.
> - **Go (1.25)** — frozen, en mantenimiento. Bug fixes sí, features nuevas no se backportean. PRs externos bienvenidos.

### Section "Sobre el Usuario"

No changes — user profile remains accurate.

## 6. Updated roadmap order

After this spec lands, the immediate roadmap is:

1. **NativeAOT path** (1–2 weeks): `--aot` flag, JSON source-gen codegen, AOT-warning sweep on dependencies, **one reference service** (likely `product_service` in-memory) published as a native binary end-to-end and added to a smoke check. The other five services stay on the classic emit path until a follow-up spec decides which to migrate. `--target go --aot` is rejected with an error at the CLI layer.
2. **Investigar .NET 10 Minimal API enhanced validation** (separate spec): evaluate whether built-in validation can replace per-DTO `Validate()` emission in the C# codegen.
3. **GUARD / VALIDATE_CHAIN instruction** (was already pending).
4. **Hot-reload de config v2** (was already pending).
5. **Linter con colores para S-expressions** (was already pending).

The Go target receives no new feature work. Bug fixes are addressed as they appear.

## 7. Out of scope for this spec

- Implementing the NativeAOT path — that gets its own spec + plan.
- Choosing a specific JSON source-gen options profile.
- Migrating any of the six reference services to AOT.
- Removing the Go codegen.
- Modifying the AST / parser.
- OpenAPI auto-generation from bytecode.
- Versioning / release labeling (v0.10 vs v1.0 etc).

## 8. Success criteria

This spec is considered delivered when:

- `CLAUDE.md` is updated with the changes in §5.
- This design doc is committed to `docs/superpowers/specs/`.
- The full regression suite remains green on both targets — verified post-edit by running:
  - **Legacy services (30 per target)**: `dotnet test` / `go test ./...` under each `bytecode/generated-{csharp,go}-{order,pricing,payment,dashboard}_service/`. Expected counts confirmed by `[Fact]` / `func Test` audit in 2026-04-27: order 11, pricing 6, payment 7, dashboard 6.
  - **product_service in-memory (6 per target)**: same commands under `bytecode/generated-{csharp,go}-product_service/`.
  - **product_service_pg (14 per target)**: requires Docker. Last verified 2026-04-24 (commit `40d36f1`) — 14/14 green on both sides. Skipped without Docker.
  - **Total per target: 30 legacy + 6 in-memory + 14 PG = 50.** Confirmed against test files (`[Fact]` count in `ApiTests.cs`, `func Test*` count in `main_test.go`), not commit messages.
- A follow-up implementation plan for the NativeAOT path (§6 item 1) exists and is approved.

## 9. Open questions for future revisits

- **When to upgrade to .NET 11 LTS?** Likely once it ships GA (estimated Nov 2027 per cadence) and is the LTS line. Tracked separately.
- **At what point does Go drop from maintenance to deleted?** Soft target: 2027-04-27 (12 months from this spec). Concrete trigger: zero external PRs against `generate_go*` codegen *and* no issues filed against generated Go output in that window → drop. Any engagement extends the maintenance window by another 6 months.
- **Does C# remain the only target, or does a second target emerge?** Possible candidates if a real need shows up: WASM via wasi-experimental, or a JVM target (Kotlin) for the Spring Boot crowd. Speculative; not committed.
- **Validation rewrite via .NET 10 Minimal APIs.** May significantly reduce codegen surface for `Validate()`. Investigated in its own spec.
