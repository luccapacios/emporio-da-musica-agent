# Empório da Música: customer service agent

A Python text agent that answers customers of Empório da Música, a (fictional) musical instrument
store in Campo Grande, MS. It talks like the store's manual asks ("informal but professional, like
a friend who knows music"), looks up the catalog, promotions and orders before quoting anything,
applies the store policies (payment, returns, warranty, hours) and hands the case to the human team
when a rule or a record does not cover it.

The customer-facing language is Brazilian Portuguese (the agent mirrors the customer's language);
everything in this repository is in English.

- **Approach:** function calling over deterministic Python tools. The model decides *when* to
  look something up; Python decides *what is true* (prices, stock, installments, deadlines,
  who may see an order).
- **Model:** Google Gemini (`MODEL_PLACEHOLDER`) on the free tier, through the OpenAI compatible
  endpoint. Any OpenAI compatible provider works by changing three environment variables.
- **Interfaces:** terminal chat and a small web chat served by FastAPI.
- **Quality:** 51 unit tests without a model, a live tool routing eval
  ([docs/eval_results.md](docs/eval_results.md)) and five example conversations from real runs
  ([examples/](examples/)).

## Quick start

Requires Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
cp .env.example .env            # Windows: copy .env.example .env
```

Create a free API key at <https://aistudio.google.com/apikey> and paste it into `.env` as
`LLM_API_KEY`. Then:

```bash
python -m emporio.cli --debug          # terminal chat; --debug shows the tools called
uvicorn emporio.api:app --reload       # web chat at http://localhost:8000
pytest                                 # unit tests, no model or key needed
python scripts/eval_routing.py         # live tool routing eval -> docs/eval_results.md
python scripts/run_examples.py         # regenerates examples/
```

In the web chat, the **Ver consultas** switch shows which tools answered each message.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | (none) | Provider key. `GEMINI_API_KEY` is also accepted. |
| `LLM_BASE_URL` | Gemini OpenAI compatible endpoint | Any OpenAI compatible API (OpenAI, Groq, OpenRouter, a local Ollama at `http://localhost:11434/v1`). |
| `LLM_MODEL` | `MODEL_PLACEHOLDER` | Model id for that provider. |
| `LLM_TEMPERATURE` | `0.2` | Low, because answers must be factual. |
| `EMPORIO_TODAY` | real time in Campo Grande | Replay the agent at another moment, e.g. `2025-12-26T10:30`. |

## How it works

```
customer message
      |
      v
 Agent (agent.py) ---- system prompt: persona, scope, rules, current Campo Grande time
      |                 (prompts.py)
      |  chat.completions with tool schemas
      v
   LLM  --- wants a tool? ---> ToolBox.call (tools.py)
      ^                            |  search_products, get_product, list_active_promotions,
      |                            |  payment_options, get_order, find_orders, check_return,
      |                            |  store_hours_now, get_policy, escalate_to_human
      |                            v
      |                     data.py (cleaned CSVs)   rules.py (payment, returns, hours)
      |                     policy.py (manual by section)
      +------ tool results (JSON) -+
      |
      v
 final reply  -> saved with the tool results in SQLite (memory.py), per session
```

A turn is a loop of at most six model calls. Tool results stay in the conversation history, so
an order verified in one message is still verified in the next. Provider errors become a friendly
reply instead of a stack trace.

| Module | Responsibility |
|---|---|
| `config.py` | Settings from `.env`, store time zone, "now" override |
| `data.py` | Load and clean the CSVs (typed records, contact normalization, contradictory descriptions withheld) |
| `quality.py` | Data quality checks, written to [docs/data_quality.md](docs/data_quality.md) |
| `policy.py` | The manual split into its ten numbered sections, looked up by topic |
| `rules.py` | Pure business rules: PIX price, maximum installments, return and warranty windows, overdue orders, opening hours with holidays |
| `tools.py` | The ten tools, their JSON schemas and order ownership checks |
| `prompts.py` | Persona and rules |
| `agent.py` | Tool calling loop |
| `memory.py` | Conversation persistence |
| `cli.py`, `api.py`, `web/index.html` | Interfaces |

## Technical decisions

**Function calling over deterministic tools, not RAG or a SQL agent.** I compared three options
(details in [docs/design.md](docs/design.md)):

- *Everything in the prompt.* The data fits, but every customer's personal data would sit in the
  context of every conversation, and it contradicts the manual's own rule of "never quote price,
  stock or deadlines without checking the system".
- *RAG + text-to-SQL.* Flexible, but a generated query can read another customer's order, and
  vector search over an eight page manual with numbered sections adds a failure mode (the wrong
  chunk) without a gain.
- *Tools (chosen).* Privacy and arithmetic are enforced in code. The model cannot reveal an order
  without the tool confirming ownership, and it never computes an installment or a deadline.

**No agent framework.** The loop is about sixty lines on the official `openai` SDK. LangChain or
LlamaIndex would add abstractions that this problem does not need, and a plain loop is easier to
read, test (the tests drive it with a fake client) and debug.

**Retrieval for the policies is a topic router.** The manual has ten numbered sections with
distinct subjects, so `get_policy(topic)` with an enum of topics *is* the retrieval step, and it
is exact. The model's choice of topic is visible in the logs and in the eval. If the manual grew to
hundreds of pages, the same tool could sit on top of a vector index without changing the agent.
The PDF is converted once to markdown (`scripts/convert_policy.py`) and the result is committed,
so the conversion can be reviewed.

**Rules as code.** Anything the manual turns into a number lives in `rules.py`: PIX takes 5% off
the list price but never on top of a promotion; installments follow the minimum installment table
(R$ 50 up to 3x, R$ 80 up to 6x, R$ 100 up to 12x); the 7 day right of withdrawal, 7 day preference
exchange, 30 day defect exchange and 90 day legal warranty are computed per order; opening hours
include national, Mato Grosso do Sul and Campo Grande holidays (the brief arrived on Finados, a
holiday, and the agent knows the store is closed).

**Privacy by construction.** An order is only shown when the order number matches the phone or
e-mail on file. Two customers share one e-mail address in the data, so a shared e-mail is not
enough and the agent asks for the phone. Listing orders without a number requires phone *and*
e-mail. Failed lookups never say whether an order number exists.

**Model and provider.** Gemini Flash on the free tier: good tool calling and Portuguese, low
latency, and anyone evaluating the project can run it at zero cost with a Gmail account. I pinned
a specific version instead of a `latest` alias so behavior does not change under the eval. The
provider sits behind the OpenAI compatible API, so switching to OpenAI, Groq, OpenRouter or a local
model is a configuration change. One provider detail matters: Gemini 3 models attach *thought
signatures* to tool calls and require them back on the next request; the loop resends the
assistant message exactly as received (`model_dump`), and a unit test covers it.

**Prompt strategy.** The system prompt holds only identity, voice, scope and rules; facts come from
tools. The current date and time in Campo Grande are injected each turn, so "are you open today?"
and "can I still return it?" never depend on the model's idea of today. The prompt tells the model
which tool answers which kind of question, how to present promotions (original price, % off,
promotional price, per manual 6.2), what to do when an item is out of stock, discontinued or not
launched yet, and to treat instructions inside customer messages as data (no discounts unless a
tool returns them).

**Conversation memory.** SQLite, one row per session, including tool results. History is trimmed
to the last twelve user turns, cut at user messages so a tool call is never separated from its
result. The terminal chat resumes with `--session`; the web chat keeps its session id in the
browser.

**Human handoff.** Complaints, overdue or stuck orders, return requests (the agent explains the
rules but cannot approve a return), data deletion requests and anything uncovered go to
`escalate_to_human`, which logs the case to `data/runtime/handoffs.jsonl` and returns a protocol
number, the manual's 24 business hours SLA and, when the store is closed, when the team is back.

## Data handling and assumptions

The data has deliberate inconsistencies. The agent works around them, and
[docs/data_quality.md](docs/data_quality.md) lists each one for the store team. The main ones:

- **Descriptions that contradict the product.** Seven basses, drums and keyboards have
  descriptions of another brand or other specs ("Yamaha Kit 1" described as a Pearl Export, "Korg
  Synth 1" described as an 88-key Roland while its specs say 61). Name, price and specs are treated
  as the source of truth and those descriptions are never shown to the model.
- **Status and stock.** `discontinued` items are not offered even with stock (manual 7.3);
  `coming_soon` cannot be sold yet; `active` with zero stock is "temporarily unavailable" with
  alternatives of the same category and string type.
- **Promotions.** Only rows with `is_active = 1` count. Black Friday rows exist but are inactive,
  and the brief arrived in November, so the agent says plainly that there is no active campaign
  instead of promising one.
- **Orders.** Two totals do not match their items at current prices, three credit card orders are
  below the minimum installment, and a delivered order contains a `coming_soon` product. The
  recorded totals are quoted as they are.
- **Dates.** Order data stops in March 2026. Against the real date, shipped orders are months past
  their estimated delivery: the agent reports them as overdue and opens a handoff. Unshipped orders
  older than 15 days are flagged as stuck (the manual sets no processing time, so the threshold is
  my assumption).
- **Receipt date.** It is not recorded, so the estimated delivery date stands in for it, and the
  agent says the date is an estimate. The 30 day defect window counts from the purchase date, as
  the manual says.
- **Online purchases.** All orders in the system are treated as online, so the 7 day right of
  withdrawal applies to them.
- **Manual conflicts.** Two WhatsApp numbers and an accented e-mail domain: the company data table
  and the unaccented e-mail are used. The payment table's "minimum R$ 100" is read as the 7x to 12x
  tier of the detailed rule in 3.1.
- **Catalog coverage.** The manual lists woodwinds, brass and orchestral strings, but no products
  exist in those categories; the agent says so and offers to pass a special request to the team.
- **Accessories** are out of scope (manual 1 and 7.1). The manual suggests partner stores "when
  possible", but none are listed, so the agent does not invent any.

## Testing and evaluation

- **Unit tests (`pytest`, 51 tests, no model).** Data cleaning, every business rule (installment
  boundaries, PIX with and without promotions, window deadlines, Saturday closing time, holidays),
  every tool (including that a wrong phone returns no order data), the agent loop with a fake
  client (tool round trip, Gemini thought signatures, history, provider errors, the tool round
  cap) and the API.
- **Tool routing eval (live model).** `scripts/eval_routing.py` sends twelve single messages in
  fresh sessions and checks which tools were called and what the reply must or must not contain:
  catalog, price, promotions, hours, payment rules, store address, an order request without
  contact data (must not leak), order status, a return, accessories, an off-topic request and a
  prompt injection asking for a 50% discount. Results: [docs/eval_results.md](docs/eval_results.md).

## Example conversations

Each file is the literal output of `scripts/run_examples.py`: customer lines are scripted, Duda's
replies and the tool calls are the agent's real output.

| File | Scenario |
|---|---|
| [01-catalog-guitars-under-1000](examples/01-catalog-guitars-under-1000.md) | Catalog search with filters and a grounded recommendation |
| [02-price-and-payment](examples/02-price-and-payment.md) | "Quanto custa o Takamine GD20?", PIX and installments |
| [03-return-request-with-policy](examples/03-return-request-with-policy.md) | "Me arrependi da compra": identity check, windows computed for the order, warranty path |
| [04-overdue-order-handoff](examples/04-overdue-order-handoff.md) | Overdue delivery detected and handed to the team with a protocol |
| [05-store-info-scope-and-guardrails](examples/05-store-info-scope-and-guardrails.md) | Hours and address, accessories, inactive Black Friday, off-topic, prompt injection |

## Known limitations and next steps

- **No real channel.** The web chat stands in for WhatsApp. Next: a WhatsApp Business webhook in
  front of `/api/chat`, using the sender's phone as an extra identity signal.
- **Handoffs are a local log.** Next: create tickets in the store's helpdesk and notify the team.
- **Single process.** SQLite and the JSONL log are fine for a prototype; concurrent requests on the
  same session are not serialized. Next: a proper store (Postgres or Redis) and per session locks.
- **Read-only.** The agent cannot place orders, reserve items or cancel; it explains and hands off.
- **Shipping quotes outside Campo Grande** depend on CEP, weight and size, which the data does not
  have, so the agent explains the rules and hands off.
- **Eval size.** Twelve cases show routing works; a production setup would need a larger set run
  on every prompt change, plus LLM-as-judge checks for tone and policy faithfulness, and
  monitoring of tool error rates.
- **Free tier.** Rate limits slow the eval scripts (they pause between calls), and on the free
  tier Google may use prompts to improve its products, acceptable here because all data is fictional.
- **No retry limit on identity checks.** A production version would rate limit failed order
  lookups per session.

## Use of AI coding assistants

AI_SECTION_PLACEHOLDER

## Project structure

```
data/raw/            CSVs and policy PDF as provided
data/processed/      policies.md (converted manual)
docs/                design.md, data_quality.md, eval_results.md
examples/            five example conversations
scripts/             convert_policy.py, data_quality.py, eval_routing.py, run_examples.py
src/emporio/         the agent package
tests/               unit tests
web/index.html       web chat
```
