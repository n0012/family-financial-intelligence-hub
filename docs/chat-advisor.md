# Google Chat Financial Advisor (FinSage)

**FinSage** operates as an interactive Google Chat Bot supporting 1:1 direct messages and collaborative family chat spaces. Powered by **Gemini Flash**, it combines deterministic BigQuery analytical queries, real-time Monarch API lookups, multimodal computer vision, and Vertex AI long-term memory.

---

## 1. Private-Ingress Chat Architecture

FinSage connects to Google Chat without a public endpoint:

```mermaid
sequenceDiagram
    autonumber
    actor User as Household User (Google Chat)
    participant ChatAPI as Google Chat API
    participant PubSub as Cloud Pub/Sub (monarch-chat-incoming)
    participant Worker as Cloud Run /chat/pubsub
    participant Gemini as Gemini Flash Brain (AFC)
    participant BQ as BigQuery Warehouse
    participant Monarch as Monarch Money GraphQL

    User->>ChatAPI: Sends message or image (/brief, /sweep, receipt)
    ChatAPI->>PubSub: Publishes user event
    PubSub->>Worker: OIDC-authenticated push (internal ingress)
    Worker->>Gemini: Dispatches prompt + tools
    Gemini->>BQ: Calls run_readonly_sql_tool()
    BQ-->>Gemini: Deterministic SQL results
    opt Live Verification
        Gemini->>Monarch: Calls get_live_account_balance() / get_live_transaction()
        Monarch-->>Gemini: Real-time balance / status
    end
    Gemini-->>Worker: Synthesized advice / action card
    Worker->>ChatAPI: Authenticated HTTPS POST (Card v2 / text)
    ChatAPI-->>User: Renders interactive response
```

Chat events are delivered by a **Pub/Sub push subscription** (`monarch-chat-push`) to `/chat/pubsub`. The service accepts only internal traffic and verifies each request's OIDC token, so it has no public endpoint, and it scales to zero between messages. Answers that take longer than 12 seconds get an interim "Analyzing..." reply, and the full answer is posted to the thread before the push request is acknowledged.

Only emails in `ALLOWED_CHAT_USERS` can use the app; on Cloud Run an unset list refuses everyone.

The SQL tool runs read-only queries, and a free dry run first checks every table the query would read. Only the financial tables (`raw_accounts`, `raw_categories`, `raw_transactions`, `receipt_records`, `brief_history`) and the `v_*` views over them are allowed; chat history, preferences, the audit log and `INFORMATION_SCHEMA` are refused.

The legacy streaming-pull worker (`app/chat_worker.py`) is still available by setting `ENABLE_CHAT_PULL_WORKER=true`, but it needs an always-on instance (`--min-instances 1 --no-cpu-throttling`), which bills a full vCPU around the clock.

---

## 2. Slash Commands Cheatsheet

| Command | Action Description | Primary Tools / Views Used |
| :--- | :--- | :--- |
| **`/brief`** | Shows today's summarized brief: recent activity, goal pacing, the most notable new findings, and 13-week trends. Viewing it on demand does not use up the next scheduled brief's findings. | `app/daily_brief.py`, `brief_history` |
| **`/alerts`** | Runs every spend alert check and replies in the thread with the full list, each with a 7-day snooze button. | `v_duplicate_charges`, `v_subscription_price_creep`, `v_annual_bill_radar` |
| **`/sweep`** | Evaluates checking liquidity to calculate safe surplus sweeps to high-rate debt. | `v_paycheck_surplus_allocation`, `v_annual_bill_radar` |
| **`/tax [YYYY]`** | Displays annual tax deductibility summary (Schedule C, HSA, Charities). | `v_tax_deductible_summary` |
| **`/digest [weekly\|monthly]`** | Generates an executive CFO performance briefing. | `v_debt_summary`, `v_spend_classification` |
| **`/categorize [N]`** | Researches up to N (default 10) uncategorized or split merchants on the web and posts one review card of category fixes to tick and apply. | `app/category_research.py`, `merchant_category_decisions` |
| **`/sync`** | Triggers immediate Monarch Money ingestion into BigQuery. | `monarch_service.sync_accounts_to_bq()` |
| **`/receipt`** *(with image)* | Extracts line items, scrubs PII, classifies tax deductibility, and matches bank ledger. | Gemini Vision, `raw_transactions` |
| **`/help`** | Displays quick command reference and suggested natural language prompts. | Built-in |

---

## 3. Conversational Reasoning & Natural Language

Gemini automatically maps user intent to BigQuery analytical views using Automatic Function Calling (AFC):

* *"What is our daily debt interest cost across mortgage and HELOC right now?"*  
  → Queries `v_debt_summary` and reports total balance, daily carry, and mortgage vs HELOC breakdowns.
* *"Which subscriptions increased in price over the last year?"*  
  → Queries `v_subscription_price_creep` and advises on the exact annualized increase.
* *"How much did we spend on dining out vs groceries last month?"*  
  → Queries `v_food_efficiency` to compare grocery baseline vs dining markups.
* *"Where are our top micro-transaction leaks under $35?"*  
  → Queries `v_micro_transaction_leakage` for coffee shops and convenience spending.
* *"How much safe surplus can we sweep from checking to pay down debt today?"*  
  → Queries `v_paycheck_surplus_allocation` to determin## 4. Daily Brief (`/brief`) and Full Alert Scan (`/alerts`)

Every morning at 08:00 AM the `monarch-alerts-job` posts a short **daily brief** card. Ask for it any time with `/brief`, or in plain words (*"show me today's summary"*, *"daily brief"*). The card has four sections:

1. **Recent activity**: transactions in the past three days, the biggest one compared with what you usually spend at that merchant, and month-to-date spend against the same point last month.
2. **🎯 Goals**: spending caps and the HELOC payoff date read from long-term memory, with a progress bar and pacing, for example `██░░░░░░░░ $100 of $400 · ahead of pace`.
3. **🔍 Worth a look**: at most two findings, chosen by dollar impact from:
   * categories running well above the median of the prior three 4-week windows (or well below),
   * merchants visited about twice as often as usual,
   * first-ever merchants,
   * accounts that have stopped reporting transactions,
   * one-time alerts such as duplicate charges.

   Shown findings are recorded in `brief_history`. A trend is not repeated for 14 days, a stale account for 7, and a one-time alert for a year, so each day surfaces something new. A quiet day says so instead of repeating old items.
4. **📈 13-week trends**: weekly sparklines for your three largest spending categories, each with an up or down arrow.

Viewing the brief on demand does not record its findings, so it never uses up the next scheduled brief. On Mondays the job also posts the weekly digest.

**`/alerts`** is the exhaustive view: it runs every alert check and replies in the thread with each alert and a **7-day snooze button** (HMAC-SHA256 signed). `python -m app.job full-scan` posts the same alerts to the space together with the older synopsis (pacing thermometer, cash posture and debt carry), which is also available from the `/advisor/morning-brief` API.


---

## 5. Paycheck Surplus Sweep Engine (`/sweep`)

When payroll deposits arrive, `/sweep` protects essential cash reserves before accelerating debt paydown:
1. Calculates liquid checking balances.
2. Protects 30-day non-negotiable fixed overhead burn with a **15% safety buffer**.
3. Protects upcoming 30-day lump-sum bills (insurance, property tax) from `v_annual_bill_radar`.
4. Sweeps the safe surplus directly to the highest-rate liability (e.g. variable HELOC).
5. Displays immediate daily, monthly, and lifetime interest eliminated.

---

## 6. Multimodal Receipt & Tax Ingestion (`/receipt`, `/tax`)

Users can paste photos or PDFs of receipts and invoices directly into Google Chat:
1. **Gemini 2.5 Flash Vision**: Extracts merchant name, transaction date, total amount, sales tax, tip, and itemized line items.
2. **Zero-PII Scrubbing**: Deterministically redacts SSNs, EINs, and credit card PANs before saving to BigQuery.
3. **IRS Deductibility Analysis**: Evaluates items against IRS IRC §162 (Schedule C business expenses), IRC §213(d) (HSA/FSA medical expenses), and IRC §170 (501(c)(3) charitable donations).
4. **Asymmetric Bank Matcher**: Searches `raw_transactions` in a `[-3 days, +10 days]` window, accommodating delayed batch posting and restaurant tips up to +35%.
5. **Interactive Review Card**: Displays parsed data with one-click verification.

---

## 7. Guarded Mutations & Cryptographic Confirmation

When modifying categories or making ledger changes in Monarch Money, FinSage enforces physical confirmation:
* **Interactive Card v2 Confirmation Widget**: Shows transaction details, original category, and proposed new category.
* **Cryptographic Tamper Protection**: Action buttons include an HMAC-SHA256 signature containing transaction ID, category ID, the requesting user, and timestamp. Signatures expire in 15 minutes. The key is `mutation-hmac-secret`.
* **Bound to the requester**: Only the person who asked for the change can confirm it, even in a shared space. A proposal that can't be attributed to a Chat user is never created.
* **Signed snoozes**: Alert snooze buttons carry the same kind of signature, and unsigned or expired snoozes are rejected.
* **Audit Trail**: Every confirmed or canceled mutation is permanently recorded in BigQuery `mutation_audit_log`.

---

## 8. Researched Category Reviews (`/categorize`)

`/categorize` (or *"review my categories"*) cleans up categories in batches:

1. **Candidates, ranked by impact**: merchants with uncategorized transactions or transactions split across categories, ranked by the dollars at stake: uncategorized spend plus spend filed outside the merchant's main category. The last 12 months count in full and older spend at half, and uncategorized spend counts 1.5x because it is missing from every category report. Merchants touching transfer or income categories are skipped.
2. **What Monarch already says**: the review reads your live Monarch categories and transaction rules. A merchant covered by one of your Monarch rules (merchant name only, no amount or account conditions) gets that rule's category, so older transactions that predate the rule are brought in line. A merchant you confirmed in an earlier review gets that category too. Neither needs research, but both compete on the same impact ranking as researched merchants rather than jumping the queue.
3. **Web research**: the remaining merchants go to Gemini grounded on Google Search, a few at a time in parallel. It strips payment-processor prefixes (`SQ *`, `TST*`), searches the merchant name only (never amounts), follows the conventions in your existing rules, and picks one of your Monarch categories with a confidence and a one-line reason. It can also say a split is legitimate and should be left alone. Suggestions below 75% confidence are dropped.
4. **One review card**: up to 10 merchants, biggest fix first, each ticked by default, showing the dollars that will be recategorized, the transaction count, current categories, proposed category and reason. **Apply selected** recategorizes the ticked merchants' transactions in Monarch Money (and BigQuery) and adds a Monarch rule (*merchant name equals …* → category) for any merchant not already covered, so future transactions are categorized by Monarch itself. The rule is created with *apply to existing transactions* off, because the review has already updated those one by one. If a rule can't be created, the transaction fixes still apply and the result card says so. **Reject all** dismisses the review.
5. **Memory**: applied merchants are recorded with the Monarch rule ID, unticked ones are not proposed again for 180 days, and merchants the model left alone are not re-researched for 90 days (`merchant_category_decisions`).

The card carries the same protections as other mutations: it is signed for the requester, expires after an hour, can only be applied once, and every outcome is written to `mutation_audit_log`. Gemini can also start a review itself through the `start_category_review` tool. Each review runs a handful of grounded searches, which Vertex AI bills per query. Monarch's Python client has no rules API, so rules are read and created with the same GraphQL operations Monarch's web app uses (`GetTransactionRules`, `createTransactionRuleV2`); a change on Monarch's side could break rule creation without affecting the transaction updates.

---

## 9. Vertex AI Long-Term Memory Bank

FinSage integrates with Vertex AI Agent Platform to persist family financial preferences across conversation threads:
* Remembers user-specific targets (e.g. *"Our dining goal is under $600/month"*, *"Prioritize paying off the HELOC before the auto loan"*).
* Resolves conflicting preferences autonomously.
* Injects consolidated financial rules directly into Gemini's reasoning context. Stored facts are sanitized and presented as data, not instructions, so a saved "preference" can't rewrite the prompt.
