![Automated Family Financial Intelligence Hub](./static/workflow.png)

---

## Ready-to-Publish LinkedIn Post

"What's our daily interest burn right now?" — now our family Google Chat answers before my morning coffee. ☕📉

Weekend build: a serverless financial intelligence hub for my partner and me that bridges Monarch Money into Google BigQuery, with Gemini 3.8 Flash acting as our continuous financial copilot right inside Google Chat.

Most budgeting apps do a fine job telling you where your money went last month. But none of them tell you what your debt is costing you today, or whether cutting two takeout orders this week actually makes a dent in a variable-rate balance.

The fun part is the plumbing. 

AI models are notoriously bad at doing math in their heads. Ask an LLM to calculate compounding interest on a variable-rate HELOC or spot subscription price creep across three years of transactions, and it'll happily hallucinate a convincing number. 

So Gemini doesn't do the math. BigQuery does.

Every morning, Cloud Run synchronizes transactions via Monarch's API into BigQuery. Pre-computed SQL views model all the deterministic arithmetic:
• Exact daily compounding interest cost ((balance * apr) / 365)
• Subscription overlap and historical price creep detection
• Food efficiency ratios (groceries vs dining & delivery markups)
• Micro-transaction leakage under $35

When either of us asks a question in Google Chat, Gemini uses Automatic Function Calling (AFC) to query those analytical views directly. The AI handles the natural language reasoning and contextual synthesis; the database handles the math.

The multimodal piece is where it gets really fun: paste a screenshot of an annual compensation outlook, a bonus projection table, or an unlinked statement into the chat. Gemini Flash parses the visual layout, pulls the numbers, and models the optimal debt-payoff sweep on the spot.

A few favorite details:
• Daily proactive alerts land at 8:00 AM via Google Chat Cards v2 with actionable buttons
• Fully configurable rates and account overrides via a clean local `config.yaml` or Secret Manager
• Serverless economics: running on Cloud Run + BigQuery Always Free Tier costs ~$0.37/month

Standing on shoulders: huge credit to the open-source community around the monarchmoney Python library that made programmatic ingestion possible, and an earlier inspiration thread (chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339) that sparked the idea to turn personal finance into an interactive copilot.

Full code, BigQuery analytical views, and Terraform configs are open-sourced on GitHub 👉 https://github.com/n0012/family-financial-intelligence-hub

Financial discipline isn't about staring at spreadsheets on Sunday night — it's about having deterministic answers the moment decisions actually happen. 

#GoogleCloud #BigQuery #GeminiAI #PersonalFinance #Serverless #CloudRun #Fintech #Python #FastAPI #BuildingInPublic
