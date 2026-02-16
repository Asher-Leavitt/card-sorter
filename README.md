# Card Sorter Control System

A web-based control panel for a physical trading card sorting machine.
Works in **simulation mode** on any laptop, or with **real GPIO** on a Raspberry Pi.

## Quick Start (Laptop / Simulation)

```bash
# 1. Make sure you have Python 3 installed
python3 --version

# 2. Install dependencies
pip install flask requests

# 3. Run the server
python app.py

# 4. Open in your browser
#    → http://localhost:5000
```

You should see:
```
[SIM] RPi.GPIO not found — running in simulation mode

🃏 Card Sorter Control System
   Mode: SIMULATION
   Dashboard:  http://localhost:5000
   Webhook:    http://localhost:5000/webhook
```

**Open http://localhost:5000 in Chrome/Firefox/Edge** — NOT from the file system.

> ⚠️ You cannot open `dashboard.html` directly as a file. It's a Flask
> template that must be served by the Python server.

## What You Can Do in Simulation

- **Search Scryfall** — type a card name, pick a printing, hit "Scan Card"
- **See the card image** — pulled live from Scryfall's API
- **Test sorting rules** — add/edit/reorder rules, see which pile each card goes to
- **Simulate beam break** — block/clear the virtual sensor
- **Motor controls** — forward/reverse/stop (simulated, no real movement)
- **Export CSV** — download all scanned cards as a spreadsheet

## Sorting Rule Fields

| Field            | Type    | Example Values             | Notes                        |
|------------------|---------|----------------------------|------------------------------|
| `price`          | number  | `5`, `0.50`, `100`         | From Delver (USD)            |
| `cmc`            | number  | `0`, `1`, `3`, `7`         | From Scryfall                |
| `rarity`         | string  | `common`, `uncommon`, `rare`, `mythic` | From Scryfall      |
| `color_identity` | array   | `W`, `U`, `B`, `R`, `G`   | Use `contains` operator      |
| `colors`         | array   | `W`, `U`, `B`, `R`, `G`   | Use `contains` operator      |
| `type_line`      | string  | `Creature`, `Instant`, `Enchantment — Aura` | Use `contains`  |
| `name`           | string  | `Lightning Bolt`           | Use `contains` or `==`       |
| `edition`        | string  | `Secret Lair Drop`         | Use `contains`               |
| `keywords`       | array   | `Flying`, `Trample`        | Use `contains`               |
| `finish`         | string  | `regular`, `foil`          | Use `==`                     |

### Operators

| Operator   | Works With      | Example                               |
|------------|-----------------|---------------------------------------|
| `>`        | numbers         | `price > 5`                           |
| `<`        | numbers         | `cmc < 3`                             |
| `>=`       | numbers         | `price >= 1`                          |
| `<=`       | numbers         | `cmc <= 2`                            |
| `==`       | numbers/strings | `rarity == mythic`                    |
| `!=`       | numbers/strings | `finish != foil`                      |
| `contains` | strings/arrays  | `type_line contains Creature`         |

## Deploy to Raspberry Pi Zero 2 W

```bash
# On the Pi:
mkdir card_sorter && cd card_sorter
python3 -m venv venv
source venv/bin/activate
pip install flask requests gunicorn

# Copy app.py, templates/, and rules.json to this folder

# Run:
gunicorn -w 1 -b 0.0.0.0:5000 app:app
```

Then open `http://<pi-ip>:5000` from your laptop browser.

### With Delver Webhooks

1. Run ngrok on the Pi: `ngrok http 5000`
2. Copy the `https://...ngrok-free.dev` URL
3. Paste into Delver → Settings → Webhook Endpoint
4. Scan a card — it appears in your dashboard

## File Structure

```
card-sorter/
├── app.py              # Main server (Flask + GPIO + Scryfall)
├── rules.json          # Sorting rules (editable from UI)
├── templates/
│   └── dashboard.html  # Web dashboard
└── README.md
```
