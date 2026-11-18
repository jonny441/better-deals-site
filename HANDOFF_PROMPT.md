# Prompt to paste into ChatGPT (attach the zip with it)

I'm attaching a starter repo called **better-deals-site**. Help me finish it, deploy it on GitHub, and keep it running. I'm not a programmer, so give me exact clicks and commands.

## What it is
A free personal deal-finder for Columbus, OH (ZIP 43213). There's no AI and nothing paid in the update loop. Every day, GitHub Actions runs `scripts/update.py`, which:
1. Searches Flipp weekly ads (`https://backflipp.wishabi.com/flipp/items/search?locale=en-us&postal_code=43213&q=<term>`) for every item in `data/watchlist.json`.
2. Reads the size from the product name and works out the **price per unit** (oz, fl oz, count, load, roll).
3. Subtracts printed rewards (CVS ExtraBucks, Walgreens Register Rewards, DG cash, BOGO) plus any by-hand offers in `data/manual_offers.csv`, which is where app coupons, Ibotta and Fetch go.
4. Ranks every store, tags each one with a store type from `data/stores.json`, and compares the best deal to my regular prices in `data/baseline.csv`. Verdicts: BUY (15%+ under regular), ok, skip.
5. Keeps 180 days of price history in `data/history.json` and flags 90-day lows.
6. Scans all ads, not just my watchlist, for free or near-free items (net $1 or less after rewards).
7. Writes `docs/data.json`. The site at `docs/index.html` (GitHub Pages) has search, category tabs (oral, face, hair, skin, household, free), a store-type filter, a sort, and an all-stores table per item.

## What I need you to do
1. **Deploy:** walk me through creating a public repo, uploading these files (web upload is fine), turning on GitHub Pages (branch `main`, folder `/docs`), and running the "Update deals" workflow once by hand (Actions tab → Run workflow).
2. **Check the first live run.** This code was tested on sample data only, because Flipp couldn't be reached from where it was written. Open the workflow log and `docs/data.json`, confirm the Flipp field names match (`merchant_name`, `name`, `current_price`, `pre_price_text`, `post_price_text`, `sale_story`, `valid_to`, `clean_image_url`), and fix `parse_deal` / `parse_size` in `scripts/update.py` if they don't. If Flipp returns 403 or an empty list from GitHub's servers, suggest the smallest fix, like a different User-Agent or headers.
3. **Tune the matching.** Look at which ad items are matched to each watchlist entry, tighten `must_include` where wrong products sneak in (e.g. kids' toothpaste, travel sizes), and add sizes the regex misses.
4. **Load my real staples.** Replace the starter list in `data/watchlist.json` with my staples (I'll paste them), and fill `data/baseline.csv` with regular prices from Sam's Club, Walmart, Kroger and ALDI (price + size).
5. Keep it simple: standard-library Python only, one static HTML page, no frameworks, no paid services, no API keys.

## Rules for the numbers
- Net = shelf − item coupons − rewards earned − cash back. Per unit = net ÷ units.
- Multi-buy ads like "2/$5" are priced per item.
- A deal only counts as BUY if its net per unit beats the best regular price per unit.

## Files
- `data/watchlist.json`: staples, category, search term, unit, must_include words, free-scan terms
- `data/stores.json`: store name → type (drugstore, dollar store, grocery, big box, warehouse, off-price)
- `data/baseline.csv`: regular prices (`item,store,regular_price,size,unit,checked`)
- `data/manual_offers.csv`: app coupons and cash-back offers I add by hand (`item,store,offer,amount_off,expires,note`)
- `scripts/update.py`: the updater
- `.github/workflows/update.yml`: daily schedule (~6:23 AM ET) plus a manual run button
- `docs/index.html`: the website
