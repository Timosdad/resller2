# -*- coding: utf-8 -*-
"""
Daily Equip-Bid ROI Scraper
===========================
Pulls all active auctions within a radius of a given ZIP code, estimates
resale value via eBay Sold-Items API, and flags lots that can clear a
user-defined profit floor (default: $100) after Equip-Bid buyer premium.

Outputs a Markdown report and optionally emails or posts to Slack.

Required environment variables
------------------------------
EBAY_APP_ID          – eBay developer AppID for Finding/Sell APIs
ZIP_CODE             – search center (e.g. 64012)
RADIUS_MILES         – search radius (default 25)
PROFIT_FLOOR         – min. expected profit (default 100)
OUTPUT_PATH          – where to save daily report (default ./reports)
SENDGRID_API_KEY     – (optional) for email delivery
SLACK_WEBHOOK_URL    – (optional) to push message to Slack

Usage
-----
$ python equipbid_roi_scraper.py    # run once
Set up a cron job or GitHub Actions workflow to run daily.
"""
from __future__ import annotations
import os
import re
import pathlib
import datetime as dt
import time
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
EBAY_HEADERS = {
    "X-EBAY-SOA-OPERATION-NAME": "findCompletedItems",
    "X-EBAY-SOA-SERVICE-VERSION": "1.13.0",
    "X-EBAY-SOA-REQUEST-DATA-FORMAT": "JSON",
    "X-EBAY-SOA-SECURITY-APPNAME": os.getenv("EBAY_APP_ID", ""),
}
EQUIPBID_BASE = "https://www.equip-bid.com"
BUYER_PREMIUM = 0.12  # 12% default – adjust if auction varies


@dataclass
class Lot:
    auction_id: str
    lot_id: str
    title: str
    current_bid: float
    url: str
    est_resale: Optional[float] = None
    profit: Optional[float] = None
    ceiling_bid: Optional[float] = None

    def compute_profit_and_ceiling(self, profit_floor: float = 100.0) -> None:
        if self.est_resale is None:
            return
        self.ceiling_bid = max(0, (self.est_resale - profit_floor) / (1 + BUYER_PREMIUM))
        self.profit = self.est_resale - (self.current_bid * (1 + BUYER_PREMIUM))


def fetch_auction_list(zip_code: str, radius: int = 25) -> List[Dict[str, Any]]:
    """Return JSON list of active Equip-Bid auctions in radius."""
    url = (
        f"{EQUIPBID_BASE}/auction/list?sort_field=end&affiliate=0&closing=&"
        f"closing_mask=&distance_radius={radius}&distance_zip={zip_code}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    auctions = []
    for a in soup.select("div.auction-card > a[href*='/auction/']"):
        href = a.get("href")
        if not href:
            continue
        auction_id = re.search(r"/auction/(\d+)", href)
        if auction_id:
            auctions.append({"id": auction_id.group(1), "url": EQUIPBID_BASE + href})
    return auctions


def fetch_lots(auction_id: str) -> List[Lot]:
    """Scrape all lots for a given auction ID."""
    lots: List[Lot] = []
    page = 1
    while True:
        url = f"{EQUIPBID_BASE}/auction/{auction_id}?page={page}"
        res = requests.get(url, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        lot_divs = soup.select("div.lot-row")
        if not lot_divs:
            break
        for div in lot_divs:
            title_tag = div.select_one(".lot-title")
            bid_tag = div.select_one(".lot-current-bid")
            lot_href = div.select_one("a[href*='/item/']")
            if title_tag and bid_tag and lot_href:
                lot_id_match = re.search(r"/item/(\d+)", lot_href.get("href"))
                if not lot_id_match:
                    continue
                lot_id = lot_id_match.group(1)
                title = title_tag.get_text(strip=True)
                bid_text = bid_tag.get_text(strip=True)
                bid = float(re.sub(r"[^0-9.]", "", bid_text) or 0)
                lots.append(
                    Lot(
                        auction_id=auction_id,
                        lot_id=lot_id,
                        title=title,
                        current_bid=bid,
                        url=f"{EQUIPBID_BASE}{lot_href.get('href')}",
                    )
                )
        if not soup.select_one("ul.pagination li.next:not(.disabled)"):
            break
        page += 1
    return lots


def estimate_resale_price(title: str, lookback_days: int = 90) -> Optional[float]:
    """Query eBay sold listings to estimate median sale price for given title."""
    if not EBAY_HEADERS["X-EBAY-SOA-SECURITY-APPNAME"]:
        print("[WARN] eBay AppID not provided; skipping resale estimate.")
        return None
    params = {
        "paginationInput": {"entriesPerPage": 10},
        "itemFilter": [{"name": "SoldItemsOnly", "value": True}],
        "keywords": title,
        "outputSelector": ["SellerInfo"],
        "sortOrder": "EndTimeSoonest",
    }
    body = {"findCompletedItemsRequest": params}
    r = requests.post(EBAY_FINDING_URL, headers=EBAY_HEADERS, json=body, timeout=15)
    if not r.ok:
        return None
    data = r.json()
    try:
        items = data["findCompletedItemsResponse"][0]["searchResult"][0]["item"]
        prices = [
            float(it["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"])
            for it in items
        ]
        prices = [p for p in prices if p > 10]
        if not prices:
            return None
        prices.sort()
        mid = len(prices) // 2
        return prices[mid] if len(prices) % 2 else sum(prices[mid - 1 : mid + 1]) / 2
    except (KeyError, IndexError):
        return None


def build_report(lots: List[Lot], profit_floor: float = 100.0) -> str:
    """Return Markdown report string with lots meeting profit criteria."""
    header = (
        f"# Equip-Bid ROI Report – {dt.date.today().isoformat()}\n\n"
        f"Only lots that can clear **${profit_floor}+** net profit after a 12% buyer premium.\n\n"
    )
    table_header = (
        "| Auction/Lot | Item | Bid | Est. Resale | Est. Profit | Max Bid |\n"
        "|---|---|---|---|---|---|\n"
    )
    rows = []
    for lot in lots:
        if lot.profit is not None and lot.profit >= profit_floor:
            rows.append(
                f"| {lot.auction_id}/{lot.lot_id} | [{lot.title}]({lot.url}) | $ {lot.current_bid:.2f} | $ {lot.est_resale:.2f} | $ {lot.profit:.2f} | $ {lot.ceiling_bid:.2f} |"
            )
    if not rows:
        rows.append("| – | No lots met the threshold today | – | – | – | – |")
    return header + table_header + "\n".join(rows)


def save_report(md: str, path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    fname = path / f"equipbid_report_{dt.date.today().isoformat()}.md"
    fname.write_text(md, encoding="utf-8")
    return fname


########################## SNIPING LOGIC ##########################

def _parse_end_time(html: str) -> Optional[dt.datetime]:
    """Attempt to extract auction end time from lot HTML."""
    soup = BeautifulSoup(html, "html.parser")
    time_tag = soup.select_one("[data-countdown]") or soup.select_one("[data-end]")
    if time_tag:
        ts = time_tag.get("data-countdown") or time_tag.get("data-end")
        if ts:
            try:
                return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Ends[^\d]*(\d{4}-\d{2}-\d{2}[^\d]*\d{1,2}:\d{2}\s*(?:am|pm)?)", text, re.I)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
        except ValueError:
            pass
    return None


def snipe_bid(lot_url: str, max_bid: float, lead_seconds: int = 3) -> None:
    """Attempt to place a bid seconds before the lot closes."""
    print(f"[INFO] Preparing to snipe {lot_url} with max total ${max_bid:.2f}")
    try:
        resp = requests.get(lot_url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Unable to fetch lot page: {exc}")
        return

    end_time = _parse_end_time(resp.text)
    if not end_time:
        print("[WARN] Could not determine end time; bidding immediately")
        delay = 0
    else:
        now = dt.datetime.utcnow()
        delay = (end_time - dt.timedelta(seconds=lead_seconds) - now).total_seconds()
        if delay < 0:
            delay = 0
    if delay:
        print(f"[INFO] Waiting {delay:.1f} seconds before bidding...")
        time.sleep(delay)

    bid_amount = max_bid / (1 + BUYER_PREMIUM)
    lot_id_match = re.search(r"/item/(\d+)", lot_url)
    lot_id = lot_id_match.group(1) if lot_id_match else "?"
    success = _place_bid(lot_id, bid_amount)
    if success:
        print(f"[INFO] Bid of ${bid_amount:.2f} placed on lot {lot_id}")
    else:
        print("[ERROR] Failed to place bid")


def _place_bid(lot_id: str, amount: float) -> bool:
    """Placeholder HTTP request to submit a bid."""
    print(f"[MOCK] Would bid ${amount:.2f} on lot {lot_id}")
    # Real implementation would authenticate and submit form here
    return True

###################################################################


def main_scan() -> None:
    zip_code = os.getenv("ZIP_CODE", "64012")
    radius = int(os.getenv("RADIUS_MILES", 25))
    profit_floor = float(os.getenv("PROFIT_FLOOR", 100))

    auctions = fetch_auction_list(zip_code, radius)
    print(f"[INFO] Found {len(auctions)} auctions…")

    all_lots: List[Lot] = []
    for auc in auctions:
        lots = fetch_lots(auc["id"])
        print(f"  • Auction {auc['id']}: {len(lots)} lots")
        all_lots.extend(lots)

    for lot in all_lots:
        lot.est_resale = estimate_resale_price(lot.title)
        lot.compute_profit_and_ceiling(profit_floor)

    report_md = build_report(all_lots, profit_floor)
    report_path = save_report(report_md, pathlib.Path(os.getenv("OUTPUT_PATH", "reports")))
    print(f"[INFO] Report saved to {report_path}")

    if os.getenv("SENDGRID_API_KEY"):
        send_email(report_md, str(report_path))
    if os.getenv("SLACK_WEBHOOK_URL"):
        post_to_slack(report_md)


##############  Optional helper stubs  ###################

def send_email(markdown: str, attachment_path: str) -> None:
    """Send the report via SendGrid (placeholder)."""
    print("[MOCK] Email would be sent with report…")


def post_to_slack(markdown: str) -> None:
    """Post the report to Slack via Incoming Webhook (placeholder)."""
    print("[MOCK] Slack message posted…")

##########################################################


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Equip-Bid ROI tool with optional bid sniping"
    )
    parser.add_argument(
        "--snipe",
        nargs=3,
        metavar=("LOT_URL", "MAX_BID", "LEAD_SEC"),
        help="Place a last-second bid at LOT_URL up to MAX_BID with LEAD_SEC seconds lead",
    )
    args = parser.parse_args()
    if args.snipe:
        url, bid_str, lead_str = args.snipe
        try:
            bid_val = float(bid_str)
            lead = int(lead_str)
        except ValueError:
            parser.error("MAX_BID must be a number and LEAD_SEC must be an integer")
        snipe_bid(url, bid_val, lead)
    else:
        main_scan()
