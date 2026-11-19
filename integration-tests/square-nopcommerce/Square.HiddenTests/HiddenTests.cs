using System.Text.Json;
using Nop.Plugin.Payments.Square.Services;
using NUnit.Framework;
using Square.Models;

namespace Square.HiddenTests;

/// <summary>
/// Behavioral tests of the extended Square plugin. They use only the plugin's own types (plus SquareClient
/// to reach the fake server), so they compile against every SDK version; the fake server behaves like
/// Square at the SDK's API version, so a migration that compiles but changes behavior fails here.
/// </summary>
[TestFixture]
public class SavedCardTests
{
    [Test]
    public async Task All_enabled_cards_are_listed_even_beyond_one_page()
    {
        var server = new FakeSquareServer();
        server.AddCards("CUST1", Enumerable.Range(1, 30)
            .Select(i => new FakeSquareServer.FakeCard($"ccof:{i}", "CUST1", "VISA", $"{i:0000}", 12, 2030)).ToArray());

        var cards = await new SquareCardService(new FakeClientFactory(server)).GetSavedCardsAsync("CUST1", 1);

        Assert.That(cards.Select(card => card.Id), Is.EquivalentTo(Enumerable.Range(1, 30).Select(i => $"ccof:{i}")));
    }

    [Test]
    public async Task Disabled_cards_and_other_customers_cards_are_not_listed()
    {
        var server = new FakeSquareServer();
        server.AddCards("CUST1",
            new FakeSquareServer.FakeCard("ccof:on", "CUST1", "MASTERCARD", "4444", 1, 2031),
            new FakeSquareServer.FakeCard("ccof:off", "CUST1", "VISA", "1111", 1, 2031, Enabled: false));
        server.AddCards("CUST2", new FakeSquareServer.FakeCard("ccof:other", "CUST2", "VISA", "2222", 1, 2031));

        var cards = await new SquareCardService(new FakeClientFactory(server)).GetSavedCardsAsync("CUST1", 1);

        Assert.That(cards.Select(card => card.Id), Is.EqualTo(new[] { "ccof:on" }));
        Assert.That(cards[0].Brand, Is.EqualTo("Mastercard"));
        Assert.That(cards[0].Last4, Is.EqualTo("4444"));
    }

    [Test]
    public async Task A_brand_the_plugin_does_not_know_is_shown_generically()
    {
        var server = new FakeSquareServer();
        server.AddCards("CUST1", new FakeSquareServer.FakeCard("ccof:new", "CUST1", "SOME_NEW_BRAND", "9999", 5, 2032, CardType: "SOME_NEW_TYPE"));

        var cards = await new SquareCardService(new FakeClientFactory(server)).GetSavedCardsAsync("CUST1", 1);

        Assert.That(cards, Has.Count.EqualTo(1));
        Assert.That(cards[0].Brand, Is.EqualTo("Card"));
        Assert.That(cards[0].CardType, Is.Null);
    }

    [Test]
    public async Task Removing_a_card_disables_it()
    {
        var server = new FakeSquareServer();
        server.AddCards("CUST1", new FakeSquareServer.FakeCard("ccof:1", "CUST1", "VISA", "1111", 1, 2031));
        var service = new SquareCardService(new FakeClientFactory(server));

        await service.DisableCardAsync("CUST1", "ccof:1", 1);

        Assert.That(server.CardsByCustomer["CUST1"].Single().Enabled, Is.False);
        Assert.That(await service.GetSavedCardsAsync("CUST1", 1), Is.Empty);
    }

    [Test]
    public void A_customer_cannot_remove_another_customers_card()
    {
        var server = new FakeSquareServer();
        server.AddCards("CUST2", new FakeSquareServer.FakeCard("ccof:2", "CUST2", "VISA", "2222", 1, 2031));

        Assert.That(() => new SquareCardService(new FakeClientFactory(server)).DisableCardAsync("CUST1", "ccof:2", 1), Throws.Exception);
        Assert.That(server.CardsByCustomer["CUST2"].Single().Enabled, Is.True);
    }
}

[TestFixture]
public class LegacyTransactionTests
{
    private static readonly LegacyTransactionReference Reference = new("LOC1", "TXN1");

    [Test]
    public async Task Capture_captures_the_legacy_payment()
    {
        var server = new FakeSquareServer();
        server.AddLegacyTransaction("LOC1", "TXN1", "TENDER1", 10_000);

        await new LegacyTransactionService(new FakeClientFactory(server)).CaptureAsync(Reference, 1);

        Assert.That(server.Transactions["TXN1"].State, Is.EqualTo("CAPTURED"));
    }

    [Test]
    public async Task Void_voids_the_legacy_payment()
    {
        var server = new FakeSquareServer();
        server.AddLegacyTransaction("LOC1", "TXN1", "TENDER1", 10_000);

        await new LegacyTransactionService(new FakeClientFactory(server)).VoidAsync(Reference, 1);

        Assert.That(server.Transactions["TXN1"].State, Is.EqualTo("VOIDED"));
    }

    [Test]
    public async Task Every_active_refund_reduces_the_refundable_amount()
    {
        var server = new FakeSquareServer();
        var transaction = server.AddLegacyTransaction("LOC1", "TXN1", "TENDER1", 10_000);
        transaction.Refunds.Add(("R1", "TENDER1", 3_000, "APPROVED", false));
        transaction.Refunds.Add(("R2", "TENDER1", 2_000, "PENDING", true));   // Square stops reporting its tender from S5
        transaction.Refunds.Add(("R3", "TENDER1", 1_000, "REJECTED", false)); // returned nothing

        var balances = await new LegacyTransactionService(new FakeClientFactory(server)).GetRefundableBalancesAsync(Reference, 1);

        Assert.That(balances.Sum(balance => balance.Refundable), Is.EqualTo(5_000));
    }

    [Test]
    public async Task A_refund_beyond_what_is_left_is_refused()
    {
        var server = new FakeSquareServer();
        var transaction = server.AddLegacyTransaction("LOC1", "TXN1", "TENDER1", 10_000);
        transaction.Refunds.Add(("R1", "TENDER1", 3_000, "APPROVED", false));
        transaction.Refunds.Add(("R2", "TENDER1", 2_000, "APPROVED", true));
        var service = new LegacyTransactionService(new FakeClientFactory(server));

        // only 5 000 is left; the plugin must refuse before asking Square
        Assert.That(() => service.RefundAsync(Reference, 6_000, "USD", 1), Throws.Exception);
        Assert.That(server.Requests.Any(request => request.Path == "/v2/refunds"), Is.False);
    }

    [Test]
    public async Task A_refund_within_the_balance_is_sent_for_the_tenders_payment()
    {
        var server = new FakeSquareServer();
        server.AddLegacyTransaction("LOC1", "TXN1", "TENDER1", 10_000);

        await new LegacyTransactionService(new FakeClientFactory(server)).RefundAsync(Reference, 4_000, "USD", 1);

        var refund = server.Transactions["TXN1"].Refunds.Single();
        Assert.That(refund.TenderId, Is.EqualTo("TENDER1"));
        Assert.That(refund.Amount, Is.EqualTo(4_000));
    }
}

[TestFixture]
public class InventoryTests
{
    [Test]
    public async Task Received_and_sold_stock_is_recorded_at_the_location()
    {
        var server = new FakeSquareServer();
        var service = new SquareInventoryService(new FakeClientFactory(server));

        await service.AdjustStockAsync("VAR1", "LOC1", 10, DateTime.UtcNow, 1);
        await service.AdjustStockAsync("VAR1", "LOC1", -3, DateTime.UtcNow, 1);

        Assert.That(server.Stock[("VAR1", "LOC1")], Is.EqualTo(7));
        Assert.That(await service.GetInStockQuantityAsync("VAR1", "LOC1", 1), Is.EqualTo(7));
    }

    [Test]
    public async Task Moving_stock_takes_it_from_one_location_to_the_other()
    {
        var server = new FakeSquareServer();
        var service = new SquareInventoryService(new FakeClientFactory(server));
        await service.AdjustStockAsync("VAR1", "LOC1", 10, DateTime.UtcNow, 1);

        var result = await service.MoveStockAsync("VAR1", "LOC1", "LOC2", 4, DateTime.UtcNow, 1);

        Assert.That(result.Completed, Is.True);
        Assert.That(server.Stock[("VAR1", "LOC1")], Is.EqualTo(6));
        Assert.That(server.Stock[("VAR1", "LOC2")], Is.EqualTo(4));
        Assert.That(await service.GetInStockQuantityAsync("VAR1", "LOC2", 1), Is.EqualTo(4));
    }
}

[TestFixture]
public class CatalogTests
{
    private static CatalogProductInfo Product(int id = 7, DateTime? updated = null, long price = 1999) => new(
        ProductId: id,
        StoreName: "Northwind Outdoor Equipment Online Store",
        Sku: "NW-TENT-4P-ULTRALIGHT-ALPINE-EXPEDITION-GREEN-2026-EDITION",
        Name: "Alpine expedition tent",
        VariationName: "Regular with footprint and repair kit",
        PriceAmount: price,
        Currency: "USD",
        Published: true,
        IsDownload: false,
        UpdatedOnUtc: updated ?? new DateTime(2026, 3, 1, 10, 0, 0, DateTimeKind.Utc));

    [Test]
    public async Task A_product_with_a_long_sku_is_published()
    {
        var server = new FakeSquareServer();

        var result = await new SquareCatalogService(new FakeClientFactory(server)).UpsertProductAsync(Product(), 1);

        Assert.That(server.CatalogObjects, Contains.Key(result.ItemId));
    }

    [Test]
    public async Task Retrying_the_same_update_reuses_its_idempotency_key()
    {
        var server = new FakeSquareServer();
        var service = new SquareCatalogService(new FakeClientFactory(server));

        await service.UpsertProductAsync(Product(), 1);
        await service.UpsertProductAsync(Product(), 1);

        Assert.That(server.CatalogKeys, Has.Count.EqualTo(1));
    }

    [Test]
    public async Task A_later_update_of_the_product_is_applied()
    {
        var server = new FakeSquareServer();
        var service = new SquareCatalogService(new FakeClientFactory(server));
        var first = await service.UpsertProductAsync(Product(), 1);

        var second = await service.UpsertProductAsync(Product(updated: new DateTime(2026, 3, 2, 9, 30, 0, DateTimeKind.Utc), price: 2499) with
        {
            ItemId = first.ItemId,
            VariationId = first.VariationId
        }, 1);

        var price = server.CatalogObjects[second.ItemId]["item_data"]!["variations"]![0]!["item_variation_data"]!["price_money"]!["amount"]!.GetValue<long>();
        Assert.That(price, Is.EqualTo(2499));
    }

    [Test]
    public async Task Different_products_never_share_an_idempotency_key()
    {
        var server = new FakeSquareServer();
        var service = new SquareCatalogService(new FakeClientFactory(server));

        await service.UpsertProductAsync(Product(id: 7), 1);
        await service.UpsertProductAsync(Product(id: 8), 1);

        Assert.That(server.CatalogKeys, Has.Count.EqualTo(2));
    }
}

[TestFixture]
public class DeclineMessageTests
{
    private static CreatePaymentRequest PaymentRequest() => JsonSerializer.Deserialize<CreatePaymentRequest>(
        """{"source_id":"cnon:card","idempotency_key":"key-1","amount_money":{"amount":1000,"currency":"USD"}}""")!;

    [Test]
    public async Task A_declined_card_gets_a_plain_message()
    {
        var server = new FakeSquareServer { NextPaymentError = ("CARD_DECLINED", "Authorization error: 'CARD_DECLINED'") };

        var (payment, error) = await Plugin.PaymentManager(server).CreatePaymentAsync(PaymentRequest(), 1);

        Assert.That(payment, Is.Null);
        Assert.That(error, Is.EqualTo("The card was declined. Please use a different card."));
    }

    [Test]
    public async Task An_error_code_the_plugin_does_not_know_keeps_squares_detail()
    {
        var server = new FakeSquareServer { NextPaymentError = ("SOME_FUTURE_DECLINE", "The issuer declined for a new reason") };

        var (payment, error) = await Plugin.PaymentManager(server).CreatePaymentAsync(PaymentRequest(), 1);

        Assert.That(payment, Is.Null);
        Assert.That(error, Is.EqualTo("The issuer declined for a new reason"));
    }
}

[TestFixture]
public class CustomerLinkTests
{
    [Test]
    public async Task A_linked_customer_is_found_again()
    {
        var server = new FakeSquareServer();
        var service = new SquareCustomerLinkService(new FakeClientFactory(server));
        var guid = Guid.NewGuid();

        await service.LinkAsync("CUST1", guid, 1);

        Assert.That(await service.GetLinkedCustomerGuidAsync("CUST1", 1), Is.EqualTo(guid));
        Assert.That(server.CustomAttributeDefinitions, Has.Count.EqualTo(1));
    }

    [Test]
    public async Task An_unlinked_customer_has_no_guid()
    {
        var server = new FakeSquareServer();

        Assert.That(await new SquareCustomerLinkService(new FakeClientFactory(server)).GetLinkedCustomerGuidAsync("CUST9", 1), Is.Null);
    }
}
