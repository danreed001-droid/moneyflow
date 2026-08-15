#!/usr/bin/env python3
"""
Cross-asset "money flow" snapshot: SPY/ES futures, US 10Y yield, DXY, Gold, Bitcoin.

Meant to run on GitHub Actions (or any machine with normal internet access) --
NOT inside a locked-down sandbox with a network allowlist.

Writes two files to the repo each run:
    latest.txt   - plain-text report (unchanged format from before)
    index.html   - self-contained visual snapshot (3H / 3D / 30D bars per
                   asset + a plain-English flow summary), so the repo always
                   has an up-to-date visual you can open directly or serve
                   via GitHub Pages -- no external tooling required to view it.

Env vars (all optional):
    NTFY_TOPIC   - if set, posts the report to https://ntfy.sh/<topic> as a push notification.
                   Subscribe to the same topic in the ntfy app (iOS/Android) or at ntfy.sh/<topic>.
"""

import os
import sys
import html
import urllib.request
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency. Run: pip install yfinance")

ASSETS = [
    ("ES=F", "S&P 500 futures (ES=F)", "S&P 500 futures"),
    ("^TNX", "US 10-Year Treasury yield", "US 10-Year Treasury yield"),
    ("DX-Y.NYB", "US Dollar Index (DXY)", "US Dollar Index"),
    ("GC=F", "Gold futures (GC=F)", "Gold futures"),
    ("BTC-USD", "Bitcoin (BTC-USD)", "Bitcoin"),
]

# "since previous close" ladder -- used for the headline statement.
THRESHOLDS = {
    "ES=F":     [(0.1, "Flat"), (0.3, "Mild"), (0.8, "Medium"), (float("inf"), "Heavy")],
    "DX-Y.NYB": [(0.1, "Flat"), (0.2, "Mild"), (0.5, "Medium"), (float("inf"), "Heavy")],
    "GC=F":     [(0.2, "Flat"), (0.5, "Mild"), (1.5, "Medium"), (float("inf"), "Heavy")],
    "BTC-USD":  [(0.5, "Flat"), (1.5, "Mild"), (3.0, "Medium"), (float("inf"), "Heavy")],
}
TNX_BPS_THRESHOLDS = [(2, "Flat"), (4, "Mild"), (8, "Medium"), (float("inf"), "Heavy")]

# ~3h ladder (15m bars, 12 back) -- tighter than the "since previous close" one.
THRESHOLDS_3H = {
    "ES=F":     [(0.05, "Flat"), (0.15, "Mild"), (0.4, "Medium"), (float("inf"), "Heavy")],
    "DX-Y.NYB": [(0.05, "Flat"), (0.12, "Mild"), (0.3, "Medium"), (float("inf"), "Heavy")],
    "GC=F":     [(0.1, "Flat"), (0.3, "Mild"), (0.8, "Medium"), (float("inf"), "Heavy")],
    "BTC-USD":  [(0.3, "Flat"), (0.8, "Mild"), (1.8, "Medium"), (float("inf"), "Heavy")],
}
TNX_BPS_THRESHOLDS_3H = [(1, "Flat"), (2.5, "Mild"), (5, "Medium"), (float("inf"), "Heavy")]

# 3-day ladder -- roughly 2-3x the 3h ladder above.
THRESHOLDS_3D = {
    "ES=F":     [(0.3, "Flat"), (0.8, "Mild"), (2.0, "Medium"), (float("inf"), "Heavy")],
    "DX-Y.NYB": [(0.2, "Flat"), (0.5, "Mild"), (1.2, "Medium"), (float("inf"), "Heavy")],
    "GC=F":     [(0.4, "Flat"), (1.0, "Mild"), (2.5, "Medium"), (float("inf"), "Heavy")],
    "BTC-USD":  [(1.0, "Flat"), (3.0, "Mild"), (6.0, "Medium"), (float("inf"), "Heavy")],
}
TNX_BPS_THRESHOLDS_3D = [(4, "Flat"), (8, "Mild"), (15, "Medium"), (float("inf"), "Heavy")]

# 30-day ladder -- much larger cumulative moves expected.
THRESHOLDS_30D = {
    "ES=F":     [(1.0, "Flat"), (3.0, "Mild"), (7.0, "Medium"), (float("inf"), "Heavy")],
    "DX-Y.NYB": [(0.7, "Flat"), (2.0, "Mild"), (4.5, "Medium"), (float("inf"), "Heavy")],
    "GC=F":     [(1.5, "Flat"), (4.0, "Mild"), (9.0, "Medium"), (float("inf"), "Heavy")],
    "BTC-USD":  [(4.0, "Flat"), (10.0, "Mild"), (20.0, "Medium"), (float("inf"), "Heavy")],
}
TNX_BPS_THRESHOLDS_30D = [(10, "Flat"), (20, "Mild"), (40, "Medium"), (float("inf"), "Heavy")]

# (flow target name, word when money flows IN, word when money flows OUT).
# ^TNX's inversion (yield UP -> bond price DOWN -> OUT of bonds) is handled
# entirely in the change>0/change<0 branches in build_report(), not here --
# keep this tuple in the same shape as every other asset.
FLOW_WORDS = {
    "ES=F":     ("equities", "into", "out of"),
    "DX-Y.NYB": ("the dollar", "into", "out of"),
    "GC=F":     ("gold", "into", "out of"),
    "BTC-USD":  ("BTC", "into", "out of"),
    "^TNX":     ("bonds", "into", "out of"),
}

TF_ORDER = ["3h", "3d", "30d"]
TF_LABELS_SHORT = {"3h": "3 hours", "3d": "3 days", "30d": "30 days"}
TF_COL_LABELS = {"3h": "3H", "3d": "3D", "30d": "30D"}
MAG_TO_WIDTH = {"Flat": 0, "Mild": 34, "Medium": 62, "Heavy": 90}


def magnitude(abs_move, ladder):
    for cutoff, label in ladder:
        if abs_move < cutoff:
            return label
    return ladder[-1][1]


def fetch_intraday(ticker, interval="15m", period="1d"):
    try:
        hist = yf.Ticker(ticker).history(period=period, interval=interval)
        return hist if len(hist) else None
    except Exception as e:
        print(f"  [warn] intraday fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def fetch_daily(ticker, period="90d"):
    """Daily bars, far enough back to safely index 3 and 30 sessions ago for
    every asset in ASSETS -- including BTC, which trades every calendar day."""
    try:
        hist = yf.Ticker(ticker).history(period=period, interval="1d")
        return hist if len(hist) else None
    except Exception as e:
        print(f"  [warn] daily fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def flow_direction(ticker, raw_direction):
    """Map a raw up/down/flat price/yield direction to in/out/flat money-flow
    language, applying the bond inversion for ^TNX."""
    if raw_direction == "flat":
        return "flat"
    if ticker == "^TNX":
        return "out" if raw_direction == "up" else "in"
    return "in" if raw_direction == "up" else "out"


def compute_window_metric(ticker, c_now, c_then, pct_ladder, bps_ladder):
    """Return a dict {val_str, mag, direction, flow} for one timeframe, or
    None if c_now/c_then aren't usable."""
    if c_now is None or c_then is None:
        return None
    if ticker == "^TNX":
        change = c_now - c_then
        bps = change * 100
        mag = magnitude(abs(bps), bps_ladder)
        direction = "flat" if mag == "Flat" else ("up" if bps > 0 else "down")
        return {
            "val_str": f"{bps:+.1f}bps",
            "mag": mag,
            "direction": direction,
            "flow": flow_direction(ticker, direction),
        }
    else:
        if not c_then:
            return None
        pct = (c_now - c_then) / c_then * 100
        mag = magnitude(abs(pct), pct_ladder)
        direction = "flat" if mag == "Flat" else ("up" if pct > 0 else "down")
        return {
            "val_str": f"{pct:+.2f}%",
            "mag": mag,
            "direction": direction,
            "flow": flow_direction(ticker, direction),
        }


def build_report():
    """Returns (report_text, assets) where `assets` is a list of structured
    dicts consumed by render_html()."""
    lines = []
    flow_in, flow_out, flow_flat = [], [], []
    assets = []
    now = datetime.now(timezone.utc)

    for ticker, label, short_name in ASSETS:
        t = yf.Ticker(ticker)
        last_price = prev_close = None
        try:
            fi = t.fast_info
            last_price = fi["last_price"]
            prev_close = fi["previous_close"]
        except Exception:
            pass

        hist = fetch_intraday(ticker, interval="15m", period="1d")
        if last_price is None or prev_close is None:
            if hist is None or len(hist) < 2:
                lines.append(f"{label}: DATA UNAVAILABLE")
                assets.append({
                    "ticker": ticker, "label": label, "short_name": short_name,
                    "unavailable": True,
                })
                continue
            last_price = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[0]

        change = last_price - prev_close
        pct = (change / prev_close) * 100 if prev_close else 0.0

        # --- per-window structured metrics (3h / 3d / 30d) ---
        windows = {}
        if hist is not None and len(hist) >= 4:
            closes = hist["Close"]
            three_hr_ago = closes.iloc[max(0, len(closes) - 12)]
            windows["3h"] = compute_window_metric(
                ticker, closes.iloc[-1], three_hr_ago,
                THRESHOLDS_3H.get(ticker), TNX_BPS_THRESHOLDS_3H,
            )

        daily_hist = fetch_daily(ticker)
        if daily_hist is not None:
            closes_d = daily_hist["Close"]
            if len(closes_d) > 3:
                windows["3d"] = compute_window_metric(
                    ticker, closes_d.iloc[-1], closes_d.iloc[-4],
                    THRESHOLDS_3D.get(ticker), TNX_BPS_THRESHOLDS_3D,
                )
            if len(closes_d) > 30:
                windows["30d"] = compute_window_metric(
                    ticker, closes_d.iloc[-1], closes_d.iloc[-31],
                    THRESHOLDS_30D.get(ticker), TNX_BPS_THRESHOLDS_30D,
                )

        extra_tags = ""
        for tf in TF_ORDER:
            m = windows.get(tf)
            if m:
                extra_tags += f" [{tf}: {m['val_str']}, {m['mag']}, {m['direction']}]"

        if ticker == "^TNX":
            bps = change * 100
            mag = magnitude(abs(bps), TNX_BPS_THRESHOLDS)
            target, into_word, outof_word = FLOW_WORDS[ticker]
            if mag == "Flat":
                statement = "little change / roughly flat"
                since_prev_flow = "flat"
            elif change > 0:
                # yield UP -> bond price DOWN -> money flows OUT of bonds
                statement = f"money flowing {outof_word} {target} -- {mag}"
                since_prev_flow = "out"
            else:
                # yield DOWN -> bond price UP -> money flows INTO bonds
                statement = f"money flowing {into_word} {target} -- {mag}"
                since_prev_flow = "in"
            lines.append(f"{label}: {last_price:.3f}% ({change:+.3f}, {bps:+.1f}bps) -- {statement}{extra_tags}")
            bucket = flow_flat if mag == "Flat" else (flow_out if change > 0 else flow_in)
            bucket.append(f"bonds ({mag.lower()})")
            value_str = f"{last_price:.3f}%"
            delta_str = f"{'▲' if change >= 0 else '▼'} {abs(change):.3f} · {change:+.1f}bps".replace(f"{change:+.1f}bps", f"{bps:+.1f}bps")
        else:
            mag = magnitude(abs(pct), THRESHOLDS[ticker])
            target, into_word, outof_word = FLOW_WORDS[ticker]
            if mag == "Flat":
                statement = "little change / roughly flat"
                since_prev_flow = "flat"
            elif change > 0:
                statement = f"money flowing {into_word} {target} -- {mag}"
                since_prev_flow = "in"
            else:
                statement = f"money flowing {outof_word} {target} -- {mag}"
                since_prev_flow = "out"
            lines.append(f"{label}: {last_price:,.2f} ({change:+,.2f}, {pct:+.2f}%) -- {statement}{extra_tags}")
            bucket = flow_flat if mag == "Flat" else (flow_in if change > 0 else flow_out)
            bucket.append(f"{target} ({mag.lower()})")
            value_str = f"{last_price:,.2f}"
            delta_str = f"{'▲' if change >= 0 else '▼'} {abs(change):,.2f} · {pct:+.2f}%"

        assets.append({
            "ticker": ticker,
            "label": label,
            "short_name": short_name,
            "unavailable": False,
            "value_str": value_str,
            "delta_str": delta_str,
            "since_prev": {"pct": pct, "mag": mag, "flow": since_prev_flow, "statement": statement},
            "windows": windows,
        })

    report = []
    report.append("Cross-asset money flow snapshot")
    report.extend(lines)
    report.append("")
    report.append(f"In: {', '.join(flow_in) if flow_in else 'none'}.")
    report.append(f"Out: {', '.join(flow_out) if flow_out else 'none'}.")
    if flow_flat:
        report.append(f"Flat: {', '.join(flow_flat)}.")
    report.append(f"As of: {now.strftime('%Y-%m-%d %H:%M UTC')} (yfinance/Yahoo real-time feed).")
    report.append("Automated informational snapshot, not financial advice.")
    return "\n".join(report), assets, now


# --------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------

def join_windows(tfs):
    names = [TF_LABELS_SHORT[tf] for tf in tfs]
    if len(names) == 1:
        return f"the last {names[0]}"
    if len(names) == 2:
        return f"the last {names[0]} and {names[1]}"
    return "the last " + ", ".join(names[:-1]) + f" and {names[-1]}"


def describe_flow_across_windows(flows):
    """flows: {"3h": "in"/"out"/"flat"/None, "3d": ..., "30d": ...} ->
    a plain-English sentence contrasting the windows, e.g.
    'Flat over the last 3 hours, but flowing in over the last 3 days and 30 days.'"""
    available = [(tf, flows[tf]) for tf in TF_ORDER if flows.get(tf) is not None]
    missing = [tf for tf in TF_ORDER if flows.get(tf) is None]
    if not available:
        return "No timeframe data available this run."

    groups = []
    for tf, val in available:
        if groups and groups[-1][0] == val:
            groups[-1][1].append(tf)
        else:
            groups.append([val, [tf]])

    def phrase(val):
        return {"in": "flowing in", "out": "flowing out", "flat": "flat"}[val]

    if len(groups) == 1:
        val, tfs = groups[0]
        if val == "flat":
            sentence = f"Flat across {join_windows(tfs)} — no real signal"
        else:
            sentence = f"{phrase(val).capitalize()} consistently across {join_windows(tfs)}"
    else:
        parts = []
        for i, (val, tfs) in enumerate(groups):
            clause = f"{phrase(val)} over {join_windows(tfs)}"
            if i == 0:
                clause = clause[0].upper() + clause[1:]
            parts.append(clause)
        sentence = (f"{parts[0]}, but {parts[1]}" if len(parts) == 2
                    else "; ".join(parts[:-1]) + f", and {parts[-1]}")

    if missing:
        sentence += f" ({' and '.join(TF_LABELS_SHORT[tf] for tf in missing)} data unavailable)"

    return sentence + "."


def build_overall_summary(assets):
    mixed, consistent_in, consistent_out, no_signal = [], [], [], []
    for a in assets:
        if a.get("unavailable"):
            continue
        available = [v["flow"] for v in a["windows"].values() if v]
        distinct = set(available)
        if len(distinct) <= 1:
            only = next(iter(distinct)) if distinct else None
            if only == "in":
                consistent_in.append(a["short_name"])
            elif only == "out":
                consistent_out.append(a["short_name"])
            elif only == "flat":
                no_signal.append(a["short_name"])
        else:
            mixed.append(a["short_name"])

    parts = []
    if mixed:
        verb = "show" if len(mixed) > 1 else "shows"
        parts.append(f"{', '.join(mixed)} {verb} a different flow direction between the short- and long-term windows this run")
    if consistent_in:
        verb = "are" if len(consistent_in) > 1 else "is"
        parts.append(f"{', '.join(consistent_in)} {verb} flowing in consistently across every window available")
    if consistent_out:
        verb = "are" if len(consistent_out) > 1 else "is"
        parts.append(f"{', '.join(consistent_out)} {verb} flowing out consistently across every window available")
    if no_signal:
        verb = "show" if len(no_signal) > 1 else "shows"
        parts.append(f"{', '.join(no_signal)} {verb} no real signal in any window")

    if not parts:
        return "No cross-timeframe data available this run."
    return "; ".join(parts) + "."


def biggest_mover(assets):
    """Largest abs since-prev-close % across ES=F, DXY, GC=F, BTC-USD (skip
    ^TNX, which is bps not %). Returns the asset dict, or None on a tie/no data."""
    candidates = [a for a in assets if not a.get("unavailable") and a["ticker"] != "^TNX"]
    if not candidates:
        return None
    candidates.sort(key=lambda a: abs(a["since_prev"]["pct"]), reverse=True)
    if len(candidates) > 1 and abs(candidates[0]["since_prev"]["pct"]) == abs(candidates[1]["since_prev"]["pct"]):
        return None
    return candidates[0]


def render_bar(direction_flow, mag):
    """Return the HTML for one mini 3-way bar-track (out | baseline | in)."""
    width = MAG_TO_WIDTH.get(mag, 0)
    out_w = width if direction_flow == "out" else 0
    in_w = width if direction_flow == "in" else 0
    dot = '<div class="tf-flat-dot"></div>' if direction_flow == "flat" else ""
    return (
        '<div class="tf-track">'
        f'<div class="tf-half tf-half-out"><div class="tf-fill out" style="width:{out_w}%"></div></div>'
        f'<div class="tf-baseline">{dot}</div>'
        f'<div class="tf-half tf-half-in"><div class="tf-fill in" style="width:{in_w}%"></div></div>'
        '</div>'
    )


def render_row(asset, badge=False):
    esc = html.escape
    name = esc(asset["short_name"])
    ticker = esc(asset["ticker"]) if asset["ticker"] != "^TNX" else ""
    badge_html = '<span class="badge">★ Biggest mover</span>' if badge else ""

    tf_cols = []
    flows_for_sentence = {}
    for tf in TF_ORDER:
        m = asset["windows"].get(tf)
        col_label = TF_COL_LABELS[tf]
        if m:
            flows_for_sentence[tf] = m["flow"]
            bar_html = render_bar(m["flow"], m["mag"])
            caption = f'{esc(m["val_str"])} · {esc(m["mag"])}'
            tf_cols.append(
                f'<div class="tf-col"><div class="tf-label">{col_label}</div>{bar_html}'
                f'<div class="tf-caption">{caption}</div></div>'
            )
        else:
            flows_for_sentence[tf] = None
            bar_html = render_bar("flat", "Flat")
            tf_cols.append(
                f'<div class="tf-col"><div class="tf-label">{col_label}</div>{bar_html}'
                f'<div class="tf-na">n/a</div></div>'
            )

    sentence = esc(describe_flow_across_windows(flows_for_sentence))
    dot_class = "flat"  # neutral dot; the sentence itself carries the direction detail

    tip = f'{name} {esc(asset["value_str"])}, {esc(asset["delta_str"])} since previous close. {sentence}'

    return f'''      <div class="row" data-tip="{tip}">
        <div class="row-top">
          <div class="asset-id">
            <span class="asset-name">{name}</span>
            <span class="asset-ticker">{ticker}</span>
            {badge_html}
          </div>
          <div class="asset-price">
            <span class="price-value">{esc(asset["value_str"])}</span>
            <span class="price-delta">{esc(asset["delta_str"])}</span>
          </div>
        </div>
        <div class="timeframe-bars">
{"".join(tf_cols)}
        </div>
        <div class="row-bottom">
          <span class="flow-tag"><span class="dot {dot_class}"></span>{sentence}</span>
        </div>
      </div>
'''


def render_table_row(asset):
    esc = html.escape
    cells = [esc(asset["short_name"]), esc(asset["value_str"])]
    sp = asset["since_prev"]
    cells.append(f'{sp["pct"]:+.2f}% ({sp["mag"]})' if asset["ticker"] != "^TNX"
                 else f'{sp["pct"]:+.3f} ({sp["mag"]})')
    for tf in TF_ORDER:
        m = asset["windows"].get(tf)
        cells.append(f'{esc(m["val_str"])} ({esc(m["mag"])})' if m else "n/a")
    tds = "".join(f'<td class="num">{c}</td>' for c in cells[1:])
    return f'<tr><td>{cells[0]}</td>{tds}</tr>'


CSS = """
  :root { color-scheme: light; }
  .viz-root {
    --surface-1: #fcfcfb; --page-plane: #f9f9f7; --text-primary: #0b0b0b;
    --text-secondary: #52514e; --text-muted: #898781; --gridline: #e1e0d9;
    --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --div-in: #2a78d6; --div-in-track: #cde2fb; --div-out: #e34948;
    --div-out-track: #f8d9d9; --div-neutral: #f0efec; --status-good: #0ca30c;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) .viz-root {
      color-scheme: dark; --surface-1: #1a1a19; --page-plane: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --div-in: #3987e5; --div-in-track: #184f95; --div-out: #e66767;
      --div-out-track: #6b2323; --div-neutral: #383835; --status-good: #0ca30c;
    }
  }
  :root[data-theme="dark"] .viz-root {
    color-scheme: dark; --surface-1: #1a1a19; --page-plane: #0d0d0d;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
    --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
    --div-in: #3987e5; --div-in-track: #184f95; --div-out: #e66767;
    --div-out-track: #6b2323; --div-neutral: #383835; --status-good: #0ca30c;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--page-plane); font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  .viz-root { background: var(--page-plane); min-height: 100vh; padding: 28px 16px 48px; display: flex; justify-content: center; }
  .card { width: 100%; max-width: 760px; background: var(--surface-1); border: 1px solid var(--border); border-radius: 16px; padding: 28px 26px 22px; }
  .card-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 4px; }
  h1 { margin: 0; font-size: 19px; font-weight: 650; color: var(--text-primary); letter-spacing: -0.01em; }
  .subtitle { margin: 6px 0 0; font-size: 13px; color: var(--text-secondary); line-height: 1.5; max-width: 46ch; }
  .theme-toggle { flex: none; border: 1px solid var(--border); background: transparent; color: var(--text-secondary); border-radius: 8px; padding: 6px 10px; font-size: 12px; cursor: pointer; font-family: inherit; }
  .theme-toggle:hover { color: var(--text-primary); border-color: var(--text-muted); }
  .timestamp { margin-top: 14px; font-size: 12px; color: var(--text-muted); }
  .legend { display: flex; align-items: center; gap: 18px; margin: 20px 0 6px; font-size: 12px; color: var(--text-secondary); flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .dot.out { background: var(--div-out); } .dot.in { background: var(--div-in); } .dot.flat { background: var(--baseline); }
  .rows { margin-top: 8px; }
  .row { padding: 16px 0; border-top: 1px solid var(--gridline); position: relative; }
  .row:first-child { border-top: none; }
  .row-top { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
  .asset-id { display: flex; align-items: baseline; gap: 7px; min-width: 0; flex-wrap: wrap; }
  .asset-name { font-size: 14px; font-weight: 600; color: var(--text-primary); }
  .asset-ticker { font-size: 11px; color: var(--text-muted); }
  .badge { margin-left: 6px; font-size: 10.5px; font-weight: 600; color: var(--status-good); border: 1px solid currentColor; border-radius: 999px; padding: 1px 7px 2px; white-space: nowrap; }
  .asset-price { text-align: right; flex: none; }
  .price-value { font-size: 15px; font-weight: 600; color: var(--text-primary); }
  .price-delta { display: block; font-size: 11.5px; color: var(--text-secondary); margin-top: 1px; }
  .row-bottom { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-top: 10px; font-size: 12px; }
  .flow-tag { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); }
  .flow-tag .dot { width: 7px; height: 7px; flex: none; margin-top: 2px; }
  .row[data-tip]:hover::after { content: attr(data-tip); position: absolute; left: 0; right: 0; bottom: -6px; transform: translateY(100%); background: var(--text-primary); color: var(--surface-1); font-size: 11.5px; line-height: 1.5; padding: 8px 10px; border-radius: 8px; z-index: 5; box-shadow: 0 6px 18px rgba(0,0,0,0.18); }
  .note { margin-top: 18px; padding-top: 14px; border-top: 1px solid var(--gridline); font-size: 11.5px; color: var(--text-muted); line-height: 1.6; }
  .blurb { margin-top: 14px; padding: 14px 16px; background: var(--div-neutral); border-radius: 10px; font-size: 12.5px; color: var(--text-secondary); line-height: 1.6; }
  .blurb strong { color: var(--text-primary); }
  details.table-toggle { margin-top: 10px; }
  details.table-toggle summary { cursor: pointer; font-size: 12px; color: var(--text-secondary); user-select: none; }
  table.data-table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 11.5px; }
  table.data-table th, table.data-table td { text-align: left; padding: 6px 7px; border-bottom: 1px solid var(--gridline); color: var(--text-secondary); font-variant-numeric: tabular-nums; }
  table.data-table th { color: var(--text-muted); font-weight: 600; font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.03em; }
  table.data-table td.num { color: var(--text-primary); }
  .timeframe-bars { display: flex; gap: 12px; margin-top: 4px; }
  .tf-col { flex: 1; min-width: 0; }
  .tf-label { font-size: 9.5px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 5px; text-align: center; }
  .tf-track { position: relative; display: flex; align-items: center; height: 14px; }
  .tf-half { width: 50%; height: 12px; display: flex; align-items: center; position: relative; }
  .tf-half-out { justify-content: flex-end; padding-right: 1px; } .tf-half-in { justify-content: flex-start; padding-left: 1px; }
  .tf-fill { height: 12px; min-width: 0; }
  .tf-fill.out { background: var(--div-out); border-radius: 3px 0 0 3px; }
  .tf-fill.in { background: var(--div-in); border-radius: 0 3px 3px 0; }
  .tf-baseline { position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--baseline); transform: translateX(-0.5px); }
  .tf-flat-dot { position: absolute; left: 50%; top: 50%; width: 6px; height: 6px; border-radius: 50%; background: var(--baseline); transform: translate(-50%, -50%); }
  .tf-caption { font-size: 10px; color: var(--text-secondary); margin-top: 5px; text-align: center; }
  .tf-na { font-size: 10px; color: var(--text-muted); font-style: italic; text-align: center; margin-top: 5px; }
"""

SCRIPT = """
  const btn = document.getElementById('themeToggle');
  const root = document.documentElement;
  function syncLabel() {
    const explicit = root.getAttribute('data-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const isDark = explicit ? explicit === 'dark' : prefersDark;
    btn.textContent = isDark ? 'Light mode' : 'Dark mode';
  }
  btn.addEventListener('click', () => {
    const current = root.getAttribute('data-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const currentlyDark = current ? current === 'dark' : prefersDark;
    root.setAttribute('data-theme', currentlyDark ? 'light' : 'dark');
    syncLabel();
  });
  syncLabel();
"""


def render_html(assets, now):
    esc = html.escape
    as_of = now.strftime("%Y-%m-%d %H:%M UTC")
    available_assets = [a for a in assets if not a.get("unavailable")]
    mover = biggest_mover(available_assets)

    rows_html = "".join(
        render_row(a, badge=(mover is not None and a is mover))
        for a in available_assets
    )
    table_rows = "".join(render_table_row(a) for a in available_assets)
    summary = esc(build_overall_summary(available_assets))

    missing_30d = [a["short_name"] for a in available_assets if "30d" not in a["windows"]]
    note_extra = ""
    if missing_30d:
        note_extra = f" 30-day data was unavailable this run for: {esc(', '.join(missing_30d))}."

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cross-Asset Money Flow — As of {esc(as_of)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="viz-root">
  <div class="card">
    <div class="card-head">
      <div>
        <h1>Cross-asset money flow</h1>
        <p class="subtitle">Where money is moving right now across equities, bonds, the dollar, gold and bitcoin — over the last 3 hours, 3 days, and 30 days.</p>
      </div>
      <button class="theme-toggle" id="themeToggle" type="button">Dark mode</button>
    </div>
    <div class="timestamp">As of {esc(as_of)} · yfinance / Yahoo real-time feed</div>
    <div class="legend">
      <div class="legend-item"><span class="dot out"></span>Money flowing out</div>
      <div class="legend-item"><span class="dot flat"></span>Flat</div>
      <div class="legend-item"><span class="dot in"></span>Money flowing in</div>
    </div>
    <div class="rows">
{rows_html}    </div>

    <div class="blurb"><strong>Across all three windows:</strong> {summary}</div>

    <details class="table-toggle">
      <summary>Show data table</summary>
      <table class="data-table">
        <thead><tr><th>Asset</th><th>Value</th><th>Since prev close</th><th>3H</th><th>3D</th><th>30D</th></tr></thead>
        <tbody>
{table_rows}
        </tbody>
      </table>
    </details>
    <p class="note">Automated informational snapshot from the moneyflow GitHub Actions feed — not financial advice.{note_extra}</p>
  </div>
</div>
<script>{SCRIPT}</script>
</body>
</html>
"""


def send_ntfy(text, topic):
    url = f"https://ntfy.sh/{topic}"
    req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST",
                                  headers={"Title": "Money Flow Snapshot"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"ntfy: posted, status {resp.status}")
    except Exception as e:
        print(f"ntfy: FAILED to post: {e}", file=sys.stderr)


def main():
    report, assets, now = build_report()
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Money Flow Snapshot\n\n```\n" + report + "\n```\n")

    # Plain-text report (unchanged format), read by e.g. a Claude scheduled
    # task that fetches the public raw URL without needing GitHub API/auth.
    with open("latest.txt", "w") as f:
        f.write(report + "\n")

    # Self-contained HTML visual, always up to date in the repo. Name it
    # index.html so it also works out of the box if you enable GitHub Pages
    # (Settings -> Pages -> Deploy from branch -> main -> / (root)).
    with open("index.html", "w") as f:
        f.write(render_html(assets, now))

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        send_ntfy(report, topic)
    else:
        print("[info] NTFY_TOPIC not set -- skipping push notification.")


if __name__ == "__main__":
    main()
