using System.Text.Json.Serialization;

namespace AotProbe;

public record ProbeDto(string Name, int Count);

[JsonSourceGenerationOptions(WriteIndented = false)]
[JsonSerializable(typeof(ProbeDto))]
internal partial class AotProbeJsonContext : JsonSerializerContext;
