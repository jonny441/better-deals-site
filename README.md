# Better Deals

A free, self-updating personal deal finder for ZIP 43213. Every morning GitHub
runs `scripts/update.py`, reads weekly-ad prices from Flipp, compares net prices
per unit, and updates the static site in `docs/`.

No API keys, paid services, frameworks, or AI are used by the updater.

## Update the data

- `data/watchlist.json`: items and matching rules
- `data/baseline.csv`: regular prices used for BUY/ok/skip verdicts
- `data/manual_offers.csv`: app coupons, Ibotta, and Fetch offers
- `data/stores.json`: store types

After editing a file on GitHub, choose **Commit changes**. The next scheduled
run will refresh the site. To refresh immediately, open **Actions**, choose
**Update deals**, choose **Run workflow**, then choose the green **Run workflow**
button.

## Test locally

```powershell
python -m unittest discover -s tests -v
python scripts/update.py
```

The updater uses only the Python standard library.
