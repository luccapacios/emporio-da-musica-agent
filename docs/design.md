# Design: Empório da Música customer service agent

Date: 2026-11-02. Status: approved before implementation; later decisions are recorded in the README.

## Goal

A Python text agent that answers Empório da Música customers the way the store's own manual
asks: friendly but precise, never stating a price, stock level or deadline without checking
the system, and handing off to a human when a rule or a record does not cover the case.

## Approach

Three options were considered:

| Option | Why not / why |
|---|---|
| Everything in the prompt (all CSVs + manual) | It fits (65 products), but puts every customer's personal data in context, breaks the manual's rule of "check the system before quoting", and does not scale. |
| RAG with embeddings + text-to-SQL agent | Flexible, but generated SQL can read another customer's order, and vector search over an 8-page manual with numbered sections adds a failure mode (missed chunk) without a gain. |
| **Function calling over deterministic tools (chosen)** | The model decides *when* to look something up; Python decides *what is true*. Privacy checks, prices, installments and return windows are computed in code, not by the model. |

The manual is converted once into sectioned markdown and exposed through a `get_policy(topic)`
tool. With ten numbered sections, picking a section by topic is retrieval enough. If the
manual grows, the same tool can sit on top of a vector index without changing the agent.

## Components

```
src/emporio/
  config.py    settings from env (.env): provider, model, "today" override
  data.py      load and clean the CSVs, data quality report
  policy.py    sectioned manual (data/processed/policies.md), topic lookup
  rules.py     pure business rules: payment options, return windows, store hours
  tools.py     tool functions the model can call + their JSON schemas
  prompts.py   persona and system prompt
  agent.py     tool calling loop over an OpenAI compatible chat API
  memory.py    conversation persistence (SQLite, one row per session)
  cli.py       terminal chat
  api.py       FastAPI backend + static web chat (web/index.html)
```

Each unit can be tested alone. `rules.py` and `data.py` have no LLM dependency.

## Tools

| Tool | Purpose |
|---|---|
| `search_products` | Catalog search with filters: text, category, price range, nylon/steel strings, electric, in stock only. |
| `get_product` | Fuzzy name or id lookup ("Takamine GD20"). Returns price, stock, active promotion, PIX price and similar in-stock alternatives when unavailable or discontinued. |
| `list_active_promotions` | Only rows with `is_active = 1`. |
| `payment_options` | PIX price (5% off, not on promotional prices) and the maximum interest free installments allowed by the minimum installment table. |
| `get_order` | Order status, items, tracking and ETA. Requires order id plus the phone or e-mail on file. |
| `find_orders` | When the customer does not know the order id: requires both phone and e-mail. |
| `check_return` | Applies the return, exchange and warranty windows to a verified order. |
| `store_hours_now` | Open or closed right now in America/Campo_Grande, with national, MS state and Campo Grande holidays, and the next opening time. |
| `get_policy` | Returns a manual section by topic. |
| `escalate_to_human` | Logs complaints, overdue deliveries and anything the agent cannot resolve; returns a protocol number and the 24 business hours SLA from the manual. |

## Persona and prompt strategy

"Duda", the store's virtual assistant. Tone from manual section 7.1: informal but professional,
like a friend who knows music. Duda says it is a virtual assistant, answers in the customer's
language, keeps messages short (chat, not e-mail), and follows the standard flow of section
7.2 (greet, understand, look up, answer, close).

The system prompt holds only identity, scope and rules. Facts come from tools. The current
date and time in Campo Grande are injected on every turn so the model never guesses "today".

## Data handling and assumptions

Found while reading the data; each one becomes either code or a documented assumption.

* **Product descriptions contradict names** for several basses, drums and keyboards
  (e.g. "Yamaha Kit 1" described as a Pearl Export, "Korg Synth 1" described as an 88-key
  Roland while its specs say 61 keys). Name, price and specs are treated as the source of
  truth; conflicting descriptions are hidden from the agent and listed in the data quality report.
* **Customers are not uniquely identified by name or e-mail.** Two customers share
  `bruno.martins@jmail.com` and several names are near duplicates. An order is only disclosed
  when the order id matches the phone or e-mail on file; a shared e-mail requires the phone.
* **Statuses:** `discontinued` products are not offered (manual 7.3); `coming_soon` cannot be
  sold yet; `active` with zero stock is "temporarily unavailable" with alternatives.
* **Promotions:** only `is_active = 1` counts. Black Friday rows exist but are inactive, and
  it is November: the agent must not promise the discount (manual 7.3).
* **Order data stops in March 2026.** Against the real date, shipped orders are months past
  their ETA. The agent reports this as overdue and offers a handoff. The reference date can be
  overridden with `EMPORIO_TODAY` to replay scenarios.
* **Receipt date is not recorded.** `estimated_delivery` is used as the receipt date for
  delivered orders; the 30-day defect window counts from `order_date` (manual 4.2 says
  "after purchase").
* **All orders in the system are treated as online purchases**, so the 7-day right of
  withdrawal (manual 4.1) applies to them.
* **Order inconsistencies** (two totals that do not match their items, three credit
  card orders below the minimum installment, a `coming_soon` product marked delivered) are
  reported, not "fixed": the agent quotes the recorded total.
* **Manual conflicts:** two WhatsApp numbers ((67) 3341-4444 in the company data table,
  (67) 3321-4500 in section 7) and an accented e-mail domain. The company data table and the
  unaccented e-mail from the cover are used. Installments follow the detailed table in 3.1.
* **Catalog coverage:** the manual mentions woodwinds, brass and orchestral strings and
  "300+ instruments", but the data has 65 products and none in those three categories. The
  agent says there are no items in the system for those categories and offers a handoff.
* **Accessories** are out of scope (manual 1 and 7.1). No partner stores are listed, so the
  agent does not invent any.

## Conversation memory

SQLite, one row per session id, storing the full message list (including tool results, so an
order verified earlier in the chat stays verified). History is trimmed at user turn
boundaries so tool call pairs never break. The CLI resumes with `--session`; the web chat keeps
its session id in the browser.

## Error handling

* Tool errors are returned to the model as structured errors, never raised to the user.
* The tool loop is capped; if the cap is hit, Duda apologizes and offers a handoff.
* Provider errors (rate limits, timeouts) produce a friendly message and are logged.

## Testing

* `pytest` covers rules, data cleaning, verification and tool outputs without calling a model.
* `scripts/eval_routing.py` runs thirteen customer messages against the live model and checks
  that the expected tools were called (data vs policy vs none). This is the measurable version
  of "knows when to consult data and when to consult policies".
* `scripts/run_examples.py` produces the example conversations in `examples/` from real runs.

## Interfaces

* CLI: `python -m emporio.cli` (with `--debug` to show tool calls).
* Web: `uvicorn emporio.api:app`, serving a small chat page and `POST /api/chat`.

## Model and provider

Google Gemini on the free tier, called through its OpenAI compatible endpoint with the
official `openai` SDK. Any OpenAI compatible provider (OpenAI, Groq, OpenRouter, a local
Ollama) works by changing `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`.
