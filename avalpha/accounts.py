"""User provisioning and portfolio authorization.

Cloudflare Access proves an email address. This module turns that external
identity into database-owned authorization state and never trusts a portfolio
or administrator claim supplied by the request.
"""

import sqlite3
from dataclasses import dataclass

from avalpha.db import utcnow

ADMIN_EMAIL = "avi@arboretuminvestments.net"


@dataclass(frozen=True)
class User:
    id: int
    email: str
    is_admin: bool
    portfolio_id: int
    portfolio_name: str


@dataclass(frozen=True)
class PortfolioAccess:
    user: User
    portfolio_id: int
    portfolio_name: str
    owner_user_id: int
    owner_email: str

    @property
    def read_only(self) -> bool:
        return self.owner_user_id != self.user.id


def normalize_email(email: str) -> str:
    """Canonical form used for lookup and the database unique key."""
    return email.strip().casefold()


def resolve_login(conn: sqlite3.Connection, verified_email: str) -> User:
    """Resolve a verified email, provisioning one empty portfolio if new."""
    email = normalize_email(verified_email)
    if not email or "@" not in email:
        raise ValueError("verified identity carries an invalid email")

    now = utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO users (email, is_admin, created_at) VALUES (?, 0, ?)",
        (email, now),
    )
    row = conn.execute(
        "SELECT id, email, is_admin FROM users WHERE email = ?", (email,)
    ).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO portfolios (owner_user_id, name) VALUES (?, ?)",
        (row["id"], _portfolio_name(email)),
    )
    conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, row["id"]))
    portfolio = conn.execute(
        "SELECT id, name FROM portfolios WHERE owner_user_id = ?", (row["id"],)
    ).fetchone()
    conn.commit()
    return User(
        id=row["id"],
        email=row["email"],
        is_admin=bool(row["is_admin"]),
        portfolio_id=portfolio["id"],
        portfolio_name=portfolio["name"],
    )


def authorize_portfolio(
    conn: sqlite3.Connection, user: User, portfolio_id: int | None = None
) -> PortfolioAccess | None:
    """Return an authorized portfolio or ``None`` without leaking existence."""
    selected = user.portfolio_id if portfolio_id is None else portfolio_id
    row = conn.execute(
        """
        SELECT p.id, p.name, p.owner_user_id, u.email AS owner_email
        FROM portfolios p JOIN users u ON u.id = p.owner_user_id
        WHERE p.id = ?
        """,
        (selected,),
    ).fetchone()
    if row is None:
        return None
    if row["owner_user_id"] != user.id and not user.is_admin:
        return None
    return PortfolioAccess(
        user=user,
        portfolio_id=row["id"],
        portfolio_name=row["name"],
        owner_user_id=row["owner_user_id"],
        owner_email=row["owner_email"],
    )


def admin_users(conn: sqlite3.Connection) -> list[dict]:
    """User directory for the administrator dashboard."""
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT u.id, u.email, u.last_login_at, p.id AS portfolio_id,
                   COUNT(CASE WHEN ph.active = 1 THEN 1 END) AS active_holdings
            FROM users u
            JOIN portfolios p ON p.owner_user_id = u.id
            LEFT JOIN portfolio_holdings ph ON ph.portfolio_id = p.id
            GROUP BY u.id, u.email, u.last_login_at, p.id
            ORDER BY u.email
            """
        )
    ]


def default_portfolio_id(conn: sqlite3.Connection) -> int:
    """Avi's portfolio for administrator CLI operations and legacy migration."""
    row = conn.execute(
        """
        SELECT p.id FROM portfolios p JOIN users u ON u.id = p.owner_user_id
        WHERE u.email = ?
        """,
        (ADMIN_EMAIL,),
    ).fetchone()
    if row is None:
        raise RuntimeError("administrator portfolio is not provisioned")
    return row["id"]


def portfolio_owner_email(conn: sqlite3.Connection, portfolio_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT u.email FROM portfolios p JOIN users u ON u.id = p.owner_user_id
        WHERE p.id = ?
        """,
        (portfolio_id,),
    ).fetchone()
    return row["email"] if row else None


def _portfolio_name(email: str) -> str:
    if email == ADMIN_EMAIL:
        return "Avi's Portfolio"
    return "My Portfolio"
