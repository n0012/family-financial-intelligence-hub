# Business Trips and Business Tags

Work travel keeps its normal category (an airline stays *Airfare* or *Travel & Vacation*) and is marked
with a Monarch **tag** instead. The same merchant can be business on one trip and a vacation on the next,
so trips are tagged one at a time and never become Monarch rules.

## Business spending is left out of household totals

The nightly sync copies each transaction's Monarch tags into BigQuery (`raw_transactions.tags`) and sets
`is_business` when the business tag is present (`BUSINESS_TAG`, default `Business`, matched ignoring case).
Transactions with `is_business = TRUE` are excluded from:

- budget-cap alerts (dining, groceries) and month-to-date pacing
- the weekly and monthly digest (totals, top categories, top merchants)
- the daily brief (recent activity, month spend, goal pacing, category and merchant trends)
- the `v_spend_classification`, `v_food_efficiency` and `v_micro_transaction_leakage` views

Duplicate-charge, subscription and bill views still see business charges. The chat SQL tool leaves business
spending out of household questions unless you ask about business spending, a trip or reimbursements.
Tags you add by hand in Monarch work the same way after the next sync.

Monarch's own budgets and reports ignore tags, so in the Monarch app business travel still counts toward its
category. Use Monarch's per-transaction *hide from budget* if that matters for a reimbursed trip.

## `/trip`: tag a trip's charges

```
/trip Springfield Mar 10-14, flew Example Air, Example Hotel, rewards card, include meals, reimbursed
```

Only the dates are required. Everything else improves the default ticks:

| Detail | Effect |
|---|---|
| Dates | Trip window. Airfare is searched from 60 days before (flights are booked ahead); lodging up to 3 days after checkout (folios post late); ground transport the day before through the day after. |
| Destination | Names the trip tag (`Trip: Springfield Mar 2026`). A restaurant whose name contains the destination is ticked. |
| Airline / hotel | Charges from the named airline or hotel are ticked, including ones booked ahead; other airlines and hotels are listed unticked. |
| Card | Only charges on matching accounts are listed. If nothing matches, all cards are shown and the card says so. |
| `include meals` | Restaurant charges during the trip are ticked (they are listed unticked otherwise). |
| `reimbursed` | Adds a `Reimbursable` tag (`REIMBURSABLE_TAG`). |
| Amounts | Charges of exactly those amounts are ticked (see below). |

### Paste or attach an expense report

Paste an expense report, receipts or booking confirmations after `/trip` (up to 8,000 characters; one line of
dates is still needed if the report doesn't state its travel period):

```
/trip Springfield Mar 10-14
Mar 2  Example Air        $400.00
Mar 14 Example Hotel      $600.00
Mar 12 Example Taxi        $25.00
```

Or attach the report to a `/trip` message instead of pasting it: **xlsx** (as Concur exports it), **csv**, **txt**,
**pdf** or a screenshot. Spreadsheets and text files are converted to text in code (up to 40,000 characters);
PDFs and images go to Gemini as files. With an attachment, `/trip` alone is enough when the report states its
travel period, and a spreadsheet or text file sent on its own (Chat often sends a file as a separate message)
is read as a trip report without `/trip`. A PDF or image still needs `/trip` in the same message, since on its
own it goes to the general advisor. Older `.xls` workbooks aren't read; save them as xlsx or csv. Attachments are read only by
`/trip`; describing a trip in plain language passes your words, not your files.

Each amount is matched to one charge of exactly that amount, which is ticked and marked 🧾 whatever its
merchant, category or card. A dated line matches a charge posted from a day before to 3 days after it; an
undated amount only matches a charge that looks like travel or falls within the trip. The card lists any
amounts with no matching charge (not posted yet, or paid another way). Mileage, per diem and cash lines are
skipped.

Gemini reads only the trip description and any attachments, to pull out dates, hints and amounts;
transactions are matched in code:

- Travel agencies (Amex GBT, Egencia, Navan, Concur, Expedia, Priceline, Booking.com) count as airfare.
- Airfare under $40 charged before the trip (seat, bag and wifi fees from an earlier trip) is skipped unless it
  is from the airline you named.
- A hotel charged before the trip is listed only if you named it or its name contains the destination; other
  early hotel charges belong to earlier trips.
- Other travel-category charges are offered unticked during the trip.
- Airline brand names that are also ordinary words (for example a youth club or a utility sharing an
  airline's name) only count when they are the whole merchant name.
- Food delivery never counts as ground transport.

The card lists up to 40 charges grouped by airfare, lodging, ground transport, meals and other travel.
**Tag selected** adds the business tag, the trip tag and (if reimbursed) `Reimbursable` to the ticked charges
in Monarch, keeping any tags they already had, and mirrors them to BigQuery. Categories never change and no
rules are created. Charges already tagged for the trip are not listed again, so running `/trip` a second time
picks up charges that posted late.

The card is signed for the requester, expires after an hour, can be applied once, and every outcome is
written to `mutation_audit_log` as `BUSINESS_TRIP`. Trips are saved in `business_trips` (dates, trip tag,
details, proposed charges, status). Gemini can also start a trip review through the
`start_business_trip_review` tool when you describe a work trip in plain language.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `BUSINESS_TAG` | `Business` | Monarch tag that marks business spending |
| `REIMBURSABLE_TAG` | `Reimbursable` | Tag added when a trip is reimbursed |
| `TRIP_PARSE_MODEL` | `CATEGORY_RESEARCH_MODEL` | Model that reads the trip description and attachments |
