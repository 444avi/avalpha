"""FastAPI portfolio console with database-backed multi-user authorization."""

import hashlib
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from avalpha import accounts, calendar_store, db, watchlist
from avalpha.accounts import PortfolioAccess, User
from avalpha.calendar_store import Event, is_bio
from avalpha.config import Config, load_config
from avalpha.web import queries
from avalpha.web.auth import AccessDenied, Authenticator, extract_token, load_access_config
from avalpha.web.jobs import JobRunner

_HERE = Path(__file__).resolve().parent
_VALID_JOBS = {"matcher", "scorer", "digest"} | {
    f"collector:{s}" for s in queries.SOURCES
}
_MANUAL_KINDS = {"manual", "pdufa", "analyst_day", "product_launch"}


def _asset_version() -> str:
    h = hashlib.sha256()
    for name in ("styles.css", "app.js"):
        try:
            h.update((_HERE / "static" / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


def _valid_date(value: str) -> bool:
    try:
        __import__("datetime").date.fromisoformat(value)
        return True
    except ValueError:
        return False


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    app = FastAPI(title=f"{config.web_fund_name} — Portfolio Console")
    app.state.config = config
    app.state.auth = Authenticator(load_access_config())
    app.state.jobs = JobRunner(config)

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["money"] = _money
    templates.env.filters["pct"] = _pct
    templates.env.filters["shortdt"] = _shortdt
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    asset_version = _asset_version()

    @app.middleware("http")
    async def _static_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    # -- request-scoped authorization -------------------------------------

    def get_conn(request: Request):
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request, conn=Depends(get_conn)) -> User:
        token = extract_token(request.headers, request.cookies)
        email = request.app.state.auth.member_email(token)
        try:
            return accounts.resolve_login(conn, email)
        except ValueError as exc:
            raise AccessDenied(str(exc)) from exc

    def selected_access(
        portfolio_id: int | None = None,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
    ) -> PortfolioAccess:
        access = accounts.authorize_portfolio(conn, user, portfolio_id)
        if access is None:
            raise AccessDenied("you do not have access to that portfolio")
        return access

    def require_admin(user: User) -> None:
        if not user.is_admin:
            raise AccessDenied("administrator access is required")

    def require_writable(access: PortfolioAccess) -> None:
        if access.read_only:
            raise AccessDenied("administrator portfolio views are read-only")

    def suffix(access: PortfolioAccess) -> str:
        return f"?portfolio_id={access.portfolio_id}" if access.read_only else ""

    def render(
        request: Request,
        name: str,
        user: User,
        access: PortfolioAccess | None = None,
        **ctx,
    ) -> HTMLResponse:
        base = {
            "request": request,
            "fund_name": config.web_fund_name,
            "member": user.email,
            "current_user": user,
            "portfolio_access": access,
            "read_only": access.read_only if access else False,
            "view_suffix": suffix(access) if access else "",
            "asset_version": asset_version,
            "path": request.url.path,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    def back(
        url: str,
        access: PortfolioAccess | None = None,
        msg: str | None = None,
        err: str | None = None,
    ) -> RedirectResponse:
        params = {}
        if access and access.read_only:
            params["portfolio_id"] = str(access.portfolio_id)
        if msg:
            params["msg"] = msg
        if err:
            params["err"] = err
        if params:
            url = f"{url}?{urlencode(params)}"
        return RedirectResponse(url, status_code=303)

    def dashboard_response(
        request: Request,
        conn,
        user: User,
        access: PortfolioAccess,
    ) -> HTMLResponse:
        holdings = queries.portfolio(conn, access.portfolio_id)
        show_ops = user.is_admin and not access.read_only
        return render(
            request,
            "dashboard.html",
            user,
            access,
            holdings=holdings,
            total_weight=queries.total_weight(holdings),
            health=queries.health(conn) if show_ops else None,
            scores=queries.recent_scores(
                conn, limit=25, portfolio_id=access.portfolio_id
            ),
            jobs=queries.recent_jobs(conn) if show_ops else [],
            sources=queries.SOURCES,
            upcoming=queries.upcoming_events(conn, access.portfolio_id, days=7),
            badges=queries.ticker_badges(conn, access.portfolio_id),
            show_ops=show_ops,
        )

    # -- access-denied handling -------------------------------------------

    @app.exception_handler(AccessDenied)
    async def _denied(request: Request, exc: AccessDenied):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            return templates.TemplateResponse(
                request,
                "403.html",
                {
                    "request": request,
                    "fund_name": config.web_fund_name,
                    "reason": exc.reason,
                    "asset_version": asset_version,
                },
                status_code=403,
            )
        return JSONResponse({"error": "forbidden", "detail": exc.reason}, status_code=403)

    # -- routes ------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        return dashboard_response(request, conn, user, access)

    @app.get("/admin", response_class=HTMLResponse)
    def admin(
        request: Request,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
    ):
        require_admin(user)
        return render(request, "admin.html", user, users=accounts.admin_users(conn))

    @app.get("/admin/portfolio/{portfolio_id}", response_class=HTMLResponse)
    def admin_portfolio(
        request: Request,
        portfolio_id: int,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
    ):
        require_admin(user)
        access = accounts.authorize_portfolio(conn, user, portfolio_id)
        if access is None:
            raise AccessDenied("you do not have access to that portfolio")
        return dashboard_response(request, conn, user, access)

    @app.get("/holding/{ticker}", response_class=HTMLResponse)
    def holding(
        request: Request,
        ticker: str,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        ticker = ticker.upper()
        detail = queries.holding_detail(conn, ticker, access.portfolio_id)
        if detail is None:
            return back("/", access, err=f"{ticker} is not in this portfolio.")
        events = queries.events_for_ticker(conn, ticker, access.portfolio_id)
        return render(
            request,
            "holding.html",
            user,
            access,
            d=detail,
            events=events,
            is_bio=is_bio(detail["holding"].industry),
            show_ops=user.is_admin and not access.read_only,
        )

    @app.post("/holding/add")
    def add_holding(
        request: Request,
        ticker: str = Form(...),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        value = ticker.strip().upper()
        if not value.isalpha() or len(value) > 6:
            return back("/", access, err=f"'{ticker}' is not a valid ticker symbol.")
        res = request.app.state.jobs.trigger(
            f"enrich:{value}", user.email, access.portfolio_id
        )
        return (
            back("/", access, msg=res.message)
            if res.accepted
            else back("/", access, err=res.message)
        )

    @app.post("/holding/{ticker}/weight")
    def set_weight(
        ticker: str,
        weight: float = Form(...),
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        ticker = ticker.upper()
        if weight < 0 or weight > 100:
            return back(
                f"/holding/{ticker}", access, err="Weight must be 0–100%."
            )
        ok = watchlist.set_weight(conn, ticker, weight, access.portfolio_id)
        return back(
            f"/holding/{ticker}",
            access,
            msg=f"{ticker} weight set to {weight:g}%." if ok else None,
            err=None if ok else f"{ticker} not found in this portfolio.",
        )

    @app.post("/holding/{ticker}/deactivate")
    def deactivate(
        ticker: str,
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        ticker = ticker.upper()
        ok = watchlist.deactivate(conn, ticker, access.portfolio_id)
        return back(
            "/",
            access,
            msg=f"Deactivated {ticker}." if ok else None,
            err=None if ok else f"{ticker} was not active in this portfolio.",
        )

    @app.post("/holding/{ticker}/activate")
    def activate(
        ticker: str,
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        ticker = ticker.upper()
        ok = watchlist.activate(conn, ticker, access.portfolio_id)
        return back(
            "/",
            access,
            msg=f"Reactivated {ticker}." if ok else None,
            err=None if ok else f"{ticker} not found in this portfolio.",
        )

    @app.post("/jobs/{job_key:path}")
    def run_job(
        request: Request,
        job_key: str,
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_admin(user)
        require_writable(access)
        if job_key not in _VALID_JOBS:
            return back("/", access, err=f"Unknown job '{job_key}'.")
        res = request.app.state.jobs.trigger(job_key, user.email)
        return (
            back("/", access, msg=res.message)
            if res.accepted
            else back("/", access, err=res.message)
        )

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar(
        request: Request,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        show_passed = request.query_params.get("passed") == "1"
        return render(
            request,
            "calendar.html",
            user,
            access,
            agenda=queries.calendar_agenda(
                conn, access.portfolio_id, include_passed=show_passed
            ),
            holdings=[
                h for h in queries.portfolio(conn, access.portfolio_id) if h["active"]
            ],
            show_passed=show_passed,
        )

    @app.post("/calendar/add")
    def calendar_add(
        title: str = Form(...),
        event_date: str = Form(...),
        kind: str = Form("manual"),
        ticker: str = Form(""),
        conn=Depends(get_conn),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        kind = kind.strip().lower()
        title = title.strip()
        ticker = ticker.strip().upper() or None
        if kind not in _MANUAL_KINDS:
            return back("/calendar", access, err=f"'{kind}' is not a kind you can add by hand.")
        if not title:
            return back("/calendar", access, err="An event needs a title.")
        if not _valid_date(event_date):
            return back("/calendar", access, err="Date must be YYYY-MM-DD.")
        holding_row = (
            queries.holding_detail(conn, ticker, access.portfolio_id) if ticker else None
        )
        if ticker and holding_row is None:
            return back("/calendar", access, err=f"{ticker} is not in this portfolio.")
        if kind == "pdufa" and not (
            holding_row and is_bio(holding_row["holding"].industry)
        ):
            return back(
                "/calendar", access, err="PDUFA events are only for bio/pharma holdings."
            )
        calendar_store.upsert_event(
            conn,
            Event(
                kind=kind,
                ticker=ticker,
                portfolio_id=access.portfolio_id,
                title=title,
                event_date=event_date,
                status="scheduled",
                source="manual",
                confidence="high",
                dedup_key=calendar_store.manual_key(),
                meta={"added_by": user.email},
            ),
        )
        conn.commit()
        return back("/calendar", access, msg=f"Added “{title}”.")

    @app.post("/calendar/{event_id:int}/edit")
    def calendar_edit(
        event_id: int,
        title: str = Form(...),
        event_date: str = Form(...),
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        row = calendar_store.get_event(conn, event_id)
        if (
            row is None
            or row["source"] != "manual"
            or row["portfolio_id"] != access.portfolio_id
        ):
            return back("/calendar", access, err="That event is not editable.")
        if not title.strip() or not _valid_date(event_date):
            return back(
                "/calendar", access, err="An event needs a title and a valid date."
            )
        calendar_store.apply_manual_edit(
            conn, event_id, title=title.strip(), event_date=event_date
        )
        conn.commit()
        return back("/calendar", access, msg="Event updated.")

    @app.post("/calendar/{event_id:int}/delete")
    def calendar_delete(
        event_id: int,
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        require_writable(access)
        row = calendar_store.get_event(conn, event_id)
        if (
            row is None
            or row["source"] != "manual"
            or row["portfolio_id"] != access.portfolio_id
        ):
            return back("/calendar", access, err="That event cannot be deleted.")
        calendar_store.delete_event(conn, event_id)
        conn.commit()
        return back("/calendar", access, msg="Event deleted.")

    @app.get("/digests", response_class=HTMLResponse)
    def digest_list(
        request: Request,
        conn=Depends(get_conn),
        user: User = Depends(current_user),
        access: PortfolioAccess = Depends(selected_access),
    ):
        return render(
            request,
            "digests.html",
            user,
            access,
            digests=queries.digests(conn, access.portfolio_id),
            show_ops=user.is_admin and not access.read_only,
        )

    @app.get("/digests/{date}.pdf")
    def digest_pdf(
        date: str,
        conn=Depends(get_conn),
        access: PortfolioAccess = Depends(selected_access),
    ):
        row = conn.execute(
            "SELECT pdf_path FROM digests WHERE portfolio_id = ? AND date = ?",
            (access.portfolio_id, date),
        ).fetchone()
        if not row or not Path(row["pdf_path"]).exists():
            return back("/digests", access, err=f"No digest PDF for {date}.")
        return FileResponse(
            row["pdf_path"],
            media_type="application/pdf",
            filename=f"avalpha-{date}.pdf",
        )

    @app.get("/me")
    def me(user: User = Depends(current_user)):
        return {
            "id": user.id,
            "email": user.email,
            "is_admin": user.is_admin,
            "portfolio_id": user.portfolio_id,
        }

    return app


def _money(value) -> str:
    if value is None:
        return "—"
    if value >= 1e12:
        return f"${value / 1e12:.2f}T"
    if value >= 1e9:
        return f"${value / 1e9:.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    return f"${value:,.2f}"


def _pct(value) -> str:
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def _shortdt(value) -> str:
    if not value:
        return "—"
    return value.replace("T", " ").rstrip("Z")[5:16]


_app = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
