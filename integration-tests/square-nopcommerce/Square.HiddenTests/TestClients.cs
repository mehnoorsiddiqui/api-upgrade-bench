using Moq;
using Nop.Core;
using Nop.Plugin.Payments.Square.Services;
using Nop.Services.Configuration;
using Nop.Services.Logging;
using Square;
using Square.Core.Configuration;
using Square.Servers;

namespace Square.HiddenTests;

/// <summary>
/// Hands the plugin Square clients that talk to the fake server
/// </summary>
public sealed class FakeClientFactory(FakeSquareServer server) : ISquareClientFactory
{
    public Task<SquareClient> CreateAsync(int storeId) => Task.FromResult(new SquareClient(new HttpClient(server, disposeHandler: false), new SquareClientOptions
    {
        BearerAuth = "test-token",
        Environment = ServerEnvironment.Production,
        Retry = RetryOptions.Disabled()
    }));
}

public static class Plugin
{
    public static SquarePaymentManager PaymentManager(FakeSquareServer server) => new(
        new FakeClientFactory(server),
        new Mock<ILogger>().Object,
        new Mock<ISettingService>().Object,
        new Mock<IWorkContext>().Object,
        null!);
}
