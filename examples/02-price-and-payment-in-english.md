# Price and payment conditions, in English

**What it shows:** Fuzzy product lookup, PIX price and the installment rule computed from the manual's minimum installment table. The customer writes in English and Duda answers in English: the store's customers write in Portuguese, but the agent follows the customer's language.

Run on 03/11/2026 at 00:36 (Campo Grande time) with `gemini-3.5-flash-lite`. Customer lines are scripted; Duda's replies and tool calls are the agent's real output.

---

**Customer:** Hi! How much is the Takamine GD20?

> Tools: `search_products(query="Takamine GD20")`

**Duda:** I am Duda, the virtual assistant of Empório da Música, and I am happy to help! The Takamine GD20 Dreadnought Natural is available for **R$ 2.199,00** (or **R$ 2.089,05** via PIX, in up to **12x de R$ 183,25** without interest). 🎸  
  
- Dreadnought steel-string acoustic guitar  
- Natural finish  
- In stock with 5 units available

> Product cards shown in the web chat: Takamine GD20 Dreadnought Natural, R$ 2.199,00 (PIX R$ 2.089,05); ou até 12x de R$ 183,25 sem juros

> Quick replies: Payment options | Shipping info | Another model

**Customer:** Can I pay in installments? And what is the price with PIX?

> Tools: `payment_options(product_id=95)`

**Duda:** Yes, you can pay in installments of up to **12x de R$ 183,25** without interest, and the price with PIX is **R$ 2.089,05** with a 5% discount. ✨  
  
- Up to 12 interest-free installments on credit cards  
- PIX price: **R$ 2.089,05**  
- Other options include debit card and bank slip

> Quick replies: Shipping info | Store hours | Another model
