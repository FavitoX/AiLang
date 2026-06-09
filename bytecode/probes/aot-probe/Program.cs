using System.IdentityModel.Tokens.Jwt;
using System.Net.Http;
using System.Security.Claims;
using System.Text;
using AotProbe;
using Microsoft.AspNetCore.Authentication.JwtBearer;
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

// Npgsql data source builder — this is where pool/multiplexer types instantiate
// (mirrors transpiler PG codegen). Do not open a real connection.
var ds = new NpgsqlDataSourceBuilder(nb.ConnectionString).Build();
_ = ds.ConnectionString;

// Minimal APIs + source-gen JSON.
var builder = WebApplication.CreateSlimBuilder(args);
builder.Services.ConfigureHttpJsonOptions(o =>
    o.SerializerOptions.TypeInfoResolverChain.Insert(0, AotProbeJsonContext.Default));

// JWT validation path — mirrors transpiler.py JwtBearer wiring. This is where
// JwtBearer historically surfaces AOT analyzer warnings.
builder.Services.AddAuthentication("Bearer").AddJwtBearer("Bearer", options =>
{
    options.TokenValidationParameters = new TokenValidationParameters
    {
        ValidateIssuer = false,
        ValidateAudience = false,
        ValidateLifetime = false,
        IssuerSigningKey = key
    };
});
builder.Services.AddAuthorization();

// HTTP resilience — mirrors transpiler.py CALL_SERVICE wiring
// (Microsoft.Extensions.Http.Resilience + Polly strategy options).
builder.Services.AddHttpClient("probe", c => c.BaseAddress = new Uri("http://localhost"))
    .AddResilienceHandler("probe-pipeline", b =>
    {
        b.AddRetry(new Polly.Retry.RetryStrategyOptions<HttpResponseMessage>
        {
            MaxRetryAttempts = 1,
            ShouldHandle = new Polly.PredicateBuilder<HttpResponseMessage>()
                .Handle<HttpRequestException>()
                .HandleResult(r => (int)r.StatusCode >= 500),
        });
        b.AddTimeout(TimeSpan.FromSeconds(1));
    });

var app = builder.Build();
app.MapGet("/probe", () => new ProbeDto("probe", 1));

// Print and exit immediately — we want publish-clean, not a long-running probe.
System.Console.WriteLine("[AOT-PROBE] dependencies wired");
System.Console.WriteLine($"[AOT-PROBE] pipeline={pipeline.GetType().Name}");
