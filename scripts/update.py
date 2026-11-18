"""Better Deals updater.

Runs daily on GitHub Actions (no AI, no paid APIs). For each watchlist item it
searches Flipp weekly ads near the ZIP, works out size and price per unit,
subtracts printed rewards (ExtraBucks, Register Rewards, BOGO...), subtracts
manual offers you add by hand, ranks every store, compares to your regular
baseline prices, and keeps a price history. Output: docs/data.json for the site.

Standard library only.
"""
import csv
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "docs" / "data.json"
HISTORY = DATA / "history.json"
FLIPP = "https://backflipp.wishabi.com/flipp/items/search"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/131 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------- size / unit parsing ----------
UNIT_PATTERNS = {
    "fl_oz": r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:fl\.?\s*oz|fluid\s*ounces?)",
    "oz": r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:oz|ounces?)\b",
    "load": r"(\d+)(?:\s*-\s*\d+)?[-\s]*(?:loads?|ld)\b",
    "roll": r"(\d+)(?:\s*-\s*\d+)?[-\s]*(?:(?:single|double|triple|mega|family|giant|huge|regular|big|plus)[-\s]*)*(?:rolls?|rl)\b",
    "ct": r"(\d+)(?:\s*-\s*\d+)?[-\s]*(?:ct|count|pk|pack|pc|pcs)\b",
    "sheet": r"(\d+)(?:\s*-\s*\d+)?[-\s]*sheets?\b",
}
ML_PER_FLOZ = 29.5735


def _matches(pattern, text, multiplier=1.0):
    return [(float(m.group(1)) * multiplier, m.start(), m.end())
            for m in re.finditer(pattern, text)]


def parse_size(text, unit, keywords=()):
    """Read a conservative package size, preferring the size nearest the item name.

    Flipp often puts several eligible products in one name (for example toothpaste
    and mouthwash). Proximity prevents the first unrelated size from being used.
    For ranges such as 16-18 oz, the lower size is used so savings are not
    overstated.
    """
    t = (text or "").lower()
    candidates = []
    if unit == "fl_oz":
        candidates += _matches(UNIT_PATTERNS["fl_oz"], t)
        candidates += _matches(UNIT_PATTERNS["oz"], t)
        candidates += _matches(r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*ml\b", t, 1 / ML_PER_FLOZ)
        candidates += _matches(r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:l|liters?)\b", t, 1000 / ML_PER_FLOZ)
    elif unit == "oz":
        candidates += _matches(UNIT_PATTERNS["oz"], t)
    else:
        pat = UNIT_PATTERNS.get(unit)
        candidates += _matches(pat, t) if pat else []

    if not candidates:
        return None
    positions = [(m.start(), m.end()) for k in keywords
                 for m in re.finditer(re.escape(k.lower()), t) if k]
    if positions:
        def gap(candidate, keyword):
            return max(keyword[0] - candidate[2], candidate[1] - keyword[1], 0)
        value, _, _ = min(candidates, key=lambda c: min(gap(c, p) for p in positions))
    else:
        # In mixed volume listings the largest option is usually the full-size
        # product; range values themselves remain conservative (lower bound).
        value, _, _ = max(candidates, key=lambda c: c[0])
    return round(value, 2)


# ---------- price / reward parsing ----------
def money(s):
    m = re.search(r"\$?\s*(\d+(?:\.\d{1,2})?)", s or "")
    return float(m.group(1)) if m else None


def parse_deal(item):
    """Return (per-item shelf price, printed coupon, reward, note)."""
    price = item.get("current_price")
    pre = (item.get("pre_price_text") or "").lower()
    post = (item.get("post_price_text") or "").lower()
    story = (item.get("sale_story") or "")
    blob = " ".join([pre, post, story.lower(), (item.get("name") or "").lower()])

    try:
        price = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price = money(str(price))

    # "2/$5" style multi-buy
    qty = 1
    m = (re.search(r"^\s*(\d+)\s*/\s*$", pre)
         or re.search(r"\b(\d+)\s*(?:/|for)\s*\$?\s*\d", blob))
    if m and price is not None:
        qty = int(m.group(1))
        price = price / qty

    coupon, reward, notes = 0.0, 0.0, []
    coupon_match = re.search(
        r"\$(\d+(?:\.\d{1,2})?)\s*(?:off|on)\s*(\d+)\b[^+;]{0,50}\bcoupon\b", blob)
    if coupon_match:
        coupon_total, coupon_qty = float(coupon_match.group(1)), int(coupon_match.group(2))
        coupon = coupon_total / max(coupon_qty, 1)
        notes.append(f"${coupon_total:g} on {coupon_qty} printed coupon (${coupon:g}/item)")

    reward_words = r"(?:extrabucks|extra bucks|register rewards?|rr\b|cash back|rewards?|in rewards|dg cash)"
    reward_match = re.search(
        rf"buy\s*(\d+)[^+;]{{0,50}}?get\s*\$(\d+(?:\.\d{{1,2}})?)\s*({reward_words})", blob)
    if reward_match:
        reward_qty, reward_total = int(reward_match.group(1)), float(reward_match.group(2))
        reward = reward_total / max(reward_qty, 1)
        notes.append(f"buy {reward_qty}, get ${reward_total:g} {reward_match.group(3).strip()} (${reward:g}/item)")
    else:
        # Common form: "Spend $20, get $10 ExtraBucks".
        spend_first = re.search(
            rf"spend\s*\$(\d+(?:\.\d{{1,2}})?)[^+;]{{0,50}}?get\s*\$(\d+(?:\.\d{{1,2}})?)\s*({reward_words})", blob)
        if spend_first and price:
            threshold, reward_total = float(spend_first.group(1)), float(spend_first.group(2))
            reward_qty = max(math.ceil(threshold / price), 1)
            reward = reward_total / reward_qty
            notes.append(f"spend ${threshold:g}, get ${reward_total:g} {spend_first.group(3).strip()} (~${reward:g}/item)")
        else:
            generic = re.search(rf"(?:get|earn)?\s*\$(\d+(?:\.\d{{1,2}})?)\s*({reward_words})", blob)
            if generic:
                reward = float(generic.group(1))
                notes.append(f"${reward:g} {generic.group(2).strip()} back")
    if re.search(r"\bbogo\b|buy 1,? get 1 free|buy one,? get one free", blob):
        if price is not None:
            reward += price / 2
        notes.append("BOGO (priced as 2 at half each)")
    elif re.search(r"buy 1,? get 1 50%|bogo 50%", blob):
        if price is not None:
            reward += price / 4
        notes.append("B1G1 50% off (priced as 2)")
    if story and not notes:
        notes.append(story.strip()[:80])
    return price, coupon, reward, "; ".join(notes)


# ---------- helpers ----------
def load_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def load_csv(p):
    try:
        with open(p, newline="") as f:
            return [r for r in csv.DictReader(f)]
    except FileNotFoundError:
        return []


def store_type(name, types):
    n = (name or "").lower()
    for k, v in types.items():
        if k.lower() in n:
            return v
    return "other"


def flipp_search(term, zip_code):
    q = urllib.parse.urlencode({"locale": "en-us", "postal_code": zip_code, "q": term})
    req = urllib.request.Request(f"{FLIPP}?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data.get("items", []) or []


def still_valid(item):
    vt = item.get("valid_to")
    if not vt:
        return True
    try:
        return datetime.fromisoformat(vt.replace("Z", "+00:00")).date() >= date.today()
    except ValueError:
        return True


# ---------- main ----------
def build(search_fn=flipp_search):
    wl = load_json(DATA / "watchlist.json", {"items": []})
    types = load_json(DATA / "stores.json", {"types": {}}).get("types", {})
    zip_code = wl.get("zip", "43213")
    baseline = load_csv(DATA / "baseline.csv")
    manual = [r for r in load_csv(DATA / "manual_offers.csv")
              if not r.get("expires") or r["expires"] >= date.today().isoformat()]
    history = load_json(HISTORY, {})
    today = date.today().isoformat()

    out_items, free_finds, errors = [], [], []

    for w in wl.get("items", []):
        unit = w["unit"]
        try:
            ads = search_fn(w["search"], zip_code)
        except Exception as e:  # keep going if one search fails
            errors.append(f"{w['name']}: {e}")
            ads = []
        time.sleep(1.5)  # be polite

        offers = []
        for a in ads:
            name = a.get("name") or ""
            if not still_valid(a):
                continue
            if any(k.lower() not in name.lower() for k in w.get("must_include", [])):
                continue
            if any(re.search(rf"\b{re.escape(k.lower())}\b", name.lower())
                   for k in w.get("must_not_include", [])):
                continue
            shelf, printed_off, reward, note = parse_deal(a)
            if shelf is None:
                continue
            merchant = a.get("merchant_name") or "?"
            manual_off = sum(float(r.get("amount_off") or 0) for r in manual
                             if r["item"].lower() == w["name"].lower()
                             and r["store"].lower() in merchant.lower())
            manual_notes = [r["offer"] for r in manual
                            if r["item"].lower() == w["name"].lower()
                            and r["store"].lower() in merchant.lower()]
            net = max(shelf - printed_off - reward - manual_off, 0)
            size = parse_size(name + " " + (a.get("post_price_text") or ""), unit,
                              w.get("size_keywords") or w.get("must_include") or [w["name"]])
            if size is not None and size < float(w.get("min_size", 0)):
                continue
            offers.append({
                "store": merchant,
                "store_type": store_type(merchant, types),
                "product": name,
                "shelf": round(shelf, 2),
                "printed_off": round(printed_off, 2),
                "reward_back": round(reward, 2),
                "manual_off": round(manual_off, 2),
                "net": round(net, 2),
                "size": size,
                "per_unit": round(net / size, 4) if size else None,
                "note": "; ".join(filter(None, [note] + manual_notes)),
                "valid_to": (a.get("valid_to") or "")[:10],
                "image": a.get("clean_image_url") or a.get("image_url"),
            })
            if shelf > 0 and net <= 0.5:
                free_finds.append({"item": w["name"], **offers[-1]})

        # rank: per-unit first (when known), then net price
        offers.sort(key=lambda o: (o["per_unit"] is None, o["per_unit"] or 0, o["net"]))

        # baseline: cheapest regular price per unit
        base_rows = [b for b in baseline
                     if b["item"].lower() == w["name"].lower()
                     and b.get("unit", "").lower() == unit.lower()]
        base_best = None
        for b in base_rows:
            try:
                pu = float(b["regular_price"]) / float(b["size"])
            except (ValueError, ZeroDivisionError, KeyError):
                continue
            if base_best is None or pu < base_best["per_unit"]:
                base_best = {"store": b["store"], "per_unit": round(pu, 4),
                             "price": float(b["regular_price"]), "size": float(b["size"])}

        best = next((o for o in offers if o["per_unit"] is not None), offers[0] if offers else None)
        verdict, pct = "no ad deal this week", None
        if best and base_best and best["per_unit"]:
            pct = round((1 - best["per_unit"] / base_best["per_unit"]) * 100)
            verdict = "BUY - beats regular price" if pct >= 15 else (
                "ok - small savings" if pct > 0 else "skip - regular price is cheaper")
        elif best:
            verdict = "ad deal (no baseline yet to compare)"

        # history (keep 180 days of best per-unit)
        h = history.setdefault(w["name"], [])
        if best and best["per_unit"]:
            h = [p for p in h if p["d"] != today]
            h.append({"d": today, "pu": best["per_unit"], "store": best["store"]})
            cutoff = (date.today() - timedelta(days=180)).isoformat()
            history[w["name"]] = [p for p in h if p["d"] >= cutoff]
        low90 = None
        cutoff90 = (date.today() - timedelta(days=90)).isoformat()
        recent = [p["pu"] for p in history.get(w["name"], []) if p["d"] >= cutoff90]
        if recent:
            low90 = min(recent)

        out_items.append({
            "name": w["name"], "category": w["category"], "unit": unit,
            "best": best, "baseline": base_best, "pct_vs_baseline": pct,
            "verdict": verdict, "low_90d": low90,
            "at_90d_low": bool(best and best.get("per_unit") and low90 and best["per_unit"] <= low90 * 1.02),
            "offers": offers[:15],
            "history": history.get(w["name"], []),
        })

    # extra scan: freebies across ALL ads, not just watchlist
    deduped = {}
    for find in free_finds:
        key = (find["store"], find["product"])
        if key not in deduped or find["net"] < deduped[key]["net"]:
            deduped[key] = find
    free_finds = list(deduped.values())
    seen = set(deduped)
    for term in wl.get("free_scan_terms", []):
        try:
            ads = search_fn(term, zip_code)
        except Exception as e:
            errors.append(f"scan {term}: {e}")
            continue
        time.sleep(1.5)
        for a in ads:
            if not still_valid(a):
                continue
            shelf, printed_off, reward, note = parse_deal(a)
            if not shelf or printed_off + reward <= 0:
                continue
            net = max(shelf - printed_off - reward, 0)
            key = (a.get("merchant_name"), a.get("name"))
            if net <= 1.0 and key not in seen:
                seen.add(key)
                free_finds.append({"item": "(scan) " + term, "store": a.get("merchant_name"),
                                   "store_type": store_type(a.get("merchant_name"), types),
                                   "product": a.get("name"), "shelf": round(shelf, 2),
                                   "printed_off": round(printed_off, 2),
                                   "reward_back": round(reward, 2), "net": round(net, 2),
                                   "note": note, "valid_to": (a.get("valid_to") or "")[:10]})
    free_finds.sort(key=lambda f: f["net"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "updated": datetime.now().isoformat(timespec="minutes"),
        "zip": zip_code, "items": out_items, "free": free_finds, "errors": errors,
    }, indent=1))
    HISTORY.write_text(json.dumps(history, indent=1))
    print(f"wrote {len(out_items)} items, {len(free_finds)} free/near-free, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(build())
