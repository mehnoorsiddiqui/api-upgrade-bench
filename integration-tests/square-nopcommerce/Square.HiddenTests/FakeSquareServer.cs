using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Web;

namespace Square.HiddenTests;

/// <summary>
/// An in-memory Square server that behaves like Square at the API version of the SDK under test.
///
/// Only raw JSON is used here, never SDK model types, so these tests compile against every SDK version.
/// The API version follows the SDK assembly version (1.0.1 = Square 2024-04-17 ... 5.0.0 = 2026-09-16);
/// SQUARE_FAKE_API_VERSION overrides it.
/// </summary>
public sealed class FakeSquareServer : HttpMessageHandler
{
    public FakeSquareServer(Version apiVersion = null)
    {
        ApiVersion = apiVersion
            ?? (Version.TryParse(Environment.GetEnvironmentVariable("SQUARE_FAKE_API_VERSION"), out var forced) ? forced : null)
            ?? typeof(global::Square.SquareClient).Assembly.GetName().Version!;
    }

    /// <summary>API version the server emulates (the SDK version it was generated for)</summary>
    public Version ApiVersion { get; }

    // S4 (3.0.0): Customer.cards removed; catalog idempotency keys limited to 128 characters
    private bool CustomerHasCards => ApiVersion < new Version(3, 0, 0);
    private bool CatalogKeyLimited => ApiVersion >= new Version(3, 0, 0);

    // S5 (4.0.0): Refund.tender_id is optional and Square leaves it out for refunds not tied to one tender
    private bool OmitsUnattributedTenderIds => ApiVersion >= new Version(4, 0, 0);

    // S6 (5.0.0): legacy capture/void and inventory transfers are gone; adjustments carry from/to locations
    private bool HasLegacyCaptureVoid => ApiVersion < new Version(5, 0, 0);
    private bool HasTransfers => ApiVersion < new Version(5, 0, 0);

    public List<(string Method, string Path, string Body)> Requests { get; } = [];

    #region State

    public sealed record FakeCard(string Id, string CustomerId, string Brand, string Last4, int ExpMonth, int ExpYear, bool Enabled = true, string CardType = "CREDIT");

    public sealed class FakeTransaction
    {
        public required string Id { get; init; }
        public required string LocationId { get; init; }
        public List<(string TenderId, long Amount)> Tenders { get; } = [];
        public List<(string Id, string TenderId, long Amount, string Status, bool Unattributed)> Refunds { get; } = [];
        public string State { get; set; } = "AUTHORIZED";
    }

    public Dictionary<string, List<FakeCard>> CardsByCustomer { get; } = [];
    public Dictionary<string, FakeTransaction> Transactions { get; } = [];
    public Dictionary<(string ObjectId, string LocationId), decimal> Stock { get; } = [];
    public Dictionary<string, JsonObject> CatalogObjects { get; } = [];
    public Dictionary<string, string> CatalogKeys { get; } = [];
    public HashSet<string> CustomAttributeDefinitions { get; } = [];
    public Dictionary<(string CustomerId, string Key), string> CustomAttributes { get; } = [];
    public (string Code, string Detail)? NextPaymentError { get; set; }

    private readonly Dictionary<string, string> _transferStates = [];
    private int _sequence;

    private string NextId(string prefix) => $"{prefix}{Interlocked.Increment(ref _sequence)}";

    public void AddCards(string customerId, params FakeCard[] cards)
    {
        if (!CardsByCustomer.TryGetValue(customerId, out var list))
            CardsByCustomer[customerId] = list = [];
        list.AddRange(cards);
    }

    public FakeTransaction AddLegacyTransaction(string locationId, string transactionId, string tenderId, long amount)
    {
        var transaction = new FakeTransaction { Id = transactionId, LocationId = locationId };
        transaction.Tenders.Add((tenderId, amount));
        Transactions[transactionId] = transaction;
        return transaction;
    }

    #endregion

    #region Responses

    private static HttpResponseMessage Json(JsonNode body, HttpStatusCode status = HttpStatusCode.OK) => new(status)
    {
        Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json")
    };

    private static HttpResponseMessage Error(HttpStatusCode status, string category, string code, string detail, string field = null)
    {
        var error = new JsonObject { ["category"] = category, ["code"] = code, ["detail"] = detail };
        if (field != null)
            error["field"] = field;
        return Json(new JsonObject { ["errors"] = new JsonArray(error) }, status);
    }

    private static HttpResponseMessage NotFound(string detail) => Error(HttpStatusCode.NotFound, "INVALID_REQUEST_ERROR", "NOT_FOUND", detail);

    private static JsonObject Money(long amount) => new() { ["amount"] = amount, ["currency"] = "USD" };

    private static JsonObject CardJson(FakeCard card) => new()
    {
        ["id"] = card.Id,
        ["card_brand"] = card.Brand,
        ["card_type"] = card.CardType,
        ["last_4"] = card.Last4,
        ["exp_month"] = card.ExpMonth,
        ["exp_year"] = card.ExpYear,
        ["customer_id"] = card.CustomerId,
        ["enabled"] = card.Enabled
    };

    #endregion

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        var path = request.RequestUri!.AbsolutePath.TrimEnd('/');
        var query = HttpUtility.ParseQueryString(request.RequestUri.Query);
        var bodyText = request.Content == null ? null : await request.Content.ReadAsStringAsync(cancellationToken);
        var body = string.IsNullOrEmpty(bodyText) ? null : JsonNode.Parse(bodyText)!.AsObject();
        var method = request.Method.Method;
        lock (Requests)
            Requests.Add((method, path, bodyText));

        var segments = path.Split('/', StringSplitOptions.RemoveEmptyEntries);
        string Seg(int i) => i < segments.Length ? Uri.UnescapeDataString(segments[i]) : null;

        return (method, segments.Length) switch
        {
            // customers
            ("GET", 3) when Seg(1) == "customers" => RetrieveCustomer(Seg(2)),
            ("GET", 2) when Seg(1) == "cards" => ListCards(query),
            ("POST", 4) when Seg(1) == "cards" && Seg(3) == "disable" => DisableCard(Seg(2)),

            // customer custom attributes
            ("GET", 4) when Seg(1) == "customers" && Seg(2) == "custom-attribute-definitions" =>
                CustomAttributeDefinitions.Contains(Seg(3)!)
                    ? Json(new JsonObject { ["custom_attribute_definition"] = new JsonObject { ["key"] = Seg(3), ["version"] = 1 } })
                    : NotFound("Custom attribute definition not found"),
            ("POST", 3) when Seg(1) == "customers" && Seg(2) == "custom-attribute-definitions" => CreateDefinition(body!),
            ("POST", 5) when Seg(1) == "customers" && Seg(3) == "custom-attributes" => UpsertAttribute(Seg(2)!, Seg(4)!, body!),
            ("GET", 5) when Seg(1) == "customers" && Seg(3) == "custom-attributes" => RetrieveAttribute(Seg(2)!, Seg(4)!),

            // legacy transactions
            ("GET", 5) when Seg(1) == "locations" && Seg(3) == "transactions" => RetrieveTransaction(Seg(2)!, Seg(4)!),
            ("POST", 6) when Seg(1) == "locations" && Seg(3) == "transactions" && Seg(5) is "capture" or "void" =>
                HasLegacyCaptureVoid ? SetTransactionState(Seg(4)!, Seg(5) == "capture" ? "CAPTURED" : "VOIDED") : NotFound("Unknown endpoint"),

            // payments and refunds
            ("POST", 2) when Seg(1) == "payments" => CreatePayment(),
            ("POST", 4) when Seg(1) == "payments" && Seg(3) is "complete" or "cancel" => CompleteOrCancelPayment(Seg(2)!, Seg(3)!),
            ("POST", 2) when Seg(1) == "refunds" => RefundPayment(body!),

            // inventory
            ("POST", 4) when Seg(1) == "inventory" && Seg(2) == "changes" && Seg(3) == "batch-create" => BatchChange(body!),
            ("POST", 3) when Seg(1) == "inventory" && Seg(2) == "batch-change" => BatchChange(body!),
            ("POST", 4) when Seg(1) == "inventory" && Seg(2) == "counts" && Seg(3) == "batch-retrieve" => BatchCounts(body!),
            ("POST", 3) when Seg(1) == "inventory" && Seg(2) == "batch-retrieve-counts" => BatchCounts(body!),
            ("GET", 4) when Seg(1) == "inventory" && Seg(2) == "transfers" =>
                HasTransfers && _transferStates.TryGetValue(Seg(3)!, out var state)
                    ? Json(new JsonObject { ["transfer"] = new JsonObject { ["id"] = Seg(3), ["state"] = state } })
                    : NotFound("Unknown endpoint"),

            // catalog
            ("POST", 3) when Seg(1) == "catalog" && Seg(2) == "object" => UpsertCatalogObject(body!),
            ("POST", 3) when Seg(1) == "catalog" && Seg(2) == "batch-delete" => BatchDelete(body!),

            _ => NotFound($"Fake Square server has no {method} {path}")
        };
    }

    #region Handlers

    private HttpResponseMessage RetrieveCustomer(string customerId)
    {
        var customer = new JsonObject { ["id"] = customerId };
        if (CustomerHasCards)
            customer["cards"] = new JsonArray(CardsByCustomer.GetValueOrDefault(customerId, []).Select(card => (JsonNode)CardJson(card)).ToArray());
        return Json(new JsonObject { ["customer"] = customer });
    }

    private HttpResponseMessage ListCards(System.Collections.Specialized.NameValueCollection query)
    {
        const int pageSize = 25;
        var includeDisabled = string.Equals(query["include_disabled"], "true", StringComparison.OrdinalIgnoreCase);
        var cards = (query["customer_id"] is { } customerId ? CardsByCustomer.GetValueOrDefault(customerId, []) : CardsByCustomer.Values.SelectMany(list => list))
            .Where(card => includeDisabled || card.Enabled)
            .ToList();
        var offset = int.TryParse(query["cursor"], out var cursor) ? cursor : 0;
        var page = cards.Skip(offset).Take(pageSize).Select(card => (JsonNode)CardJson(card)).ToArray();
        var response = new JsonObject { ["cards"] = new JsonArray(page) };
        if (offset + pageSize < cards.Count)
            response["cursor"] = (offset + pageSize).ToString();
        return Json(response);
    }

    private HttpResponseMessage DisableCard(string cardId)
    {
        foreach (var list in CardsByCustomer.Values)
        {
            var index = list.FindIndex(card => card.Id == cardId);
            if (index < 0)
                continue;
            list[index] = list[index] with { Enabled = false };
            return Json(new JsonObject { ["card"] = CardJson(list[index]) });
        }
        return NotFound("Card not found");
    }

    private HttpResponseMessage CreateDefinition(JsonObject body)
    {
        var key = body["custom_attribute_definition"]?["key"]?.GetValue<string>();
        if (string.IsNullOrEmpty(key))
            return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "MISSING_REQUIRED_PARAMETER", "key is required", "custom_attribute_definition.key");
        CustomAttributeDefinitions.Add(key);
        return Json(new JsonObject { ["custom_attribute_definition"] = new JsonObject { ["key"] = key, ["version"] = 1 } });
    }

    private HttpResponseMessage UpsertAttribute(string customerId, string key, JsonObject body)
    {
        if (!CustomAttributeDefinitions.Contains(key))
            return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "INVALID_VALUE", $"No custom attribute definition with key {key}");
        var value = body["custom_attribute"]?["value"]?.GetValue<string>();
        CustomAttributes[(customerId, key)] = value!;
        return Json(new JsonObject { ["custom_attribute"] = new JsonObject { ["key"] = key, ["value"] = value, ["version"] = 1 } });
    }

    private HttpResponseMessage RetrieveAttribute(string customerId, string key) =>
        CustomAttributes.TryGetValue((customerId, key), out var value)
            ? Json(new JsonObject { ["custom_attribute"] = new JsonObject { ["key"] = key, ["value"] = value, ["version"] = 1 } })
            : NotFound("Custom attribute not found");

    private HttpResponseMessage RetrieveTransaction(string locationId, string transactionId)
    {
        if (!Transactions.TryGetValue(transactionId, out var transaction) || transaction.LocationId != locationId)
            return NotFound("Transaction not found");

        var tenders = transaction.Tenders.Select(tender => (JsonNode)new JsonObject
        {
            ["id"] = tender.TenderId,
            ["location_id"] = locationId,
            ["transaction_id"] = transactionId,
            ["type"] = "CARD",
            ["amount_money"] = Money(tender.Amount)
        }).ToArray();

        var refunds = transaction.Refunds.Select(refund =>
        {
            var json = new JsonObject
            {
                ["id"] = refund.Id,
                ["location_id"] = locationId,
                ["transaction_id"] = transactionId,
                ["reason"] = "refund",
                ["amount_money"] = Money(refund.Amount),
                ["status"] = refund.Status
            };
            if (!(refund.Unattributed && OmitsUnattributedTenderIds))
                json["tender_id"] = refund.TenderId;
            return (JsonNode)json;
        }).ToArray();

        return Json(new JsonObject
        {
            ["transaction"] = new JsonObject
            {
                ["id"] = transactionId,
                ["location_id"] = locationId,
                ["tenders"] = new JsonArray(tenders),
                ["refunds"] = new JsonArray(refunds)
            }
        });
    }

    private HttpResponseMessage SetTransactionState(string transactionId, string state)
    {
        if (!Transactions.TryGetValue(transactionId, out var transaction))
            return NotFound("Transaction not found");
        transaction.State = state;
        return Json(new JsonObject());
    }

    private HttpResponseMessage CreatePayment()
    {
        if (NextPaymentError is { } error)
            return Error(HttpStatusCode.PaymentRequired, "PAYMENT_METHOD_ERROR", error.Code, error.Detail);
        return Json(new JsonObject { ["payment"] = new JsonObject { ["id"] = NextId("pay_"), ["status"] = "COMPLETED" } });
    }

    private HttpResponseMessage CompleteOrCancelPayment(string paymentId, string action)
    {
        // a legacy tender's id is the id of its payment
        var transaction = Transactions.Values.FirstOrDefault(t => t.Tenders.Any(tender => tender.TenderId == paymentId));
        if (transaction == null)
            return NotFound($"Payment {paymentId} not found");
        transaction.State = action == "complete" ? "CAPTURED" : "VOIDED";
        return Json(new JsonObject { ["payment"] = new JsonObject { ["id"] = paymentId, ["status"] = action == "complete" ? "COMPLETED" : "CANCELED" } });
    }

    private HttpResponseMessage RefundPayment(JsonObject body)
    {
        var paymentId = body["payment_id"]?.GetValue<string>();
        var amount = body["amount_money"]?["amount"]?.GetValue<long>() ?? 0;
        var transaction = Transactions.Values.FirstOrDefault(t => t.Tenders.Any(tender => tender.TenderId == paymentId));
        if (transaction == null)
            return NotFound($"Payment {paymentId} not found");

        var paid = transaction.Tenders.Sum(tender => tender.Amount);
        var refunded = transaction.Refunds.Where(r => r.Status is "PENDING" or "APPROVED").Sum(r => r.Amount);
        if (amount > paid - refunded)
            return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "REFUND_AMOUNT_INVALID", "The refund exceeds the payment");

        var id = NextId("refund_");
        transaction.Refunds.Add((id, paymentId!, amount, "APPROVED", false));
        return Json(new JsonObject { ["refund"] = new JsonObject { ["id"] = id, ["status"] = "COMPLETED", ["amount_money"] = Money(amount), ["payment_id"] = paymentId } });
    }

    private HttpResponseMessage BatchChange(JsonObject body)
    {
        var applied = new JsonArray();
        foreach (var change in body["changes"]?.AsArray() ?? [])
        {
            var type = change!["type"]?.GetValue<string>();
            switch (type)
            {
                case "ADJUSTMENT":
                {
                    var adjustment = change["adjustment"]!.AsObject();
                    var objectId = adjustment["catalog_object_id"]?.GetValue<string>();
                    var quantity = decimal.Parse(adjustment["quantity"]?.GetValue<string>() ?? "0", System.Globalization.CultureInfo.InvariantCulture);
                    var fromState = adjustment["from_state"]?.GetValue<string>();
                    var toState = adjustment["to_state"]?.GetValue<string>();
                    string fromLocation, toLocation;
                    if (HasTransfers)
                        fromLocation = toLocation = adjustment["location_id"]?.GetValue<string>();
                    else
                    {
                        fromLocation = adjustment["from_location_id"]?.GetValue<string>();
                        toLocation = adjustment["to_location_id"]?.GetValue<string>();
                    }

                    if (fromState == "IN_STOCK" && string.IsNullOrEmpty(fromLocation) || toState == "IN_STOCK" && string.IsNullOrEmpty(toLocation))
                        return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "MISSING_REQUIRED_PARAMETER", "The adjustment has no location for an IN_STOCK state", "changes[].adjustment");

                    if (fromState == "IN_STOCK")
                        Stock[(objectId!, fromLocation!)] = Stock.GetValueOrDefault((objectId!, fromLocation!)) - quantity;
                    if (toState == "IN_STOCK")
                        Stock[(objectId!, toLocation!)] = Stock.GetValueOrDefault((objectId!, toLocation!)) + quantity;

                    var result = adjustment.DeepClone().AsObject();
                    result["id"] = NextId("adj_");
                    applied.Add(new JsonObject { ["type"] = "ADJUSTMENT", ["adjustment"] = result });
                    break;
                }
                case "TRANSFER" when HasTransfers:
                {
                    var transfer = change["transfer"]!.AsObject();
                    var objectId = transfer["catalog_object_id"]!.GetValue<string>();
                    var quantity = decimal.Parse(transfer["quantity"]!.GetValue<string>(), System.Globalization.CultureInfo.InvariantCulture);
                    var from = transfer["from_location_id"]!.GetValue<string>();
                    var to = transfer["to_location_id"]!.GetValue<string>();
                    Stock[(objectId, from)] = Stock.GetValueOrDefault((objectId, from)) - quantity;
                    Stock[(objectId, to)] = Stock.GetValueOrDefault((objectId, to)) + quantity;

                    var id = NextId("transfer_");
                    _transferStates[id] = "IN_STOCK";
                    var result = transfer.DeepClone().AsObject();
                    result["id"] = id;
                    applied.Add(new JsonObject { ["type"] = "TRANSFER", ["transfer"] = result });
                    break;
                }
                default:
                    return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "INVALID_ENUM_VALUE", $"Unknown inventory change type {type}", "changes[].type");
            }
        }

        return Json(new JsonObject { ["changes"] = applied, ["counts"] = new JsonArray() });
    }

    private HttpResponseMessage BatchCounts(JsonObject body)
    {
        var objectIds = body["catalog_object_ids"]?.AsArray().Select(n => n!.GetValue<string>()).ToHashSet() ?? [];
        var locationIds = body["location_ids"]?.AsArray().Select(n => n!.GetValue<string>()).ToHashSet() ?? [];
        var counts = Stock
            .Where(entry => (objectIds.Count == 0 || objectIds.Contains(entry.Key.ObjectId)) && (locationIds.Count == 0 || locationIds.Contains(entry.Key.LocationId)))
            .Select(entry => (JsonNode)new JsonObject
            {
                ["catalog_object_id"] = entry.Key.ObjectId,
                ["location_id"] = entry.Key.LocationId,
                ["state"] = "IN_STOCK",
                ["quantity"] = entry.Value.ToString(System.Globalization.CultureInfo.InvariantCulture)
            })
            .ToArray();
        return Json(new JsonObject { ["counts"] = new JsonArray(counts) });
    }

    private HttpResponseMessage UpsertCatalogObject(JsonObject body)
    {
        var key = body["idempotency_key"]?.GetValue<string>() ?? string.Empty;
        if (CatalogKeyLimited && key.Length > 128)
            return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "VALUE_TOO_LONG", "Field must not be greater than 128 length", "idempotency_key");

        var objectJson = body["object"]!.ToJsonString();
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(objectJson)));
        if (CatalogKeys.TryGetValue(key, out var previous) && previous != digest)
            return Error(HttpStatusCode.BadRequest, "INVALID_REQUEST_ERROR", "IDEMPOTENCY_KEY_REUSED", "The idempotency key was already used for a different request", "idempotency_key");
        CatalogKeys[key] = digest;

        var item = body["object"]!.DeepClone().AsObject();
        var mappings = new JsonArray();
        string Resolve(JsonObject obj)
        {
            var id = obj["id"]!.GetValue<string>();
            if (!id.StartsWith('#'))
                return id;
            var realId = id.TrimStart('#').ToUpperInvariant();
            mappings.Add(new JsonObject { ["client_object_id"] = id, ["object_id"] = realId });
            obj["id"] = realId;
            return realId;
        }

        var itemId = Resolve(item);
        foreach (var variation in item["item_data"]?["variations"]?.AsArray() ?? [])
        {
            var variationObject = variation!.AsObject();
            Resolve(variationObject);
            if (variationObject["item_variation_data"] is JsonObject data)
                data["item_id"] = itemId;
        }

        CatalogObjects[itemId] = item;
        return Json(new JsonObject { ["catalog_object"] = item.DeepClone(), ["id_mappings"] = mappings });
    }

    private HttpResponseMessage BatchDelete(JsonObject body)
    {
        var ids = body["object_ids"]?.AsArray().Select(n => n!.GetValue<string>()).ToList() ?? [];
        foreach (var id in ids)
            CatalogObjects.Remove(id);
        return Json(new JsonObject { ["deleted_object_ids"] = new JsonArray(ids.Select(id => (JsonNode)id).ToArray()) });
    }

    #endregion
}
