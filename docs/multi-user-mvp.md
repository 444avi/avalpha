# Multi-User MVP

The MVP keeps the existing SQLite deployment and implements one user, one
portfolio, plus one read-only administrator.

## Data model

Add three core concepts:

```text
users
- id
- email (normalized, unique)
- is_admin
- created_at
- last_login_at

portfolios
- id
- owner_user_id (unique)
- name

portfolio_holdings
- portfolio_id
- ticker
- weight
- active
- added_at
- deactivated_at
- unique(portfolio_id, ticker)
```

Keep company information global. The existing `watchlist` metadata—ticker,
CIK, legal name, aliases, industry, IR feed, and shares outstanding—becomes the
shared security catalog.

Move only the user-specific fields out of it:

- Weight
- Active/inactive state
- Added/deactivated timestamps

This allows two users to hold the same ticker at different weights while news,
prices, matching, and scoring are calculated once.

## Login behavior

When a verified email logs in:

1. Look up the normalized email in `users`.
2. If it is new, create one user and one portfolio owned by that user.
3. Show that portfolio.
4. New users begin with an empty portfolio.

Migrate the existing shared portfolio to `avi@arboretuminvestments.net`. Do not
give the existing holdings to every new user.

The current authentication mechanism can remain. Instead of returning only an
email, it should resolve that email to the internal user and portfolio.

## Administrator behavior

Mark `avi@arboretuminvestments.net` as `is_admin = true`.

The administrator gets an `/admin` page showing:

- User email
- Number of active holdings
- Last login
- A link to view the user's portfolio

When the administrator views another user's portfolio:

- Show the normal dashboard with a clear "Viewing Jane's portfolio" banner.
- Make the portfolio read-only.
- Hide edit, add, deactivate, calendar-edit, and job-trigger controls.
- Enforce read-only access on the server, not just in the UI.

Ordinary users must never be able to supply another user or portfolio ID and
access it. Administrator status must come from the database, not a URL
parameter, cookie, or request header.

## Portfolio scoping

Every customer-facing operation must receive a `portfolio_id`:

- Dashboard holdings
- Holding details
- Adding a holding
- Changing weights
- Activating and deactivating holdings
- Manual calendar events
- Digest history and PDF downloads
- User-triggered enrichment jobs

For example, changing a weight must mean:

```text
Update NVDA
where portfolio_id = the logged-in user's portfolio
```

It must not mean simply "update NVDA."

Feed-generated calendar events, prices, news, matches, and scores remain global,
but are displayed only for securities in the selected portfolio.

## Background jobs

Collectors should operate on the distinct union of every active portfolio's
tickers:

```text
Alice: NVDA, AAPL
Bob:   NVDA, MSFT

Collector universe: NVDA, AAPL, MSFT
```

NVDA is collected and scored once.

Adding a holding must carry the requesting portfolio ID through the enrichment
job. Otherwise, when enrichment finishes, the system will not know which
portfolio should receive the holding.

Global operational jobs—collectors, matcher, scorer, system health, and global
job logs—should be visible and triggerable only by the administrator.

## Digests

Make each digest belong to a portfolio:

- Unique by `(portfolio_id, date)`.
- Store PDFs under a portfolio-specific directory.
- Check portfolio ownership before serving a PDF.
- Generate separate portfolio content for each user.
- Send it to that portfolio owner's verified email.

The existing digest key is only the date, so it cannot represent multiple
users.

## Minimal rollout

1. Add users, portfolios, and portfolio holdings.
2. Create Avi's user and portfolio.
3. Migrate the existing holdings and manual events to Avi.
4. Provision an empty portfolio for every new login.
5. Scope every read and mutation by portfolio.
6. Add the read-only administrator user list and portfolio viewer.
7. Update collectors to use the union of active tickers.
8. Make digests portfolio-specific.
9. Add isolation tests.

## Release-blocking tests

- Alice cannot see or modify Bob's portfolio.
- Alice and Bob can hold the same ticker at different weights.
- A new user sees an empty portfolio.
- Avi can view both portfolios.
- Avi cannot accidentally edit another portfolio while in administrator view.
- Alice cannot access Bob's manual events, jobs, or digest PDFs by guessing IDs.

## Corporate accounts later

The `portfolios` table preserves a simple upgrade path. Later, add:

```text
portfolio_members
- portfolio_id
- user_id
- role
```

Existing owners become the first members. Billing and invitations can then be
added without redesigning holdings, digests, or the analysis pipeline.

The MVP is therefore one user, one portfolio, one special administrator, and
strict portfolio scoping.
