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

import sys
from decimal import Decimal

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm  # used for the post-preview Y/N (works fine in all terminals)
from rich.table import Table

from . import __version__, fill_log, keychain
from .brokers import SUPPORTED, BrokerError, make
from .creds_file import CredsFileError, broker_kwargs_from_env, load_into_env
from .csv_parser import CSVParseError, parse_csv
from .diff import build_preview
from .login_errors import explain_login_error
from .market_hours import market_status
from .models import Side
from .prompts import ask_secret, ask_text, ask_yes_no, env_value

c = Console()


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
            return resp.read().decode("utf-8")
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
@click.pass_context
def status(ctx: click.Context, broker_opt: str | None, creds_file: str | None) -> None:
    """Show account NAV, positions, market status. No orders."""
    if creds_file:
        _load_creds_file_or_exit(creds_file)
    broker = _resolve_broker_name(ctx, broker_opt)
    b = _load_broker(broker)
    bal = b.balances()
    pos = b.positions()
    ms = market_status()

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
@click.option("--threshold", default=0.04, type=float, show_default=True, help="Drift threshold (fraction of NAV).")
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Read the target CSV from a file instead of stdin.")
@click.option("--csv-url", default=None, help="Fetch the target CSV from a URL instead of stdin.")
@click.option("--creds-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Load credentials from a JSON or KEY=VALUE file (headless).")
@click.pass_context
def rebalance(
    ctx: click.Context,
    broker_opt: str | None,
    dry_run: bool,
    yes: bool,
    threshold: float,
    csv_file: str | None,
    csv_url: str | None,
    creds_file: str | None,
) -> None:
    """Paste a ticker,weight CSV → preview → confirm → execute.

    Manual: run with no flags, paste the CSV, confirm with y.
    Headless: --broker NAME --creds-file creds.json --csv-file targets.csv --yes
    (or use --csv-url and exported env vars). No prompts, no keychain needed.
    """
    if creds_file:
        _load_creds_file_or_exit(creds_file)
    broker = _resolve_broker_name(ctx, broker_opt)

    ms = market_status()
    if broker != "paper":
        if ms.status == "closed" and not dry_run:
            c.print(f"[red]Market closed. Next open: {ms.next_open}.[/red]")
            sys.exit(2)
        if ms.status in ("premarket", "afterhours") and not dry_run:
            c.print(f"[red]Market in {ms.status} session — v1 only supports RTH market orders.[/red]")
            sys.exit(2)

    if csv_file and csv_url:
        c.print("[red]✗ pass only one of --csv-file / --csv-url.[/red]")
        sys.exit(1)
    if csv_file:
        with open(csv_file, encoding="utf-8") as f:
            csv_text = f.read()
    elif csv_url:
        csv_text = _fetch_url_or_exit(csv_url)
    else:
        c.print("\n[bold cyan]Paste CSV (ticker,weight), then Ctrl+D (Unix) or Ctrl+Z+Enter (Windows):[/bold cyan]")
        csv_text = sys.stdin.read()

    try:
        targets = parse_csv(csv_text)
    except CSVParseError as e:
        c.print(f"[red]✗ CSV parse error:[/red] {e}")
        sys.exit(1)

    c.print(f"[green]✓ loaded {len(targets)} targets.[/green]")

    b = _load_broker(broker)
    bal = b.balances()
    pos = b.positions()
    universe = sorted({tg.ticker for tg in targets} | set(pos.keys()))
    c.print(f"Quoting {len(universe)} symbols via {b.name}...", style="dim")
    quotes = b.quote(universe)
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
    )

    _render_preview(preview, b.name, b.account_id, ms)

    if preview.has_blockers:
        c.print("[red]✗ blockers present — refusing to execute.[/red]")
        sys.exit(1)
    if not preview.orders:
        c.print("[green]Nothing to do — portfolio within drift on every ticker.[/green]")
        return
    if dry_run:
        c.print("[yellow]--dry-run set, exiting without sending orders.[/yellow]")
        return
    if not yes and not Confirm.ask(f"\nExecute [bold]{len(preview.orders)}[/bold] orders on [bold]{b.name}[/bold]?", default=False):
        c.print("[red]Cancelled.[/red]")
        sys.exit(0)

    _execute(b, preview)


def _render_preview(preview, broker_name: str, account_id: str, ms) -> None:
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


def _execute(broker, preview) -> None:
    total = len(preview.orders)
    sent = 0
    failed = 0
    for i, o in enumerate(preview.orders, 1):
        c.print(f"[{i}/{total}] {o.ticker} {o.side.value} {o.quantity:.2f} @ MKT ...", end=" ")
        try:
            result = broker.place_market(o, dry_run=False)
        except Exception as e:
            result = {"status": "error", "reason": str(e), "ticker": o.ticker}
        status = result.get("status", "?")
        if status in ("error", "skipped"):
            failed += 1
            c.print(f"[red]{status.upper()}[/red] {result.get('reason', '')}")
        else:
            sent += 1
            c.print(f"[green]{status.upper()}[/green]  id={result.get('order_id', '?')}")
        fill_log.append({"event": "order", "broker": broker.name, **result, "side": o.side.value, "quantity": float(o.quantity)})

    c.print(f"\n[bold]Done.[/bold]  sent: {sent}  ·  failed: {failed}  ·  log: {fill_log.log_dir()}")


if __name__ == "__main__":
    main()
