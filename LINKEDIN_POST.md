![Automated Family Financial Intelligence Hub](./static/workflow.png)

---

## Ready-to-Publish LinkedIn Post

"What's our daily interest burn right now?" — now our family Google Chat answers before my morning coffee. ☕📉

Weekend build: a serverless financial intelligence hub for my wife and I that bridges Monarch Money into Google BigQuery, with Gemini 3.8 Flash answering our questions right inside Google Chat.

The fun part is the plumbing. 

AI models are notoriously bad at doing math in their heads — ask one to calculate compounding interest on variable-rate debt or spot subscription price creep across 3 years of transactions, and it'll happily hallucinate a convincing number. 

So Gemini doesn't do the math; BigQuery does. 

Every morning, Cloud Run syncs transactions via Monarch's API into BigQuery. Pre-computed SQL views model the deterministic arithmetic: exact daily compounding interest ((balance * apr) / 365), subscription overlap, food efficiency ratios (dining vs groceries), and micro-transaction leakage under $35. When either of us asks a question in chat, Gemini queries those views directly via Automatic Function Calling.

You can even paste a screenshot of a compensation outlook or an unlinked statement into the chat — Gemini Flash parses the visual layout and models the debt payoff impact on the spot. Proactive spend alerts hit our phones at 8 AM via Google Chat Cards v2, and the whole stack runs on GCP's free tier for ~$0.37/month.

Standing on shoulders: credit to @hammem for the monarchmoney library that made programmatic access possible, and an earlier concept thread (chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339) that sparked the idea to turn personal finance into an interactive copilot.

Source code, SQL models + full writeup 👉 https://github.com/n0012/family-financial-intelligence-hub

Financial discipline isn't about staring at spreadsheets on Sunday night — it's about having deterministic answers the moment decisions happen.

#GoogleCloud #BigQuery #GeminiAI #Monarch #MonarchMoney #PersonalFinance #Serverless #CloudRun #Fintech #Python #BuildingInPublic
