# AOT Probe

Standalone .NET 10 project that imports every dependency the AILang C# codegen emits and is configured with `<PublishAot>true</PublishAot>` + `<TreatWarningsAsErrors>true</TreatWarningsAsErrors>`. If any dependency surfaces an AOT analyzer warning, `dotnet publish` fails — which is what we want.

## Run

```
cd bytecode/probes/aot-probe
dotnet publish -c Release -r linux-x64   # or win-x64 / osx-arm64
```

A successful publish prints `Build succeeded` with zero warnings, and emits `bin/Release/net10.0/<rid>/publish/AotProbe(.exe)`. Run the binary; it should print `[AOT-PROBE] dependencies wired` and exit.

Re-run this probe whenever we bump a package version listed in `AotProbe.csproj` so AOT-warning regressions are caught before they reach the transpiler.
