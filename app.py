"""
Card Sorter Control System
===========================
Runs on laptop (simulated GPIO) or Raspberry Pi Zero 2 W (real GPIO).
Integrates with Delver webhooks + Scryfall API for card data enrichment.

Setup:
  pip install flask requests
  python app.py

Then open http://localhost:5000 in your browser.
"""

import threading
import time
import json
import os
import requests as http_requests  # renamed to avoid clash with flask.request
from datetime import datetime
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# GPIO ABSTRACTION
# ---------------------------------------------------------------------------

try:
    import RPi.GPIO as GPIO
    SIMULATED = False
    print("[HW] Running on Raspberry Pi — real GPIO active")
except ImportError:
    SIMULATED = True
    print("[SIM] RPi.GPIO not found — running in simulation mode")

    class _FakeGPIO:
        BCM = 11; OUT = 0; IN = 1; HIGH = 1; LOW = 0; PUD_UP = 22

        def __init__(self):
            self._pins = {}
            self._beam_blocked = False

        def setmode(self, m): pass
        def setwarnings(self, f): pass
        def setup(self, pin, d, pull_up_down=None): self._pins[pin] = 0
        def output(self, pin, val): self._pins[pin] = val
        def input(self, pin):
            if pin == 17:
                return self.LOW if self._beam_blocked else self.HIGH
            return self._pins.get(pin, self.HIGH)
        def cleanup(self): self._pins.clear()

    GPIO = _FakeGPIO()


# ---------------------------------------------------------------------------
# PIN ASSIGNMENTS
# ---------------------------------------------------------------------------

STEP_PIN   = 18
DIR_PIN    = 23
SENSOR_PIN = 17

GPIO.setmode(GPIO.BCM)
if not SIMULATED:
    GPIO.setwarnings(False)
GPIO.setup(STEP_PIN, GPIO.OUT)
GPIO.setup(DIR_PIN, GPIO.OUT)
GPIO.setup(SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)


# ---------------------------------------------------------------------------
# SCRYFALL API
# ---------------------------------------------------------------------------

SCRYFALL_CACHE = {}  # in-memory cache: scryfallId -> card data

def fetch_scryfall(scryfall_id):
    """Fetch card data from Scryfall API. Returns dict or None."""
    if not scryfall_id:
        return None

    # Check cache first
    if scryfall_id in SCRYFALL_CACHE:
        return SCRYFALL_CACHE[scryfall_id]

    url = f"https://api.scryfall.com/cards/{scryfall_id}"
    try:
        resp = http_requests.get(url, headers={
            "User-Agent": "CardSorterPi/1.0",
            "Accept": "application/json",
        }, timeout=5)

        if resp.status_code == 200:
            data = resp.json()
            result = {
                "cmc":            data.get("cmc", 0),
                "colors":         data.get("colors", []),
                "color_identity": data.get("color_identity", []),
                "type_line":      data.get("type_line", ""),
                "mana_cost":      data.get("mana_cost", ""),
                "oracle_text":    data.get("oracle_text", ""),
                "power":          data.get("power", ""),
                "toughness":      data.get("toughness", ""),
                "keywords":       data.get("keywords", []),
                "set_name":       data.get("set_name", ""),
                "rarity":         data.get("rarity", ""),
                "image_uri":      "",
                "image_art_crop": "",
            }

            # Handle image URIs (double-faced cards have faces instead)
            image_uris = data.get("image_uris")
            if image_uris:
                result["image_uri"]      = image_uris.get("large", image_uris.get("normal", ""))
                result["image_art_crop"] = image_uris.get("art_crop", "")
            elif data.get("card_faces"):
                face = data["card_faces"][0]
                face_imgs = face.get("image_uris", {})
                result["image_uri"]      = face_imgs.get("large", face_imgs.get("normal", ""))
                result["image_art_crop"] = face_imgs.get("art_crop", "")

            SCRYFALL_CACHE[scryfall_id] = result
            print(f"[SCRYFALL] Fetched: {data.get('name', '?')} — CMC {result['cmc']}, colors {result['colors']}")
            return result
        else:
            print(f"[SCRYFALL] Error {resp.status_code} for {scryfall_id}")
            return None

    except Exception as e:
        print(f"[SCRYFALL] Request failed: {e}")
        return None


def enrich_card(delver_card):
    """Take raw Delver card data + fetch Scryfall enrichment. Returns merged dict."""
    scryfall_id = delver_card.get("scryfallId", "")
    scryfall_data = fetch_scryfall(scryfall_id)

    enriched = {
        # From Delver (always available)
        "name":         delver_card.get("name", "Unknown"),
        "edition":      delver_card.get("edition", ""),
        "editionCode":  delver_card.get("editionCode", ""),
        "number":       delver_card.get("number", ""),
        "rarity":       delver_card.get("rarity", ""),
        "price":        delver_card.get("price", 0),
        "fmtPrice":     delver_card.get("fmtPrice", ""),
        "finish":       delver_card.get("finish", "regular"),
        "cardType":     delver_card.get("cardType", ""),
        "scryfallId":   scryfall_id,
    }

    if scryfall_data:
        # From Scryfall (richer data)
        enriched["cmc"]            = scryfall_data["cmc"]
        enriched["colors"]         = scryfall_data["colors"]
        enriched["color_identity"] = scryfall_data["color_identity"]
        enriched["type_line"]      = scryfall_data["type_line"]
        enriched["mana_cost"]      = scryfall_data["mana_cost"]
        enriched["oracle_text"]    = scryfall_data["oracle_text"]
        enriched["power"]          = scryfall_data["power"]
        enriched["toughness"]      = scryfall_data["toughness"]
        enriched["keywords"]       = scryfall_data["keywords"]
        enriched["image_uri"]      = scryfall_data["image_uri"]
        enriched["image_art_crop"] = scryfall_data["image_art_crop"]
    else:
        # Fallback — use what Delver gives us
        enriched["cmc"]            = 0
        enriched["colors"]         = []
        enriched["color_identity"] = []
        enriched["type_line"]      = delver_card.get("cardType", "")
        enriched["mana_cost"]      = ""
        enriched["oracle_text"]    = ""
        enriched["power"]          = ""
        enriched["toughness"]      = ""
        enriched["keywords"]       = []
        enriched["image_uri"]      = ""
        enriched["image_art_crop"] = ""

    return enriched


# ---------------------------------------------------------------------------
# MOTOR STATE
# ---------------------------------------------------------------------------

class MotorState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.direction = 1
        self.speed = 0.001
        self.steps_taken = 0
        self.steps_target = 0
        self.card_detected = False
        self.stop_on_beam = True

motor = MotorState()


# ---------------------------------------------------------------------------
# SCAN LOG + CURRENT CARD
# ---------------------------------------------------------------------------

scan_log = []
scan_log_lock = threading.Lock()

current_card = {"card": None}  # mutable container so threads can update
current_card_lock = threading.Lock()


# ---------------------------------------------------------------------------
# SORTING RULES
# ---------------------------------------------------------------------------

RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")

DEFAULT_RULES = [
    {"name": "High Value",     "field": "price",          "operator": ">",        "value": 5,    "pile": 1},
    {"name": "Mythics",        "field": "rarity",         "operator": "==",       "value": "mythic", "pile": 2},
    {"name": "Rares",          "field": "rarity",         "operator": "==",       "value": "rare",   "pile": 3},
    {"name": "Blue Cards",     "field": "color_identity", "operator": "contains", "value": "U",  "pile": 4},
    {"name": "Creatures",      "field": "type_line",      "operator": "contains", "value": "Creature", "pile": 5},
]

def load_rules():
    if os.path.exists(RULES_FILE):
        with open(RULES_FILE) as f:
            return json.load(f)
    return list(DEFAULT_RULES)

def save_rules(rules):
    with open(RULES_FILE, "w") as f:
        json.dump(rules, f, indent=2)

def evaluate_rules(card, rules):
    """Return pile number for a card, or 0 (default) if no rule matches."""
    for rule in rules:
        field = rule["field"]
        op = rule["operator"]
        target = rule["value"]
        field_val = card.get(field)

        if field_val is None:
            continue

        # Handle array fields (colors, color_identity, keywords)
        if isinstance(field_val, list):
            if op == "contains":
                if str(target).upper() in [str(v).upper() for v in field_val]:
                    return rule["pile"]
            elif op == "==":
                # Exact match: compare sorted
                target_list = [t.strip().upper() for t in str(target).split(",")]
                field_list = [str(v).upper() for v in field_val]
                if sorted(target_list) == sorted(field_list):
                    return rule["pile"]
            elif op == "len==":
                try:
                    if len(field_val) == int(target):
                        return rule["pile"]
                except (ValueError, TypeError):
                    pass
            continue

        # Coerce types for numeric comparison
        try:
            if isinstance(target, str) and target.replace(".", "", 1).replace("-", "", 1).isdigit():
                target = float(target)
            if isinstance(target, (int, float)) and isinstance(field_val, str):
                field_val = float(field_val)
            if isinstance(field_val, (int, float)) and isinstance(target, str):
                target = float(target)
        except (ValueError, TypeError):
            pass

        match = False
        if op == ">"  : match = field_val > target
        elif op == "<"  : match = field_val < target
        elif op == ">=": match = field_val >= target
        elif op == "<=": match = field_val <= target
        elif op == "==": match = str(field_val).lower() == str(target).lower()
        elif op == "!=": match = str(field_val).lower() != str(target).lower()
        elif op == "contains":
            match = str(target).lower() in str(field_val).lower()

        if match:
            return rule["pile"]

    return 0


# ---------------------------------------------------------------------------
# MOTOR THREAD
# ---------------------------------------------------------------------------

def motor_loop():
    while True:
        with motor.lock:
            running   = motor.running
            direction = motor.direction
            speed     = motor.speed
            stop_beam = motor.stop_on_beam
            target    = motor.steps_target

        if running:
            if direction == 1 and stop_beam and GPIO.input(SENSOR_PIN) == GPIO.LOW:
                with motor.lock:
                    motor.running = False
                    motor.card_detected = True
                print("[MOTOR] Beam broken — card detected, motor stopped")
                continue

            GPIO.output(DIR_PIN, GPIO.HIGH if direction == 1 else GPIO.LOW)
            GPIO.output(STEP_PIN, GPIO.HIGH)
            time.sleep(speed)
            GPIO.output(STEP_PIN, GPIO.LOW)
            time.sleep(speed)

            with motor.lock:
                motor.steps_taken += 1
                if target > 0 and motor.steps_taken >= target:
                    motor.running = False
                    motor.steps_taken = 0
                    motor.steps_target = 0
                    print(f"[MOTOR] Completed {target} steps — stopped")
        else:
            time.sleep(0.01)


motor_thread = threading.Thread(target=motor_loop, daemon=True)
motor_thread.start()


# ---------------------------------------------------------------------------
# FLASK APP
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))


# -- Dashboard page --------------------------------------------------------

@app.route("/")
def index():
    return render_template("dashboard.html", simulated=SIMULATED)


# -- Status API ------------------------------------------------------------

@app.route("/api/status")
def api_status():
    with motor.lock:
        data = {
            "running":       motor.running,
            "direction":     motor.direction,
            "speed":         motor.speed,
            "steps_taken":   motor.steps_taken,
            "card_detected": motor.card_detected,
            "beam_blocked":  GPIO.input(SENSOR_PIN) == GPIO.LOW,
            "simulated":     SIMULATED,
        }
    with scan_log_lock:
        data["total_scans"] = len(scan_log)
    with current_card_lock:
        data["current_card"] = current_card["card"]
    return jsonify(data)


# -- Motor control API -----------------------------------------------------

@app.route("/api/motor/forward", methods=["POST"])
def motor_forward():
    body = request.json or {}
    steps = body.get("steps", 0)
    with motor.lock:
        motor.direction = 1
        motor.running = True
        motor.card_detected = False
        motor.steps_taken = 0
        motor.steps_target = steps
        motor.stop_on_beam = True
    return jsonify({"ok": True, "action": "forward", "steps": steps})

@app.route("/api/motor/reverse", methods=["POST"])
def motor_reverse():
    body = request.json or {}
    steps = body.get("steps", 200)
    with motor.lock:
        motor.direction = -1
        motor.running = True
        motor.card_detected = False
        motor.steps_taken = 0
        motor.steps_target = steps
        motor.stop_on_beam = False
    return jsonify({"ok": True, "action": "reverse", "steps": steps})

@app.route("/api/motor/stop", methods=["POST"])
def motor_stop():
    with motor.lock:
        motor.running = False
    return jsonify({"ok": True, "action": "stop"})

@app.route("/api/motor/speed", methods=["POST"])
def motor_speed():
    body = request.json or {}
    delay = body.get("delay", 0.001)
    delay = max(0.0003, min(0.01, delay))
    with motor.lock:
        motor.speed = delay
    return jsonify({"ok": True, "speed": delay})


# -- Beam simulation -------------------------------------------------------

@app.route("/api/sim/beam", methods=["POST"])
def sim_beam():
    if not SIMULATED:
        return jsonify({"ok": False, "error": "Not in simulation mode"}), 400
    body = request.json or {}
    GPIO._beam_blocked = body.get("blocked", False)
    return jsonify({"ok": True, "beam_blocked": GPIO._beam_blocked})


# -- Rules API -------------------------------------------------------------

@app.route("/api/rules", methods=["GET"])
def get_rules():
    return jsonify(load_rules())

@app.route("/api/rules", methods=["POST"])
def set_rules():
    rules = request.json
    if not isinstance(rules, list):
        return jsonify({"error": "Expected a JSON array"}), 400
    save_rules(rules)
    return jsonify({"ok": True, "count": len(rules)})


# -- Scan log API ----------------------------------------------------------

@app.route("/api/scans", methods=["GET"])
def get_scans():
    with scan_log_lock:
        return jsonify(list(scan_log))

@app.route("/api/scans/clear", methods=["POST"])
def clear_scans():
    with scan_log_lock:
        scan_log.clear()
    with current_card_lock:
        current_card["card"] = None
    return jsonify({"ok": True})

@app.route("/api/scans/export", methods=["GET"])
def export_scans():
    with scan_log_lock:
        lines = ["timestamp,name,edition,rarity,cmc,colors,color_identity,type_line,price,pile"]
        for s in scan_log:
            colors = "|".join(s.get("colors", []))
            ci = "|".join(s.get("color_identity", []))
            lines.append(
                f'{s["timestamp"]},'
                f'"{s["name"]}",'
                f'"{s.get("edition","")}",'
                f'{s.get("rarity","")},'
                f'{s.get("cmc",0)},'
                f'{colors},'
                f'{ci},'
                f'"{s.get("type_line","")}",'
                f'{s.get("price",0)},'
                f'{s["pile"]}'
            )
    return "\n".join(lines), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=scans.csv"
    }


# -- Delver Webhook --------------------------------------------------------

def add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

@app.route("/webhook", methods=["POST", "OPTIONS"])
def webhook():
    if request.method == "OPTIONS":
        return add_cors(jsonify({"message": "CORS preflight"})), 200

    data = request.json or {}
    event_type = data.get("type", "")

    if event_type == "card_scanned":
        cards = data.get("cards", [])
        if not cards:
            return add_cors(jsonify({"status": "no cards"})), 200

        raw_card = cards[0]
        enriched = enrich_card(raw_card)

        rules = load_rules()
        pile = evaluate_rules(enriched, rules)

        entry = {**enriched, "timestamp": datetime.now().isoformat(), "pile": pile}

        with scan_log_lock:
            scan_log.append(entry)
        with current_card_lock:
            current_card["card"] = entry

        print(f"[WEBHOOK] {entry['name']} | CMC {entry.get('cmc',0)} | "
              f"Colors {entry.get('color_identity',[])} | {entry.get('type_line','')} → Pile {pile}")

        return add_cors(jsonify({"status": "ok", "pile": pile, "card": entry["name"]})), 200

    elif event_type == "scanner_started":
        print("[WEBHOOK] Scanner started")
    elif event_type == "scanner_paused":
        print("[WEBHOOK] Scanner paused")

    return add_cors(jsonify({"status": "ok"})), 200


# -- Simulate a scan (for testing) -----------------------------------------

@app.route("/api/sim/scan", methods=["POST"])
def sim_scan():
    """Inject a fake card scan. Optionally provide a scryfallId for real enrichment."""
    body = request.json or {}

    fake_delver_card = {
        "name":        body.get("name", "Birds of Paradise"),
        "edition":     body.get("edition", "Secret Lair Drop"),
        "editionCode": body.get("editionCode", "sld"),
        "number":      body.get("number", "92"),
        "rarity":      body.get("rarity", "R"),
        "price":       body.get("price", 8.36),
        "fmtPrice":    body.get("fmtPrice", ""),
        "finish":      body.get("finish", "regular"),
        "cardType":    body.get("cardType", "Creature — Bird"),
        "scryfallId":  body.get("scryfallId", ""),
    }

    enriched = enrich_card(fake_delver_card)
    rules = load_rules()
    pile = evaluate_rules(enriched, rules)

    entry = {**enriched, "timestamp": datetime.now().isoformat(), "pile": pile}

    with scan_log_lock:
        scan_log.append(entry)
    with current_card_lock:
        current_card["card"] = entry

    print(f"[SIM SCAN] {entry['name']} | CMC {entry.get('cmc',0)} | → Pile {pile}")
    return jsonify({"ok": True, "card": entry})


# -- Scryfall lookup (for UI search) ---------------------------------------

@app.route("/api/scryfall/search", methods=["GET"])
def scryfall_search():
    """Search Scryfall by card name — used by the UI to find scryfallIds."""
    q = request.args.get("q", "")
    if not q:
        return jsonify({"error": "Missing ?q= parameter"}), 400

    try:
        resp = http_requests.get(
            "https://api.scryfall.com/cards/search",
            params={"q": q, "unique": "prints", "order": "released", "dir": "desc"},
            headers={"User-Agent": "CardSorterPi/1.0", "Accept": "application/json"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            cards = []
            for c in data.get("data", [])[:8]:
                img = ""
                if c.get("image_uris"):
                    img = c["image_uris"].get("small", "")
                elif c.get("card_faces") and c["card_faces"][0].get("image_uris"):
                    img = c["card_faces"][0]["image_uris"].get("small", "")
                cards.append({
                    "id":       c.get("id", ""),
                    "name":     c.get("name", ""),
                    "set_name": c.get("set_name", ""),
                    "set":      c.get("set", ""),
                    "number":   c.get("collector_number", ""),
                    "rarity":   c.get("rarity", ""),
                    "image":    img,
                })
            return jsonify(cards)
        elif resp.status_code == 404:
            return jsonify([])
        else:
            return jsonify({"error": f"Scryfall returned {resp.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # On Pi: bind to all interfaces so phone/laptop can reach it
    # On laptop (sim): bind to 127.0.0.1 to avoid VPN/firewall issues
    host = "0.0.0.0" if not SIMULATED else "127.0.0.1"
    port = 5000 if not SIMULATED else 8080

    print("\n🃏 Card Sorter Control System")
    print(f"   Mode: {'SIMULATION' if SIMULATED else 'RASPBERRY PI'}")
    print(f"   Dashboard:  http://127.0.0.1:{port}")
    print(f"   Webhook:    http://127.0.0.1:{port}/webhook\n")
    app.run(host=host, port=port, debug=SIMULATED)