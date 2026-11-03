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
- **Model:** Google Gemini Flash-Lite (`gemini-3.5-flash-lite`) on the free tier, through the
  OpenAI compatible endpoint. `gemini-3.5-flash` works too when its smaller free quota allows. Any
  OpenAI compatible provider works by changing three environment variables.
- **Interfaces:** terminal chat, and a web chat served by FastAPI where Duda, the assistant,
  answers with product cards built from the catalog.
- **Quality:** 121 unit tests without a model, a live tool routing eval
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

### The web chat

The terminal chat is enough to evaluate the agent; the web chat shows what it would be like for a
store customer. Its feature set came from a research pass over reference products (Intercom
Messenger, Zendesk Messaging, Lu do Magalu, Amazon Rufus, ChatGPT and Claude, WhatsApp Business,
Nielsen Norman Group and WCAG guidance), compared against this code.

- **Answers with cards:** product cards (price, PIX, installments, stock, "Detalhes" sheet with
  specs, "Quero este", compare 2 or 3 side by side), order card with a status timeline and
  tracking code, return-window card, store hours and location card, and a protocol card when the
  case goes to the team.
- **Transparency:** a live step list while Duda works ("Procurando no catálogo...") and, under each
  answer, which sources it used. Duda is labeled as an AI assistant, says what it cannot do, and
  "Falar com a equipe" is always one tap away.
- **Conversations:** a history per browser (no accounts; an unguessable id that the server stores
  only as a hash), search, rename, delete one or all, automatic titles, close with a 1 to 5 rating,
  edit the last question or regenerate the answer, download or copy the conversation.
- **Trust and privacy:** a privacy notice with the store's LGPD section, a warning before sending
  something that looks like a card number or CPF (card numbers are also masked on the server),
  90-day retention, and links only to allowed hosts.
- **Accessibility and PWA:** WCAG 2.2 AA contrast, keyboard and screen reader support, 44px touch
  targets, reduced motion; installable on phone and desktop, opens offline (store data is never
  cached).

Duda's portrait was generated with an image model for this prototype.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LLM_API_KEY` | (none) | Provider key. `GEMINI_API_KEY` is also accepted. |
| `LLM_BASE_URL` | Gemini OpenAI compatible endpoint | Any OpenAI compatible API (OpenAI, Groq, OpenRouter, a local Ollama at `http://localhost:11434/v1`). |
| `LLM_MODEL` | `gemini-3.5-flash-lite` | Model id for that provider. |
| `LLM_TEMPERATURE` | `0.2` | Low, because answers must be factual. |
| `EMPORIO_TODAY` | real time in Campo Grande | Replay the agent at another moment, e.g. `2025-12-26T10:30`. |
| `EMPORIO_RETENTION_DAYS` | `90` | Web chat conversations untouched for longer are deleted. |

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

**Model and provider.** Gemini on the free tier, so anyone evaluating the project can run it at
zero cost with a Gmail account. The choice of version came from measuring, not from the docs:

| Model | What happened |
|---|---|
| `gemini-2.5-flash` | Listed by the API but retired (404). |
| `gemini-3.8-flash` | No answer within 45 s on the free tier. |
| `gemini-3.5-flash` | Answers in about 3 s with correct tool calls (the server reports it as `gemini-3.6-flash`), but the free tier allows only 20 requests a day per model, which one eval run plus the examples exceed. |
| `gemini-3.5-flash-lite` | Own quota, about 2.5 s per turn, correct tool calls and Portuguese. In testing it once claimed to have forwarded a case without calling the tool, which led to the guard below. |

Flash-Lite is the default; set `LLM_MODEL=gemini-3.5-flash` for the stronger model when quota
allows. I pinned versions instead of a `latest` alias so behavior does not change under the eval.
The provider sits behind the OpenAI compatible API, so switching to OpenAI, Groq, OpenRouter or a
local model is a configuration change. One provider detail matters: Gemini 3 models attach
*thought signatures* to tool calls and require them back on the next request; the loop resends the
assistant message exactly as received (`model_dump`), and a unit test covers it.

**A guard against claimed actions.** A reply that says the case was registered or forwarded, or
that cites a protocol number, must be backed by a protocol that `escalate_to_human` actually issued
in that conversation. If it is not, the agent sends the draft back to the model once with a note
("call the tool now, or rephrase without claiming it"); the draft and the note are never saved or
shown. Invented protocol numbers are caught the same way. The prompt states the rule too, but a
rule in code holds even when a smaller model ignores the prompt, and the eval fails any answer that
makes an unbacked claim.

**Prompt strategy.** The system prompt holds only identity, voice, scope and rules; facts come from
tools. The current date and time in Campo Grande are injected each turn, so "are you open today?"
and "can I still return it?" never depend on the model's idea of today. The prompt tells the model
which tool answers which kind of question, how to present promotions (original price, % off,
promotional price, per manual 6.2), what to do when an item is out of stock, discontinued or not
launched yet, and to treat instructions inside customer messages as data (no discounts unless a
tool returns them).

Reading the generated examples changed the prompt twice. Flash-Lite answered an English question
in Portuguese, so the agent now detects the language of each message (Portuguese, English or
Spanish, from small word lists) and states it in the system prompt for that turn. It also told a
customer he still had the 90-day legal warranty on an order whose windows `check_return` had just
reported closed, and it added praise for a guitar that the data does not support; the prompt now
says to answer follow-ups from the order's own windows and to describe products only with facts
from the tools.

**Conversation memory.** SQLite, one row per session, including tool results. History is trimmed
to the last twelve user turns, cut at user messages so a tool call is never separated from its
result. The terminal chat resumes with `--session`; the web chat lists the browser's conversations
through an index table (owner hash, title, status, protocol, rating). Turns on one conversation are
serialized with a per-session lock, and deleting a conversation waits for a turn in flight, so a
deleted conversation can never be written back.

**Human handoff.** Complaints, overdue or stuck orders, return requests (the agent explains the
rules but cannot approve a return), data deletion requests and anything uncovered go to
`escalate_to_human`, which logs the case to `data/runtime/handoffs.jsonl` and returns a protocol
number, the manual's 24 business hours SLA and, when the store is closed, when the team is back.

**Product cards without trusting the model's numbers.** In the web chat, answers come with product
cards. The model only says *which* products to show, in a hidden last line (`[[cards: 83, 85]]`),
plus up to three tap-to-send replies (`[[quick: Só nylon | Só aço]]`). The server strips these hints
and builds every card from the catalog at that moment, so the price, PIX price, installments and
stock on a card can never be a hallucination. I chose hints in the text over a `show_products` tool
because a tool costs one more model round trip per answer; if the model forgets a hint, only the
card is lost, since the text still names the products and their prices.

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

- **Unit tests (`pytest`, 121 tests, no model).** Data cleaning, every business rule (installment
  boundaries, PIX with and without promotions, window deadlines, Saturday closing time, holidays),
  every tool (including that a wrong phone returns no order data), the agent loop with a fake
  client (tool round trip, Gemini thought signatures, history, provider errors, the tool round
  cap, the claimed-action guard) and the API (conversation ownership across browsers, deletes
  during a turn in flight, card masking, edit and regenerate that survive a provider failure).
- **Review.** Before merging the chat features, three independent reviews (privacy and security,
  accessibility, functional regressions) went over the code; every high and medium finding was
  fixed and covered by a test or checked in the browser.
- **Tool routing eval (live model).** `scripts/eval_routing.py` sends thirteen single messages in
  fresh sessions and checks which tools were called and what the reply must or must not contain:
  catalog, price, promotions, hours, payment rules, store address, an order request without
  contact data (must not leak), an overdue order (must look it up and hand it off), a return,
  accessories, a question in English (must be answered in English), an off-topic request and a
  prompt injection asking for a 50% discount. Every case also fails if the reply claims a handoff
  or protocol that no tool issued. Results: [docs/eval_results.md](docs/eval_results.md).

## Example conversations

Each file is the literal output of `scripts/run_examples.py`: customer lines are scripted, Duda's
replies and the tool calls are the agent's real output.

| File | Scenario |
|---|---|
| [01-catalog-guitars-under-1000](examples/01-catalog-guitars-under-1000.md) | Catalog search with filters and a grounded recommendation |
| [02-price-and-payment-in-english](examples/02-price-and-payment-in-english.md) | "How much is the Takamine GD20?": PIX and installments, answered in the customer's language |
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
- **Eval size.** Thirteen cases show routing works; a production setup would need a larger set run
  on every prompt change, plus LLM-as-judge checks for tone and policy faithfulness, and
  monitoring of tool error rates.
- **Free tier.** Daily quotas are small (20 requests a day for `gemini-3.5-flash`), so the eval and
  example scripts pause between calls, retry rate limits, and refuse to write a report when a case
  did not run because of the provider. On the free tier Google may use prompts to improve its
  products, acceptable here because all data is fictional.
- **No retry limit on identity checks.** A production version would rate limit failed order
  lookups per session.

## Use of AI coding assistants

I built this project with **Claude Code** (Anthropic's coding agent, running Claude Opus 5.5) as a
pair programmer in the Claude desktop app. Commits made in those sessions carry a
`Co-Authored-By: Claude` trailer, so the history shows where it was involved.

**My part: direction, decisions and review**

- Shared the brief, the store manual and the data, and set the ground rules: Python, a model that
  evaluators can run for free, deliverables in English, and no call to an external API (model or
  GitHub) and nothing published without my explicit go-ahead.
- Picked the provider (Gemini on the free tier) and the interface (terminal plus web chat) from the
  options Claude laid out, and approved the design doc before any code was written.
- Directed the interface. I rejected the first web chat as too plain, chose one of three directions
  that Claude rendered side by side (each based on a real product: Duolingo, Intercom Messenger,
  Spotify), asked for the mascot to be a person instead of an instrument, rejected a vector "3D"
  attempt, and generated Duda's portrait myself with an image generation model, from a prompt Claude
  wrote around the store's colors. I also asked for conversation history and for the missing chatbot
  features, which Claude then researched against reference products.
- Tested the chat in the browser and reported defects (a double focus ring, cards that could only be
  scrolled with the scrollbar, an unreadable active button).
- Created the API key on my own account and pasted it into `.env`; the assistant never saw it.

**Claude Code's part: analysis and implementation**

- Read the manual and the CSVs and listed the inconsistencies before proposing anything
  ([docs/data_quality.md](docs/data_quality.md)).
- Proposed three architectures with trade-offs, wrote [docs/design.md](docs/design.md) and an
  implementation plan, then implemented in small steps: tests first for the rules and tools, one
  commit per step.
- Ran the tests, checked the web chat in an embedded browser against a fake agent (no model calls),
  and probed the available Gemini models when the first choice failed (see the model table above).
  When the free quota of 3.5 Flash ran out, it read the quota error, proposed Flash-Lite, and on the
  first test caught Flash-Lite claiming a handoff it had not made, which became the claimed-action
  guard and an eval check. I approved the switch.
- For the chatbot features, ran a multi-agent research pass over reference products (support
  messengers, retail assistants, AI chat apps, WhatsApp Business, chatbot UX guidelines) and a gap
  analysis against this code. It then wrote an API contract and built the features in two parallel
  tracks (backend and frontend), followed by three independent reviews (privacy and security,
  accessibility, functional regressions) and a fix pass for every high and medium finding.
- Ran the eval and generated the example conversations from real runs.

**How the result stays trustworthy**

- No fact the customer sees (price, stock, installments, deadline, order status) is written by a
  language model: it comes from code covered by unit tests, and the model only decides what to look up.
- The example conversations and the eval results are script output, not hand-written.

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
