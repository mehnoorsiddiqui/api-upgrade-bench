# Square nopCommerce plugin: hidden behavioral tests

Hidden tests for the Square upgrade runs on
[`mehnoorsiddiqui/bench-square-nopcommerce`](https://github.com/mehnoorsiddiqui/bench-square-nopcommerce),
starting from the extended plugin (branch `extended-plugin`, tag `s0-extended`, ApimaticBench.Square 1.0.1).

The agent doing an upgrade never sees these tests. A run is scored by copying them into its result and running them.

## Why they exist

Several breaking changes in the Square history compile fine after a naive fix but behave wrongly.
These tests catch that, so "it builds" is not the whole score.

| Step (SDK) | Change | Test that catches the naive fix |
| --- | --- | --- |
| S3 (2.0.0) | card brand / type become open enums | `A_brand_the_plugin_does_not_know_is_shown_generically` |
| S4 (3.0.0) | `Customer.cards` removed: cards come from `ListCards`, 25 per page | `All_enabled_cards_are_listed_even_beyond_one_page`, `Disabled_cards_and_other_customers_cards_are_not_listed` |
| S4 (3.0.0) | catalog `idempotency_key` limited to 128 characters (not enforced by the SDK) | `A_product_with_a_long_sku_is_published`, `Retrying_the_same_update_reuses_its_idempotency_key`, `A_later_update_of_the_product_is_applied`, `Different_products_never_share_an_idempotency_key` |
| S5 (4.0.0) | `Refund.tender_id` becomes optional and is left out for some refunds | `Every_active_refund_reduces_the_refundable_amount`, `A_refund_beyond_what_is_left_is_refused` |
| S6 (5.0.0) | legacy capture/void removed: use the tender's payment (tender id = payment id) | `Capture_captures_the_legacy_payment`, `Void_voids_the_legacy_payment` |
| S6 (5.0.0) | inventory transfers removed; adjustments get `from_location_id` / `to_location_id` | `Received_and_sold_stock_is_recorded_at_the_location`, `Moving_stock_takes_it_from_one_location_to_the_other` |

Controls that should pass with any correct fix: decline messages (`DeclineMessageTests`), the customer link
(`CustomerLinkTests`, S5 request-record rename), card removal and legacy refunds within the balance.

## How they work

- `FakeSquareServer` is an in-memory Square that behaves like Square at the SDK's API version
  (read from the `ApimaticBench.Square` assembly version; `SQUARE_FAKE_API_VERSION` overrides it).
  It works on raw JSON only, never SDK model types.
- The tests call only the plugin's own services and records (`SquareCardService`, `LegacyTransactionService`,
  `SquareInventoryService`, `SquareCatalogService`, `SquareCustomerLinkService`, `SquarePaymentManager`) through
  `ISquareClientFactory`, so they compile against every SDK version as long as the run keeps those signatures.
  A run that renames them fails to compile here, and that counts as a failure.

## Running

From the root of an app checkout:

```sh
cp -r <bench>/integration-tests/square-nopcommerce/Square.HiddenTests src/Tests/Square.HiddenTests
dotnet test src/Tests/Square.HiddenTests/Square.HiddenTests.csproj
```

Remove the folder again before committing anything in the app checkout.

## Expected results

- `s0-extended` (1.0.1) and `ref/s2-extended` (1.2.0): 20 of 20 pass.
- The unmodified extended plugin does not compile against 2.0.0 or later, so each later step needs a migration first.
- With the 1.0.1 code and the server forced to a later version (`SQUARE_FAKE_API_VERSION`), the tests listed above for
  that version and every earlier one fail (8 at 3.0.0, 9 at 4.0.0, 13 at 5.0.0). This only shows that the server
  changes are detected; the real check is the migrated code built against that SDK version.
