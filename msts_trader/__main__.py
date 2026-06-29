"""msts-trader CLI: paste a CSV, preview the rebalance, execute it on your broker.

Subcommands:
  login [--broker NAME]      — store creds in OS keychain (per-broker)
  status [--broker NAME]     — show NAV / positions / market status
  rebalance [--broker NAME]  — (default) paste CSV, preview, prompt, execute
  liquidate [--broker NAME]  — flatten the account to cash via the limit-chase
  logout [--broker NAME]     — clear stored creds for one broker
  brokers                    — list supported and currently-configured brokers
  paper-reset                — reset the paper-broker book
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm  # used for the post-preview Y/N (works fine in all terminals)
from rich.table import Table

from . import __version__, config, fill_log, keychain, notifications, retry, runstate, safety
from .brokers import SUPPORTED, BrokerError, make
from .creds_file import CredsFileError, broker_kwargs_from_env, load_into_env
from .csv_parser import CSVParseError, parse_csv
from .diff import DRIFT_THRESHOLD, apply_margin_aware, build_preview
from .login_errors import explain_login_error
from .market_hours import market_status
from .models import Side
from .prompts import ask_secret, ask_text, ask_yes_no, env_value
from .verify import check_convergence, converged_within_buying_power


def _harden_console_encoding() -> None:
    """Keep legacy Windows code pages from killing the CLI.

    Help text and previews use arrows / check marks / ellipses that
    cp1252 / cp437 consoles can't encode, and click writes them straight
    to stdout — a plain `msts-trader --help` would die with
    UnicodeEncodeError. Degrade unencodable characters to '?' instead.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (stream.encoding or "").lower().replace("-", "")
            if enc and enc != "utf8":
                stream.reconfigure(errors="replace")
        except Exception:
            pass  # non-reconfigurable stream (pytest capture, pipes) — already safe


_harden_console_encoding()

c = Console()

# Set per-invocation by the rebalance command.
_QUIET = False
_JSON = False


def say(msg: str = "", *, style: str | None = None, end: str = "\n") -> None:
    """Print unless we're in --quiet or --json mode."""
    if _QUIET or _JSON:
        return
    c.print(msg, style=style, end=end)


def _fail(msg: str, code: int = 1):
    """Emit an error (JSON-aware) and exit.

    The message is rich-escaped so literal brackets (e.g. "[[account]]",
    broker error payloads) render verbatim instead of being eaten as markup.
    """
    if _JSON:
        print(json.dumps({"error": msg}))
    else:
        c.print(f"[red]✗ {escape(msg)}[/red]")
    sys.exit(code)


def _load_config_or_exit(path: str | None) -> dict:
    try:
        return config.load(path)
    except config.ConfigError as e:
        _fail(str(e))


def _do_notify(text, *, notify_url, tg_token, tg_chat) -> None:
    """Send a notification and surface the outcome (best-effort, never raises).

    A configured channel that fails to deliver is reported as a warning so a
    dead webhook (or an n8n test URL that wasn't actively listening) can't fail
    silently — the previous behaviour left users guessing why nothing arrived.
    """
    sent, failed = notifications.notify(text, notify_url=notify_url, telegram_token=tg_token, telegram_chat_id=tg_chat)
    if sent:
        say(f"[dim]notified: {', '.join(sent)}[/dim]")
    if failed:
        say(f"[yellow]notify failed (check URL/token, see channel): {', '.join(failed)}[/yellow]")


def _emit_json(broker, preview, *, dry_run: bool, duplicate: bool) -> None:
    gross = sum((row.target_pct for row in preview.rows), Decimal(0))
    payload = {
        "broker": broker.name,
        "account_id": broker.account_id,
        "nav": str(preview.nav),
        "cash": str(preview.cash),
        "buying_power": str(preview.buying_power),
        "gross_exposure": str(gross),
        "dry_run": dry_run,
        "duplicate_today": duplicate,
        "warnings": preview.warnings,
        "blockers": preview.blockers,
        "orders": [
            {
                "ticker": o.ticker,
                "side": o.side.value,
                "quantity": str(o.quantity),
                "estimated_price": str(o.estimated_price) if o.estimated_price else None,
                "notional": str(o.notional),
            }
            for o in preview.orders
        ],
    }
    print(json.dumps(payload, default=str))


_BROKER_OPT = click.option(
    "--broker",
    "broker_opt",
    default=None,
    help=f"Broker name. Supported: {', '.join(SUPPORTED)}",
)


def _prompt_choice(prompt: str, choices: list[str], default: str) -> str:
    while True:
        val = ask_text(prompt, default=default, allow_blank=False).lower().strip()
        if val in choices:
            return val
        c.print(f"[red]invalid choice. options: {', '.join(choices)}[/red]")


def _resolve_broker_name(ctx: click.Context, explicit: str | None) -> str:
    """Resolve broker from (subcommand --broker) > (group --broker) > stored default."""
    chosen = explicit or (ctx.obj or {}).get("broker")
    if chosen:
        return chosen.lower().strip()
    stored = keychain.get_default()
    if not stored:
        c.print(
            "[red]no broker selected and no default stored — pass --broker NAME "
            "or run `msts-trader login --broker NAME` first[/red]"
        )
        sys.exit(1)
    return stored


def _load_creds_file_or_exit(path: str) -> None:
    try:
        # An explicitly-passed --creds-file is a deliberate choice; it must win
        # over any stale value already exported in the shell (e.g. a revoked
        # TT_REFRESH_TOKEN), which would otherwise silently shadow the file.
        keys = load_into_env(path, overwrite=True)
    except CredsFileError as e:
        c.print(f"[red]✗ could not read creds file:[/red] {escape(str(e))}")
        sys.exit(1)
    # Show the key NAMES (never values) so a duplicate/misspelled key in the
    # file is visible immediately instead of silently collapsing the count.
    names = ", ".join(sorted(keys))
    c.print(f"[green]✓ loaded {len(keys)} value(s) from {path}[/green] [dim]({escape(names)})[/dim]")


def _stored_creds_or_empty(broker: str) -> dict:
    try:
        return keychain.load(broker)
    except keychain.CredsMissingError:
        return {}


def _fetch_url_or_exit(url: str) -> str:
    """Fetch CSV text from a URL using the stdlib (no extra dependency)."""
    import urllib.request

    if not url.lower().startswith(("http://", "https://")):
        c.print(f"[red]✗ --csv-url must be http(s): {url}[/red]")
        sys.exit(1)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:  # noqa: S310 (scheme checked above)
            # Cap at 5 MB — a weights CSV is tiny; this guards against a
            # misconfigured URL streaming something huge.
            data = resp.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024:
            c.print(f"[red]✗ {url} returned more than 5 MB — refusing (is this really a CSV?).[/red]")
            sys.exit(1)
        return data.decode("utf-8")
    except SystemExit:
        raise
    except Exception as e:
        c.print(f"[red]✗ could not fetch {url}:[/red] {e}")
        sys.exit(1)


def _apply_margin_aware(broker, preview, buying_power, max_passes: int = 3, whole_shares: bool = False) -> None:
    """Scale buys to fit buying power, using the broker's REAL margin when it
    exposes it (Tastytrade / IBKR / Tradier), else the notional approximation.

    With real margin, re-confirm: after scaling, re-query the broker on the
    now-smaller book and scale again if non-linear margin tiers still push it
    over. The notional path is linear, so it's exact in a single pass. Emits
    one cumulative message regardless of how many passes ran.
    """
    # Cheap pre-check so default-on margin-aware costs nothing in the common
    # case: buying power consumed by a long buy is at most its notional, so if
    # the notional already fits the available BP, no scaling can be needed —
    # skip the (per-order) broker margin queries entirely.
    buys0 = [o for o in preview.orders if o.side == Side.BUY]
    gross0 = sum((o.notional for o in buys0), Decimal(0))
    sells0 = sum((o.notional for o in preview.orders if o.side == Side.SELL), Decimal(0))
    # Fits within full buying power -> nothing to scale, no broker queries.
    # (Cushion is applied only when actually scaling an over-BP book.)
    if gross0 <= buying_power + sells0:
        preview.warnings = [w for w in preview.warnings if "re-run with --margin-aware" not in w]
        return

    mr = getattr(broker, "margin_requirement", None)
    has_real = callable(mr)
    cumulative = Decimal(1)
    passes = 0
    used_real = False

    for _ in range(max_passes):
        buys = [o for o in preview.orders if o.side == Side.BUY]
        if not buys:
            break
        real = None
        if has_real:
            try:
                real = mr(buys)
            except Exception:
                real = None
        used_real = used_real or real is not None
        scale = apply_margin_aware(
            preview, buying_power=buying_power, real_margin=real, add_warning=False, whole_shares=whole_shares
        )
        passes += 1
        cumulative *= scale
        # Notional (real is None) is exact in one pass; stop once nothing scales.
        if scale >= Decimal(1) or real is None:
            break

    # One summary message for the whole operation.
    buys = [o for o in preview.orders if o.side == Side.BUY]
    gross = sum((o.notional for o in buys), Decimal(0))
    sells = sum((o.notional for o in preview.orders if o.side == Side.SELL), Decimal(0))
    available = buying_power + sells
    src = "real broker margin" if used_real else "estimated"
    if cumulative < Decimal(1):
        passes_note = f" over {passes} passes" if passes > 1 else ""
        preview.warnings.append(
            f"Margin-aware ({src}): scaled all buys by {cumulative:.1%} to fit "
            f"${available:,.0f} buying power (weight-preserving){passes_note}."
        )
    elif gross > buying_power:
        preview.warnings.append(
            f"Margin-aware ({src}): buys fit ${available:,.0f} buying power "
            f"(incl. ${sells:,.0f} sell proceeds) — no scaling needed."
        )


def _load_broker(name: str):
    """Build a broker from env/creds-file first (headless), else the keychain.

    Env-derived creds (set directly or loaded via --creds-file) take
    precedence so a cron job / GitHub Action never needs an interactive
    `login`. Falls back to the OS keychain for the manual workflow.
    """
    creds = broker_kwargs_from_env(name)
    if creds is None:
        try:
            creds = keychain.load(name)
        except keychain.CredsMissingError:
            c.print(
                f"[red]✗ no credentials for {name!r}.[/red] Either run "
                f"[bold]msts-trader login --broker {name}[/bold] (manual), or pass "
                f"[bold]--creds-file[/bold] / set the env vars (headless)."
            )
            sys.exit(1)
    try:
        return make(name, **creds)
    except BrokerError as e:
        c.print(f"[red]✗ broker init failed:[/red] {escape(str(e))}")
        sys.exit(1)


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="msts-trader")
@click.option("--broker", default=None, help=f"Broker name. Supported: {', '.join(SUPPORTED)}")
@click.pass_context
def main(ctx: click.Context, broker: str | None) -> None:
    """Paste a target-weights CSV, preview the rebalance, execute it on your broker.

    Run with no command to rebalance the default broker (paste a
    ticker,weight CSV, review, confirm). Credentials live only in your OS
    keychain — set one up with `msts-trader login`. Supports Tastytrade,
    Alpaca, Tradier, IBKR, Schwab, Hyperliquid, and a local paper simulator.
    """
    ctx.ensure_object(dict)
    ctx.obj["broker"] = broker
    if ctx.invoked_subcommand is None:
        ctx.invoke(rebalance)


@main.command()
@_BROKER_OPT
@click.option(
    "--creds-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Load credentials from a JSON or KEY=VALUE file instead of typing them.",
)
@click.option(
    "--reauth",
    is_flag=True,
    help="Force a fresh OAuth flow even if a cached token exists. Schwab: re-runs "
    "the browser authorization so the 7-day refresh token restarts — run this on "
    "a weekend to guarantee auth through the trading week.",
)
@click.pass_context
def login(ctx: click.Context, broker_opt: str | None, creds_file: str | None, reauth: bool) -> None:
    """Store broker creds in OS keychain."""
    if creds_file:
        _load_creds_file_or_exit(creds_file)

    broker = (
        broker_opt
        or ctx.obj.get("broker")
        or _prompt_choice(f"broker [{'|'.join(SUPPORTED)}]", choices=list(SUPPORTED), default="tastytrade")
    )
    flow = _LOGIN_FLOWS.get(broker)
    if flow is None:
        c.print(f"[red]unknown broker {broker!r}[/red]")
        sys.exit(1)
    if reauth and broker == "schwab":
        # Schwab reuses the cached OAuth token when present; deleting it is
        # what forces the full browser flow (and a fresh 7-day refresh token).
        from .brokers.schwab import clear_token, has_token, token_location

        if has_token():
            location = token_location()
            clear_token()
            c.print(f"[yellow]cleared cached Schwab token ({location}) - the browser flow will run fresh.[/yellow]")
        else:
            c.print("[dim]no cached Schwab token — the browser flow runs anyway.[/dim]")
    flow()


def _login_tastytrade() -> None:
    c.print(
        Panel.fit(
            "[bold]Tastytrade OAuth setup[/bold]\n\n"
            "1. Sign in at [cyan]https://developer.tastytrade.com[/cyan]\n"
            "2. Create an OAuth application → copy [bold]provider secret[/bold]\n"
            "3. Run their authorization flow → copy [bold]refresh token[/bold]\n"
            "4. Find your [bold]account number[/bold] in Tastytrade dashboard "
            "(or leave blank to auto-pick first account)\n\n"
            "[dim]Avoid typing: put TT_PROVIDER_SECRET / TT_REFRESH_TOKEN / "
            "TT_ACCOUNT_ID in a file and run with --creds-file, or export them "
            "as environment variables first. Using certification (sandbox) "
            "keys? Add TT_TEST=1 — cert keys are rejected by production.[/dim]",
            border_style="cyan",
        )
    )
    provider_secret = ask_secret("provider secret", env_var="TT_PROVIDER_SECRET")
    refresh_token = ask_secret("refresh token", env_var="TT_REFRESH_TOKEN")
    account_id = env_value("TT_ACCOUNT_ID") or ask_text("account id (optional)", default="", allow_blank=True)
    account_id = account_id.strip() or None
    raw_test = env_value("TT_TEST")
    is_test = raw_test is not None and raw_test.lower() in {"1", "true", "yes", "test", "sandbox", "cert"}

    try:
        b = make(
            "tastytrade",
            provider_secret=provider_secret,
            refresh_token=refresh_token,
            account_id=account_id,
            is_test=is_test,
        )
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('tastytrade', e))}[/red]")
        if not is_test:
            c.print("[dim]Using certification (sandbox) keys? Add TT_TEST=1 to your creds file / env.[/dim]")
        sys.exit(1)

    keychain.save(
        "tastytrade",
        {
            "provider_secret": provider_secret,
            "refresh_token": refresh_token,
            "account_id": account_id or b.account_id,
            "is_test": is_test,
        },
    )
    keychain.set_default("tastytrade")
    env_label = " (test/cert)" if is_test else ""
    c.print(f"[green]✓ stored.[/green] tastytrade{env_label} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


def _login_alpaca() -> None:
    c.print(
        Panel.fit(
            "[bold]Alpaca API key setup[/bold]\n\n"
            "1. Sign in at [cyan]https://alpaca.markets[/cyan] (or paper dashboard)\n"
            "2. Generate an API key pair under your account settings\n"
            "3. Paste the key id and secret below\n"
            "4. Choose paper or live mode\n\n"
            "[dim]Avoid typing: put APCA_API_KEY_ID / APCA_API_SECRET_KEY / "
            "APCA_PAPER in a file and run with --creds-file, or export them "
            "as environment variables first.[/dim]",
            border_style="cyan",
        )
    )
    api_key = ask_secret("api key id", env_var="APCA_API_KEY_ID")
    secret_key = ask_secret("secret key", env_var="APCA_API_SECRET_KEY")
    env_paper = env_value("APCA_PAPER")
    if env_paper is not None:
        paper = env_paper.lower() in {"1", "true", "yes", "paper"}
    else:
        paper = ask_yes_no("paper account?", default=True)

    try:
        b = make("alpaca", api_key=api_key, secret_key=secret_key, paper=paper)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('alpaca', e))}[/red]")
        sys.exit(1)

    keychain.save("alpaca", {"api_key": api_key, "secret_key": secret_key, "paper": paper})
    keychain.set_default("alpaca")
    c.print(
        f"[green]✓ stored.[/green] alpaca {'(paper)' if paper else '(live)'} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}"
    )


def _login_tradier() -> None:
    c.print(
        Panel.fit(
            "[bold]Tradier setup[/bold]\n\n"
            "1. Get an access token at [cyan]https://developer.tradier.com[/cyan]\n"
            "   (a free [bold]sandbox[/bold] token is great for testing)\n"
            "2. Your [bold]account number[/bold] is optional — auto-discovered\n"
            "3. Choose sandbox or production\n\n"
            "[dim]Headless: TRADIER_ACCESS_TOKEN / TRADIER_ACCOUNT_ID / "
            "TRADIER_SANDBOX via --creds-file or env.[/dim]",
            border_style="cyan",
        )
    )
    access_token = ask_secret("access token", env_var="TRADIER_ACCESS_TOKEN")
    account_id = (
        env_value("TRADIER_ACCOUNT_ID") or ask_text("account number (optional)", default="", allow_blank=True)
    ).strip() or None
    raw = env_value("TRADIER_SANDBOX")
    sandbox = (
        raw.lower() in {"1", "true", "yes", "sandbox"} if raw is not None else ask_yes_no("sandbox?", default=True)
    )

    try:
        b = make("tradier", access_token=access_token, account_id=account_id, sandbox=sandbox)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('tradier', e))}[/red]")
        sys.exit(1)

    keychain.save(
        "tradier", {"access_token": access_token, "account_id": account_id or b.account_id, "sandbox": sandbox}
    )
    keychain.set_default("tradier")
    c.print(
        f"[green]✓ stored.[/green] tradier {'(sandbox)' if sandbox else '(production)'} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}"
    )


def _login_ibkr() -> None:
    c.print(
        Panel.fit(
            "[bold]IBKR setup[/bold]\n\n"
            "1. Start [cyan]TWS[/cyan] or [cyan]IB Gateway[/cyan] (live or paper) — \n"
            "   Configure → API → enable [bold]ActiveX and Socket Clients[/bold]\n"
            "2. Note the API socket port:\n"
            "     TWS live 7496 · TWS paper 7497\n"
            "     Gateway live 4001 · Gateway paper 4002\n"
            "3. Confirm host (default 127.0.0.1; remote/Docker = its IP)",
            border_style="cyan",
        )
    )
    host = env_value("IBKR_HOST") or ask_text("host", default="127.0.0.1")
    port = int(env_value("IBKR_PORT") or ask_text("port", default="4002"))
    client_id = int(env_value("IBKR_CLIENT_ID") or ask_text("client id (any free int)", default="17"))
    account_id = (
        env_value("IBKR_ACCOUNT_ID") or ask_text("account id (optional)", default="", allow_blank=True)
    ).strip() or None

    try:
        b = make("ibkr", host=host, port=port, client_id=client_id, account_id=account_id)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('ibkr', e))}[/red]")
        sys.exit(1)

    keychain.save(
        "ibkr", {"host": host, "port": port, "client_id": client_id, "account_id": account_id or b.account_id}
    )
    keychain.set_default("ibkr")
    c.print(f"[green]✓ stored.[/green] ibkr account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


def _login_schwab() -> None:
    c.print(
        Panel.fit(
            "[bold]Schwab OAuth2 setup[/bold]\n\n"
            "1. Register a developer app at [cyan]https://developer.schwab.com[/cyan]\n"
            "2. Set the callback URL to [bold]https://127.0.0.1:8182[/bold]\n"
            "   [yellow]The callback url below must match the registered one\n"
            "   EXACTLY — character for character, trailing slash included.[/yellow]\n"
            "3. Copy your [bold]app key[/bold] and [bold]app secret[/bold]\n"
            "4. A browser will open for authorization. Refresh token lasts\n"
            "   [bold]7 days[/bold] — re-run this login when it expires.\n\n"
            "[dim]Tip: run `msts-trader login --broker schwab --reauth` on a\n"
            "weekend to restart the 7-day clock before the trading week.[/dim]",
            border_style="cyan",
        )
    )
    stored = _stored_creds_or_empty("schwab")
    env_app_key = env_value("SCHWAB_APP_KEY")
    env_app_secret = env_value("SCHWAB_APP_SECRET")
    app_key = ask_secret("app key", env_var="SCHWAB_APP_KEY") if env_app_key else stored.get("app_key")
    app_secret = ask_secret("app secret", env_var="SCHWAB_APP_SECRET") if env_app_secret else stored.get("app_secret")
    callback_url = env_value("SCHWAB_CALLBACK_URL") or stored.get("callback_url")
    account_hash = env_value("SCHWAB_ACCOUNT_HASH") or stored.get("account_hash")
    if (app_key and app_secret) and (not env_app_key or not env_app_secret):
        c.print("[dim]using stored Schwab app credentials from the OS keychain.[/dim]")
    if not app_key:
        app_key = ask_secret("app key", env_var="SCHWAB_APP_KEY")
    if not app_secret:
        app_secret = ask_secret("app secret", env_var="SCHWAB_APP_SECRET")
    callback_url = callback_url or ask_text(
        "callback url (must EXACTLY match your app's registered callback)",
        default="https://127.0.0.1:8182",
    )

    try:
        b = make(
            "schwab",
            app_key=app_key,
            app_secret=app_secret,
            callback_url=callback_url,
            account_hash=account_hash,
        )
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('schwab', e))}[/red]")
        sys.exit(1)

    keychain.save(
        "schwab",
        {"app_key": app_key, "app_secret": app_secret, "callback_url": callback_url, "account_hash": b.account_hash},
    )
    keychain.set_default("schwab")
    c.print(f"[green]✓ stored.[/green] schwab account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


def _login_hyperliquid() -> None:
    c.print(
        Panel.fit(
            "[bold]Hyperliquid setup (experimental — crypto perps)[/bold]\n\n"
            "1. Create an API wallet at [cyan]https://app.hyperliquid.xyz/API[/cyan]\n"
            "2. Copy its [bold]private key[/bold] (hex)\n"
            "3. Optionally provide your main [bold]account address[/bold]\n"
            "   (defaults to the API wallet's address)\n\n"
            "[yellow]Test on testnet first (answer yes below) with tiny size.[/yellow]",
            border_style="cyan",
        )
    )
    private_key = ask_secret("private key", env_var="HL_PRIVATE_KEY")
    account_address = (
        env_value("HL_ACCOUNT_ADDRESS") or ask_text("account address (optional)", default="", allow_blank=True)
    ).strip() or None
    raw = env_value("HL_TESTNET")
    testnet = raw.lower() in {"1", "true", "yes"} if raw is not None else ask_yes_no("use testnet?", default=True)

    try:
        b = make("hyperliquid", private_key=private_key, account_address=account_address, testnet=testnet)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {escape(explain_login_error('hyperliquid', e))}[/red]")
        sys.exit(1)

    keychain.save("hyperliquid", {"private_key": private_key, "account_address": account_address, "testnet": testnet})
    keychain.set_default("hyperliquid")
    c.print(
        f"[green]✓ stored.[/green] hyperliquid {'(testnet)' if testnet else '(mainnet)'} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}"
    )


def _login_paper() -> None:
    starting = env_value("PAPER_STARTING_CASH") or ask_text("starting cash", default="100000")
    keychain.save("paper", {"starting_cash": starting})
    keychain.set_default("paper")
    from .brokers.paper import STATE_PATH

    had_state = STATE_PATH.exists()
    b = make("paper", starting_cash=Decimal(starting))
    bal = b.balances()
    if had_state:
        c.print(
            f"[green]✓ paper creds stored.[/green] (existing book at {STATE_PATH} — starting cash only seeds new books. Use `paper-reset` to apply ${starting}.)"
        )
    else:
        c.print(f"[green]✓ paper book ready.[/green] cash ${bal.cash:,.2f}")


# Login dispatch — every broker in SUPPORTED must have an entry here.
# test_login_flows_cover_all_brokers pins this so a new broker can never
# ship wired into the factory but missing a login flow (the bug that hit
# Hyperliquid in 0.3.x).
_LOGIN_FLOWS = {
    "tastytrade": _login_tastytrade,
    "alpaca": _login_alpaca,
    "tradier": _login_tradier,
    "ibkr": _login_ibkr,
    "schwab": _login_schwab,
    "hyperliquid": _login_hyperliquid,
    "paper": _login_paper,
}


@main.command()
@_BROKER_OPT
@click.pass_context
def logout(ctx: click.Context, broker_opt: str | None) -> None:
    """Clear stored creds for a broker."""
    broker = broker_opt or ctx.obj.get("broker")
    if not broker:
        broker = _prompt_choice("broker to forget", choices=list(SUPPORTED), default=SUPPORTED[0])
    keychain.clear(broker)
    cleared_extra = ""
    if broker == "schwab":
        from .brokers.schwab import clear_token

        clear_token()
        cleared_extra = " and cached token"
    if keychain.get_default() == broker:
        keychain.clear_default()
    c.print(f"[green]✓ creds{cleared_extra} cleared for {broker}.[/green]")


@main.command()
def brokers() -> None:
    """Show supported brokers and which ones currently have stored creds."""
    configured = set(keychain.list_brokers())
    default = keychain.get_default()
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Broker")
    table.add_column("Configured")
    table.add_column("Default")
    for name in SUPPORTED:
        ok = "[green]✓[/green]" if name in configured else "[dim]—[/dim]"
        d = "[cyan]★[/cyan]" if name == default else ""
        table.add_row(name, ok, d)
    c.print(table)


def _resolve_paper_starting_cash() -> Decimal:
    """Starting cash for paper-reset: env/creds-file, then keychain, else default."""
    from .brokers.paper import STARTING_CASH

    creds = broker_kwargs_from_env("paper")
    if creds and creds.get("starting_cash"):
        return Decimal(str(creds["starting_cash"]))
    try:
        stored = keychain.load("paper")
        if stored.get("starting_cash"):
            return Decimal(str(stored["starting_cash"]))
    except keychain.CredsMissingError:
        pass
    return STARTING_CASH


@main.command(name="paper-reset")
def paper_reset() -> None:
    """Reset the paper broker book to its starting cash."""
    from .brokers.paper import Paper

    Paper().reset(starting_cash=_resolve_paper_starting_cash())
    c.print("[green]✓ paper book reset.[/green]")


@main.command()
@_BROKER_OPT
@click.option(
    "--creds-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Load credentials from a JSON or KEY=VALUE file (headless).",
)
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON (NAV, balances, positions).")
@click.pass_context
def status(ctx: click.Context, broker_opt: str | None, creds_file: str | None, json_out: bool) -> None:
    """Show account NAV, positions, market status. No orders."""
    if creds_file:
        _load_creds_file_or_exit(creds_file)
    broker = _resolve_broker_name(ctx, broker_opt)
    b = _load_broker(broker)
    bal = b.balances()
    pos = b.positions()
    ms = market_status()

    if json_out:
        payload = {
            "broker": b.name,
            "account_id": b.account_id,
            "nav": str(bal.nav),
            "cash": str(bal.cash),
            "buying_power": str(bal.buying_power),
            "market": ms.status,
            "minutes_to_close": ms.minutes_to_close,
            "positions": [
                {
                    "ticker": p.ticker,
                    "quantity": str(p.quantity),
                    "price": str(p.price),
                    "market_value": str(p.market_value),
                    "pct_nav": str(p.market_value / bal.nav) if bal.nav else "0",
                }
                for p in sorted(pos.values(), key=lambda x: -x.market_value)
            ],
        }
        print(json.dumps(payload, default=str))
        return

    c.print(
        f"\n[bold]{b.name}[/bold]  ·  account [bold]{b.account_id}[/bold]  ·  "
        f"NAV [green]${bal.nav:,.2f}[/green]  ·  "
        f"cash ${bal.cash:,.2f}  ·  BP ${bal.buying_power:,.2f}"
    )
    c.print(
        f"Market: [bold]{ms.status}[/bold]"
        + (f"  ·  closes in {ms.minutes_to_close} min" if ms.minutes_to_close is not None else "")
    )

    if not pos:
        c.print("[yellow]No open positions.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Symbol")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("% NAV", justify="right")
    for p in sorted(pos.values(), key=lambda x: -x.market_value):
        pct = (p.market_value / bal.nav * 100) if bal.nav else Decimal(0)
        table.add_row(p.ticker, f"{p.quantity:.2f}", f"${p.price:,.2f}", f"${p.market_value:,.0f}", f"{pct:.1f}%")
    c.print(table)


@main.command()
@_BROKER_OPT
@click.option(
    "--creds-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Load credentials from a JSON or KEY=VALUE file (headless).",
)
@click.option("--dry-run", is_flag=True, help="Preview only — never sends orders.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirm prompt (auto-execute). Required for unattended runs.")
@click.option("--only", default=None, help="Comma-separated tickers: liquidate ONLY these.")
@click.option("--exclude", default=None, help="Comma-separated tickers to KEEP (do not liquidate).")
@click.option(
    "--aggression",
    type=float,
    default=0.0,
    help="Chase price vs the mid: 0 pegs the mid (default); NEGATIVE is more "
    "passive (rest above the mid on a sell — better price, slower fill); "
    "POSITIVE crosses toward the touch (faster, worse price).",
)
@click.option(
    "--retries", type=int, default=6, help="Limit-chase reprice attempts before the market mop-up. Default 6."
)
@click.option(
    "--interval", type=float, default=8.0, help="Seconds to rest each limit rung waiting for a fill. Default 8."
)
@click.option(
    "--pace", type=float, default=0.0, help="Seconds to wait between names — spread the flatten out. Default 0."
)
@click.option(
    "--no-fallback",
    is_flag=True,
    help="Do NOT market mop-up the unfilled/fractional remainder (may leave dust unsold).",
)
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON result.")
@click.pass_context
def liquidate(
    ctx: click.Context,
    broker_opt: str | None,
    creds_file: str | None,
    dry_run: bool,
    yes: bool,
    only: str | None,
    exclude: str | None,
    aggression: float,
    retries: int,
    interval: float,
    pace: float,
    no_fallback: bool,
    json_out: bool,
) -> None:
    """Flatten the account — sell every position to cash via the patient limit-chase.

    Longs are SOLD, shorts BOUGHT to cover, largest first. Each line is worked as
    a LIMIT pegged to the mid and chased, with a MARKET mop-up for the unfilled /
    fractional remainder. RTH only for live execution; --dry-run previews any
    time. Live execution needs confirmation (or --yes for unattended runs).
    """
    import time as _time

    from . import liquidate as _liq

    global _JSON
    if json_out:
        _JSON = True

    if creds_file:
        _load_creds_file_or_exit(creds_file)
    broker_name = _resolve_broker_name(ctx, broker_opt)
    broker = _load_broker(broker_name)

    try:
        positions = broker.positions()
    except Exception as e:
        _fail(f"could not fetch positions: {e}")

    only_list = [t.strip() for t in only.split(",") if t.strip()] if only else None
    excl_list = [t.strip() for t in exclude.split(",") if t.strip()] if exclude else None
    plan = _liq.build_plan(positions, only=only_list, exclude=excl_list)

    if not plan.orders:
        if json_out:
            print(
                json.dumps(
                    {
                        "broker": broker.name,
                        "account_id": broker.account_id,
                        "orders": [],
                        "note": "nothing to liquidate",
                    }
                )
            )
        else:
            c.print("[yellow]Nothing to liquidate — no matching open positions.[/yellow]")
        return

    ms = market_status()

    if not json_out:
        c.print(
            f"\n[bold]{broker.name}[/bold] · account [bold]{broker.account_id}[/bold] · market [bold]{ms.status}[/bold]"
        )
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", justify="right")
        table.add_column("Symbol")
        table.add_column("Side")
        table.add_column("Qty", justify="right")
        table.add_column("Est. value", justify="right")
        for i, o in enumerate(plan.orders, 1):
            qty_str = f"{o.quantity:.4f}".rstrip("0").rstrip(".")
            table.add_row(str(i), o.ticker, o.side.value, qty_str, f"${o.notional:,.0f}")
        c.print(table)
        c.print(f"[bold]{len(plan.orders)} positions[/bold] · gross ~[bold]${plan.gross:,.0f}[/bold] to liquidate")
        if plan.skipped:
            c.print(f"[dim]keeping: {', '.join(t for t, _ in plan.skipped)}[/dim]")

    if not dry_run:
        if ms.status != "open":
            _fail(
                f"market is {ms.status!r} — the liquidation chase + market mop-up are RTH-only. "
                f"Re-run during regular hours, or use --dry-run to preview."
            )
        if not yes:
            if not sys.stdin.isatty():
                _fail("refusing to liquidate without --yes in a non-interactive run.")
            if not Confirm.ask(
                f"Liquidate {len(plan.orders)} positions (~${plan.gross:,.0f}) on {broker.name} {broker.account_id}?",
                default=False,
            ):
                c.print("[yellow]Aborted — no orders sent.[/yellow]")
                return

    cfg = _liq.liquidation_config(
        retries=retries, interval=interval, aggression=aggression, fallback_to_market=not no_fallback
    )
    results = _liq.run_liquidation(broker, plan, cfg, dry_run=dry_run, pace=pace, log=say, sleep=_time.sleep)

    sent = sum(1 for r in results if _is_clean_send(r.get("status", "?")))
    failed = len(results) - sent

    remaining: dict = {}
    if not dry_run:
        try:
            remaining = {t: p for t, p in broker.positions().items() if p.quantity != 0}
        except Exception:
            remaining = {}

    if json_out:
        print(
            json.dumps(
                {
                    "broker": broker.name,
                    "account_id": broker.account_id,
                    "dry_run": dry_run,
                    "market": ms.status,
                    "planned": len(plan.orders),
                    "sent": sent,
                    "failed": failed,
                    "results": results,
                    "remaining_positions": [{"ticker": t, "quantity": str(p.quantity)} for t, p in remaining.items()],
                },
                default=str,
            )
        )
        return

    suffix = " (dry-run, no orders sent)" if dry_run else ""
    c.print(f"\n[bold]Done.[/bold] {broker.name}: {sent} ok, {failed} failed{suffix}")
    if not dry_run:
        if remaining:
            c.print(
                f"[yellow]Still holding {len(remaining)}: {', '.join(sorted(remaining))} — re-run to finish.[/yellow]"
            )
        else:
            c.print("[green]Account is flat.[/green]")


@main.command()
@_BROKER_OPT
@click.option("--dry-run", is_flag=True, help="Preview only — never sends orders.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirm prompt (auto-execute). Required for unattended runs.")
@click.option("--threshold", default=None, type=float, help="Drift threshold (fraction of NAV). Default 0.04.")
@click.option(
    "--min-weight",
    type=float,
    default=None,
    help="Ignore CSV rows with 0 < weight < this (e.g. 0.01): no buy, no sell, existing position left untouched.",
)
@click.option(
    "--stop-pct",
    "default_stop",
    type=float,
    default=None,
    help="Default protective stop as a fraction below entry (e.g. 0.015 = 1.5%%), applied to every bought/held target with no per-row stop_pct. Use when the weights feed omits stops (e.g. the hydra raw cache). Per-row stop_pct always wins.",
)
@click.option(
    "--threshold-mode",
    type=click.Choice(["nav", "position"]),
    default=None,
    help="Drift denominator: 'nav' (default, delta vs whole book) or 'position' "
    "(delta vs the line itself — for scaled/composite books whose small "
    "lines could never move 4%% of NAV).",
)
@click.option(
    "--rebalance-scope",
    type=click.Choice(["whole-book", "per-ticker"]),
    default=None,
    help="Execution scope. 'whole-book' (default): the threshold is a TRIGGER — if any "
    "line breaches it, snap the WHOLE book to target (higher CAGR, more turnover). "
    "'per-ticker': trade ONLY the breaching lines, leave the rest (lower turnover, "
    "better Sharpe/drawdown).",
)
@click.option(
    "--sweep/--no-sweep",
    default=None,
    help="Sweep (default): liquidate any held ticker NOT in the CSV — the CSV is the "
    "complete book. --no-sweep: touch ONLY the CSV's tickers and leave all other "
    "positions untouched (run a sleeve inside a mixed account). Under --no-sweep, "
    "close a rotated-out name by listing it with weight 0.",
)
@click.option(
    "--allocation",
    type=float,
    default=None,
    help="Dollar amount the weights apply to (run a sub-portfolio inside a bigger account). Default: full NAV.",
)
@click.option(
    "--csv-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Read the target CSV from a file instead of stdin.",
)
@click.option("--csv-url", default=None, help="Fetch the target CSV from a URL instead of stdin.")
@click.option(
    "--creds-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Load credentials from a JSON or KEY=VALUE file (headless).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Config file for defaults (TOML).",
)
@click.option("--max-notional", type=float, default=None, help="Refuse if gross buys exceed this dollar amount.")
@click.option(
    "--max-stale-hours", type=float, default=None, help="Refuse if the CSV's `# asof:` time is older than this."
)
@click.option("--notify-url", default=None, help="Webhook (Discord/Slack/generic) to ping on execute.")
@click.option(
    "--margin-aware/--no-margin-aware",
    default=None,
    help="Scale buys to fit buying power (weight-preserving). On by default; --no-margin-aware to disable.",
)
@click.option(
    "--moc",
    is_flag=True,
    default=None,
    help="Submit market-on-close orders (fill in the closing auction). Alpaca / IBKR / Schwab / paper; whole shares; submit before ~15:50 ET.",
)
@click.option(
    "--whole-shares",
    is_flag=True,
    default=None,
    help="Round every order down to whole shares. Use for IBKR/accounts without fractional-API permission (avoids error 10243 'fractional order cannot be placed via API').",
)
@click.option(
    "--order-type",
    type=click.Choice(["market", "limit-chase"]),
    default=None,
    help="market (default) or limit-chase: work each order as a LIMIT pegged to the live mid, repricing every few seconds, then fall back to a market order. RTH only; supported brokers fall back to market.",
)
@click.option(
    "--chase-retries",
    type=int,
    default=None,
    help="limit-chase: reprice attempts before the market fallback (default 5).",
)
@click.option(
    "--chase-interval",
    type=float,
    default=None,
    help="limit-chase: seconds to wait for a fill before repricing (default 5).",
)
@click.option(
    "--chase-poll",
    type=float,
    default=None,
    help="limit-chase: status-poll cadence in seconds within each rung (default 1).",
)
@click.option(
    "--chase-aggression",
    type=float,
    default=None,
    help="limit-chase: fraction past the mid toward the fill side, e.g. 0.001 = 0.1%% (default 0 = pure mid).",
)
@click.option(
    "--chase-fallback/--no-chase-fallback",
    default=None,
    help="limit-chase: send a market order for any unfilled remainder when the chase exhausts (default on).",
)
@click.option("--force", is_flag=True, help="Run even if identical targets were already executed today.")
@click.option(
    "--no-verify",
    is_flag=True,
    default=False,
    help="Skip the post-trade verification (by default, after fills the account is re-fetched and checked against target; any leg still off-target is reported/alerted).",
)
@click.option(
    "--no-self-heal",
    is_flag=True,
    default=False,
    help="Disable self-heal. By default, if post-trade verify finds the book off target, the residual legs are re-executed once (market-open only) to converge — set this to report-only.",
)
@click.option(
    "--heal-passes",
    type=int,
    default=1,
    show_default=True,
    help="Max self-heal re-execution passes when the book hasn't converged.",
)
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON instead of tables.")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output (for cron logs).")
@click.pass_context
def rebalance(
    ctx: click.Context,
    broker_opt: str | None,
    dry_run: bool,
    yes: bool,
    threshold: float | None,
    min_weight: float | None,
    default_stop: float | None,
    threshold_mode: str | None,
    rebalance_scope: str | None,
    sweep: bool | None,
    allocation: float | None,
    csv_file: str | None,
    csv_url: str | None,
    creds_file: str | None,
    config_path: str | None,
    max_notional: float | None,
    max_stale_hours: float | None,
    notify_url: str | None,
    margin_aware: bool | None,
    moc: bool | None,
    whole_shares: bool | None,
    order_type: str | None,
    chase_retries: int | None,
    chase_interval: float | None,
    chase_poll: float | None,
    chase_aggression: float | None,
    chase_fallback: bool | None,
    force: bool,
    no_verify: bool,
    no_self_heal: bool,
    heal_passes: int,
    json_out: bool,
    quiet: bool,
) -> None:
    """Paste a ticker,weight CSV → preview → confirm → execute.

    Manual: run with no flags, paste the CSV, confirm with y.
    Headless: --broker NAME --creds-file creds.json --csv-file targets.csv --yes
    (or use --csv-url and exported env vars). No prompts, no keychain needed.
    """
    cfg = _load_config_or_exit(config_path)
    threshold = float(config.pick(threshold, cfg, "threshold", 0.04))
    min_weight = config.pick(min_weight, cfg, "min_weight")
    default_stop = config.pick(default_stop, cfg, "stop_pct")
    if default_stop is not None and not (0 < float(default_stop) < 0.5):
        _fail(f"--stop-pct {default_stop} outside (0, 0.5) — it is a FRACTION below entry (0.015 = 1.5%), not a price.")
    threshold_mode = config.pick(threshold_mode, cfg, "threshold_mode", "nav")
    rebalance_scope = config.pick(rebalance_scope, cfg, "rebalance_scope", "whole-book")
    sweep = bool(config.pick(sweep, cfg, "sweep", True))
    allocation = config.pick(allocation, cfg, "allocation")
    csv_file = config.pick(csv_file, cfg, "csv_file")
    csv_url = config.pick(csv_url, cfg, "csv_url")
    creds_file = config.pick(creds_file, cfg, "creds_file")
    max_notional = config.pick(max_notional, cfg, "max_notional")
    max_stale_hours = config.pick(max_stale_hours, cfg, "max_stale_hours")
    notify_url = config.pick(notify_url, cfg, "notify_url")
    tg_token = config.pick(None, cfg, "telegram_token")
    tg_chat = config.pick(None, cfg, "telegram_chat_id")
    margin_aware = bool(config.pick(margin_aware, cfg, "margin_aware", True))
    moc = bool(config.pick(True if moc else None, cfg, "moc", False))
    whole_shares = bool(config.pick(True if whole_shares else None, cfg, "whole_shares", False))
    quiet = bool(config.pick(True if quiet else None, cfg, "quiet", False))

    order_type = str(config.pick(order_type, cfg, "order_type", "market"))
    if order_type not in ("market", "limit-chase"):
        _fail(f"invalid order_type {order_type!r} — use 'market' or 'limit-chase'.")
    chase_cfg = None
    if order_type == "limit-chase":
        if moc:
            _fail("--moc and --order-type limit-chase are mutually exclusive.")
        from .chase import ChaseConfig

        chase_cfg = ChaseConfig(
            retries=int(config.pick(chase_retries, cfg, "chase_retries", 5)),
            reprice_interval=float(config.pick(chase_interval, cfg, "chase_interval", 5.0)),
            poll_interval=float(config.pick(chase_poll, cfg, "chase_poll", 1.0)),
            aggression=Decimal(str(config.pick(chase_aggression, cfg, "chase_aggression", 0.0))),
            fallback_to_market=bool(config.pick(chase_fallback, cfg, "chase_fallback", True)),
        )

    global _QUIET, _JSON
    _QUIET, _JSON = quiet, json_out

    if creds_file:
        _load_creds_file_or_exit(creds_file)
    broker = _resolve_broker_name(ctx, broker_opt)

    ms = market_status()
    if broker not in ("paper", "hyperliquid"):  # crypto trades 24/7
        if ms.status == "closed" and not dry_run:
            _fail(f"Market closed. Next open: {ms.next_open}.", code=2)
        if ms.status in ("premarket", "afterhours") and not dry_run:
            _fail(f"Market in {ms.status} session — only RTH market orders are supported.", code=2)

    if csv_file and csv_url:
        _fail("pass only one of --csv-file / --csv-url.")
    if csv_file:
        with open(csv_file, encoding="utf-8") as f:
            csv_text = f.read()
    elif csv_url:
        csv_text = _fetch_url_or_exit(csv_url)
    else:
        if json_out or not sys.stdin.isatty():
            csv_text = sys.stdin.read()
        else:
            say("\n[bold cyan]Paste CSV (ticker,weight), then Ctrl+D (Unix) or Ctrl+Z+Enter (Windows):[/bold cyan]")
            csv_text = sys.stdin.read()

    # Stale-CSV guard (before anything else touches the broker).
    stale = safety.check_stale(csv_text, max_stale_hours)
    if stale:
        _fail(stale)

    try:
        targets = parse_csv(csv_text)
    except CSVParseError as e:
        _fail(f"CSV parse error: {e}")

    if default_stop is not None:
        targets = _apply_default_stop(targets, Decimal(str(default_stop)))

    say(f"[green]✓ loaded {len(targets)} targets.[/green]")

    b = _load_broker(broker)
    # Brokers that can't place fractional equity orders (Schwab, Tradier)
    # already truncate to whole shares at submit — so force whole-share sizing
    # in the preview too, otherwise the preview / notional / --max-notional cap
    # would over-report vs. what actually gets sent.
    if not getattr(b, "supports_fractional", True) and not whole_shares:
        whole_shares = True
        say(f"[dim]{b.name} places whole-share equity orders — sizing to whole shares.[/dim]")
    if moc:
        if not getattr(b, "supports_moc", False):
            _fail(f"{b.name} does not support market-on-close orders (MOC works on: alpaca, ibkr, schwab, paper).")
        # NYSE/Nasdaq stop accepting MOC around 15:50 ET; refuse rather than
        # let every order bounce at the broker.
        if not dry_run and ms.minutes_to_close is not None and ms.minutes_to_close < 12:
            _fail(
                f"only {ms.minutes_to_close} min to the close — exchanges stop accepting MOC orders around 15:50 ET.",
                code=2,
            )
    bal = retry.with_retry(b.balances)
    pos = retry.with_retry(b.positions)
    universe = sorted({tg.ticker for tg in targets} | set(pos.keys()))
    say(f"Quoting {len(universe)} symbols via {b.name}...", style="dim")
    quotes = retry.with_retry(lambda: b.quote(universe))
    for tk, p in pos.items():
        quotes.setdefault(tk, p.price)

    preview = build_preview(
        targets=targets,
        positions=pos,
        nav=bal.nav,
        cash=bal.cash,
        buying_power=bal.buying_power,
        quotes=quotes,
        drift_threshold=Decimal(str(threshold)),
        min_weight=Decimal(str(min_weight)) if min_weight is not None else None,
        allocation=Decimal(str(allocation)) if allocation is not None else None,
        drift_mode=threshold_mode or "nav",
        rebalance_scope=rebalance_scope or "whole-book",
        sweep=sweep,
        whole_shares=whole_shares,
    )
    if margin_aware:
        _apply_margin_aware(b, preview, bal.buying_power, whole_shares=whole_shares)
    if moc:
        for o in preview.orders:
            o.moc = True

    # Extra safety cap on top of the engine's own checks.
    cap_msg = safety.check_max_notional(preview.orders, Decimal(str(max_notional)) if max_notional else None)
    if cap_msg:
        preview.blockers.append(cap_msg)

    # Idempotency: same plan already done today? Params are part of the plan, so
    # a different --allocation/--rebalance-scope/--sweep/--threshold/etc re-runs.
    fp = runstate.fingerprint(
        b.name,
        b.account_id,
        targets,
        {
            "allocation": allocation,
            "scope": rebalance_scope,
            "sweep": sweep,
            "threshold": threshold,
            "threshold_mode": threshold_mode,
            "whole_shares": whole_shares,
            "min_weight": min_weight,
        },
    )
    duplicate = runstate.already_done(fp) and not force

    # In JSON mode the single payload carries everything (orders, warnings,
    # blockers, dry_run, duplicate); decide exit purely on those flags so we
    # never print a second JSON object.
    if json_out:
        _emit_json(b, preview, dry_run=dry_run, duplicate=duplicate)
        if preview.has_blockers:
            sys.exit(1)
        if dry_run or not preview.orders or duplicate:
            if dry_run and preview.orders:
                _do_notify(
                    notifications.format_summary(b.name, b.account_id, 0, 0, preview.orders, dry_run=True),
                    notify_url=notify_url,
                    tg_token=tg_token,
                    tg_chat=tg_chat,
                )
            return
        if not yes:
            print(json.dumps({"error": "refusing to execute without --yes in JSON/non-interactive mode"}))
            sys.exit(1)
        sent, failed, results = _execute(b, preview, order_type=order_type, chase_cfg=chase_cfg, targets=targets)
        if sent > 0 and failed == 0:
            runstate.record(fp)  # only mark done on clean success, so a partial run can re-complete
        _do_notify(
            notifications.format_summary(b.name, b.account_id, sent, failed, preview.orders),
            notify_url=notify_url,
            tg_token=tg_token,
            tg_chat=tg_chat,
        )
        vres = (
            None
            if no_verify
            else _post_trade_verify(
                b,
                targets,
                threshold=threshold,
                threshold_mode=threshold_mode,
                min_weight=min_weight,
                allocation=allocation,
                whole_shares=whole_shares,
                rebalance_scope=rebalance_scope,
                sweep=sweep,
                margin_aware=margin_aware,
                self_heal=not no_self_heal,
                heal_passes=heal_passes,
                order_type=order_type,
                moc=moc,
                recent_clean={r.get("ticker") for r in results if _is_clean_send(r.get("status", "?"))},
                chase_cfg=chase_cfg,
                notify_url=notify_url,
                tg_token=tg_token,
                tg_chat=tg_chat,
            )
        )
        out = {"executed": True, "sent": sent, "failed": failed, "results": results}
        if vres is not None:
            out["verify"] = {
                "converged": vres.ok,
                "residual_legs": len(vres.residual),
                "residual_dollars": float(vres.residual_dollars),
            }
        print(json.dumps(out, default=str))
        return

    _render_preview(preview, b.name, b.account_id, ms)

    if preview.has_blockers:
        _fail("blockers present — refusing to execute.")
    if not preview.orders:
        say("[green]Nothing to do — portfolio within drift on every ticker.[/green]")
        # No trades, but still reconcile protective stops: a held-but-not-traded
        # name whose stop was missed/filled/rejected must be backfilled (sized from
        # broker.positions(), so never a naked stop). Without this, stops are only
        # touched on days the book trades — the held-within-drift coverage gap.
        if getattr(b, "supports_stops", False):
            _reconcile_stops(b, preview, [], targets=targets)
        return
    if dry_run:
        say("[yellow]--dry-run set, exiting without sending orders.[/yellow]")
        _do_notify(
            notifications.format_summary(b.name, b.account_id, 0, 0, preview.orders, dry_run=True),
            notify_url=notify_url,
            tg_token=tg_token,
            tg_chat=tg_chat,
        )
        return
    if duplicate:
        say("[yellow]Identical targets already executed today — skipping (use --force to override).[/yellow]")
        # Same as the within-drift path: reconcile stops even when we skip the
        # trade, so a stop that was filled/cancelled since this morning's run is
        # backfilled on held positions (idempotent — correct stops aren't churned).
        if getattr(b, "supports_stops", False):
            _reconcile_stops(b, preview, [], targets=targets)
        return
    if not yes:
        if not sys.stdin.isatty():
            _fail("refusing to execute without --yes in non-interactive mode.")
        if not Confirm.ask(
            f"\nExecute [bold]{len(preview.orders)}[/bold] orders on [bold]{b.name}[/bold]?", default=False
        ):
            say("[red]Cancelled.[/red]")
            sys.exit(0)

    sent, failed, results = _execute(b, preview, order_type=order_type, chase_cfg=chase_cfg, targets=targets)
    if sent > 0 and failed == 0:
        runstate.record(fp)  # only mark done on clean success, so a partial run can re-complete

    # Notify (best-effort, never raises).
    summary = notifications.format_summary(b.name, b.account_id, sent, failed, preview.orders)
    _do_notify(summary, notify_url=notify_url, tg_token=tg_token, tg_chat=tg_chat)

    # Post-trade verification: re-fetch and confirm the account converged to target.
    if not no_verify:
        _post_trade_verify(
            b,
            targets,
            threshold=threshold,
            threshold_mode=threshold_mode,
            min_weight=min_weight,
            allocation=allocation,
            whole_shares=whole_shares,
            rebalance_scope=rebalance_scope,
            sweep=sweep,
            margin_aware=margin_aware,
            self_heal=not no_self_heal,
            heal_passes=heal_passes,
            order_type=order_type,
            moc=moc,
            recent_clean={r.get("ticker") for r in results if _is_clean_send(r.get("status", "?"))},
            chase_cfg=chase_cfg,
            notify_url=notify_url,
            tg_token=tg_token,
            tg_chat=tg_chat,
        )


def _render_preview(preview, broker_name: str, account_id: str, ms) -> None:
    if _QUIET:
        return
    c.print(
        f"\n[bold]{broker_name}[/bold] · account {account_id}  ·  "
        f"NAV [green]${preview.nav:,.2f}[/green]  ·  "
        f"cash ${preview.cash:,.2f}  ·  BP ${preview.buying_power:,.2f}"
    )
    gross = sum((row.target_pct for row in preview.rows), Decimal(0))
    if gross > Decimal("1.01"):
        c.print(
            f"Gross target exposure: [bold]{gross * 100:.0f}%[/bold] ([bold]{gross:.2f}x[/bold] leverage — uses margin)"
        )
    if ms.minutes_to_close is not None:
        marker = "[red]" if ms.minutes_to_close < 5 else "[yellow]" if ms.minutes_to_close < 15 else "[green]"
        c.print(f"Market: open  ·  closes in {marker}{ms.minutes_to_close} min[/]")

    table = Table(show_header=True, header_style="bold", title="Rebalance preview")
    table.add_column("Symbol")
    table.add_column("Current %", justify="right")
    table.add_column("Target %", justify="right")
    table.add_column("Δ $", justify="right")
    table.add_column("Action", justify="left")
    table.add_column("Note", justify="left", style="dim")

    for row in preview.rows:
        cur = f"{row.current_pct * 100:.1f}%"
        tgt = f"{row.target_pct * 100:.1f}%"
        delta = f"${row.delta_dollars:+,.0f}"
        if row.order:
            qty = f"{row.order.quantity:.2f}"
            est_px = row.order.estimated_price or 0
            action = (
                f"[green]BUY  {qty} @ ~${est_px:,.2f}[/green]"
                if row.order.side == Side.BUY
                else f"[red]SELL {qty} @ ~${est_px:,.2f}[/red]"
            )
        else:
            action = "—"
        table.add_row(row.ticker, cur, tgt, delta, action, row.note)
    c.print(table)

    for w in preview.warnings:
        c.print(f"[yellow]⚠ {escape(w)}[/yellow]")
    for b in preview.blockers:
        c.print(f"[red]✗ {escape(b)}[/red]")


def _apply_default_stop(targets, default_stop):
    """Backfill a uniform protective stop on targets that carry no per-row one.

    A per-row `stop_pct` from the CSV always wins; this only fills the blanks, so
    a weights feed that omits stops (e.g. the raw msts-live live-weights cache,
    which is a {ticker: weight} dict with no stop column) still gets protection
    via `--stop-pct` / config `stop_pct`. Exits (weight 0) are left alone — there
    is nothing to protect once the position is closed."""
    if not default_stop:
        return targets
    from dataclasses import replace

    return [replace(t, stop_pct=default_stop) if (t.stop_pct is None and t.weight > 0) else t for t in targets]


def _is_clean_send(status) -> bool:
    """A market order is a CLEAN completion only if it didn't error/skip AND isn't
    merely RESTING — a resting order was placed but is UNFILLED (e.g. a Hyperliquid
    market order whose remainder rests on a thin book). A resting leg has not
    reached target, so it must not mark the run done; the post-trade verify /
    self-heal chases the unfilled remainder and idempotency stays open for a re-run.
    """
    return str(status).lower() not in ("error", "skipped", "resting")


def _execute(broker, preview, *, order_type: str = "market", chase_cfg=None, targets=None):
    total = len(preview.orders)
    sent = 0
    failed = 0
    results = []
    # Limit-chase routing: each order is worked as a LIMIT pegged to the live
    # mid (with a market fallback) instead of a plain market order. Brokers
    # that can't chase degrade to market with a one-time warning — same pattern
    # as unsupported protective stops.
    use_chase = order_type == "limit-chase"
    if use_chase and not getattr(broker, "supports_limit_chase", False):
        say(f"[yellow]{broker.name} does not support limit-chase orders — using market orders instead.[/yellow]")
        use_chase = False
    # Pre-cancel resting stops on names we're about to SELL. A broker (e.g. tastytrade) rejects a
    # sell of shares that are reserved by an open stop order
    # ("cannot_close_against_more_than_existing_position"), so the protective stop MUST be cancelled
    # BEFORE the sell — not in _reconcile_stops afterwards (by then the sell has already bounced).
    if getattr(broker, "supports_stops", False):
        sell_tkrs = {o.ticker for o in preview.orders if o.side.value == "SELL"}
        if sell_tkrs:
            try:
                open_stops = broker.open_stops()
                for tkr in sell_tkrs:
                    for st in open_stops.get(tkr, []):
                        try:
                            broker.cancel_order(st["order_id"])
                            say(f"[dim]  pre-cancel stop {tkr} (frees shares to sell)[/dim]")
                            fill_log.append(
                                {
                                    "event": "stop_precancel",
                                    "broker": broker.name,
                                    "ticker": tkr,
                                    "order_id": st["order_id"],
                                }
                            )
                        except Exception as e:
                            say(f"[yellow]  pre-cancel stop failed for {tkr}: {e}[/yellow]")
            except Exception as e:
                say(
                    f"[yellow]  pre-cancel skipped: open_stops failed ({e}) — sells may bounce on resting stops[/yellow]"
                )
    for i, o in enumerate(preview.orders, 1):
        kind = "CHASE" if use_chase else ("MOC" if o.moc else "MKT")
        say(f"[{i}/{total}] {o.ticker} {o.side.value} {o.quantity:.2f} @ {kind} ...", end="\n" if use_chase else " ")
        try:
            # NOT retried: a market order is not idempotent. If submission
            # times out after the broker accepted it, a retry would double
            # the fill. On a transient error we report and move on; the
            # drift gate self-corrects on the next run. (The chase engine
            # manages its own cancel/reprice lifecycle, including a final
            # market fallback, so it is not wrapped in a retry either.)
            if use_chase:
                from . import chase as _chase

                result = _chase.chase_fill(broker, o, chase_cfg, log=say)
            else:
                result = broker.place_market(o, dry_run=False)
        except Exception as e:
            result = {"status": "error", "reason": str(e), "ticker": o.ticker}
        status = result.get("status", "?")
        if not _is_clean_send(status):
            failed += 1
            if str(status).lower() == "resting":
                say(f"[yellow]RESTING (unfilled)[/yellow]  id={result.get('order_id', '?')} — verify will chase it")
            else:
                say(f"[red]{status.upper()}[/red] {escape(str(result.get('reason', '')))}")
        else:
            sent += 1
            say(f"[green]{status.upper()}[/green]  id={result.get('order_id', '?')}")
        results.append(result)
        fill_log.append(
            {"event": "order", "broker": broker.name, **result, "side": o.side.value, "quantity": float(o.quantity)}
        )

    # Always show a one-line summary (except in JSON mode) so even --quiet
    # cron runs leave a trace.
    if not _JSON:
        c.print(f"[bold]Done.[/bold] {broker.name}: sent {sent}, failed {failed}")

    # CONFIRM BUY fills before any protective stop is placed. A stop must never
    # be set for shares we don't actually hold yet — an unconfirmed/unfilled buy
    # would otherwise get a naked stop that opens a short if it triggers. Fill
    # confirmation is broker-agnostic: poll broker.positions() until the bought
    # shares appear. Where the broker also exposes fills(), fold the real fill
    # price into `results` so the stop anchors on the actual entry, not the quote.
    need_fills = {o.ticker for o in preview.orders if o.side.value == "BUY" and o.stop_pct}
    if need_fills and getattr(broker, "supports_stops", False):
        import time as _time

        for _attempt in range(6):  # ~ up to ~10s
            try:
                pos_now = broker.positions()
            except Exception:
                pos_now = {}
            # Anchor the stop on the REAL entry, not the position's avg/current
            # price: pull the actual fill price from the order itself via
            # order_status (every adapter exposes filled_avg_price), with
            # tastytrade's fills() as a fallback.
            for r in results:
                t = r.get("ticker")
                oid = r.get("order_id")
                if t not in need_fills or r.get("fill_price") or not oid:
                    continue
                if hasattr(broker, "order_status"):
                    try:
                        st = broker.order_status(oid)
                    except Exception:
                        st = {}
                    avg = st.get("filled_avg_price")
                    if avg:
                        r["fill_price"] = float(avg)
            if hasattr(broker, "fills"):
                try:
                    fp = broker.fills()
                except Exception:
                    fp = {}
                for r in results:
                    t = r.get("ticker")
                    if t in fp and not r.get("fill_price"):
                        r["fill_price"] = float(fp[t])
            if all(t in pos_now and pos_now[t].quantity > 0 for t in need_fills):
                break
            _time.sleep(1.5)

    _reconcile_stops(broker, preview, results, targets=targets)
    return sent, failed, results


def _verify_once(
    b,
    targets,
    *,
    threshold,
    threshold_mode,
    min_weight,
    allocation,
    whole_shares,
    rebalance_scope="whole-book",
    sweep=True,
    margin_aware=False,
):
    """Re-fetch broker state and rebuild the diff. Returns (VerifyResult, post_fill_preview)."""
    bal = retry.with_retry(b.balances)
    pos = retry.with_retry(b.positions)
    universe = sorted({tg.ticker for tg in targets} | set(pos.keys()))
    quotes = retry.with_retry(lambda: b.quote(universe))
    for tk, p in pos.items():
        quotes.setdefault(tk, p.price)
    post = build_preview(
        targets=targets,
        positions=pos,
        nav=bal.nav,
        cash=bal.cash,
        buying_power=bal.buying_power,
        quotes=quotes,
        drift_threshold=Decimal(str(threshold)) if threshold is not None else DRIFT_THRESHOLD,
        min_weight=Decimal(str(min_weight)) if min_weight is not None else None,
        allocation=Decimal(str(allocation)) if allocation is not None else None,
        drift_mode=threshold_mode or "nav",
        rebalance_scope=rebalance_scope or "whole-book",
        sweep=sweep,
        whole_shares=whole_shares,
    )
    # When margin-aware is on, a residual buy the account can't fund is "as
    # deployed as possible", not a non-convergence — don't let self-heal chase it.
    if margin_aware:
        return converged_within_buying_power(post), post
    return check_convergence(post), post


def _post_trade_verify(
    b,
    targets,
    *,
    threshold=None,
    threshold_mode="nav",
    min_weight=None,
    allocation=None,
    whole_shares=False,
    rebalance_scope="whole-book",
    sweep=True,
    margin_aware=False,
    settle_seconds=2.0,
    self_heal=False,
    heal_passes=1,
    order_type="market",
    moc=False,
    recent_clean=None,
    chase_cfg=None,
    notify_url=None,
    tg_token=None,
    tg_chat=None,
):
    """After fills, re-fetch broker state and confirm the account CONVERGED to target.

    Reuses the exact rebalance diff (build_preview) against fresh positions: any residual order
    is a leg that didn't reach target (partial fill / failed close / not-yet-settled). Broker-
    agnostic (Broker Protocol only) and best-effort — never raises.

    SELF-HEAL (self_heal=True): if not converged, RE-EXECUTE the residual orders and re-verify,
    up to `heal_passes` times. Only re-trades while the market is OPEN; each pass goes through the
    normal _execute path (so re-bought legs get their protective stops too). A leg that simply
    cannot fill (no liquidity / repeatedly rejected) stops after the cap and is reported 🔴.
    """
    # MOC orders fill ONLY in the closing auction, so an immediate post-trade
    # convergence check always shows the book "unconverged" and self-heal would
    # re-execute the residual as a plain MARKET order that fills NOW — double-
    # executing the rebalance (one immediate market fill + the resting MOC).
    # Convergence is only meaningful after the close, so never self-heal a MOC run.
    if moc and self_heal:
        say("[dim]MOC run: self-heal disabled (MOC fills at the close; check convergence after the auction).[/dim]")
        self_heal = False

    # Legs we've already cleanly sent this run. We must NOT re-trade these even
    # if the verify diff still shows them as residual: a market order is not
    # idempotent, and a residual for a just-sent leg is almost always positions()
    # lagging the fill (eventually-consistent broker reads) — re-trading it would
    # double the position. Same bug class as the MOC self-heal double-fire, but it
    # bites normal market orders on any lagging broker. Genuinely-failed legs
    # (rejected/skipped/resting) were never recorded as clean sends, so they still
    # heal here; a real miss on a just-sent leg reconciles on the next run.
    recent = {t for t in (recent_clean or set()) if t}

    healed = 0
    res = None
    try:
        import time as _t

        for attempt in range(heal_passes + 1):
            if settle_seconds:
                _t.sleep(settle_seconds)  # give fills a moment to reflect in positions()
            res, post = _verify_once(
                b,
                targets,
                threshold=threshold,
                threshold_mode=threshold_mode,
                min_weight=min_weight,
                allocation=allocation,
                whole_shares=whole_shares,
                rebalance_scope=rebalance_scope,
                sweep=sweep,
                margin_aware=margin_aware,
            )
            if res.ok or not self_heal or attempt >= heal_passes or not post.orders:
                break
            if market_status().status != "open":
                say("[yellow]self-heal skipped — market not open.[/yellow]")
                break
            heal_orders = (
                post.orders if not recent else [o for o in post.orders if getattr(o, "ticker", None) not in recent]
            )
            if not heal_orders:
                say(
                    "[yellow]self-heal: residual legs were all already traded this run — not re-trading "
                    "(likely position-read lag, not a real miss); reconciles on the next run.[/yellow]"
                )
                break
            say(
                f"[yellow]self-heal pass {attempt + 1}/{heal_passes}: re-executing "
                f"{len(heal_orders)} residual leg(s)…[/yellow]"
            )
            post.orders = heal_orders
            try:
                ret = _execute(b, post, order_type=order_type, chase_cfg=chase_cfg, targets=targets)
                healed += 1
                # Don't re-trade these on the next pass either, for the same reason.
                heal_results = ret[2] if isinstance(ret, tuple) and len(ret) >= 3 else []
                recent |= {r.get("ticker") for r in heal_results if _is_clean_send(r.get("status", "?"))}
            except Exception as e:
                say(f"[red]self-heal execute failed: {e}[/red]")
                break
        if res is None:
            return None
        tag = f" (after {healed} self-heal pass{'es' if healed != 1 else ''})" if healed else ""
        say(("[green]" if res.ok else "[red]") + "post-trade verify: " + escape(res.summary()) + tag + "[/]")
        _do_notify(
            f"post-trade verify · {b.name} ({b.account_id}) · {res.summary()}{tag}",
            notify_url=notify_url,
            tg_token=tg_token,
            tg_chat=tg_chat,
        )
        return res
    except Exception as e:
        say(f"[yellow]post-trade verify skipped: {e}[/yellow]")
        return None


def _reconcile_stops(broker, preview, results, targets=None):
    """Idempotent protective-stop reconciliation, run after fills are confirmed.

    Every rebalance, make the broker stop book match reality:

    * **Fill-confirmed sizing.** A stop is placed only for shares we actually
      hold — quantity always comes from ``broker.positions()``, never the
      intended order size — so an unconfirmed / unfilled buy can never leave a
      naked stop (which would open a short if it triggered).
    * **SELLs / trims.** Cancel the old stop; if a remainder is still held and
      the target wants protection, re-place for the remaining quantity.
    * **Missing stops (self-heal).** Any HELD name the target wants protected
      that has no open stop gets one — even if it didn't trade this run (a stop
      that was missed, rejected, or filled-and-not-replaced last time is
      backfilled on the next rebalance).
    * **Orphan stops.** Any open stop with no live position is cancelled (a
      resting stop with nothing behind it is a naked-short risk).

    Brokers without supports_stops: warn once if the CSV asked for stops.
    """
    wants_stops = any(o.stop_pct for o in preview.orders) or any(getattr(t, "stop_pct", None) for t in (targets or []))
    if not getattr(broker, "supports_stops", False):
        if wants_stops:
            say(f"[yellow]{broker.name} does not support stop orders — stop_pct column ignored.[/yellow]")
        return
    try:
        open_stops = broker.open_stops()
    except Exception as e:
        say(f"[yellow]stop reconcile skipped: open_stops failed ({e})[/yellow]")
        return
    try:
        post_positions = broker.positions()
    except Exception:
        post_positions = {}

    # Desired protection per ticker. The target book is authoritative (it covers
    # held-but-not-traded names too); fall back to BUY-order stop_pct when no
    # targets were threaded in (keeps the direct-call/unit-test shape working).
    desired = {t.ticker: t.stop_pct for t in (targets or []) if getattr(t, "stop_pct", None)}
    for o in preview.orders:
        if getattr(o, "stop_pct", None):
            desired.setdefault(o.ticker, o.stop_pct)

    results_by_tkr = {r.get("ticker"): r for r in results}

    def _traded(tkr) -> bool:
        # The order for this ticker actually did something (clean send or a
        # partial chase fill) — vs. a failed/skipped order we should ignore.
        r = results_by_tkr.get(tkr)
        return bool(
            r
            and (
                r.get("status") not in ("error", "skipped", "dry-run")
                or r.get("chase_limit_filled")
                or r.get("filled_quantity")
            )
        )

    def _cancel_all(tkr, why):
        for stop in open_stops.get(tkr, []):
            try:
                broker.cancel_order(stop["order_id"])
                say(f"  stop cancelled: {tkr} ({why})")
                fill_log.append(
                    {"event": "stop_cancel", "broker": broker.name, "ticker": tkr, "order_id": stop["order_id"]}
                )
            except Exception as e:
                say(f"[yellow]  stop cancel failed for {tkr}: {e}[/yellow]")

    def _place(tkr, qty, px, stop_pct, why):
        stop_price = (px * (Decimal(1) - stop_pct)).quantize(Decimal("0.01"))
        try:
            res = broker.place_stop(tkr, qty, stop_price)
            say(f"  stop placed: {tkr} {qty} @ {stop_price} ({stop_pct:.1%} below ref) — {why}")
            fill_log.append({"event": "stop_place", "broker": broker.name, **res})
        except Exception as e:
            say(f"[yellow]  stop place failed for {tkr}: {e}[/yellow]")

    handled: set[str] = set()

    # 1) SELLs / trims: cancel the old stop; re-place for any still-held
    #    remainder the target still wants protected (anchored at current price —
    #    the original entry is gone with the trim).
    for o in preview.orders:
        if o.side.value != "SELL" or not _traded(o.ticker):
            continue
        tkr = o.ticker
        handled.add(tkr)
        if open_stops.get(tkr):
            _cancel_all(tkr, "position reduced")
        remaining = post_positions.get(tkr)
        pct = desired.get(tkr)
        if remaining is not None and remaining.quantity > 0 and pct:
            px = Decimal(
                str(results_by_tkr.get(tkr, {}).get("fill_price") or o.estimated_price or remaining.price or 0)
            )
            if px > 0:
                _place(tkr, remaining.quantity, px, pct, "remainder")

    # 2) Ensure every HELD name the target wants protected has exactly one stop
    #    for the held quantity. Covers fresh BUYs (re-anchored on the real fill)
    #    AND held-but-not-traded names whose stop went missing. Quantity is the
    #    confirmed holding — never an unfilled order size.
    for tkr, pct in desired.items():
        if tkr in handled:
            continue
        held = post_positions.get(tkr)
        qty = held.quantity if (held is not None and held.quantity > 0) else Decimal(0)
        if qty <= 0:
            continue  # nothing held -> nothing to protect (no naked stop)
        existing = open_stops.get(tkr, [])
        r = results_by_tkr.get(tkr)
        fill_px = None
        if r and r.get("side") == "BUY" and r.get("fill_price"):
            fill_px = Decimal(str(r.get("fill_price")))
        existing_ok = len(existing) == 1 and existing[0].get("quantity") == qty
        if fill_px is None and existing_ok:
            continue  # correct stop already resting and no fresh entry -> don't churn it
        # Anchor: a fresh fill price wins; otherwise the LIVE quote — NOT
        # position.price, which some adapters (Tradier) report as average COST,
        # not market, putting a backfilled stop at the wrong distance. Fall back
        # to position.price only if the quote is unavailable.
        if fill_px is not None:
            px = fill_px
        else:
            px = None
            try:
                q = broker.quote([tkr]).get(tkr)
                if q and Decimal(str(q)) > 0:
                    px = Decimal(str(q))
            except Exception:
                pass
            if px is None and held.price > 0:
                px = held.price
        if px is None or px <= 0:
            continue  # no usable anchor -> leave for the next run
        if existing:
            _cancel_all(tkr, "replacing stale stop" if fill_px is not None else "syncing stop")
        _place(tkr, qty, px, pct, "fresh entry" if fill_px is not None else "backfill")
        handled.add(tkr)

    # 3) Orphan sweep: an open stop with no live position is a naked-short risk —
    #    cancel it (manual exits, dropped tickers, leftovers from a prior run).
    for tkr in list(open_stops):
        if tkr in handled:
            continue
        held = post_positions.get(tkr)
        if held is None or held.quantity <= 0:
            _cancel_all(tkr, "no position (orphan)")


def _rebalance_one(
    b,
    targets,
    *,
    threshold: float,
    max_notional,
    dry_run: bool,
    force: bool,
    margin_aware: bool = False,
    min_weight=None,
    allocation=None,
    whole_shares: bool = False,
    threshold_mode: str = "nav",
    rebalance_scope: str = "whole-book",
    sweep: bool = True,
    order_type: str = "market",
    chase_cfg=None,
) -> dict:
    """Run the full rebalance pipeline for one already-built broker.

    No interactive prompts, no rich rendering — returns a result dict.
    Shared by the `multi` command (and safe to reuse elsewhere). Reuses
    the same `_execute` (single-submit, no order retry) and idempotency
    rules as the interactive `rebalance`.
    """
    # Whole-share brokers truncate at submit; size the preview to match.
    whole_shares = whole_shares or not getattr(b, "supports_fractional", True)
    bal = retry.with_retry(b.balances)
    pos = retry.with_retry(b.positions)
    universe = sorted({t.ticker for t in targets} | set(pos.keys()))
    quotes = retry.with_retry(lambda: b.quote(universe))
    for tk, p in pos.items():
        quotes.setdefault(tk, p.price)

    preview = build_preview(
        targets=targets,
        positions=pos,
        nav=bal.nav,
        cash=bal.cash,
        buying_power=bal.buying_power,
        quotes=quotes,
        drift_threshold=Decimal(str(threshold)),
        min_weight=Decimal(str(min_weight)) if min_weight is not None else None,
        allocation=Decimal(str(allocation)) if allocation is not None else None,
        drift_mode=threshold_mode or "nav",
        rebalance_scope=rebalance_scope or "whole-book",
        sweep=sweep,
        whole_shares=whole_shares,
    )
    if margin_aware:
        _apply_margin_aware(b, preview, bal.buying_power, whole_shares=whole_shares)
    cap = safety.check_max_notional(preview.orders, Decimal(str(max_notional)) if max_notional else None)
    if cap:
        preview.blockers.append(cap)

    fp = runstate.fingerprint(
        b.name,
        b.account_id,
        targets,
        {
            "allocation": allocation,
            "scope": rebalance_scope,
            "sweep": sweep,
            "threshold": threshold,
            "threshold_mode": threshold_mode,
            "whole_shares": whole_shares,
            "min_weight": min_weight,
        },
    )
    duplicate = runstate.already_done(fp) and not force

    result = {
        "broker": b.name,
        "account": b.account_id,
        "nav": str(bal.nav),
        "orders": len(preview.orders),
        "sent": 0,
        "failed": 0,
        "warnings": preview.warnings,
        "blockers": preview.blockers,
        "status": "preview",
    }
    if preview.has_blockers:
        result["status"] = "blocked"
        return result
    if not preview.orders:
        result["status"] = "nothing-to-do"
        return result
    if dry_run:
        result["status"] = "dry-run"
        return result
    if duplicate:
        result["status"] = "duplicate"
        return result

    sent, failed, _ = _execute(b, preview, order_type=order_type, chase_cfg=chase_cfg, targets=targets)
    if sent > 0 and failed == 0:
        runstate.record(fp)
    result.update(sent=sent, failed=failed, status="executed" if failed == 0 else "partial")
    return result


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Multi-account config (TOML with [[account]] tables).",
)
@click.option(
    "--csv-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Target CSV file (overrides config csv_file/url).",
)
@click.option("--csv-url", default=None, help="Target CSV URL (overrides config).")
@click.option("--dry-run", is_flag=True, help="Preview every account, send nothing.")
@click.option("--yes", "-y", is_flag=True, help="Required to actually execute (multi never prompts).")
@click.option(
    "--margin-aware/--no-margin-aware",
    default=None,
    help="Scale buys to fit buying power (on by default; --no-margin-aware to disable).",
)
@click.option("--force", is_flag=True, help="Run even if identical targets were already executed today.")
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output.")
def multi(config_path, csv_file, csv_url, dry_run, yes, margin_aware, force, json_out, quiet):
    """Run the same target weights across several accounts in one pass.

    Each `[[account]]` in the config names a broker and a creds file; the
    same CSV is rebalanced against every account sequentially, and a
    combined summary is printed. Use --dry-run first.
    """
    global _QUIET, _JSON
    _QUIET, _JSON = quiet, json_out

    cfg = _load_config_or_exit(config_path)
    accounts = cfg.get("account") or []
    if not accounts:
        _fail("no [[account]] entries in the config.")

    threshold = float(cfg.get("threshold", 0.04))
    threshold_mode = str(cfg.get("threshold_mode", "nav"))
    rebalance_scope = str(cfg.get("rebalance_scope", "whole-book"))
    sweep_default = bool(cfg.get("sweep", True))
    min_weight = cfg.get("min_weight")
    allocation_default = cfg.get("allocation")
    max_notional = cfg.get("max_notional")
    max_stale_hours = cfg.get("max_stale_hours")
    notify_url = cfg.get("notify_url")
    tg_token = cfg.get("telegram_token")
    tg_chat = cfg.get("telegram_chat_id")
    whole_shares = bool(cfg.get("whole_shares", False))
    margin_aware = bool(config.pick(margin_aware, cfg, "margin_aware", True))
    csv_file = csv_file or cfg.get("csv_file")
    csv_url = csv_url or cfg.get("csv_url")

    # Limit-chase routing for multi is config-driven (no per-flag CLI). Build a
    # ChaseConfig once; per-account `order_type` can still override below.
    order_type = str(cfg.get("order_type", "market"))
    if order_type not in ("market", "limit-chase"):
        _fail(f"invalid order_type {order_type!r} in config — use 'market' or 'limit-chase'.")
    chase_cfg = None
    if order_type == "limit-chase" or any(str(a.get("order_type", "")) == "limit-chase" for a in accounts):
        from .chase import ChaseConfig

        chase_cfg = ChaseConfig(
            retries=int(cfg.get("chase_retries", 5)),
            reprice_interval=float(cfg.get("chase_interval", 5.0)),
            poll_interval=float(cfg.get("chase_poll", 1.0)),
            aggression=Decimal(str(cfg.get("chase_aggression", 0.0))),
            fallback_to_market=bool(cfg.get("chase_fallback", True)),
        )

    if csv_file and csv_url:
        _fail("pass only one of --csv-file / --csv-url (or set one in config).")
    if csv_file:
        with open(csv_file, encoding="utf-8") as f:
            csv_text = f.read()
    elif csv_url:
        csv_text = _fetch_url_or_exit(csv_url)
    else:
        _fail("no CSV source — set csv_file/csv_url in the config or pass --csv-file/--csv-url.")

    stale = safety.check_stale(csv_text, max_stale_hours)
    if stale:
        _fail(stale)
    try:
        targets = parse_csv(csv_text)
    except CSVParseError as e:
        _fail(f"CSV parse error: {e}")

    default_stop = cfg.get("stop_pct")
    if default_stop is not None and not (0 < float(default_stop) < 0.5):
        _fail(f"stop_pct {default_stop} in config outside (0, 0.5) — it is a FRACTION below entry, not a price.")

    if not dry_run and not yes:
        _fail("refusing to execute across accounts without --yes (use --dry-run to preview).")

    from .creds_file import broker_kwargs_from_file

    results = []
    for i, acct in enumerate(accounts, 1):
        label = acct.get("name") or f"account-{i}"
        broker = (acct.get("broker") or "").lower().strip()
        creds_path = acct.get("creds_file")
        if broker not in SUPPORTED:
            results.append({"name": label, "status": "error", "reason": f"bad/missing broker {broker!r}"})
            continue
        try:
            kwargs = broker_kwargs_from_file(broker, creds_path) if creds_path else broker_kwargs_from_env(broker)
            if kwargs is None:
                raise BrokerError("no credentials resolved (check creds_file / env)")
            b = make(broker, **kwargs)
        except Exception as e:
            results.append({"name": label, "broker": broker, "status": "error", "reason": str(e)})
            continue

        say(f"\n[bold cyan]━━ {label} ({broker}) ━━[/bold cyan]")
        try:
            # per-account `allocation`, `order_type`, `stop_pct` win over top-level.
            acct_order_type = str(acct.get("order_type", order_type))
            acct_stop = acct.get("stop_pct", default_stop)
            acct_targets = _apply_default_stop(targets, Decimal(str(acct_stop))) if acct_stop is not None else targets
            r = _rebalance_one(
                b,
                acct_targets,
                threshold=threshold,
                max_notional=max_notional,
                dry_run=dry_run,
                force=force,
                margin_aware=margin_aware,
                min_weight=min_weight,
                allocation=acct.get("allocation", allocation_default),
                whole_shares=acct.get("whole_shares", whole_shares),
                threshold_mode=str(acct.get("threshold_mode", threshold_mode)),
                rebalance_scope=str(acct.get("rebalance_scope", rebalance_scope)),
                sweep=bool(acct.get("sweep", sweep_default)),
                order_type=acct_order_type,
                chase_cfg=chase_cfg if acct_order_type == "limit-chase" else None,
            )
        except Exception as e:
            r = {"broker": broker, "status": "error", "reason": str(e)}
        r["name"] = label
        results.append(r)

    executed = any(r.get("status") in ("executed", "partial") for r in results)
    if executed and (notify_url or (tg_token and tg_chat)):
        lines = [f"msts-trader multi · {len(results)} accounts"]
        for r in results:
            lines.append(f"  {r.get('name')}: {r.get('status')} ({r.get('sent', 0)} sent, {r.get('failed', 0)} failed)")
        _do_notify("\n".join(lines), notify_url=notify_url, tg_token=tg_token, tg_chat=tg_chat)

    if json_out:
        print(json.dumps({"accounts": results}, default=str))
        if any(r.get("status") in ("error", "blocked", "partial") for r in results):
            sys.exit(1)
        return

    table = Table(show_header=True, header_style="bold", title="Multi-account rebalance")
    table.add_column("Account")
    table.add_column("Broker")
    table.add_column("Status")
    table.add_column("Orders", justify="right")
    table.add_column("Sent", justify="right")
    table.add_column("Failed", justify="right")
    for r in results:
        st = r.get("status", "?")
        color = (
            "green"
            if st in ("executed", "dry-run", "nothing-to-do")
            else "yellow"
            if st in ("duplicate", "partial")
            else "red"
        )
        detail = f" {escape(str(r['reason']))}" if r.get("reason") else ""
        table.add_row(
            r.get("name", "?"),
            r.get("broker", "—"),
            f"[{color}]{st}[/{color}]{detail}",
            str(r.get("orders", "—")),
            str(r.get("sent", "—")),
            str(r.get("failed", "—")),
        )
    c.print(table)
    if any(r.get("status") in ("error", "blocked", "partial") for r in results):
        sys.exit(1)


@main.command()
@_BROKER_OPT
@click.option(
    "--creds-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Load credentials from a file first.",
)
@click.pass_context
def doctor(ctx: click.Context, broker_opt: str | None, creds_file: str | None) -> None:
    """Health check: credentials, connectivity, market status, a sample quote."""
    if creds_file:
        _load_creds_file_or_exit(creds_file)

    ms = market_status()
    c.print(
        f"Market: [bold]{ms.status}[/bold]"
        + (f" · closes in {ms.minutes_to_close} min" if ms.minutes_to_close is not None else "")
    )

    requested = (broker_opt or ctx.obj.get("broker") or "").lower().strip()
    names = [requested] if requested else list(keychain.list_brokers()) or list(SUPPORTED)

    table = Table(show_header=True, header_style="bold", title="Broker health")
    table.add_column("Broker")
    table.add_column("Creds")
    table.add_column("Connect")
    table.add_column("NAV", justify="right")
    table.add_column("Positions", justify="right")
    table.add_column("Quote SPY", justify="right")

    for name in names:
        has_creds = broker_kwargs_from_env(name) is not None or name in set(keychain.list_brokers())
        creds_cell = "[green]✓[/green]" if has_creds else "[dim]—[/dim]"
        conn = nav = npos = q = "[dim]—[/dim]"
        if has_creds:
            try:
                b = _build_broker_quiet(name)
                conn = "[green]✓[/green]"
                try:
                    nav = f"${b.balances().nav:,.0f}"
                except Exception as e:
                    nav = f"[red]err[/red] {str(e)[:30]}"
                try:
                    npos = str(len(b.positions()))
                except Exception:
                    npos = "[red]err[/red]"
                try:
                    qd = b.quote(["SPY"])
                    q = f"${qd['SPY']:,.2f}" if qd.get("SPY") else "[yellow]none[/yellow]"
                except Exception:
                    q = "[red]err[/red]"
            except Exception as e:
                conn = f"[red]✗[/red] {str(e)[:40]}"
        table.add_row(name, creds_cell, conn, nav, npos, q)
    c.print(table)


def _build_broker_quiet(name: str):
    """Build a broker without the exit-on-error wrapper (for doctor)."""
    creds = broker_kwargs_from_env(name)
    if creds is None:
        creds = keychain.load(name)
    return make(name, **creds)


if __name__ == "__main__":
    main()
