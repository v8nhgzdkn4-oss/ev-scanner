#!/usr/bin/env python3
"""
EV+ scanner – Unibet (Kambi) mot Pinnacle + Betfair, med Telegram-notiser.

Kör:  python3 ev_scanner.py          (en scan)
      python3 ev_scanner.py --loop   (scannar själv enligt schemat nedan)

Kräver: pip install requests
Fyll i ODDS_API_KEY, TELEGRAM_TOKEN och TELEGRAM_CHAT_ID nedan (eller som miljövariabler).
"""
import os, sys, json, math, time, datetime as dt
import requests

# ---------- INSTÄLLNINGAR ----------
ODDS_API_KEY     = os.getenv("ODDS_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")      # från @BotFather
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")    # ditt chat-id
BOOK             = "unibet_se"                          # Kambi
MIN_EV           = 0.01     # +1 % mot konsensus
MIN_EV_PIN       = 0.0      # och minst 0 % mot Pinnacle ensam
# --- Schema för --cloud (cron-job.org startar var 15:e minut) ---
BROAD_HOURS      = [8, 13, 18]  # BRED SCAN: kl 08, 13, 18 (svensk tid) – alla matcher BROAD_DAYS framåt
BROAD_DAYS       = 7
LINEUP_MIN       = 20           # STARTELVESCAN: matcher som startar om 20–75 min
LINEUP_MAX       = 75
LINEUP_HOURS     = list(range(7, 24)) + [0, 1]   # startelvescan körs kl 07:00–01:59
STATUS_LINEUP    = True         # skicka "✅ Startelvescan ..." även när inget hittas (bara om matcher kollades)
WINDOW_HOURS     = 30           # används av --loop / manuell körning
SCAN_HOURS       = range(10, 24)   # --loop: scanna kl 10–23 svensk tid
INTERVAL_MIN     = 60              # --loop: minuter mellan scans
MARKETS          = "h2h,totals"        # "h2h" = 1 credit/liga. "h2h,totals" = 2 credits/liga (även över/under)
STATE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ev_sent.json")

LEAGUES = [
 "soccer_epl","soccer_efl_champ","soccer_england_league1","soccer_spain_la_liga",
 "soccer_spain_segunda_division","soccer_italy_serie_a","soccer_italy_serie_b",
 "soccer_germany_bundesliga","soccer_germany_bundesliga2","soccer_france_ligue_one",
 "soccer_france_ligue_two","soccer_netherlands_eredivisie","soccer_portugal_primeira_liga",
 "soccer_belgium_first_div","soccer_turkey_super_league","soccer_greece_super_league",
 "soccer_denmark_superliga","soccer_sweden_allsvenskan","soccer_norway_eliteserien",
 "soccer_poland_ekstraklasa","soccer_austria_bundesliga","soccer_switzerland_superleague",
 "soccer_spl","soccer_uefa_champs_league","soccer_uefa_europa_league",
 "soccer_uefa_europa_conference_league","soccer_usa_mls","soccer_brazil_campeonato",
 "soccer_argentina_primera_division","soccer_mexico_ligamx","soccer_australia_aleague",
 "soccer_japan_j_league",
]
API = "https://api.the-odds-api.com/v4"
try:
    from zoneinfo import ZoneInfo
    SE = ZoneInfo("Europe/Stockholm")
except Exception:
    SE = dt.timezone(dt.timedelta(hours=1))

# ---------- MATTE ----------
def power_devig(odds):
    """Marginalfria sannolikheter med power-metoden."""
    lo, hi = 0.5, 3.0
    for _ in range(80):
        k = (lo + hi) / 2
        s = sum((1 / o) ** k for o in odds)
        lo, hi = (k, hi) if s > 1 else (lo, k)
    return [(1 / o) ** k for o in odds]

def pois_cdf(n, lam):
    return sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(n + 1)) if n >= 0 else 0.0

def ou_ev(lam, line, side, odds):
    """EV för över/under på valfri linje (hel, halv, kvart) givet Poisson(lam)."""
    def single(l):
        if abs(l - round(l)) < 1e-9:              # hel linje -> push möjlig
            L = int(round(l))
            p_push = math.exp(-lam) * lam ** L / math.factorial(L)
            p_under = pois_cdf(L - 1, lam)
            p_over = 1 - p_under - p_push
        else:                                      # halv linje
            p_under = pois_cdf(int(math.floor(l)), lam)
            p_over = 1 - p_under
            p_push = 0.0
        win = p_over if side == "Over" else p_under
        lose = 1 - win - p_push
        return win * (odds - 1) - lose
    frac = round((line * 4) % 4)
    if frac in (1, 3):                             # kvartslinje -> dela insatsen
        return (single(line - 0.25) + single(line + 0.25)) / 2
    return single(line)

def fit_lambda(line, o_over, o_under):
    """Hitta lambda så att Pinnacles (marginalfria) över/under är fair."""
    p_over, p_under = power_devig([o_over, o_under])
    fo, fu = 1 / p_over, 1 / p_under               # fair odds
    lo, hi = 0.2, 8.0
    for _ in range(80):
        lam = (lo + hi) / 2
        # vid rätt lambda har fair över-odds EV = 0
        if ou_ev(lam, line, "Over", fo) > 0: hi = lam
        else: lo = lam
    return lam

def fair_odds_ou(lam, line, side):
    lo, hi = 1.01, 50.0
    for _ in range(80):
        m = (lo + hi) / 2
        if ou_ev(lam, line, side, m) > 0: hi = m
        else: lo = m
    return m

# ---------- API ----------
credits_left = None
n_matches = 0
def get(url, params):
    global credits_left
    r = requests.get(url, params=params, timeout=20)
    if "x-requests-remaining" in r.headers:
        credits_left = r.headers["x-requests-remaining"]
    if r.status_code != 200:
        return None
    return r.json()

def h2h(bm, key="h2h"):
    for m in bm.get("markets", []):
        if m["key"] == key:
            return {o["name"]: o["price"] for o in m["outcomes"]}
    return None

def totals(bm):
    for m in bm.get("markets", []):
        if m["key"] == "totals":
            d = {}
            for o in m["outcomes"]:
                d[o["name"]] = o["price"]; d["point"] = o.get("point")
            if "Over" in d and "Under" in d:
                return d
    return None

# ---------- SCAN ----------
def scan(min_ev=None, min_ev_pin=None, start_min=10, end_min=None):
    """Scanna matcher som startar mellan nu+start_min och nu+end_min minuter."""
    global n_matches
    n_matches = 0
    min_ev = MIN_EV if min_ev is None else min_ev
    min_ev_pin = MIN_EV_PIN if min_ev_pin is None else min_ev_pin
    end_min = WINDOW_HOURS * 60 if end_min is None else end_min
    now = dt.datetime.now(dt.timezone.utc)
    t0, t1 = now + dt.timedelta(minutes=start_min), now + dt.timedelta(minutes=end_min)
    picks = []
    for lg in LEAGUES:
        ev_list = get(f"{API}/sports/{lg}/events", {"apiKey": ODDS_API_KEY})  # gratis
        if not ev_list: continue
        if not any(t0 <= dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) <= t1 for e in ev_list):
            continue
        data = get(f"{API}/sports/{lg}/odds", {"apiKey": ODDS_API_KEY, "markets": MARKETS,
                   "oddsFormat": "decimal", "bookmakers": f"pinnacle,betfair_ex_eu,{BOOK}"})
        if not data: continue
        for e in data:
            ko = dt.datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            if not (t0 <= ko <= t1): continue
            n_matches += 1
            bms = {b["key"]: b for b in e.get("bookmakers", [])}
            if "pinnacle" not in bms or BOOK not in bms: continue
            home, away = e["home_team"], e["away_team"]
            names = [home, "Draw", away]; labels = ["1", "X", "2"]
            # --- 1X2 ---
            pin, ub = h2h(bms["pinnacle"]), h2h(bms[BOOK])
            bf = h2h(bms["betfair_ex_eu"]) if "betfair_ex_eu" in bms else None
            if pin and ub and all(n in pin for n in names) and all(n in ub for n in names):
                P = [pin[n] for n in names]; U = [ub[n] for n in names]
                fp = power_devig(P)
                fb, bf_ok = None, False
                if bf and all(n in bf for n in names):
                    B = [bf[n] for n in names]
                    over = sum(1 / o for o in B)
                    if 0.92 < over < 1.08 and all(abs(b / p - 1) < 0.35 for b, p in zip(B, P)):
                        fb, bf_ok = power_devig(B), True
                f = [(a + b) / 2 for a, b in zip(fp, fb)] if bf_ok else fp
                for i in range(3):
                    ev = U[i] * f[i] - 1; ev_pin = U[i] * fp[i] - 1
                    if ev >= min_ev and ev_pin >= min_ev_pin:
                        picks.append(dict(lg=lg, match=f"{home} – {away}", ko=ko, bet=labels[i],
                            odds=U[i], p=f[i], fair=1 / f[i], ev=ev, ev_pin=ev_pin,
                            ev_bf=(U[i] * fb[i] - 1) if bf_ok else None,
                            pin_odds=P[i], bf_odds=(B[i] if bf_ok else None), bf_ok=bf_ok))
            # --- Över/Under ---
            pt, ut = totals(bms["pinnacle"]), totals(bms[BOOK])
            if pt and ut and pt.get("point") is not None and ut.get("point") is not None:
                lam = fit_lambda(pt["point"], pt["Over"], pt["Under"])
                for side in ("Over", "Under"):
                    ev = ou_ev(lam, ut["point"], side, ut[side])
                    if ev >= min_ev:
                        fo = fair_odds_ou(lam, ut["point"], side)
                        picks.append(dict(lg=lg, match=f"{home} – {away}", ko=ko,
                            bet=f"{'Över' if side=='Over' else 'Under'} {ut['point']}", odds=ut[side],
                            p=1 / fo, fair=fo, ev=ev, ev_pin=ev, ev_bf=None,
                            pin_odds=f"{pt['point']} {pt['Over']}/{pt['Under']}", bf_odds=None, bf_ok=False))
    return picks

def stake(ev, odds):
    u = 1 if ev < .05 else 2 if ev < .08 else 3 if ev < .12 else 4
    return min(u, 2) if odds >= 5 else u

def risk(odds):
    return "Låg" if odds < 1.6 else "Medel" if odds < 3.0 else "Hög"

def fmt(p):
    ko = p["ko"].astimezone(SE).strftime("%d/%m %H:%M")
    mins = int((p["ko"] - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60)
    if 0 < mins <= 120:
        ko += f" · ⏱ startar om {mins} min"
    bf = f"{p['bf_odds']:.2f}" if p["bf_odds"] else "–"
    pin = f"{p['pin_odds']:.2f}" if isinstance(p["pin_odds"], (int, float)) else p["pin_odds"]
    return (f"⚽ <b>{p['match']}</b> · {ko}\n"
            f"{p['bet']} · Kambi <b>{p['odds']:.2f}</b> · <b>EV {p['ev']*100:+.1f}%</b>\n"
            f"Pin {pin} · BF {bf}")

def telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text); return
    for cid in [c.strip() for c in str(TELEGRAM_CHAT_ID).split(",") if c.strip()]:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      data={"chat_id": cid, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=20)
        if r.status_code != 200:
            print("Telegram-fel:", cid, r.status_code, r.text)

def load_state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {}

def run_once(mode="normal"):
    if mode == "broad":
        picks = scan(start_min=10, end_min=BROAD_DAYS * 24 * 60)
    elif mode == "lineup":
        picks = scan(start_min=LINEUP_MIN, end_min=LINEUP_MAX)
    else:
        picks = scan()
    state = load_state(); new = []
    for p in sorted(picks, key=lambda x: -x["ev"]):
        key = f"{p['match']}|{p['bet']}"
        # notifiera nya spel, om Kambi höjt oddset, eller om EV stigit minst 2 %-enheter
        # (t.ex. när Pinnacle rör sig efter startelvorna men Kambi ligger kvar)
        old = state.get(key)
        prev_ev = old[2] if old and len(old) > 2 else -1
        if old is None or p["odds"] > old[0] + 1e-9 or p["ev"] >= prev_ev + 0.02:
            new.append(p); state[key] = [p["odds"], time.time(), p["ev"]]
    state = {k: v for k, v in state.items() if time.time() - v[1] < 3 * 86400}  # rensa > 3 dagar
    json.dump(state, open(STATE_FILE, "w"))
    stamp = dt.datetime.now(SE).strftime("%H:%M")
    label = {"broad": "Bred scan", "lineup": "Startelvescan"}.get(mode, "Scan")
    cr = f" · credits {credits_left}" if credits_left else ""
    if new:
        head = "🔔 <b>EV+</b>" + (" ⏱ <b>startelvor</b>" if mode == "lineup" else "")
        msg = head + "\n\n" + "\n\n".join(fmt(p) for p in new)
        telegram(msg)
    elif mode != "lineup" or (STATUS_LINEUP and n_matches > 0):
        telegram(f"✅ {label} {stamp} – inga nya EV+ ({n_matches} matcher){cr}")
    print(f"[{stamp}] {label}: {n_matches} matcher, {len(picks)} spel ≥{MIN_EV*100:.0f}% ({len(new)} nya). Credits kvar: {credits_left}")

if __name__ == "__main__":
    if "--cloud" in sys.argv:
        # cron-job.org startar var 15:e minut. Kl BROAD_HOURS (första kvarten) = bred scan,
        # annars startelvescan (matcher som börjar om LINEUP_MIN–LINEUP_MAX min).
        now = dt.datetime.now(SE); h, m = now.hour, now.minute
        if h in BROAD_HOURS and m < 15:
            run_once("broad")
        elif h in LINEUP_HOURS:
            run_once("lineup")
        else:
            print(f"{now:%H:%M} utanför schemat – ingen scan.")
        sys.exit()
    if "--test" in sys.argv:
        telegram("✅ EV-scannern är kopplad till Telegram!"); print("Testmeddelande skickat."); sys.exit()
    if "--demo" in sys.argv:
        picks = sorted(scan(min_ev=-1.0, min_ev_pin=-1.0), key=lambda x: -x["ev"])[:3]
        if not picks:
            telegram("🧪 TEST: hittade inga matcher med odds just nu."); sys.exit()
        msg = "🧪 <b>TEST</b>\n\n"
        msg += "\n\n".join(fmt(p) for p in picks)
        telegram(msg); print("Testnotis skickad med", len(picks), "spel. Credits kvar:", credits_left); sys.exit()
    if "--loop" in sys.argv:
        while True:
            if dt.datetime.now(SE).hour in SCAN_HOURS:
                try: run_once()
                except Exception as ex: print("Fel:", ex)
            time.sleep(INTERVAL_MIN * 60)
    else:
        run_once()
