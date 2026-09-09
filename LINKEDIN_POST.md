![Family Financial Intelligence Hub](./static/workflow.png)

---

## Published LinkedIn Post

An experiment to make it easier for my wife and I to stay on top of our spend. ☕💳

Like a lot of couples, we use Monarch to track our finances. But nobody actually wants to log into an app every day or sit down on Sunday night to comb through reports.

So I wanted to see if we could just ask questions in our family Google Chat and get real, immediate answers.

The tricky part is LLMs and math. They are getting better, but I like some determinism to know that I get the right answers for things like daily compounding interest on a variable-rate loan, or tallying up dining spend across three months.

So Gemini doesn't do the math. BigQuery does.

Every morning, Cloud Run pulls our transactions into BigQuery via the Monarch API. A few SQL views handle the actual numbers — daily interest cost on our debt, subscription price creep, dining vs groceries, and small convenience charges under $35. When either of us asks a question in Google Chat, Gemini just queries those views and tells us what they say.

We can also drop a screenshot of a statement or a bonus projection table right into the chat, and it pulls the numbers into the conversation on the spot.

Simple, runs on GCP's free tier for pennies a month, and most importantly: we actually use it.

Credit to @hammem for the monarchmoney Python library that made pulling the data possible, and an earlier concept thread (https://lnkd.in/g9jpP3Zq) that sparked the idea.

Code + SQL views 👉 see comments

#Monarch #MonarchMoney #GoogleCloud #BigQuery #GeminiAI #PersonalFinance #Homelab
