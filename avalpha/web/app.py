"""FastAPI app for the ops console.

Server-rendered (Jinja2 + a little HTMX), one SQLite connection per request.
Auth is a dependency that verifies the Cloudflare Access identity; everything
except /healthz requires a member. Portfolio edits and job triggers redirect
back with a ``?msg=`` flash so we need no session store.
"""

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from avalpha import db, watchlist
from avalpha.config import Config, load_config
from avalpha.web import queries
from avalpha.web.auth import AccessDenied, Authenticator, extract_token, load_access_config
from avalpha.web.jobs import JobRunner

_HERE = Path(__file__).resolve().parent
_VALID_JOBS = {"matcher", "scorer", "digest"} | {
    f"collector:{s}" for s in queries.SOURCES
}


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

    # -- request-scoped helpers --------------------------------------------

    def get_conn(request: Request):
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def member(request: Request) -> str:
        token = extract_token(request.headers, request.cookies)
        return request.app.state.auth.member_email(token)

    def render(request: Request, name: str, member_email: str, **ctx) -> HTMLResponse:
        base = {
            "request": request,
            "fund_name": config.web_fund_name,
            "member": member_email,
            "path": request.url.path,
            "msg": request.query_params.get("msg"),
            "err": request.query_params.get("err"),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base)

    def back(url: str, msg: str | None = None, err: str | None = None):
        params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
        if params:
            url = f"{url}?{urlencode(params)}"
        return RedirectResponse(url, status_code=303)

    # -- Access-denied handling --------------------------------------------

    @app.exception_handler(AccessDenied)
    async def _denied(request: Request, exc: AccessDenied):
        wants_html = "text/html" in request.headers.get("accept", "")
        if wants_html:
            return templates.TemplateResponse(
                request,
                "403.html",
                {"fund_name": config.web_fund_name, "reason": exc.reason},
                status_code=403,
            )
        return JSONResponse({"error": "forbidden", "detail": exc.reason}, status_code=403)

    # -- routes -------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn=Depends(get_conn), m: str = Depends(member)):
        holdings = queries.portfolio(conn)
        return render(
            request,
            "dashboard.html",
            m,
            holdings=holdings,
            total_weight=queries.total_weight(holdings),
            health=queries.health(conn),
            scores=queries.recent_scores(conn, limit=25),
            jobs=queries.recent_jobs(conn),
            sources=queries.SOURCES,
        )

    @app.get("/holding/{ticker}", response_class=HTMLResponse)
    def holding(request: Request, ticker: str, conn=Depends(get_conn), m: str = Depends(member)):
        detail = queries.holding_detail(conn, ticker.upper())
        if detail is None:
            return back("/", err=f"{ticker.upper()} is not on the watchlist.")
        return render(request, "holding.html", m, d=detail)

    @app.post("/holding/add")
    def add_holding(request: Request, ticker: str = Form(...), m: str = Depends(member)):
        t = ticker.strip().upper()
        if not t.isalpha() or len(t) > 6:
            return back("/", err=f"'{ticker}' is not a valid ticker symbol.")
        res = request.app.state.jobs.trigger(f"enrich:{t}", m)
        return back("/", msg=res.message) if res.accepted else back("/", err=res.message)

    @app.post("/holding/{ticker}/weight")
    def set_weight(request: Request, ticker: str, weight: float = Form(...),
                   conn=Depends(get_conn), m: str = Depends(member)):
        if weight < 0 or weight > 100:
            return back(f"/holding/{ticker.upper()}", err="Weight must be 0–100%.")
        ok = watchlist.set_weight(conn, ticker.upper(), weight)
        msg = f"{ticker.upper()} weight set to {weight:g}%." if ok else None
        err = None if ok else f"{ticker.upper()} not found."
        return back(f"/holding/{ticker.upper()}", msg=msg, err=err)

    @app.post("/holding/{ticker}/deactivate")
    def deactivate(request: Request, ticker: str, conn=Depends(get_conn), m: str = Depends(member)):
        ok = watchlist.deactivate(conn, ticker.upper())
        return back("/", msg=f"Deactivated {ticker.upper()}." if ok else None,
                    err=None if ok else f"{ticker.upper()} was not active.")

    @app.post("/holding/{ticker}/activate")
    def activate(request: Request, ticker: str, conn=Depends(get_conn), m: str = Depends(member)):
        # Reactivate without re-enrichment: flip active back on, keep metadata.
        cur = conn.execute(
            "UPDATE watchlist SET active = 1, deactivated_at = NULL WHERE ticker = ?",
            (ticker.upper(),),
        )
        conn.commit()
        ok = cur.rowcount > 0
        return back("/", msg=f"Reactivated {ticker.upper()}." if ok else None,
                    err=None if ok else f"{ticker.upper()} not found.")

    @app.post("/jobs/{job_key:path}")
    def run_job(request: Request, job_key: str, m: str = Depends(member)):
        if job_key not in _VALID_JOBS:
            return back("/", err=f"Unknown job '{job_key}'.")
        res = request.app.state.jobs.trigger(job_key, m)
        return back("/", msg=res.message) if res.accepted else back("/", err=res.message)

    @app.get("/digests", response_class=HTMLResponse)
    def digest_list(request: Request, conn=Depends(get_conn), m: str = Depends(member)):
        return render(request, "digests.html", m, digests=queries.digests(conn))

    @app.get("/digests/{date}.pdf")
    def digest_pdf(request: Request, date: str, conn=Depends(get_conn), m: str = Depends(member)):
        row = conn.execute("SELECT pdf_path FROM digests WHERE date = ?", (date,)).fetchone()
        if not row or not Path(row["pdf_path"]).exists():
            return back("/digests", err=f"No digest PDF for {date}.")
        return FileResponse(row["pdf_path"], media_type="application/pdf",
                            filename=f"avalpha-{date}.pdf")

    @app.get("/me")
    def me(m: str = Depends(member)):
        return {"email": m}

    return app


# -- template filters -------------------------------------------------------

def _money(v) -> str:
    if v is None:
        return "—"
    if v >= 1e12:
        return f"${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.2f}"


def _pct(v) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _shortdt(v) -> str:
    if not v:
        return "—"
    # ISO "YYYY-MM-DDTHH:MM:SSZ" -> "MM-DD HH:MM"
    return v.replace("T", " ").rstrip("Z")[5:16]


# Lazily build the module-level `app` for `uvicorn avalpha.web.app:app`, so that
# importing this module (e.g. in tests, which build their own app) doesn't
# require a config.toml to be present.
_app = None


def __getattr__(name: str):
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
