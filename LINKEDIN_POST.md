![Family Financial Intelligence Hub](./static/workflow.png)

---

## Ready-to-Publish LinkedIn Post

An experiment to make it easier for my wife and I to stay on top of our spend. ☕💳

Like a lot of couples, we use Monarch to track our finances. But nobody actually wants to log into an app every day or sit down on Sunday night to comb through spreadsheets.

So I wanted to see if we could just ask questions in our family Google Chat and get real, immediate answers.

The tricky part is that LLMs suck at math. If you ask an AI to calculate daily compounding interest on a variable-rate loan or tally up dining spend across three months, it'll happily make up a convincing number.

So Gemini doesn't do the math. BigQuery does.

Every morning, Cloud Run pulls our transactions into BigQuery. A few SQL views handle the actual numbers — daily interest cost on our debt, subscription price creep, dining vs groceries, and small convenience charges under $35. When either of us asks a question in Google Chat, Gemini just queries those views and tells us what they say.

We can also drop a screenshot of a statement or a bonus projection table right into the chat, and it pulls the numbers into the conversation on the spot.

Simple, runs on GCP's free tier for pennies a month, and most importantly: we actually use it.

Standing on shoulders: credit to @hammem for the monarchmoney Python library that made pulling the data possible, and an earlier concept thread (chatgpt.com/share/69f7d4a5-a8e4-83ea-b6e2-78fb8eb79339) that sparked the idea.

Code + SQL views 👉 https://github.com/n0012/family-financial-intelligence-hub

#Monarch #MonarchMoney #GoogleCloud #BigQuery #GeminiAI #PersonalFinance #Homelab
