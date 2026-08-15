#!/usr/bin/env python3
"""
Cross-asset "money flow" snapshot: SPY/ES futures, US 10Y yield, DXY, Gold, Bitcoin.

Meant to run on GitHub Actions (or any machine with normal internet access) --
NOT inside a locked-down sandbox with a network allowlist.

Env vars (all optional):
    NTFY_TOPIC   - if set, posts the report to https://ntfy.sh/<topic> as a push notification.
                   Subscribe to the same topic in the ntfy app (iOS/Android) or at ntfy.sh/<topic>.
"""

import os
import sys
import urllib.request
from datetime import datetime, timezone

try:
    import yfinance as yf
except ImportError:
    sys.exit("Missing dependency. Run: pip install yfinance")

ASSETS = [
    ("ES=F", "S&P 500 futures (ES=F)"),
    ("^TNX", "US 10-Year Treasury yield"),
    ("DX-Y.NYB", "US Dollar Index (DXY)"),
    ("GC=F", "Gold futures (GC=F)"),
    ("BTC-USD", "Bitcoin (BTC-USD)"),
]

# "since previous close" ladder -- used for the headline statement (unchanged).
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
# NOTE on ^TNX: the tuple word order matches every other asset here (2nd =
# "into" word, 3rd = "out of" word). The inversion for bonds -- yield UP
# means bond price DOWN means money flows OUT -- is handled entirely in the
# change>0/change<0 branches below, not by scrambling this tuple. (An earlier
# version of this file swapped the words *here* instead, which double-
# inverted the logic and produced "flowing into bonds" on days the yield
# rose -- the opposite of what should be reported. Keep this tuple in the
# same shape as the others.)
FLOW_WORDS = {
    "ES=F":     ("equities", "into", "out of"),
    "DX-Y.NYB": ("the dollar", "into", "out of"),
    "GC=F":     ("gold", "into", "out of"),
    "BTC-USD":  ("BTC", "into", "out of"),
    "^TNX":     ("bonds", "into", "out of"),
}


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
    """Daily bars, far enough back to safely index 3 and 30 sessions ago
    for every asset in ASSETS -- including BTC, which trades every calendar
    day, so 90 calendar days comfortably covers 31+ session bars for the
    weekday-only tickers too."""
    try:
        hist = yf.Ticker(ticker).history(period=period, interval="1d")
        return hist if len(hist) else None
    except Exception as e:
        print(f"  [warn] daily fetch failed for {ticker}: {e}", file=sys.stderr)
        return None


def change_tag(ticker, label, c_now, c_then, pct_ladder, bps_ladder):
    """Build a ' [label: +X%, Magnitude, direction]' tag (bps for ^TNX),
    or '' if either value is missing/unusable. `direction` here is purely
    the raw price/yield direction (up/down/flat) -- NOT translated into
    "into"/"out of" language, so the caller/consumer is responsible for
    applying the correct asset-specific in/out mapping (see FLOW_WORDS
    comment above for the ^TNX inversion)."""
    if c_now is None or c_then is None:
        return ""
    if ticker == "^TNX":
        change = c_now - c_then
        bps = change * 100
        mag = magnitude(abs(bps), bps_ladder)
        direction = "flat" if mag == "Flat" else ("up" if bps > 0 else "down")
        return f" [{label}: {bps:+.1f}bps, {mag}, {direction}]"
    else:
        if not c_then:
            return ""
        pct = (c_now - c_then) / c_then * 100
        mag = magnitude(abs(pct), pct_ladder)
        direction = "flat" if mag == "Flat" else ("up" if pct > 0 else "down")
        return f" [{label}: {pct:+.2f}%, {mag}, {direction}]"


def build_report():
    lines = []
    flow_in, flow_out, flow_flat = [], [], []
    now = datetime.now(timezone.utc)

    for ticker, label in ASSETS:
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
                continue
            last_price = hist["Close"].iloc[-1]
            prev_close = hist["Close"].iloc[0]

        change = last_price - prev_close
        pct = (change / prev_close) * 100 if prev_close else 0.0

        extra_tags = ""

        # --- 3h tag: magnitude + direction from 15m bars, ~12 back ---
        if hist is not None and len(hist) >= 4:
            closes = hist["Close"]
            three_hr_ago = closes.iloc[max(0, len(closes) - 12)]
            extra_tags += change_tag(
                ticker, "3h", closes.iloc[-1], three_hr_ago,
                THRESHOLDS_3H.get(ticker), TNX_BPS_THRESHOLDS_3H,
            )

        # --- 3-day / 30-day tags ---
        daily_hist = fetch_daily(ticker)
        if daily_hist is not None:
            closes_d = daily_hist["Close"]
            if len(closes_d) > 3:
                extra_tags += change_tag(
                    ticker, "3d", closes_d.iloc[-1], closes_d.iloc[-4],
                    THRESHOLDS_3D.get(ticker), TNX_BPS_THRESHOLDS_3D,
                )
            if len(closes_d) > 30:
                extra_tags += change_tag(
                    ticker, "30d", closes_d.iloc[-1], closes_d.iloc[-31],
                    THRESHOLDS_30D.get(ticker), TNX_BPS_THRESHOLDS_30D,
                )

        if ticker == "^TNX":
            bps = change * 100
            mag = magnitude(abs(bps), TNX_BPS_THRESHOLDS)
            target, into_word, outof_word = FLOW_WORDS[ticker]
            if mag == "Flat":
                statement = "little change / roughly flat"
            elif change > 0:
                # yield UP -> bond price DOWN -> money flows OUT of bonds
                statement = f"money flowing {outof_word} {target} -- {mag}"
            else:
                # yield DOWN -> bond price UP -> money flows INTO bonds
                statement = f"money flowing {into_word} {target} -- {mag}"
            lines.append(f"{label}: {last_price:.3f}% ({change:+.3f}, {bps:+.1f}bps) -- {statement}{extra_tags}")
            bucket = flow_flat if mag == "Flat" else (flow_out if change > 0 else flow_in)
            bucket.append(f"bonds ({mag.lower()})")
        else:
            mag = magnitude(abs(pct), THRESHOLDS[ticker])
            target, into_word, outof_word = FLOW_WORDS[ticker]
            if mag == "Flat":
                statement = "little change / roughly flat"
            elif change > 0:
                statement = f"money flowing {into_word} {target} -- {mag}"
            else:
                statement = f"money flowing {outof_word} {target} -- {mag}"
            lines.append(f"{label}: {last_price:,.2f} ({change:+,.2f}, {pct:+.2f}%) -- {statement}{extra_tags}")
            bucket = flow_flat if mag == "Flat" else (flow_in if change > 0 else flow_out)
            bucket.append(f"{target} ({mag.lower()})")

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
    return "\n".join(report)


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
    report = build_report()
    print(report)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write("## Money Flow Snapshot\n\n```\n" + report + "\n```\n")

    # Write the latest report to a file in the repo so it can be read back
    # (e.g. by a Claude scheduled task fetching the public raw URL) without
    # needing any GitHub API/auth access.
    with open("latest.txt", "w") as f:
        f.write(report + "\n")

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        send_ntfy(report, topic)
    else:
        print("[info] NTFY_TOPIC not set -- skipping push notification.")


if __name__ == "__main__":
    main()
