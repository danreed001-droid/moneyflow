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

THRESHOLDS = {
    "ES=F":     [(0.1, "Flat"), (0.3, "Mild"), (0.8, "Medium"), (float("inf"), "Heavy")],
    "DX-Y.NYB": [(0.1, "Flat"), (0.2, "Mild"), (0.5, "Medium"), (float("inf"), "Heavy")],
    "GC=F":     [(0.2, "Flat"), (0.5, "Mild"), (1.5, "Medium"), (float("inf"), "Heavy")],
    "BTC-USD":  [(0.5, "Flat"), (1.5, "Mild"), (3.0, "Medium"), (float("inf"), "Heavy")],
}
TNX_BPS_THRESHOLDS = [(2, "Flat"), (4, "Mild"), (8, "Medium"), (float("inf"), "Heavy")]

# (flow target name, word when money flows IN, word when money flows OUT)
FLOW_WORDS = {
    "ES=F":     ("equities", "into", "out of"),
    "DX-Y.NYB": ("the dollar", "into", "out of"),
    "GC=F":     ("gold", "into", "out of"),
    "BTC-USD":  ("BTC", "into", "out of"),
    "^TNX":     ("bonds", "out of", "into"),  # yield UP -> OUT of bonds; yield DOWN -> INTO bonds
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

        trend_note = ""
        if hist is not None and len(hist) >= 4:
            closes = hist["Close"]
            three_hr_ago = closes.iloc[max(0, len(closes) - 12)]  # ~3h back at 15m bars
            delta = closes.iloc[-1] - three_hr_ago
            direction = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            trend_note = f" [last ~3h trending {direction}]"

        if ticker == "^TNX":
            bps = change * 100
            mag = magnitude(abs(bps), TNX_BPS_THRESHOLDS)
            target, into_word, outof_word = FLOW_WORDS[ticker]
            if mag == "Flat":
                statement = "little change / roughly flat"
            elif change > 0:
                statement = f"money flowing {outof_word} {target} -- {mag}"
            else:
                statement = f"money flowing {into_word} {target} -- {mag}"
            lines.append(f"{label}: {last_price:.3f}% ({change:+.3f}, {bps:+.1f}bps) -- {statement}{trend_note}")
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
            lines.append(f"{label}: {last_price:,.2f} ({change:+,.2f}, {pct:+.2f}%) -- {statement}{trend_note}")
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

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        send_ntfy(report, topic)
    else:
        print("[info] NTFY_TOPIC not set -- skipping push notification.")


if __name__ == "__main__":
    main()
