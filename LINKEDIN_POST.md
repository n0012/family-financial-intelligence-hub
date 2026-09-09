![Automated Family Financial AI Workflow](./static/workflow.png)

---

## Ready-to-Publish LinkedIn Post

```markdown
🚀 Built a Serverless Personal Finance Copilot with Monarch Money, Google BigQuery & Gemini 3.8 Flash

Most personal finance apps (Monarch, Mint, YNAB) do a great job aggregating accounts and basic envelope budgeting. But when it comes to answering questions like:
• "What is our exact daily interest burn on our variable-rate debt right now?"
• "How much annual interest do we eliminate if we redirect our dining delivery markups to our HELOC?"
• Or evaluating a complex 2026 compensation & bonus outlook...
...you're usually stuck manually exporting CSVs into a spreadsheet.

I built an automated financial intelligence hub that turns our family financial data into an active, conversational copilot directly in Google Chat for my partner and me:

🧠 Key Architectural & Engineering Highlights:

1️⃣ Deterministic Arithmetic over LLM Hallucination:
AI models are notoriously prone to math errors when doing arithmetic in their heads. Instead of asking Gemini to compute interest or spot price creeps mentally, all financial logic (daily compounding interest, subscription price hikes, dining-to-grocery ratios, and micro-transaction leakage) is pre-modeled directly in BigQuery GoogleSQL analytical views. Gemini 3.8 Flash executes read-only SQL queries via Automatic Function Calling (AFC) to ground every recommendation in deterministic arithmetic.

2️⃣ Multimodal Vision in Google Chat:
We can paste screenshots directly into our Google Chat space—whether it's an annual compensation breakdown, bonus projection table, or an external brokerage statement. The bot downloads the image, extracts line items and dates via Gemini Flash vision, and cross-references them against live bank balances in BigQuery.

3️⃣ Dynamic Account & Migration Intelligence:
When bank mergers happen or credit cards are replaced, the analytical layer dynamically evaluates transaction recency and balances to determine primary vs superseded accounts with zero hardcoded IDs.

4️⃣ Serverless Economics (<$0.40 / month):
Runs as a lightweight FastAPI service on Google Cloud Run with scale-to-zero, synchronizing transactions via automated TOTP/MFA and posting alerts via Google Chat Cards v2. The entire architecture operates comfortably within GCP's Always Free Tier.

The full architecture, BigQuery analytical models, and Terraform scripts are open-sourced on GitHub: [Link to Repo]

#GoogleCloud #BigQuery #GeminiAI #MachineLearning #PersonalFinance #Serverless #FastAPI #Fintech #Python
```

---

## Workflow Infographic Breakdown

The visual infographic illustrates the end-to-end flow across three distinct tiers:

1. **Monarch Money & Bank Accounts (Left)**: Live aggregation of salaries, credit card spend, utility bills, investments, and mortgage/HELOC payments.
2. **Google BigQuery & Gemini AI Brain (Center)**: 
   - Pre-computed SQL views: HELOC Interest Cost curve, Subscription Price Hike & Overlap Detection, and Fixed vs Discretionary Spend Classification.
   - Grounded reasoning engine via Gemini 3.8 Flash.
3. **Google Chat Family Interface (Right)**:
   - **Partner A (Male)**: Natural language query on daily debt burn $\rightarrow$ instantaneous data-grounded metrics and repayment acceleration tips.
   - **Partner B (Female)**: Pasted spreadsheet screenshot $\rightarrow$ multimodal visual analysis extracting line items and projecting cash flow impact.
