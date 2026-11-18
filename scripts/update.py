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
    "lb": r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:lbs?|pounds?)\b",
    "g": r"(\d+(?:\.\d+)?)(?:\s*-\s*\d+(?:\.\d+)?)?\s*(?:g|grams?)\b",
    "dozen": r"(\d+(?:\.\d+)?)\s*(?:dozen|doz|dz)\b",
}
G_PER_OZ = 28.3495
G_PER_LB = 453.592
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
    elif unit == "lb":
        candidates += _matches(UNIT_PATTERNS["lb"], t)
        candidates += _matches(UNIT_PATTERNS["oz"], t, 1 / 16)
    elif unit == "g":
        candidates += _matches(UNIT_PATTERNS["g"], t)
        candidates += _matches(r"(\d+(?:\.\d+)?)\s*kg\b", t, 1000)
        candidates += _matches(UNIT_PATTERNS["oz"], t, G_PER_OZ)
        candidates += _matches(UNIT_PATTERNS["lb"], t, G_PER_LB)
    elif unit == "dozen":
        candidates += _matches(UNIT_PATTERNS["dozen"], t)
        candidates += _matches(UNIT_PATTERNS["ct"], t, 1 / 12)
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
PER_LB_TEXT = re.compile(r"^\s*(?:/|per\s*)?\s*lbs?\.?\s*$")


def has_word(text, words):
    t = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in words if w)


def compact(a, shelf, off, net, note, types):
    merchant = (a.get("merchant_name") or "?").strip()
    return {"s": merchant, "t": store_type(merchant, types), "n": a.get("name") or "",
            "p": round(shelf, 2), "o": round(off, 2), "net": round(net, 2),
            "note": note, "post": (a.get("post_price_text") or "").strip(),
            "v": (a.get("valid_to") or "")[:10]}


def build(search_fn=flipp_search, pause=1.0):
    wl = load_json(DATA / "watchlist.json", {"items": []})
    stores_cfg = load_json(DATA / "stores.json", {"types": {}})
    types = stores_cfg.get("types", {})
    skip_stores = [x.lower() for x in stores_cfg.get("skip", [])]
    my_stores = stores_cfg.get("my_stores", [])
    zip_code = wl.get("zip", "43213")
    rules = wl.get("rules", {})
    global_ex = rules.get("exclude_everywhere", [])
    cat_ex = rules.get("exclude_by_category", {})
    baseline = load_csv(DATA / "baseline.csv")
    manual = [r for r in load_csv(DATA / "manual_offers.csv")
              if not r.get("expires") or r["expires"] >= date.today().isoformat()]
    history = load_json(HISTORY, {})
    today = date.today().isoformat()

    out_items, free_finds, errors = [], [], []
    catalog = {}
    cache = {}

    def search(term):
        if term in cache:
            return cache[term]
        try:
            ads = search_fn(term, zip_code)
        except Exception as e:  # keep going if one search fails
            errors.append(f"{term}: {e}")
            ads = []
        time.sleep(pause)
        keep = []
        for a in ads:
            m = (a.get("merchant_name") or "").lower()
            if not still_valid(a) or any(x in m for x in skip_stores):
                continue
            if has_word(a.get("name"), global_ex):
                continue
            keep.append(a)
        cache[term] = keep
        return keep

    def add_catalog(a):
        key = ((a.get("merchant_name") or "").strip(), a.get("name"))
        if key in catalog:
            return
        shelf, printed_off, reward, note = parse_deal(a)
        if shelf is None:
            return
        catalog[key] = compact(a, shelf, printed_off + reward,
                               max(shelf - printed_off - reward, 0), note, types)

    for w in wl.get("items", []):
        unit = w["unit"]
        ex_words = list(w.get("must_not_include", [])) + list(cat_ex.get(w["category"], []))
        ads = search(w["search"])

        offers = []
        for a in ads:
            add_catalog(a)
            name = a.get("name") or ""
            if any(k.lower() not in name.lower() for k in w.get("must_include", [])):
                continue
            if has_word(name, ex_words):
                continue
            shelf, printed_off, reward, note = parse_deal(a)
            if shelf is None:
                continue
            merchant = (a.get("merchant_name") or "?").strip()
            mine = [r for r in manual if r["item"].lower() == w["name"].lower()
                    and r["store"].lower() in merchant.lower()]
            manual_off = sum(float(r.get("amount_off") or 0) for r in mine)
            net = max(shelf - printed_off - reward - manual_off, 0)
            post = a.get("post_price_text") or ""
            if unit == "lb" and PER_LB_TEXT.match(post):
                size = 1.0
            else:
                size = parse_size(name + " " + post, unit,
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
                "note": "; ".join(filter(None, [note] + [r["offer"] for r in mine])),
                "valid_to": (a.get("valid_to") or "")[:10],
                "image": a.get("clean_image_url") or a.get("image_url"),
            })
            if shelf > 0 and net <= 0.5:
                free_finds.append({"item": w["name"], **offers[-1]})

        offers.sort(key=lambda o: (o["per_unit"] is None, o["per_unit"] or 0, o["net"]))

        # regular prices: every store row, plus the cheapest per unit
        regular = []
        for b in baseline:
            if b["item"].lower() != w["name"].lower() or b.get("unit", "").lower() != unit.lower():
                continue
            try:
                price, size = float(b["regular_price"]), float(b["size"])
                regular.append({"store": b["store"], "product": b.get("product", ""),
                                "price": price, "size": size,
                                "per_unit": round(price / size, 4), "checked": b.get("checked", "")})
            except (ValueError, ZeroDivisionError, KeyError):
                continue
        regular.sort(key=lambda r: r["per_unit"])
        base_best = regular[0] if regular else None
        target = w.get("target")

        best = next((o for o in offers if o["per_unit"] is not None), offers[0] if offers else None)
        ref = base_best["per_unit"] if base_best else target
        ref_label = "regular price" if base_best else "your target"
        verdict, pct = "no ad deal this week", None
        if best and ref and best["per_unit"]:
            pct = round((1 - best["per_unit"] / ref) * 100)
            verdict = (f"BUY - beats {ref_label}" if pct >= 15 else
                       "ok - small savings" if pct > 0 else f"skip - {ref_label} is cheaper")
        elif best:
            verdict = "ad deal (no price to compare yet)"

        h = [p for p in history.get(w["name"], []) if p["d"] != today]
        if best and best["per_unit"]:
            h.append({"d": today, "pu": best["per_unit"], "store": best["store"]})
        cutoff = (date.today() - timedelta(days=180)).isoformat()
        history[w["name"]] = [p for p in h if p["d"] >= cutoff]
        cutoff90 = (date.today() - timedelta(days=90)).isoformat()
        recent = [p["pu"] for p in history[w["name"]] if p["d"] >= cutoff90]
        low90 = min(recent) if recent else None

        out_items.append({
            "name": w["name"], "category": w["category"], "unit": unit,
            "aliases": w.get("aliases", []), "target": target,
            "best": best, "baseline": base_best, "regular": regular,
            "pct_vs_baseline": pct, "verdict": verdict, "low_90d": low90,
            "beats_target": bool(best and best.get("per_unit") and target and best["per_unit"] <= target),
            "at_90d_low": bool(best and best.get("per_unit") and low90 and best["per_unit"] <= low90 * 1.02),
            "offers": offers[:25],
            "history": history[w["name"]],
        })

    # wider catalog for the shopping-list planner
    for term in wl.get("catalog_terms", []):
        for a in search(term):
            add_catalog(a)

    # freebies across all ads
    deduped = {}
    for find in free_finds:
        key = (find["store"], find["product"])
        if key not in deduped or find["net"] < deduped[key]["net"]:
            deduped[key] = find
    free_finds = list(deduped.values())
    seen = set(deduped)
    for term in wl.get("free_scan_terms", []):
        for a in search(term):
            add_catalog(a)
    for c in catalog.values():
        key = (c["s"], c["n"])
        if c["o"] > 0 and c["net"] <= 1.0 and key not in seen:
            seen.add(key)
            free_finds.append({"item": "(scan)", "store": c["s"], "store_type": c["t"],
                               "product": c["n"], "shelf": c["p"], "printed_off": 0,
                               "reward_back": c["o"], "net": c["net"], "note": c["note"],
                               "valid_to": c["v"]})
    free_finds.sort(key=lambda f: f["net"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "updated": datetime.now().isoformat(timespec="minutes"),
        "zip": zip_code, "my_stores": my_stores,
        "items": out_items, "free": free_finds,
        "guide": load_json(DATA / "guide.json", {}),
        "catalog": sorted(catalog.values(), key=lambda c: c["n"].lower()),
        "errors": errors,
    }, separators=(",", ":")))
    HISTORY.write_text(json.dumps(history, indent=1))
    print(f"wrote {len(out_items)} items, {len(catalog)} catalog ads, "
          f"{len(free_finds)} free/near-free, {len(errors)} errors")
    return 0


if __name__ == "__main__":
    sys.exit(build())
