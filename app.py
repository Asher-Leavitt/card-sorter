"""
Card Sorter v5 — Full Scryfall-style sorting with dummy piles, sets, languages.
"""
import threading, time, json, os, sys, traceback
import requests as http_requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template

# --- GPIO ---
try:
    import RPi.GPIO as GPIO; SIMULATED = False; print("[HW] GPIO active")
except ImportError:
    SIMULATED = True; print("[SIM] Simulation mode")
    class _FakeGPIO:
        BCM=11;OUT=0;IN=1;HIGH=1;LOW=0;PUD_UP=22
        def __init__(self): self._pins={};self._beams={}
        def setmode(self,m): pass
        def setwarnings(self,f): pass
        def setup(self,p,d,pull_up_down=None): self._pins[p]=0
        def output(self,p,v): self._pins[p]=v
        def input(self,p):
            if p in self._beams: return self.LOW if self._beams[p] else self.HIGH
            return self.HIGH
        def cleanup(self): pass
        def sim_set_beam(self,p,b): self._beams[p]=b
    GPIO=_FakeGPIO()

# --- PCA9685 ---
try:
    from adafruit_servokit import ServoKit; servo_kit=ServoKit(channels=16); SERVO_AVAILABLE=True
except Exception as e:
    SERVO_AVAILABLE=False
    class _FS:
        def __init__(self): self.angle=0
    class _FK:
        def __init__(self): self.servo=[_FS() for _ in range(16)]
    servo_kit=_FK()

def set_servo_angle(ch,a,hold=False):
    a=max(0,min(180,a))
    try:
        servo_kit.servo[ch].angle=a
        if not hold: time.sleep(0.4); release_servo(ch)
    except: pass

def release_servo(ch):
    try: servo_kit.servo[ch].angle=None
    except: pass

def get_servo_angle(ch):
    try:
        a=servo_kit.servo[ch].angle; return a if a is not None else 0
    except: return 0

# --- HARDWARE CONFIG ---
PINS = {
    "stepper1_step": 22, "stepper1_dir": 23,
    "stepper2_step": 17, "stepper2_dir": 27,
    "beam0": 10, "beam1": 9,
}

# dummy=True means no servo, card just falls off the end
PILES = {
    1: {"beam":"beam1","servo_ch":15,"servo_up":21,"servo_down":40,"dummy":False},
}

ERROR_PILE = 0
SORT_CONFIG = {
    "home_timeout_sec":20,"osc_forward_steps":800,"osc_pause_sec":1.0,
    "scan_timeout_osc":60,"eject_extra_steps":100,"pile1_shuffle_steps":300,"step_delay":0.001,
}

GPIO.setmode(GPIO.BCM)
if not SIMULATED: GPIO.setwarnings(False)
for n,p in PINS.items():
    if "step" in n or "dir" in n: GPIO.setup(p,GPIO.OUT)
    elif "beam" in n: GPIO.setup(p,GPIO.IN,pull_up_down=GPIO.PUD_UP)
if SIMULATED:
    for n,p in PINS.items():
        if "beam" in n: GPIO.sim_set_beam(p,False)
for pn,pc in PILES.items():
    if not pc.get("dummy",False): set_servo_angle(pc["servo_ch"],pc["servo_down"])

# --- STEPPER HELPERS ---
def step_motor(sp,dp,d,steps=1,delay=0.001):
    GPIO.output(dp,GPIO.HIGH if d==1 else GPIO.LOW);t=0
    for _ in range(steps):
        GPIO.output(sp,GPIO.HIGH);time.sleep(delay);GPIO.output(sp,GPIO.LOW);time.sleep(delay);t+=1
    return t

def step_dual(s1s,s1d,d1,s2s,s2d,d2,steps=1,delay=0.001):
    GPIO.output(s1d,GPIO.HIGH if d1==1 else GPIO.LOW);GPIO.output(s2d,GPIO.HIGH if d2==1 else GPIO.LOW);t=0
    for _ in range(steps):
        GPIO.output(s1s,GPIO.HIGH);GPIO.output(s2s,GPIO.HIGH);time.sleep(delay)
        GPIO.output(s1s,GPIO.LOW);GPIO.output(s2s,GPIO.LOW);time.sleep(delay);t+=1
    return t

def run_until_beam(sp,dp,bp,d,delay=0.001,mx=50000):
    GPIO.output(dp,GPIO.HIGH if d==1 else GPIO.LOW);t=0
    for _ in range(mx):
        if GPIO.input(bp)==GPIO.LOW: return True,t
        GPIO.output(sp,GPIO.HIGH);time.sleep(delay);GPIO.output(sp,GPIO.LOW);time.sleep(delay);t+=1
    return False,t

def _should_stop():
    with seq.lock: return seq.stop_requested
def _set_phase(ph,msg):
    with seq.lock: seq.phase=ph;seq.status_msg=msg
    print(f"[SEQ] {msg}")

def _step_i(sp,dp,d,steps,delay=0.001):
    GPIO.output(dp,GPIO.HIGH if d==1 else GPIO.LOW);t=0
    for _ in range(steps):
        if _should_stop(): return "stopped",t
        GPIO.output(sp,GPIO.HIGH);time.sleep(delay);GPIO.output(sp,GPIO.LOW);time.sleep(delay);t+=1
    return "done",t

def _beam_i(sp,dp,bp,d,delay=0.001,mx=50000):
    GPIO.output(dp,GPIO.HIGH if d==1 else GPIO.LOW);t=0
    for _ in range(mx):
        if _should_stop(): return "stopped",t
        if GPIO.input(bp)==GPIO.LOW: return "beam",t
        GPIO.output(sp,GPIO.HIGH);time.sleep(delay);GPIO.output(sp,GPIO.LOW);time.sleep(delay);t+=1
    return "max_steps",t

def _dual_i(s1s,s1d,d1,s2s,s2d,d2,steps,delay=0.001):
    GPIO.output(s1d,GPIO.HIGH if d1==1 else GPIO.LOW);GPIO.output(s2d,GPIO.HIGH if d2==1 else GPIO.LOW);t=0
    for _ in range(steps):
        if _should_stop(): return "stopped",t
        GPIO.output(s1s,GPIO.HIGH);GPIO.output(s2s,GPIO.HIGH);time.sleep(delay)
        GPIO.output(s1s,GPIO.LOW);GPIO.output(s2s,GPIO.LOW);time.sleep(delay);t+=1
    return "done",t

def _dual_beam_i(s1s,s1d,d1,s2s,s2d,d2,bp,delay=0.001,mx=50000):
    GPIO.output(s1d,GPIO.HIGH if d1==1 else GPIO.LOW);GPIO.output(s2d,GPIO.HIGH if d2==1 else GPIO.LOW);t=0
    for _ in range(mx):
        if _should_stop(): return "stopped",t
        if GPIO.input(bp)==GPIO.LOW: return "beam",t
        GPIO.output(s1s,GPIO.HIGH);GPIO.output(s2s,GPIO.HIGH);time.sleep(delay)
        GPIO.output(s1s,GPIO.LOW);GPIO.output(s2s,GPIO.LOW);time.sleep(delay);t+=1
    return "max_steps",t

def _dual_passthrough_i(s1s,s1d,d1,s2s,s2d,d2,bp,delay=0.001,mx=50000):
    GPIO.output(s1d,GPIO.HIGH if d1==1 else GPIO.LOW);GPIO.output(s2d,GPIO.HIGH if d2==1 else GPIO.LOW)
    t=0;ph="wb"
    for _ in range(mx):
        if _should_stop(): return "stopped",t
        bl=GPIO.input(bp)==GPIO.LOW
        if ph=="wb" and bl: ph="wc"
        elif ph=="wc" and not bl: return "passthrough",t
        GPIO.output(s1s,GPIO.HIGH);GPIO.output(s2s,GPIO.HIGH);time.sleep(delay)
        GPIO.output(s1s,GPIO.LOW);GPIO.output(s2s,GPIO.LOW);time.sleep(delay);t+=1
    return "max_steps",t

def _single_passthrough_i(sp,dp,d,bp,delay=0.001,mx=50000):
    GPIO.output(dp,GPIO.HIGH if d==1 else GPIO.LOW);t=0;ph="wb"
    for _ in range(mx):
        if _should_stop(): return "stopped",t
        bl=GPIO.input(bp)==GPIO.LOW
        if ph=="wb" and bl: ph="wc"
        elif ph=="wc" and not bl: return "passthrough",t
        GPIO.output(sp,GPIO.HIGH);time.sleep(delay);GPIO.output(sp,GPIO.LOW);time.sleep(delay);t+=1
    return "max_steps",t

# --- SCRYFALL ---
SCRYFALL_CACHE={}
def fetch_scryfall(sid):
    if not sid: return None
    if sid in SCRYFALL_CACHE: return SCRYFALL_CACHE[sid]
    try:
        resp=http_requests.get(f"https://api.scryfall.com/cards/{sid}",headers={"User-Agent":"CardSorterPi/1.0"},timeout=8)
        if resp.status_code==200:
            d=resp.json()
            r={"name":d.get("name",""),"cmc":d.get("cmc",0),"colors":d.get("colors",[]),"color_identity":d.get("color_identity",[]),
               "type_line":d.get("type_line",""),"mana_cost":d.get("mana_cost",""),
               "oracle_text":d.get("oracle_text",""),"power":d.get("power",""),"toughness":d.get("toughness",""),
               "keywords":d.get("keywords",[]),"set_name":d.get("set_name",""),"rarity":d.get("rarity",""),
               "image_uri":"","image_art_crop":"","legalities":d.get("legalities",{}),
               "prices":d.get("prices",{}),"set_code":d.get("set",""),"lang":d.get("lang","en"),
               "set_type":d.get("set_type",""),"layout":d.get("layout","normal")}
            iu=d.get("image_uris")
            if iu: r["image_uri"]=iu.get("large",iu.get("normal",""));r["image_art_crop"]=iu.get("art_crop","")
            elif d.get("card_faces"):
                fi=d["card_faces"][0].get("image_uris",{})
                r["image_uri"]=fi.get("large",fi.get("normal",""));r["image_art_crop"]=fi.get("art_crop","")
            SCRYFALL_CACHE[sid]=r; return r
        return None
    except: return None

def enrich_card(dc):
    sf=fetch_scryfall(dc.get("scryfallId",""))
    e={"name":dc.get("name","Unknown"),"edition":dc.get("edition",""),"editionCode":dc.get("editionCode",""),
       "number":dc.get("number",""),"rarity":dc.get("rarity",""),"price":dc.get("price",0),
       "fmtPrice":dc.get("fmtPrice",""),"finish":dc.get("finish","regular"),
       "cardType":dc.get("cardType",""),"scryfallId":dc.get("scryfallId","")}
    if sf:
        e.update(sf)
        if e["price"]==0:
            try: e["price"]=float(sf.get("prices",{}).get("usd",0) or 0)
            except: pass
    else:
        e.update({"cmc":0,"colors":[],"color_identity":[],"type_line":dc.get("cardType",""),
                   "mana_cost":"","oracle_text":"","power":"","toughness":"","keywords":[],
                   "image_uri":"","image_art_crop":"","legalities":{},"prices":{},
                   "set_code":dc.get("editionCode",""),"lang":"en","set_type":"","layout":"normal"})
    return e

# --- STATE ---
scan_log=[];scan_log_lock=threading.Lock()
current_card={"card":None};current_card_lock=threading.Lock()

# --- RULES ---
RULES_FILE=os.path.join(os.path.dirname(os.path.abspath(__file__)),"rules.json")
def load_rules():
    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE) as f:
                d=json.load(f)
                if isinstance(d,dict) and not isinstance(d,list): return d
        except: pass
    return {"1":{"price":{"currency":"usd","operator":">","value":5},"colors":{"selected":[],"mode":"including"},
                  "types":{"entries":[],"partial":False},"rarity":["rare","mythic"],"stats":[],"formats":[],
                  "sets":[],"languages":[],"layouts":[]}}

def save_rules(r):
    with open(RULES_FILE,"w") as f: json.dump(r,f,indent=2)

def _compare(v,op,t):
    try: v=float(v);t=float(t)
    except: return False
    if op==">": return v>t
    if op=="<": return v<t
    if op==">=": return v>=t
    if op=="<=": return v<=t
    if op=="=" or op=="==": return v==t
    if op=="!=": return v!=t
    return False

def evaluate_rules(card,rules):
    for ps in sorted(rules.keys(),key=lambda x:int(x)):
        if _matches_pile(card,rules[ps]): return int(ps)
    return 0

def _matches_pile(card,crit):
    matched_any=False

    # PRICE
    pc=crit.get("price",{})
    if pc and pc.get("value") not in (None,"",0):
        matched_any=True
        cur=pc.get("currency","usd"); prices=card.get("prices",{})
        if cur=="eur":
            try: pv=float(prices.get("eur",0) or 0)
            except: pv=0
        else:
            try: pv=float(prices.get("usd",0) or card.get("price",0))
            except: pv=float(card.get("price",0))
        if not _compare(pv,pc.get("operator",">"),pc["value"]): return False

    # COLORS
    cc=crit.get("colors",{})
    sel=[c.upper() for c in cc.get("selected",[])]
    if sel:
        matched_any=True
        card_c=[c.upper() for c in card.get("color_identity",[])]
        mode=cc.get("mode","including")
        if mode=="exactly" and sorted(card_c)!=sorted(sel): return False
        elif mode=="including" and not all(c in card_c for c in sel): return False
        elif mode=="at_most" and not all(c in sel for c in card_c): return False

    # TYPE LINE
    tc=crit.get("types",{})
    entries=tc.get("entries",[])
    if entries:
        matched_any=True
        tl=card.get("type_line","").lower()
        is_t=[e["name"].lower() for e in entries if e.get("is",True)]
        not_t=[e["name"].lower() for e in entries if not e.get("is",True)]
        for nt in not_t:
            if nt in tl: return False
        if is_t:
            if tc.get("partial",False):
                if not any(it in tl for it in is_t): return False
            else:
                if not all(it in tl for it in is_t): return False

    # RARITY
    rc=crit.get("rarity",[])
    if rc:
        matched_any=True
        if card.get("rarity","").lower() not in [r.lower() for r in rc]: return False

    # STATS
    sc=crit.get("stats",[])
    if sc:
        matched_any=True
        for s in sc:
            stat=s.get("stat","cmc")
            if stat in ("cmc","mana_value"): val=card.get("cmc",0)
            elif stat=="power":
                try: val=float(card.get("power",0))
                except: val=0
            elif stat=="toughness":
                try: val=float(card.get("toughness",0))
                except: val=0
            else: val=0
            if not _compare(val,s.get("operator",">"),s.get("value",0)): return False

    # FORMATS
    fc=crit.get("formats",[])
    if fc:
        matched_any=True
        legs=card.get("legalities",{})
        fmt_map={"standard":"standard","futurestandard":"future","historic":"historic",
                 "timeless":"timeless","gladiator":"gladiator","pioneer":"pioneer",
                 "modern":"modern","legacy":"legacy","pauper":"pauper","vintage":"vintage",
                 "pennydreadful":"penny","commander":"commander","oathbreaker":"oathbreaker",
                 "standardbrawl":"standardbrawl","brawl":"brawl","alchemy":"alchemy",
                 "paupercommander":"paupercommander","duelcommander":"duel",
                 "oldschool93/94":"oldschool","premodern":"premodern","predh":"predh"}
        for f in fc:
            fk=f.get("format","").lower().replace(" ","")
            sfk=fmt_map.get(fk,fk)
            if legs.get(sfk,"not_legal")!=f.get("legality","legal").lower(): return False

    # SETS
    sets_crit=crit.get("sets",[])
    if sets_crit:
        matched_any=True
        card_set=card.get("set_code","").upper()
        card_set_name=card.get("set_name","").lower()
        # Sets criteria: list of set codes or names
        found=False
        for s in sets_crit:
            sv=s.get("value","")
            if sv.upper()==card_set or sv.lower()==card_set_name or sv.lower() in card_set_name:
                found=True; break
        if not found: return False

    # LANGUAGES
    lang_crit=crit.get("languages",[])
    if lang_crit:
        matched_any=True
        card_lang=card.get("lang","en").lower()
        lang_map={"english":"en","spanish":"es","french":"fr","german":"de","italian":"it",
                  "portuguese":"pt","japanese":"ja","korean":"ko","russian":"ru",
                  "simplified chinese":"zhs","traditional chinese":"zht","hebrew":"he",
                  "latin":"la","ancient greek":"grc","arabic":"ar","sanskrit":"sa",
                  "phyrexian":"ph","quenya":"qya"}
        is_l=[lang_map.get(e["name"].lower(),e["name"].lower()) for e in lang_crit if e.get("is",True)]
        not_l=[lang_map.get(e["name"].lower(),e["name"].lower()) for e in lang_crit if not e.get("is",True)]
        for nl in not_l:
            if card_lang==nl: return False
        if is_l and card_lang not in is_l: return False

    # LAYOUTS
    lc=crit.get("layouts",[])
    if lc:
        matched_any=True
        card_layout=card.get("layout","normal").lower()
        is_lay=[e["name"].lower() for e in lc if e.get("is",True)]
        not_lay=[e["name"].lower() for e in lc if not e.get("is",True)]
        for nl in not_lay:
            if card_layout==nl: return False
        if is_lay and card_layout not in is_lay: return False

    return matched_any

# --- SETTINGS ---
SETTINGS_FILE=os.path.join(os.path.dirname(os.path.abspath(__file__)),"settings.json")
def load_settings():
    d={"error_pile":ERROR_PILE,"sort_config":dict(SORT_CONFIG),"piles":{str(k):v for k,v in PILES.items()}}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f: d.update(json.load(f))
        except: pass
    return d
def save_settings(s):
    with open(SETTINGS_FILE,"w") as f: json.dump(s,f,indent=2)
def get_pile_config():
    s=load_settings();p={}
    for k,v in s.get("piles",{}).items(): p[int(k)]=v
    return p
def get_error_pile(): return load_settings().get("error_pile",0)
def get_sort_config(): return load_settings().get("sort_config",SORT_CONFIG)

# --- SEQUENCE ---
class SequenceState:
    def __init__(self):
        self.lock=threading.Lock();self.running=False;self.stop_requested=False
        self.phase="idle";self.status_msg="Idle";self.error=""
        self.cycle_count=0;self.osc_count=0;self.last_scan_ts="";self.last_error_card=None
seq=SequenceState()

def continuous_sort_loop():
    s1s=PINS["stepper1_step"];s1d=PINS["stepper1_dir"]
    s2s=PINS["stepper2_step"];s2d=PINS["stepper2_dir"]
    beam0=PINS["beam0"];cfg=get_sort_config();delay=cfg["step_delay"]

    with seq.lock:
        seq.running=True;seq.stop_requested=False;seq.cycle_count=0;seq.error="";seq.last_error_card=None
        with current_card_lock: c=current_card["card"]
        seq.last_scan_ts=c["timestamp"] if c else ""

    while not _should_stop():
        cycle=seq.cycle_count+1;cfg=get_sort_config();delay=cfg["step_delay"]
        piles=get_pile_config();error_pile=get_error_pile()

        # 1. HOME
        _set_phase("homing",f"C{cycle}: Homing")
        ts=int(cfg["home_timeout_sec"]/(delay*2))
        r,_=_beam_i(s1s,s1d,beam0,direction=-1,delay=delay,max_steps=ts)
        if r=="stopped": break
        if r=="max_steps":
            with seq.lock: seq.error=f"C{cycle}: Intake error — beam0 not hit"
            break

        # 2. OSCILLATE
        _set_phase("oscillating",f"C{cycle}: Oscillating")
        with seq.lock: seq.osc_count=0
        with current_card_lock: c=current_card["card"]
        with seq.lock: seq.last_scan_ts=c["timestamp"] if c else ""
        scanned=False;max_osc=cfg.get("scan_timeout_osc",60)

        while not _should_stop() and not scanned:
            with seq.lock: seq.osc_count+=1;osc=seq.osc_count
            if osc>max_osc: break
            _set_phase("oscillating",f"C{cycle}: Osc {osc}/{max_osc}")
            r,_=_step_i(s1s,s1d,1,cfg["osc_forward_steps"],delay)
            if r=="stopped": break
            _set_phase("oscillating",f"C{cycle}: Scanning...")
            t0=time.time()
            while time.time()-t0<cfg["osc_pause_sec"]:
                if _should_stop(): break
                with current_card_lock: c=current_card["card"]
                if c and c.get("timestamp","")!=seq.last_scan_ts: scanned=True;break
                time.sleep(0.05)
            if _should_stop() or scanned: break
            r,_=_beam_i(s1s,s1d,beam0,-1,delay)
            if r=="stopped": break
            if r=="max_steps":
                with seq.lock: seq.error=f"C{cycle}: Lost beam0"; break

        if _should_stop(): break
        if not scanned:
            _set_phase("error_eject",f"C{cycle}: Scan timeout")
            _single_passthrough_i(s1s,s1d,-1,beam0,delay)
            with seq.lock: seq.error=f"C{cycle}: Unable to scan";seq.cycle_count=cycle
            time.sleep(0.2); continue

        with current_card_lock: card=current_card["card"]
        with seq.lock: seq.last_scan_ts=card["timestamp"] if card else ""
        cn=card["name"] if card else "?";pile=card.get("pile",0) if card else 0
        ap=pile
        if pile==0: ap=error_pile

        # 3. ROUTE
        if ap==0:
            _set_phase("error_eject",f"C{cycle}: No match → error")
            with seq.lock: seq.error=f"C{cycle}: No match for '{cn}'";seq.last_error_card=card
            _single_passthrough_i(s1s,s1d,-1,beam0,delay)
        elif ap==1:
            pc=piles.get(1,{});bn=pc.get("beam","beam1");bp=PINS.get(bn)
            is_dummy=pc.get("dummy",False)
            _set_phase("ejecting",f"C{cycle}: → Pile 1")
            if is_dummy:
                r,_=_dual_passthrough_i(s1s,s1d,1,s2s,s2d,1,bp,delay)
                if r=="stopped": break
            else:
                r,_=_dual_beam_i(s1s,s1d,1,s2s,s2d,1,bp,delay)
                if r=="stopped": break
                if r=="max_steps":
                    with seq.lock: seq.error=f"C{cycle}: Pile 1 beam miss"; break
                sh=cfg.get("pile1_shuffle_steps",300)
                _dual_i(s1s,s1d,-1,s2s,s2d,-1,sh,delay)
                _dual_i(s1s,s1d,1,s2s,s2d,1,sh,delay)
        else:
            pc=piles.get(ap)
            if not pc:
                with seq.lock: seq.error=f"C{cycle}: No config pile {ap}"
                _single_passthrough_i(s1s,s1d,-1,beam0,delay)
                with seq.lock: seq.cycle_count=cycle; continue

            bn=pc.get("beam","beam1");bp=PINS.get(bn)
            is_dummy=pc.get("dummy",False)
            if bp is None:
                with seq.lock: seq.error=f"C{cycle}: Beam '{bn}' missing"; break

            if is_dummy:
                # Dummy pile: no servo, just passthrough
                _set_phase("ejecting",f"C{cycle}: → Pile {ap} (dummy)")
                r,_=_dual_passthrough_i(s1s,s1d,1,s2s,s2d,1,bp,delay)
                if r=="stopped": break
                extra=cfg.get("eject_extra_steps",100)
                if extra>0: _dual_i(s1s,s1d,1,s2s,s2d,1,extra,delay)
            else:
                # Servo pile
                sc=pc.get("servo_ch",15);su=pc.get("servo_up",90);sd_a=pc.get("servo_down",0)
                _set_phase("ejecting",f"C{cycle}: → Pile {ap} servo UP")
                set_servo_angle(sc,su,hold=True);time.sleep(0.3)
                r,_=_dual_passthrough_i(s1s,s1d,1,s2s,s2d,1,bp,delay)
                if r=="stopped": set_servo_angle(sc,sd_a);break
                if r=="max_steps": set_servo_angle(sc,sd_a);seq.error=f"C{cycle}: Beam miss";break
                extra=cfg.get("eject_extra_steps",100)
                if extra>0:
                    r2,_=_dual_i(s1s,s1d,1,s2s,s2d,1,extra,delay)
                    if r2=="stopped": set_servo_angle(sc,sd_a);break
                set_servo_angle(sc,sd_a)

        with seq.lock: seq.cycle_count=cycle
        time.sleep(0.1)

    with seq.lock:
        seq.running=False
        if seq.stop_requested: seq.status_msg=f"Stopped after {seq.cycle_count} cards"
        elif not seq.error: seq.status_msg=f"Done: {seq.cycle_count} cards"
        seq.phase="idle"
    for pn,pc in get_pile_config().items():
        if not pc.get("dummy",False): set_servo_angle(pc["servo_ch"],pc.get("servo_down",0))

# --- FLASK ---
app=Flask(__name__,template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)),"templates"))

def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]="*"
    r.headers["Access-Control-Allow-Methods"]="POST,GET,OPTIONS"
    r.headers["Access-Control-Allow-Headers"]="Content-Type"
    return r

def handle_webhook():
    if request.method=="OPTIONS": return add_cors(jsonify({"ok":1})),200
    data=request.json or {};et=data.get("type","")
    if et=="card_scanned":
        cards=data.get("cards",[])
        if not cards: return add_cors(jsonify({"status":"no cards"})),200
        enriched=enrich_card(cards[0])
        pile=evaluate_rules(enriched,load_rules())
        entry={**enriched,"timestamp":datetime.now().isoformat(),"pile":pile}
        with scan_log_lock: scan_log.append(entry)
        with current_card_lock: current_card["card"]=entry
        print(f"[WEBHOOK] ✓ {entry['name']} → Pile {pile}")
        return add_cors(jsonify({"status":"ok","pile":pile})),200
    return add_cors(jsonify({"status":"ok"})),200

@app.route("/",methods=["GET","POST","OPTIONS"])
def index():
    if request.method=="GET": return render_template("dashboard.html",simulated=SIMULATED)
    return handle_webhook()
@app.route("/webhook",methods=["POST","OPTIONS"])
def webhook_route(): return handle_webhook()

@app.route("/api/status")
def api_status():
    beams={};servos={}
    for n,p in PINS.items():
        if "beam" in n: beams[n]=GPIO.input(p)==GPIO.LOW
    for pn,pc in get_pile_config().items():
        if not pc.get("dummy",False):
            ch=pc.get("servo_ch",0);servos[f"pile{pn}_ch{ch}"]=get_servo_angle(ch)
    with seq.lock:
        sd={"seq_running":seq.running,"seq_phase":seq.phase,"seq_status":seq.status_msg,
            "seq_error":seq.error,"seq_cycles":seq.cycle_count,"seq_osc":seq.osc_count,
            "seq_error_card":seq.last_error_card}
    with scan_log_lock: total=len(scan_log)
    with current_card_lock: card=current_card["card"]
    s=load_settings()
    return jsonify({"simulated":SIMULATED,"beams":beams,"servos":servos,"total_scans":total,
        "current_card":card,"pins":PINS,"piles":s.get("piles",{}),
        "error_pile":s.get("error_pile",0),"sort_config":s.get("sort_config",SORT_CONFIG),
        "servo_available":SERVO_AVAILABLE,**sd})

@app.route("/api/motor/step",methods=["POST"])
def motor_step_api():
    b=request.json or {};s=b.get("stepper",1)
    try: return jsonify({"ok":True,"steps_taken":step_motor(PINS[f"stepper{s}_step"],PINS[f"stepper{s}_dir"],b.get("direction",1),b.get("steps",200),b.get("delay",0.001))})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500
@app.route("/api/motor/dual",methods=["POST"])
def motor_dual_api():
    b=request.json or {}
    try: return jsonify({"ok":True,"steps_taken":step_dual(PINS["stepper1_step"],PINS["stepper1_dir"],b.get("s1_dir",1),PINS["stepper2_step"],PINS["stepper2_dir"],b.get("s2_dir",-1),b.get("steps",200),b.get("delay",0.001))})
    except Exception as e: return jsonify({"ok":False,"error":str(e)}),500
@app.route("/api/servo/set",methods=["POST"])
def servo_set():
    b=request.json or {};ch=b.get("channel",15);a=b.get("angle",90)
    set_servo_angle(ch,a,hold=b.get("hold",True));return jsonify({"ok":True,"channel":ch,"angle":a})
@app.route("/api/servo/nudge",methods=["POST"])
def servo_nudge():
    b=request.json or {};ch=b.get("channel",15)
    na=max(0,min(180,get_servo_angle(ch)+b.get("delta",5)))
    set_servo_angle(ch,na,hold=True);return jsonify({"ok":True,"channel":ch,"angle":na})
@app.route("/api/servo/release",methods=["POST"])
def servo_release():
    b=request.json or {};release_servo(b.get("channel",15));return jsonify({"ok":True})
@app.route("/api/seq/start",methods=["POST"])
def seq_start():
    with seq.lock:
        if seq.running: return jsonify({"ok":False,"error":"Running"}),409
    threading.Thread(target=continuous_sort_loop,daemon=True).start();return jsonify({"ok":True})
@app.route("/api/seq/stop",methods=["POST"])
def seq_stop():
    with seq.lock: seq.stop_requested=True
    return jsonify({"ok":True})
@app.route("/api/settings",methods=["GET"])
def get_settings_api(): return jsonify(load_settings())
@app.route("/api/settings",methods=["POST"])
def set_settings_api(): save_settings(request.json);return jsonify({"ok":True})
@app.route("/api/sim/beam",methods=["POST"])
def sim_beam():
    if not SIMULATED: return jsonify({"ok":False}),400
    b=request.json or {};pin=PINS.get(b.get("beam","beam0"))
    if pin is not None: GPIO.sim_set_beam(pin,b.get("blocked",False))
    return jsonify({"ok":True})
@app.route("/api/sim/scan",methods=["POST"])
def sim_scan():
    """Test the sorting algorithm with a Scryfall card. Works in both sim and live mode."""
    b=request.json or {}
    sid=b.get("scryfallId","")
    if not sid: return jsonify({"ok":False,"error":"No scryfallId provided"}),400
    fake={"name":"","edition":"","editionCode":"","number":"","rarity":"","price":0,
          "fmtPrice":"","finish":"regular","cardType":"","scryfallId":sid}
    enriched=enrich_card(fake)
    if not enriched.get("name"):
        return jsonify({"ok":False,"error":"Scryfall lookup failed — check internet connection"}),400
    pile=evaluate_rules(enriched,load_rules())
    entry={**enriched,"timestamp":datetime.now().isoformat(),"pile":pile}
    with scan_log_lock: scan_log.append(entry)
    with current_card_lock: current_card["card"]=entry
    return jsonify({"ok":True,"card":entry})
@app.route("/api/rules",methods=["GET"])
def get_rules(): return jsonify(load_rules())
@app.route("/api/rules",methods=["POST"])
def set_rules(): save_rules(request.json);return jsonify({"ok":True})
@app.route("/api/scans",methods=["GET"])
def get_scans():
    with scan_log_lock: return jsonify(list(scan_log))
@app.route("/api/scans/clear",methods=["POST"])
def clear_scans():
    with scan_log_lock: scan_log.clear()
    with current_card_lock: current_card["card"]=None
    return jsonify({"ok":True})
@app.route("/api/scans/export",methods=["GET"])
def export_scans():
    with scan_log_lock:
        lines=["timestamp,name,rarity,cmc,colors,type_line,price,pile"]
        for s in scan_log: lines.append(f'{s["timestamp"]},"{s["name"]}",{s.get("rarity","")},{s.get("cmc",0)},{"|".join(s.get("colors",[]))},"{s.get("type_line","")}",{s.get("price",0)},{s["pile"]}')
    return "\n".join(lines),200,{"Content-Type":"text/csv","Content-Disposition":"attachment; filename=scans.csv"}
@app.route("/api/scryfall/search",methods=["GET"])
def scryfall_search():
    q=request.args.get("q","")
    if not q: return jsonify({"error":"?q="}),400
    try:
        resp=http_requests.get("https://api.scryfall.com/cards/search",params={"q":q,"unique":"prints","order":"released","dir":"desc"},headers={"User-Agent":"CardSorterPi/1.0"},timeout=5)
        if resp.status_code==200:
            cards=[]
            for c in resp.json().get("data",[])[:8]:
                img=""
                if c.get("image_uris"): img=c["image_uris"].get("small","")
                elif c.get("card_faces") and c["card_faces"][0].get("image_uris"): img=c["card_faces"][0]["image_uris"].get("small","")
                cards.append({"id":c.get("id",""),"name":c.get("name",""),"set_name":c.get("set_name",""),"set":c.get("set",""),"number":c.get("collector_number",""),"rarity":c.get("rarity",""),"image":img})
            return jsonify(cards)
        elif resp.status_code==404: return jsonify([])
        else: return jsonify({"error":f"{resp.status_code}"}),502
    except Exception as e: return jsonify({"error":str(e)}),500

if __name__=="__main__":
    host="0.0.0.0" if not SIMULATED else "127.0.0.1";port=5000 if not SIMULATED else 8080
    print(f"\n🃏 Card Sorter v5\n   http://{'<pi>' if not SIMULATED else '127.0.0.1'}:{port}\n")
    app.run(host=host,port=port,debug=SIMULATED)