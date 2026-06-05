"""msts-trader CLI: paste a CSV, preview the rebalance, execute it on Tastytrade.

Subcommands:
  login       — store provider_secret + refresh_token + account_id in OS keychain
  status      — show NAV / positions / market status, no orders
  rebalance   — (default) paste CSV from stdin, preview, prompt, execute
  logout      — clear stored creds
"""
from __future__ import annotations

import sys
from decimal import Decimal

import click
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import __version__, fill_log
from .csv_parser import CSVParseError, parse_csv
from .diff import build_preview
from .keychain import CredsMissingError, clear_creds, load_creds, save_creds
from .market_hours import market_status
from .models import Side
from .tasty import Tasty

c = Console()


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="msts-trader")
@click.pass_context
def main(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(rebalance)


@main.command()
def login() -> None:
    """Store Tastytrade OAuth creds (provider_secret + refresh_token) in OS keychain."""
    c.print(
        Panel.fit(
            "[bold]Tastytrade OAuth setup[/bold]\n\n"
            "1. Sign in at [cyan]https://developer.tastytrade.com[/cyan]\n"
            "2. Create an OAuth application → copy [bold]provider secret[/bold]\n"
            "3. Run their authorization flow → copy [bold]refresh token[/bold]\n"
            "4. Find your [bold]account number[/bold] in Tastytrade dashboard "
            "(or leave blank to auto-pick first account)",
            border_style="cyan",
        )
    )
    provider_secret = Prompt.ask("provider secret", password=True)
    refresh_token = Prompt.ask("refresh token", password=True)
    account_id = Prompt.ask("account id (optional)", default="").strip() or None

    try:
        t = Tasty(provider_secret, refresh_token, account_id)
        bal = t.balances()
    except Exception as e:
        c.print(f"[red]✗ login failed:[/red] {e}")
        sys.exit(1)

    save_creds(provider_secret, refresh_token, account_id or t.account_id)
    c.print(
        f"[green]✓ stored.[/green] account [bold]{t.account_id}[/bold]  ·  "
        f"NAV ${bal.nav:,.2f}  ·  BP ${bal.buying_power:,.2f}"
    )


@main.command()
def logout() -> None:
    """Forget stored creds."""
    clear_creds()
    c.print("[green]✓ creds cleared from keychain.[/green]")


@main.command()
def status() -> None:
    """Show account NAV, positions, market status. No orders."""
    try:
        ps, rt, aid = load_creds()
    except CredsMissingError as e:
        c.print(f"[red]{e}[/red]")
        sys.exit(1)

    t = Tasty(ps, rt, aid)
    bal = t.balances()
    pos = t.positions()
    ms = market_status()

    c.print(
        f"\n[bold]Account[/bold] {t.account_id}  ·  "
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
@click.option("--dry-run", is_flag=True, help="Preview only — never sends orders.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirm prompt (auto-execute).")
@click.option("--threshold", default=0.04, type=float, show_default=True, help="Drift threshold (fraction of NAV).")
@click.option("--csv-file", type=click.Path(exists=True, dir_okay=False), default=None, help="Read CSV from file instead of stdin.")
def rebalance(dry_run: bool, yes: bool, threshold: float, csv_file: str | None) -> None:
    """Default command. Paste a ticker,weight CSV → preview → confirm → execute."""
    try:
        ps, rt, aid = load_creds()
    except CredsMissingError as e:
        c.print(f"[red]{e}[/red]")
        sys.exit(1)

    ms = market_status()
    if ms.status == "closed" and not dry_run:
        c.print(f"[red]Market closed. Next open: {ms.next_open}.[/red]")
        c.print("[yellow]Re-run with --dry-run to preview, or wait until the next session.[/yellow]")
        sys.exit(2)
    if ms.status in ("premarket", "afterhours") and not dry_run:
        c.print(f"[red]Market in {ms.status} session — v1 only supports RTH market orders.[/red]")
        c.print("[yellow]Re-run during RTH (09:30–16:00 ET) or pass --dry-run.[/yellow]")
        sys.exit(2)

    if csv_file:
        with open(csv_file, encoding="utf-8") as f:
            csv_text = f.read()
    else:
        c.print("\n[bold cyan]Paste CSV (ticker,weight), then Ctrl+D (Unix) or Ctrl+Z+Enter (Windows):[/bold cyan]")
        csv_text = sys.stdin.read()

    try:
        targets = parse_csv(csv_text)
    except CSVParseError as e:
        c.print(f"[red]✗ CSV parse error:[/red] {e}")
        sys.exit(1)

    c.print(f"[green]✓ loaded {len(targets)} targets.[/green]")

    t = Tasty(ps, rt, aid)
    bal = t.balances()
    pos = t.positions()
    universe = sorted({tg.ticker for tg in targets} | set(pos.keys()))
    c.print(f"Quoting {len(universe)} symbols...", style="dim")
    quotes = t.quote(universe)
    # supplement with last-known position prices for tickers we already hold
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

    _render_preview(preview, t.account_id, ms)

    if preview.has_blockers:
        c.print("[red]✗ blockers present — refusing to execute.[/red]")
        sys.exit(1)

    if not preview.orders:
        c.print("[green]Nothing to do — portfolio within drift on every ticker.[/green]")
        return

    if dry_run:
        c.print("[yellow]--dry-run set, exiting without sending orders.[/yellow]")
        return

    if not yes and not Confirm.ask(f"\nExecute [bold]{len(preview.orders)}[/bold] orders?", default=False):
        c.print("[red]Cancelled.[/red]")
        sys.exit(0)

    _execute(t, preview)


def _render_preview(preview, account_id: str, ms) -> None:
    c.print(
        f"\n[bold]Account[/bold] {account_id}  ·  "
        f"NAV [green]${preview.nav:,.2f}[/green]  ·  "
        f"cash ${preview.cash:,.2f}  ·  BP ${preview.buying_power:,.2f}"
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
        c.print(f"[yellow]⚠ {w}[/yellow]")
    for b in preview.blockers:
        c.print(f"[red]✗ {b}[/red]")


def _execute(t: Tasty, preview) -> None:
    total = len(preview.orders)
    sent = 0
    failed = 0
    for i, o in enumerate(preview.orders, 1):
        c.print(f"[{i}/{total}] {o.ticker} {o.side.value} {o.quantity:.2f} @ MKT ...", end=" ")
        try:
            result = t.place_market(o, dry_run=False)
        except Exception as e:
            result = {"status": "error", "reason": str(e), "ticker": o.ticker}
        status = result.get("status", "?")
        if status == "error" or status == "skipped":
            failed += 1
            c.print(f"[red]{status.upper()}[/red] {result.get('reason', '')}")
        else:
            sent += 1
            c.print(f"[green]{status.upper()}[/green]  id={result.get('order_id', '?')}")
        fill_log.append({"event": "order", **result, "side": o.side.value, "quantity": float(o.quantity)})

    c.print(f"\n[bold]Done.[/bold]  sent: {sent}  ·  failed: {failed}  ·  log: {fill_log.log_dir()}")


if __name__ == "__main__":
    main()
