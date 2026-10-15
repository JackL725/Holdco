# The shared ledger

The ledger is the one place every agent in a company reads from and writes to. It replaces free-form
"memory" as the source of truth and is designed so that a team of small local models, working in parallel
around the clock, stays consistent, auditable and hard to fool.

## Design goals

1. **One truth per fact.** A price, a URL, a policy or a customer's pipeline stage has exactly one current
   value, and every agent sees the same one.
2. **Nothing is ever lost.** The ledger is append-only. Entries are *superseded* (a newer entry for the same
   key) or *voided* (struck out), never edited or deleted. The owner can always see how a value changed, who
   changed it and when. This is what makes agents auditable, and it is how accounting ledgers earn trust.
3. **Typed, so it can be checked.** Money, contacts and metrics are validated and deduplicated on the way in
   (a transaction with the same provider id, or the same manual entry, is one transaction; a contact is one
   row per email address). Prose can't be summed or reconciled; typed books can.
4. **Cheap for small models.** Agents don't need to learn a schema. They use six write tools with obvious
   names and one read tool, and every system prompt opens with a compact summary of the current state.
5. **Visible to humans.** Every book is rendered to plain Markdown/CSV files in the company folder, shown on
   the dashboard's Ledger tab, and exportable as CSV for an accountant or a CRM.

## The model

One table, `ledger`, holds the journal:

| column | meaning |
|---|---|
| `company_id`, `agent_id` | whose entry it is (`agent_id` NULL = the owner or a connector) |
| `book` | which book: `notes`, `facts`, `decisions`, `contacts`, `transactions`, `metrics`, `handoffs` |
| `key` | the entry's identity within the book (a fact key, an email, a provider transaction id, a metric name, a task id); empty for plain notes |
| `summary` | one line of text, indexed for full-text search and shown to agents |
| `data` | the typed fields as JSON |
| `pinned` | shown to every agent in every session (notes only) |
| `supersedes` | the previous entry for the same key |
| `current` | 1 for the latest non-voided entry of a key |
| `voided` | struck out by the owner |
| `source` | `agent`, `owner`, or the connector that produced it (`stripe`, `netlify`...) |

"What do we believe now?" is `WHERE current = 1`. "How did we get here?" is the chain of `supersedes`.
Writing the same key with identical data is a no-op, which makes connector syncs and retried tool calls
idempotent.

### Books

| book | key | fields | notes |
|---|---|---|---|
| notes | none | kind (fact, lesson, decision, contact, note, owner), content | pinned notes go in every prompt |
| facts | dotted key, e.g. `pricing.pro_usd` | value, source | latest per key wins |
| decisions | slug of the title | title, decision, why, options, reversible, by | the owner's approvals, rejections and answers land here too |
| contacts | email (or name+company) | name, company, role, stage, source, notes, do_not_contact, touches, first_seen | upserts merge: fields you don't pass are kept |
| transactions | `provider:external_id`, or a hash of a manual entry | date, amount, currency, direction, category, counterparty, description | chart of accounts below |
| metrics | metric name | value, note, period | every reading is kept, so trends can be computed |
| handoffs | `task:<id>` | status, summary, deliverable, evidence, open_issues, next_step, agent | written automatically when a task finishes |

Pipeline stages: `new, contacted, replied, qualified, meeting, proposal, customer, churned, lost,
do_not_contact`.

Chart of accounts (categories): `revenue, refunds, fees, cost_of_revenue, advertising, contractors, software,
legal, taxes, sales_tax, owner_contribution, owner_draw, other`. Free-text categories are mapped onto these.
Direction (`in`/`out`) defaults from the category. Payouts and transfers between a processor and the bank are
not income or expenses and are not recorded.

## How agents use it

| tool | writes to | typical caller |
|---|---|---|
| `remember(content, kind, pin)` | notes (or decisions when kind = decision) | everyone |
| `record_fact(key, value, source)` | facts | CEO, research, custom agents |
| `record_decision(title, decision, why, options, reversible)` | decisions | CEO, product |
| `save_contact(email, name, company, role, stage, source, notes)` | contacts | sales, partnerships, support, success, lifecycle |
| `record_transaction(date, amount, category, ...)` | transactions | finance & operations |
| `record_metric(name, value, note)` | metrics | most roles |
| `recall(query, book, key)` | reads: search everything, list a book, or show a key's history | everyone |

Connector tools write too: `sync_payments` fills the money book from Stripe, Lemon Squeezy or Gumroad;
`list_signups` adds website form submissions to contacts; `site_stats` and `payments_summary` record metrics.

Every system prompt contains a compact ledger section: pinned notes, current facts, recent decisions, recent
notes, contact counts by stage, money this month and cash, and the number of task handoffs on record. The
`recall` tool returns full books with totals (for money: this month, all time, by category; for contacts:
counts by stage).

When a task finishes, its structured handoff (status, deliverable, evidence, open issues, next step) is written
to the handoffs book automatically, so any teammate can find out what happened to task #12 with
`recall(book="handoffs", key="12")`.

## How the owner uses it

The company's **Ledger** tab shows every book with search, history per key, pin/unpin, void, add-entry forms
and a CSV export. Money shows this month's in/out/net, cash all time and a breakdown by category. Owner
approvals, rejections (with feedback) and answers to agent questions are recorded as decisions by "owner".

The `ledger/` folder in the company's workspace always holds the rendered books (`facts.md`, `decisions.md`,
`notes.md`, `handoffs.md`, `contacts.csv`, `transactions.csv`, `metrics.csv`). They are regenerated on every
write; edit the ledger through the tools or the dashboard, not the files.

## Upgrading

On first start after the upgrade, rows from the old `memory` and `metrics` tables are copied into the ledger
(notes keep their pin and kind; metrics keep their history). The old tables are left in place and no longer
used. The old `/api/companies/<id>/memory` endpoints still work and are backed by the notes book.

## Extending

Adding a book means adding one entry to `ledger.BOOKS`, a typed writer function (validate, build `data` and
`summary`, call `add()`), an `entry_line` case for how agents see it, and an `export` case for the file.
Nothing else changes: search, history, void, pinning, the API and the dashboard list view are generic.
