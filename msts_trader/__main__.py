"""msts-trader CLI: paste a CSV, preview the rebalance, execute it on your broker.

Subcommands:
  login [--broker NAME]      — store creds in OS keychain (per-broker)
  status [--broker NAME]     — show NAV / positions / market status
  rebalance [--broker NAME]  — (default) paste CSV, preview, prompt, execute
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
from rich.panel import Panel
from rich.prompt import Confirm  # used for the post-preview Y/N (works fine in all terminals)
from rich.table import Table

from . import __version__, config, fill_log, keychain, notifications, retry, runstate, safety
from .brokers import SUPPORTED, BrokerError, make
from .creds_file import CredsFileError, broker_kwargs_from_env, load_into_env
from .csv_parser import CSVParseError, parse_csv
from .diff import build_preview
from .login_errors import explain_login_error
from .market_hours import market_status
from .models import Side
from .prompts import ask_secret, ask_text, ask_yes_no, env_value

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
    """Emit an error (JSON-aware) and exit."""
    if _JSON:
        print(json.dumps({"error": msg}))
    else:
        c.print(f"[red]✗ {msg}[/red]")
    sys.exit(code)


def _load_config_or_exit(path: str | None) -> dict:
    try:
        return config.load(path)
    except config.ConfigError as e:
        _fail(str(e))


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
        keys = load_into_env(path)
    except CredsFileError as e:
        c.print(f"[red]✗ could not read creds file:[/red] {e}")
        sys.exit(1)
    c.print(f"[green]✓ loaded {len(keys)} value(s) from {path}[/green]")


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
        c.print(f"[red]✗ broker init failed:[/red] {e}")
        sys.exit(1)


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="msts-trader")
@click.option("--broker", default=None, help=f"Broker name. Supported: {', '.join(SUPPORTED)}")
@click.pass_context
def main(ctx: click.Context, broker: str | None) -> None:
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
@click.pass_context
def login(ctx: click.Context, broker_opt: str | None, creds_file: str | None) -> None:
    """Store broker creds in OS keychain."""
    if creds_file:
        try:
            keys = load_into_env(creds_file)
        except CredsFileError as e:
            c.print(f"[red]✗ could not read creds file:[/red] {e}")
            sys.exit(1)
        c.print(f"[green]✓ loaded {len(keys)} value(s) from {creds_file}[/green]")

    broker = (
        broker_opt
        or ctx.obj.get("broker")
        or _prompt_choice(f"broker [{'|'.join(SUPPORTED)}]", choices=list(SUPPORTED), default="tastytrade")
    )
    if broker == "tastytrade":
        _login_tastytrade()
    elif broker == "alpaca":
        _login_alpaca()
    elif broker == "ibkr":
        _login_ibkr()
    elif broker == "schwab":
        _login_schwab()
    elif broker == "hyperliquid":
        _login_hyperliquid()
    elif broker == "paper":
        _login_paper()
    else:
        c.print(f"[red]unknown broker {broker!r}[/red]")
        sys.exit(1)


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
            "as environment variables first.[/dim]",
            border_style="cyan",
        )
    )
    provider_secret = ask_secret("provider secret", env_var="TT_PROVIDER_SECRET")
    refresh_token = ask_secret("refresh token", env_var="TT_REFRESH_TOKEN")
    account_id = env_value("TT_ACCOUNT_ID") or ask_text("account id (optional)", default="", allow_blank=True)
    account_id = account_id.strip() or None

    try:
        b = make("tastytrade", provider_secret=provider_secret, refresh_token=refresh_token, account_id=account_id)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {explain_login_error('tastytrade', e)}[/red]")
        sys.exit(1)

    keychain.save("tastytrade", {
        "provider_secret": provider_secret,
        "refresh_token": refresh_token,
        "account_id": account_id or b.account_id,
    })
    keychain.set_default("tastytrade")
    c.print(f"[green]✓ stored.[/green] tastytrade account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


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
        c.print(f"[red]✗ {explain_login_error('alpaca', e)}[/red]")
        sys.exit(1)

    keychain.save("alpaca", {"api_key": api_key, "secret_key": secret_key, "paper": paper})
    keychain.set_default("alpaca")
    c.print(f"[green]✓ stored.[/green] alpaca {'(paper)' if paper else '(live)'} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


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
    account_id = (env_value("IBKR_ACCOUNT_ID") or ask_text("account id (optional)", default="", allow_blank=True)).strip() or None

    try:
        b = make("ibkr", host=host, port=port, client_id=client_id, account_id=account_id)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {explain_login_error('ibkr', e)}[/red]")
        sys.exit(1)

    keychain.save("ibkr", {"host": host, "port": port, "client_id": client_id, "account_id": account_id or b.account_id})
    keychain.set_default("ibkr")
    c.print(f"[green]✓ stored.[/green] ibkr account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


def _login_schwab() -> None:
    c.print(
        Panel.fit(
            "[bold]Schwab OAuth2 setup[/bold]\n\n"
            "1. Register a developer app at [cyan]https://developer.schwab.com[/cyan]\n"
            "2. Set the callback URL to [bold]https://127.0.0.1:8182/[/bold]\n"
            "3. Copy your [bold]app key[/bold] and [bold]app secret[/bold]\n"
            "4. A browser will open for authorization. Refresh token lasts\n"
            "   [bold]7 days[/bold] — re-run this login when it expires.",
            border_style="cyan",
        )
    )
    app_key = ask_secret("app key", env_var="SCHWAB_APP_KEY")
    app_secret = ask_secret("app secret", env_var="SCHWAB_APP_SECRET")
    callback_url = env_value("SCHWAB_CALLBACK_URL") or ask_text("callback url", default="https://127.0.0.1:8182/")

    try:
        b = make("schwab", app_key=app_key, app_secret=app_secret, callback_url=callback_url)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {explain_login_error('schwab', e)}[/red]")
        sys.exit(1)

    keychain.save("schwab", {"app_key": app_key, "app_secret": app_secret, "callback_url": callback_url, "account_hash": b._account_hash})
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
    account_address = (env_value("HL_ACCOUNT_ADDRESS") or ask_text("account address (optional)", default="", allow_blank=True)).strip() or None
    raw = env_value("HL_TESTNET")
    testnet = raw.lower() in {"1", "true", "yes"} if raw is not None else ask_yes_no("use testnet?", default=True)

    try:
        b = make("hyperliquid", private_key=private_key, account_address=account_address, testnet=testnet)
        bal = b.balances()
    except Exception as e:
        c.print(f"[red]✗ {explain_login_error('hyperliquid', e)}[/red]")
        sys.exit(1)

    keychain.save("hyperliquid", {"private_key": private_key, "account_address": account_address, "testnet": testnet})
    keychain.set_default("hyperliquid")
    c.print(f"[green]✓ stored.[/green] hyperliquid {'(testnet)' if testnet else '(mainnet)'} account [bold]{b.account_id}[/bold] · NAV ${bal.nav:,.2f}")


def _login_paper() -> None:
    starting = env_value("PAPER_STARTING_CASH") or ask_text("starting cash", default="100000")
    keychain.save("paper", {"starting_cash": starting})
    keychain.set_default("paper")
    b = make("paper", starting_cash=Decimal(starting))
    bal = b.balances()
    c.print(f"[green]✓ paper book ready.[/green] cash ${bal.cash:,.2f}")


@main.command()
@_BROKER_OPT
@click.pass_context
def logout(ctx: click.Context, broker_opt: str | None) -> None:
    """Clear stored creds for a broker."""
    broker = broker_opt or ctx.obj.get("broker")
    if not broker:
        broker = _prompt_choice("broker to forget", choices=list(SUPPORTED), default=SUPPORTED[0])
    keychain.clear(broker)
    if keychain.get_default() == broker:
        keychain.clear_default()
    c.print(f"[green]✓ creds cleared for {broker}.[/green]")


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


@main.command(name="paper-reset")
def paper_reset() -> None:
    """Reset the paper broker book to its starting cash."""
    from .brokers.paper import Paper
    Paper().reset()
    c.print("[green]✓ paper book reset.[/green]")


@main.command()
@_BROKER_OPT
@click.option("--creds-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Load credentials from a JSON or KEY=VALUE file (headless).")
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
    c.print(f"Market: [bold]{ms.status}[/bold]" + (f"  ·  closes in {ms.minutes_to_close} min" if ms.minutes_to_close is not None else ""))

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
@click.option("--dry-run", is_flag=True, help="Preview only — never sends orders.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirm prompt (auto-execute). Required for unattended runs.")
@click.option("--threshold", default=None, type=float, help="Drift threshold (fraction of NAV). Default 0.04.")
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Read the target CSV from a file instead of stdin.")
@click.option("--csv-url", default=None, help="Fetch the target CSV from a URL instead of stdin.")
@click.option("--creds-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Load credentials from a JSON or KEY=VALUE file (headless).")
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False), default=None, help="Config file for defaults (TOML).")
@click.option("--max-notional", type=float, default=None, help="Refuse if gross buys exceed this dollar amount.")
@click.option("--max-stale-hours", type=float, default=None, help="Refuse if the CSV's `# asof:` time is older than this.")
@click.option("--notify-url", default=None, help="Webhook (Discord/Slack/generic) to ping on execute.")
@click.option("--force", is_flag=True, help="Run even if identical targets were already executed today.")
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON instead of tables.")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output (for cron logs).")
@click.pass_context
def rebalance(
    ctx: click.Context,
    broker_opt: str | None,
    dry_run: bool,
    yes: bool,
    threshold: float | None,
    csv_file: str | None,
    csv_url: str | None,
    creds_file: str | None,
    config_path: str | None,
    max_notional: float | None,
    max_stale_hours: float | None,
    notify_url: str | None,
    force: bool,
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
    csv_file = config.pick(csv_file, cfg, "csv_file")
    csv_url = config.pick(csv_url, cfg, "csv_url")
    creds_file = config.pick(creds_file, cfg, "creds_file")
    max_notional = config.pick(max_notional, cfg, "max_notional")
    max_stale_hours = config.pick(max_stale_hours, cfg, "max_stale_hours")
    notify_url = config.pick(notify_url, cfg, "notify_url")
    quiet = bool(config.pick(True if quiet else None, cfg, "quiet", False))

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

    say(f"[green]✓ loaded {len(targets)} targets.[/green]")

    b = _load_broker(broker)
    bal = retry.with_retry(b.balances)
    pos = retry.with_retry(b.positions)
    universe = sorted({tg.ticker for tg in targets} | set(pos.keys()))
    say(f"Quoting {len(universe)} symbols via {b.name}...", style="dim")
    quotes = retry.with_retry(lambda: b.quote(universe))
    for tk, p in pos.items():
        quotes.setdefault(tk, p.price)

    preview = build_preview(
        targets=targets, positions=pos, nav=bal.nav, cash=bal.cash,
        buying_power=bal.buying_power, quotes=quotes,
        drift_threshold=Decimal(str(threshold)),
    )

    # Extra safety cap on top of the engine's own checks.
    cap_msg = safety.check_max_notional(preview.orders, Decimal(str(max_notional)) if max_notional else None)
    if cap_msg:
        preview.blockers.append(cap_msg)

    # Idempotency: same targets already done today?
    fp = runstate.fingerprint(b.name, b.account_id, targets)
    duplicate = runstate.already_done(fp) and not force

    # In JSON mode the single payload carries everything (orders, warnings,
    # blockers, dry_run, duplicate); decide exit purely on those flags so we
    # never print a second JSON object.
    if json_out:
        _emit_json(b, preview, dry_run=dry_run, duplicate=duplicate)
        if preview.has_blockers:
            sys.exit(1)
        if dry_run or not preview.orders or duplicate:
            return
        if not yes:
            print(json.dumps({"error": "refusing to execute without --yes in JSON/non-interactive mode"}))
            sys.exit(1)
        sent, failed, results = _execute(b, preview)
        if sent > 0 and failed == 0:
            runstate.record(fp)  # only mark done on clean success, so a partial run can re-complete
        notifications.notify(
            notifications.format_summary(b.name, b.account_id, sent, failed, preview.orders),
            notify_url=notify_url,
        )
        print(json.dumps({"executed": True, "sent": sent, "failed": failed, "results": results}, default=str))
        return

    _render_preview(preview, b.name, b.account_id, ms)

    if preview.has_blockers:
        _fail("blockers present — refusing to execute.")
    if not preview.orders:
        say("[green]Nothing to do — portfolio within drift on every ticker.[/green]")
        return
    if dry_run:
        say("[yellow]--dry-run set, exiting without sending orders.[/yellow]")
        return
    if duplicate:
        say("[yellow]Identical targets already executed today — skipping (use --force to override).[/yellow]")
        return
    if not yes:
        if not sys.stdin.isatty():
            _fail("refusing to execute without --yes in non-interactive mode.")
        if not Confirm.ask(f"\nExecute [bold]{len(preview.orders)}[/bold] orders on [bold]{b.name}[/bold]?", default=False):
            say("[red]Cancelled.[/red]")
            sys.exit(0)

    sent, failed, _ = _execute(b, preview)
    if sent > 0 and failed == 0:
        runstate.record(fp)  # only mark done on clean success, so a partial run can re-complete

    # Notify (best-effort, never raises).
    summary = notifications.format_summary(b.name, b.account_id, sent, failed, preview.orders)
    channels = notifications.notify(summary, notify_url=notify_url)
    if channels and not quiet:
        say(f"[dim]notified: {', '.join(channels)}[/dim]")


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
        c.print(f"Gross target exposure: [bold]{gross * 100:.0f}%[/bold] ([bold]{gross:.2f}x[/bold] leverage — uses margin)")
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
        c.print(f"[yellow]⚠ {w}[/yellow]")
    for b in preview.blockers:
        c.print(f"[red]✗ {b}[/red]")


def _execute(broker, preview):
    total = len(preview.orders)
    sent = 0
    failed = 0
    results = []
    for i, o in enumerate(preview.orders, 1):
        say(f"[{i}/{total}] {o.ticker} {o.side.value} {o.quantity:.2f} @ MKT ...", end=" ")
        try:
            # NOT retried: a market order is not idempotent. If submission
            # times out after the broker accepted it, a retry would double
            # the fill. On a transient error we report and move on; the
            # drift gate self-corrects on the next run.
            result = broker.place_market(o, dry_run=False)
        except Exception as e:
            result = {"status": "error", "reason": str(e), "ticker": o.ticker}
        status = result.get("status", "?")
        if status in ("error", "skipped"):
            failed += 1
            say(f"[red]{status.upper()}[/red] {result.get('reason', '')}")
        else:
            sent += 1
            say(f"[green]{status.upper()}[/green]  id={result.get('order_id', '?')}")
        results.append(result)
        fill_log.append({"event": "order", "broker": broker.name, **result, "side": o.side.value, "quantity": float(o.quantity)})

    # Always show a one-line summary (except in JSON mode) so even --quiet
    # cron runs leave a trace.
    if not _JSON:
        c.print(f"[bold]Done.[/bold] {broker.name}: sent {sent}, failed {failed}")
    return sent, failed, results


def _rebalance_one(b, targets, *, threshold: float, max_notional, dry_run: bool, force: bool) -> dict:
    """Run the full rebalance pipeline for one already-built broker.

    No interactive prompts, no rich rendering — returns a result dict.
    Shared by the `multi` command (and safe to reuse elsewhere). Reuses
    the same `_execute` (single-submit, no order retry) and idempotency
    rules as the interactive `rebalance`.
    """
    bal = retry.with_retry(b.balances)
    pos = retry.with_retry(b.positions)
    universe = sorted({t.ticker for t in targets} | set(pos.keys()))
    quotes = retry.with_retry(lambda: b.quote(universe))
    for tk, p in pos.items():
        quotes.setdefault(tk, p.price)

    preview = build_preview(
        targets=targets, positions=pos, nav=bal.nav, cash=bal.cash,
        buying_power=bal.buying_power, quotes=quotes,
        drift_threshold=Decimal(str(threshold)),
    )
    cap = safety.check_max_notional(preview.orders, Decimal(str(max_notional)) if max_notional else None)
    if cap:
        preview.blockers.append(cap)

    fp = runstate.fingerprint(b.name, b.account_id, targets)
    duplicate = runstate.already_done(fp) and not force

    result = {
        "broker": b.name, "account": b.account_id, "nav": str(bal.nav),
        "orders": len(preview.orders), "sent": 0, "failed": 0,
        "warnings": preview.warnings, "blockers": preview.blockers, "status": "preview",
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

    sent, failed, _ = _execute(b, preview)
    if sent > 0 and failed == 0:
        runstate.record(fp)
    result.update(sent=sent, failed=failed, status="executed" if failed == 0 else "partial")
    return result


@main.command()
@click.option("--config", "config_path", type=click.Path(exists=True, dir_okay=False), required=True, help="Multi-account config (TOML with [[account]] tables).")
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Target CSV file (overrides config csv_file/url).")
@click.option("--csv-url", default=None, help="Target CSV URL (overrides config).")
@click.option("--dry-run", is_flag=True, help="Preview every account, send nothing.")
@click.option("--yes", "-y", is_flag=True, help="Required to actually execute (multi never prompts).")
@click.option("--force", is_flag=True, help="Run even if identical targets were already executed today.")
@click.option("--json", "json_out", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--quiet", "-q", is_flag=True, help="Minimal output.")
def multi(config_path, csv_file, csv_url, dry_run, yes, force, json_out, quiet):
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
    max_notional = cfg.get("max_notional")
    max_stale_hours = cfg.get("max_stale_hours")
    notify_url = cfg.get("notify_url")
    csv_file = csv_file or cfg.get("csv_file")
    csv_url = csv_url or cfg.get("csv_url")

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
            r = _rebalance_one(b, targets, threshold=threshold, max_notional=max_notional, dry_run=dry_run, force=force)
        except Exception as e:
            r = {"broker": broker, "status": "error", "reason": str(e)}
        r["name"] = label
        results.append(r)

    executed = any(r.get("status") in ("executed", "partial") for r in results)
    if executed and notify_url:
        lines = [f"msts-trader multi · {len(results)} accounts"]
        for r in results:
            lines.append(f"  {r.get('name')}: {r.get('status')} ({r.get('sent', 0)} sent, {r.get('failed', 0)} failed)")
        notifications.notify("\n".join(lines), notify_url=notify_url)

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
        color = "green" if st in ("executed", "dry-run", "nothing-to-do") else "yellow" if st in ("duplicate", "partial") else "red"
        detail = f" {r['reason']}" if r.get("reason") else ""
        table.add_row(r.get("name", "?"), r.get("broker", "—"), f"[{color}]{st}[/{color}]{detail}", str(r.get("orders", "—")), str(r.get("sent", "—")), str(r.get("failed", "—")))
    c.print(table)
    if any(r.get("status") in ("error", "blocked", "partial") for r in results):
        sys.exit(1)


@main.command()
@_BROKER_OPT
@click.option("--creds-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Load credentials from a file first.")
@click.pass_context
def doctor(ctx: click.Context, broker_opt: str | None, creds_file: str | None) -> None:
    """Health check: credentials, connectivity, market status, a sample quote."""
    if creds_file:
        _load_creds_file_or_exit(creds_file)

    ms = market_status()
    c.print(f"Market: [bold]{ms.status}[/bold]" + (f" · closes in {ms.minutes_to_close} min" if ms.minutes_to_close is not None else ""))

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
