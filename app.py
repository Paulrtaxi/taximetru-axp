"""
Bolt Fleet Web App - FINAL
Login automat fara Selenium, refresh token automat
"""

from flask import Flask, jsonify, request, session, redirect, url_for
from datetime import datetime, timedelta
import threading
import time
import json
import requests
import os

# ============================================================
#  CONFIGURARE
# ============================================================

BOLT_EMAIL        = "PUNE_EMAIL_AICI"
BOLT_PAROLA       = "PUNE_PAROLA_AICI"
COMPANY_ID        = 108961
USER_ID           = 126684
PRAG_MINIM_LEI_KM = 5.0
INTERVAL_MINUTE   = 3
PORT              = 5000
ORS_API_KEY       = "uoLf9fiZQl_qm6E2jOJeyIc"

# ============================================================

app = Flask(__name__)
app.secret_key = "bolt-fleet-axp-secret-2026"
APP_PASSWORD = "Voineasa22"

# Upstash Redis - stocare persistenta
UPSTASH_REDIS_URL = "https://ideal-dove-81236.upstash.io"
UPSTASH_REDIS_TOKEN = "gQAAAAAAAT1UAAIncDE0ZDcyZjVlZTZiMDY0ODMwYWZlMDYwNzg5YTc0MzNhNXAxODEyMzY"

def redis_get(key):
    """Citeste o valoare din Upstash Redis via REST API."""
    try:
        r = requests.get(
            f"{UPSTASH_REDIS_URL}/get/{key}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"},
            timeout=5
        )
        if r.status_code == 200:
            result = r.json().get("result")
            print(f"[✅] Redis GET {key}: {'gasit' if result else 'gol'}")
            return result
        print(f"[WARN] Redis GET {key}: {r.status_code}")
    except Exception as e:
        print(f"[WARN] Redis GET {key}: {e}")
    return None

def redis_set(key, value):
    """Salveaza o valoare in Upstash Redis via REST API."""
    try:
        import urllib.parse
        val_enc = urllib.parse.quote(str(value), safe='')
        r = requests.post(
            f"{UPSTASH_REDIS_URL}/set/{key}/{val_enc}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_TOKEN}"},
            timeout=5
        )
        if r.status_code == 200:
            print(f"[✅] Redis SET {key} OK")
            return True
        print(f"[WARN] Redis SET {key}: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[WARN] Redis SET {key}: {e}")
    return False

# Porneste background thread la import (pentru gunicorn pe Render)
import atexit
_bg_started = False
def start_background():
    global _bg_started
    if not _bg_started:
        _bg_started = True
        load_cache()
        load_token()
        load_telegram_ids()
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
                json={"url": "https://bolt-fleet-axp.onrender.com/telegram-webhook"},
                timeout=10
            )
            print(f"[✅] Telegram webhook: {r.json().get('description', 'OK')}")
        except Exception as e:
            print(f"[WARN] Webhook setup: {e}")
        threading.Thread(target=refresh_loop, daemon=True).start()
        print("[✅] Background thread pornit!")
TELEGRAM_TOKEN = "8252714098:AAHwHh-IPHfNso6Gpcn_OKYtPhscovhg6Q4"
TELEGRAM_CHAT_IDS = ["1533169280"]  # ID-ul tau principal
TELEGRAM_IDS_FILE = "telegram_ids.json"

def save_telegram_ids():
    try:
        with open(TELEGRAM_IDS_FILE, "w") as f:
            json.dump(TELEGRAM_CHAT_IDS, f)
        print(f"[✅] Telegram IDs salvate: {TELEGRAM_CHAT_IDS}")
    except Exception as e:
        print(f"[WARN] Nu am putut salva telegram IDs: {e}")

def load_telegram_ids():
    global TELEGRAM_CHAT_IDS
    try:
        if os.path.exists(TELEGRAM_IDS_FILE):
            with open(TELEGRAM_IDS_FILE, "r") as f:
                ids = json.load(f)
                # Asigura ca ID-ul principal e mereu prezent
                if "1533169280" not in ids:
                    ids.append("1533169280")
                TELEGRAM_CHAT_IDS.clear()
                TELEGRAM_CHAT_IDS.extend(ids)
                print(f"[✅] Telegram IDs incarcate: {TELEGRAM_CHAT_IDS}")
    except Exception as e:
        print(f"[WARN] Nu am putut incarca telegram IDs: {e}")

push_subscriptions = []

rezervate_server = set()  # order_ids rezervate de pe telefon
ultimele_notificari = set()  # order_ids pentru care s-a trimis deja notificare

state = {
    "curse":         [],
    "ultima_update": None,
    "status":        "Se inițializează...",
    "eroare":        None,
    "se_incarca":    False,
    "prag_push":     25,
}

auth = {
    "refresh_token": None,
    "access_token":  None,
    "access_expiry": None,
}

TOKEN_FILE = "token.json"

def save_token(refresh_token):
    # Valideaza tokenul inainte de salvare
    refresh_token = refresh_token.strip()
    if len(refresh_token) < 100:
        print(f"[WARN] Token prea scurt ({len(refresh_token)} chars) - nu salvez!")
        return
    # Salveaza in Redis (persistent) si local (fallback)
    if redis_set("bolt_refresh_token", refresh_token):
        print(f"[✅] Token salvat în Redis (persistent, {len(refresh_token)} chars)")
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"refresh_token": refresh_token}, f)
    except:
        pass

def load_token():
    # Incearca Redis intai (persistent dupa deploy)
    rt = redis_get("bolt_refresh_token")
    if rt and isinstance(rt, str) and len(rt) > 100:
        auth["refresh_token"] = rt.strip()
        print(f"[✅] Token încărcat din Redis! (lungime: {len(rt)})")
        return
    elif rt:
        print(f"[WARN] Token din Redis invalid (lungime: {len(rt) if rt else 0}) - ignorat")
    # Fallback la fisier local
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
                rt = data.get("refresh_token")
                if rt:
                    auth["refresh_token"] = rt
                    print(f"[✅] Token încărcat din token.json")
    except Exception as e:
        print(f"[WARN] Nu am putut încărca token: {e}")

HEADERS_BASE = {
    "Content-Type": "application/json",
    "Accept":       "application/json",
    "Origin":       "https://fleets.bolt.eu",
    "Referer":      "https://fleets.bolt.eu/",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36",
}

API_BASE = "https://fleetownerportal.live.boltsvc.net/fleetOwnerPortal"
API_PARAMS = f"language=ro-ro&version=FO.3.1880&brand=bolt"


# ──────────────────────────────────────────
#  AUTENTIFICARE
# ──────────────────────────────────────────

LOGIN_ENDPOINTS = [
    f"{API_BASE}/login?{API_PARAMS}",
    f"{API_BASE}/loginWithPassword?{API_PARAMS}",
    f"{API_BASE}/authenticate?{API_PARAMS}",
    f"https://fleets.bolt.eu/api/v1/login",
    f"https://fleets.bolt.eu/api/v1/auth/login",
    f"https://node.taxify.eu/user/login",
]

def do_login():
    """Login automat dezactivat - folosim refresh token manual via /token."""
    print("[INFO] Nu am token - accesează /token pentru a adăuga unul!")
    state["eroare"] = "Token lipsă! Accesează /token pentru a reînnoi."
    return False


def get_access_token():
    """Obține access token folosind refresh token."""
    if not auth["refresh_token"]:
        print("[INFO] Nu am refresh token, fac login...")
        if not do_login():
            return None

    # Verifică dacă access token-ul mai e valid
    if (auth["access_token"] and auth["access_expiry"] and
            datetime.now() < auth["access_expiry"]):
        return auth["access_token"]

    print("[INFO] Obțin access token...")
    url = f"{API_BASE}/getAccessToken?{API_PARAMS}"
    body = {
        "refresh_token": auth["refresh_token"],
        "company": {
            "company_id": COMPANY_ID,
            "company_type": "fleet_company"
        }
    }

    try:
        r = requests.post(url, json=body, headers=HEADERS_BASE, timeout=10)
        print(f"[DEBUG] getAccessToken → {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                token_data = data.get("data", {})
                at = token_data.get("access_token")
                expires = token_data.get("expires_in_seconds", 900)
                if at:
                    auth["access_token"]  = at
                    auth["access_expiry"] = datetime.now() + timedelta(seconds=expires - 60)
                    print(f"[✅] Access token obținut! Valabil {expires}s.")
                    return at
        elif r.status_code == 401:
            print("[INFO] Refresh token expirat, refac login...")
            auth["refresh_token"] = None
            auth["access_token"]  = None
            if do_login():
                return get_access_token()
        # Verifica code 210 - refresh token invalid
        try:
            resp_data = r.json()
            if resp_data.get("code") == 210:
                print("[WARNING] REFRESH TOKEN INVALID - reinnoieste pe /token !")
                auth["refresh_token"] = None
                auth["access_token"]  = None
                state["eroare"] = "Token expirat! Deschide /token si reinnoieste-l."
                try:
                    if os.path.exists(TOKEN_FILE):
                        os.remove(TOKEN_FILE)
                except:
                    pass
                return None
        except:
            pass
        print(f"[EROARE] getAccessToken: {r.text[:200]}")
    except Exception as e:
        print(f"[EROARE] getAccessToken: {e}")

    return None


# ──────────────────────────────────────────
#  FETCH CURSE
# ──────────────────────────────────────────

def fetch_rides(completa=True):
    access_token = get_access_token()
    if not access_token:
        raise Exception("Nu s-a putut obține access token!")

    azi    = datetime.now().strftime("%Y-%m-%d")
    viitor = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    url = (f"{API_BASE}/getScheduledRides"
           f"?{API_PARAMS}&company_id={COMPANY_ID}&user_id={USER_ID}")
    headers = {**HEADERS_BASE, "Authorization": f"Bearer {access_token}"}
    print(f"[INFO] Cerere curse: {azi} → {viitor}")

    def fetch_pagina(offset=0, limit=50):
        body = {"limit": limit, "offset": offset, "start": azi, "end": viitor}
        r = requests.post(url, json=body, headers=headers, timeout=15)
        print(f"[DEBUG] getScheduledRides offset={offset} → {r.status_code}")
        if r.status_code == 401:
            auth["access_token"] = None
            return None, "retry"
        if r.status_code == 429:
            print(f"[WARN] Rate limit 429!")
            return None, "429"
        if r.status_code != 200:
            raise Exception(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if data.get("code") == 503:
            hint = data.get("error_hint", "")
            if "token" in hint.lower():
                auth["access_token"] = None
                return None, "retry"
            raise Exception(f"NOT_AUTHORIZED: {hint}")
        if data.get("code") != 0:
            raise Exception(f"Eroare API: {data}")
        return data.get("data"), None

    # Prima pagina
    data_p1, err = fetch_pagina(offset=0, limit=50)
    if err == "retry":
        return fetch_rides()
    if not data_p1:
        raise Exception("Nu s-au primit date!")

    print("[✅] Curse primite!")

    # Verifica daca are paginare (order_ids are mai mult de 50 elemente sau exista total_count)
    order_ids_p1 = data_p1.get("order_ids", [])
    total_count = data_p1.get("total_count") or data_p1.get("total") or 0
    print(f"[INFO] Pagina 1: {len(order_ids_p1)} curse, total_count={total_count}")

    # Daca sunt exact 50 de curse si exista mai multe, citeste si restul
    if len(order_ids_p1) >= 50 and completa:
        # Combina datele din toate paginile
        cols_combined = {c["key"]: list(c["cells"]) for c in data_p1.get("columns", [])}
        order_ids_all = list(order_ids_p1)
        value_maps = {c["key"]: c.get("value_map", {}) for c in data_p1.get("columns", [])}

        offset = 50
        pagina = 2
        while True:
            time.sleep(5)  # Pauza 5 secunde intre pagini pentru rate limit
            data_pn, err = fetch_pagina(offset=offset, limit=50)
            if err == "retry":
                break
            if err == "429":
                print(f"[WARN] Rate limit la pagina {pagina} - opresc aici ({len(order_ids_all)} curse)")
                break
            if not data_pn:
                break
            if not data_pn:
                break
            order_ids_pn = data_pn.get("order_ids", [])
            if not order_ids_pn:
                break
            print(f"[INFO] Pagina {pagina}: {len(order_ids_pn)} curse")
            order_ids_all.extend(order_ids_pn)
            for col in data_pn.get("columns", []):
                key = col["key"]
                if key in cols_combined:
                    cols_combined[key].extend(col["cells"])
            if len(order_ids_pn) < 50:
                break
            offset += 50
            pagina += 1
            if pagina >= 5:
                print(f"[INFO] Limita 5 pagini atinsa (250 curse)")
                break

        # Reconstruieste structura data cu toate cursele
        data_p1["order_ids"] = order_ids_all
        for col in data_p1.get("columns", []):
            if col["key"] in cols_combined:
                col["cells"] = cols_combined[col["key"]]
        print(f"[INFO] Total {len(order_ids_all)} curse din {pagina} pagini")

    return data_p1


# ──────────────────────────────────────────
#  PARSARE
# ──────────────────────────────────────────
# Cache pentru durate ORS ca sa nu facem prea multe cereri
_durata_cache = {}
_gmaps_azi = {"data": "", "count": 0}
GMAPS_LIMIT_ZI = 30  # maxim 30 cereri Google Maps pe zi
CACHE_FILE = "gmaps_cache.json"

def load_cache():
    """Incarca cache-ul de pe disc la pornire."""
    global _durata_cache
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                _durata_cache = json.load(f)
            print(f"[✅] Cache durate încărcat: {len(_durata_cache)} intrări")
    except Exception as e:
        print(f"[WARN] Nu am putut încărca cache: {e}")

def save_cache():
    """Salveaza cache-ul pe disc."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_durata_cache, f)
    except Exception as e:
        print(f"[WARN] Nu am putut salva cache: {e}")

def geocodeaza_adresa(adresa):
    """Converteste adresa in coordonate lat/lon folosind ORS."""
    try:
        url = "https://api.openrouteservice.org/geocode/search"
        params = {
            "api_key": ORS_API_KEY,
            "text": adresa + ", București, Romania",
            "size": 1,
            "boundary.country": "RO"
        }
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            features = r.json().get("features", [])
            if features:
                coords = features[0]["geometry"]["coordinates"]
                return coords[0], coords[1]  # lon, lat
    except Exception as e:
        print(f"[WARN] Geocodare eșuată: {e}")
    return None, None

def durata_ors(pickup, dropoff):
    """Obține durata reală de la ORS Directions API."""
    cache_key = f"{pickup}|{dropoff}"
    if cache_key in _durata_cache:
        return _durata_cache[cache_key]

    try:
        lon1, lat1 = geocodeaza_adresa(pickup)
        lon2, lat2 = geocodeaza_adresa(dropoff)
        if not lon1 or not lon2:
            return None

        url = "https://api.openrouteservice.org/v2/directions/driving-car"
        headers = {
            "Authorization": ORS_API_KEY,
            "Content-Type": "application/json"
        }
        body = {
            "coordinates": [[lon1, lat1], [lon2, lat2]],
            "units": "km"
        }
        r = requests.post(url, json=body, headers=headers, timeout=8)
        if r.status_code == 200:
            data = r.json()
            durata_sec = data["routes"][0]["summary"]["duration"]
            minute = round(durata_sec / 60)
            # Validare - ignora rezultate aberante (>90 min pentru curse scurte)
            if minute > 90:
                print(f"[WARN] ORS rezultat aberant: {minute} min pentru {dist_km if 'dist_km' in dir() else '?'}km - ignorat")
                return None
            _durata_cache[cache_key] = minute
            save_cache()
            return minute
    except Exception as e:
        print(f"[WARN] ORS Directions eșuat: {e}")
    return None

def durata_gmaps(pickup, dropoff, departure_time_ts):
    """Obține durata reală de la Google Maps Distance Matrix API cu trafic live."""
    cache_key = f"gmaps|{pickup}|{dropoff}|{departure_time_ts//3600}"
    if cache_key in _durata_cache:
        return _durata_cache[cache_key]
    try:
        import urllib.parse
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        # Curata adresa si adauga context Romania
        def prep_addr(a):
            # Sterge coduri postale si adauga Romania
            import re
            a = re.sub(r'\b0\d{5}\b', '', a).strip().rstrip(',').strip()
            if 'romania' not in a.lower() and 'bucurești' not in a.lower() and 'bucharest' not in a.lower():
                a += ', București, Romania'
            return a

        params = {
            "origins": prep_addr(pickup),
            "destinations": prep_addr(dropoff),
            "mode": "driving",
            "departure_time": int(departure_time_ts),
            "traffic_model": "best_guess",
            "language": "ro",
            "key": GMAPS_API_KEY
        }
        r = requests.get(url, params=params, timeout=8)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "OK":
                element = data["rows"][0]["elements"][0]
                if element.get("status") == "OK":
                    # Prefer duration_in_traffic daca e disponibil
                    if "duration_in_traffic" in element:
                        sec = element["duration_in_traffic"]["value"]
                    else:
                        sec = element["duration"]["value"]
                    minute = round(sec / 60)
                    _durata_cache[cache_key] = minute
                    save_cache()
                    print(f"[GMAPS] {pickup[:20]}→{dropoff[:20]}: {minute} min")
                    return minute
            print(f"[WARN] GMAPS status: {data.get('status')} | {data.get('error_message','')}")
    except Exception as e:
        print(f"[WARN] Google Maps eroare: {e}")
    return None

def calculeaza_durata_fallback(dist_km, ora_timestamp):
    """Fallback estimare locala daca ORS nu raspunde."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz_ro = ZoneInfo("Europe/Bucharest")
    dt_ro = datetime.fromtimestamp(ora_timestamp, tz=tz_ro)
    ora = dt_ro.hour
    minut = dt_ro.minute
    ora_decimal = ora + minut / 60.0
    if   0.0 <= ora_decimal < 5.0:   viteza = 50
    elif 5.0 <= ora_decimal < 6.0:   viteza = 45
    elif 6.0 <= ora_decimal < 7.0:   viteza = 18
    elif 7.0 <= ora_decimal < 9.5:   viteza = 17
    elif 9.5 <= ora_decimal < 12.0:  viteza = 30
    elif 12.0 <= ora_decimal < 14.0: viteza = 24
    elif 14.0 <= ora_decimal < 17.0: viteza = 30
    elif 17.0 <= ora_decimal < 20.0: viteza = 15
    elif 20.0 <= ora_decimal < 23.0: viteza = 35
    else:                             viteza = 50
    return max(3, round((dist_km / viteza) * 60))



def calculeaza(data):
    if not data or "columns" not in data:
        return []

    cols      = {c["key"]: c["cells"] for c in data["columns"]}
    order_ids = data.get("order_ids", [])

    scheduled  = cols.get("scheduled_for", [])
    routes     = cols.get("route", [])
    categories = cols.get("category", [])
    distances  = cols.get("distance", [])
    prices     = cols.get("price", [])
    payments   = cols.get("payment_method", [])
    drivers    = cols.get("driver", [])

    payment_map = {}
    for col in data["columns"]:
        if col["key"] == "payment_method":
            payment_map = col.get("value_map", {})

    curse = []
    for i in range(len(scheduled)):
        try:
            pret    = float(prices[i])    if i < len(prices)    else 0
            dist_km = float(distances[i]) if i < len(distances) else 0
            if dist_km <= 0 or pret <= 0:
                continue

            lei_km     = round(pret / dist_km, 2)
            pret_net   = round(pret * 0.75, 2)
            lei_km_net = round(pret_net / dist_km, 2)
            ts     = scheduled[i] if i < len(scheduled) else 0
            from zoneinfo import ZoneInfo
            tz_ro  = ZoneInfo("Europe/Bucharest")
            dt_ro  = datetime.fromtimestamp(ts, tz=tz_ro) if ts else None
            ora    = dt_ro.strftime("%H:%M") if dt_ro else "N/A"
            prog   = dt_ro.strftime("%d.%m %H:%M") if dt_ro else "N/A"

            ruta    = routes[i]     if i < len(routes)     else ["N/A", "N/A"]
            pickup  = ruta[0]       if len(ruta) > 0       else "N/A"
            dropoff = ruta[1]       if len(ruta) > 1       else "N/A"
            categ   = categories[i] if i < len(categories) else "N/A"
            plata   = payment_map.get(
                payments[i] if i < len(payments) else "", "N/A")
            driver  = drivers[i]    if i < len(drivers)    else {}
            sofer   = driver.get("name", "Neatribuit") if isinstance(driver, dict) else "Neatribuit"

            # Detecteaza curse aeroport
            aeroport_keywords = ["aeroport", "airport", "otopeni", "otp", "henri coanda", "baneasa", "aurel vlaicu"]
            este_aeroport = any(kw in pickup.lower() or kw in dropoff.lower() for kw in aeroport_keywords)

            # Detecteaza preluare din afara Bucurestiului (Ilfov)
            ilfov_keywords = [
                "voluntari", "otopeni", "ilfov", "mogosoaia", "bragadiru",
                "popesti", "popeşti", "comuna pantelimon", "pantelimon 077", "dobroesti", "dobroeşti",
                "chiajna", "clinceni", "corbeanca", "tunari", "stefanestii",
                "ştefăneştii", "balotesti", "baloteşti", "snagov", "afumati",
                "afumaţi", "dragomiresti", "dragomireşti", "1 decembrie",
                "magurele", "măgurele", "jilava", "glina", "cernica",
                "branesti", "brăneşti", "ciorogarla", "cioro", "chitila",
                "dudu", "rosu", "roşu", "dimieni", "gruiu", "peris", "periş",
                "judetul ilfov", "județul ilfov", "com.", "comuna",
                "henri coanda", "departures, bucharest henri coanda", "plecări, aeroportul henri coandă"
            ]
            import re
            # Pickup din afara Bucurestiului
            pickup_ilfov = any(kw in pickup.lower() for kw in ilfov_keywords)
            if not pickup_ilfov:
                pickup_ilfov = len(re.findall(r'\b077\d{3}\b', pickup)) > 0
            # Dropoff in afara Bucurestiului
            dropoff_ilfov = any(kw in dropoff.lower() for kw in ilfov_keywords)
            if not dropoff_ilfov:
                dropoff_ilfov = len(re.findall(r'\b077\d{3}\b', dropoff)) > 0
            este_ilfov = pickup_ilfov or dropoff_ilfov

            curse.append({
                "ts":       ts,
                "aeroport": este_aeroport,
                "ilfov":        este_ilfov,
                "pickup_ilfov": pickup_ilfov,
                "dropoff_ilfov": dropoff_ilfov,
                "durata":   calculeaza_durata_fallback(dist_km, ts) if ts else 0,
                "durata_ors": False,
                "ora":      ora,
                "prog":     prog,
                "order_id": order_ids[i] if i < len(order_ids) else "N/A",
                "categ":    categ,
                "pickup":   pickup,
                "dropoff":  dropoff,
                "dist":     dist_km,
                "pret":      pret,
                "pret_net":  pret_net,
                "lei_km_net": lei_km_net,
                "lei_km":   lei_km,
                "plata":    plata,
                "sofer":    sofer,
                "ok":       lei_km_net >= PRAG_MINIM_LEI_KM,
                "lunga":    dist_km >= 20,
            })
        except Exception as e:
            print(f"[WARN] Cursă {i}: {e}")

    # Filtreaza cursele trecute (mai vechi de 15 minute)
    acum = datetime.now().timestamp()
    curse = [c for c in curse if c["ts"] > acum - 900]  # 15 minute toleranta
    # Deduplicare dupa order_id
    vazute = set()
    curse_unice = []
    for c in curse:
        oid = c.get("order_id")
        if oid and oid not in vazute:
            vazute.add(oid)
            curse_unice.append(c)
        elif not oid:
            curse_unice.append(c)
    curse = curse_unice

    curse.sort(key=lambda x: (x["ts"], -x["lei_km_net"]))
    return curse


def trimite_telegram(mesaj):
    """Trimite notificare pe Telegram."""
    try:
        for chat_id in TELEGRAM_CHAT_IDS:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            requests.post(url, json={
                "chat_id": chat_id,
                "text": mesaj,
                "parse_mode": "HTML"
            }, timeout=10)
            print(f"[✅] Telegram trimis la {chat_id}")
    except Exception as e:
        print(f"[WARN] Telegram eroare: {e}")

def trimite_push(titlu, mesaj):
    """Trimite notificare push - placeholder."""
    print(f"[PUSH] {titlu}: {mesaj} ({len(push_subscriptions)} subscriberi)")

def imbunatateste_durate(curse):
    """Actualizeaza duratele cu ORS in background (Google Maps doar la cerere)."""
    ok = 0
    for c in curse:
        try:
            # Verifica mai intai cache-ul GMAPS
            cache_key = f"gmaps|{c['pickup']}|{c['dropoff']}|{c['ts']//3600}"
            if cache_key in _durata_cache:
                c["durata"] = _durata_cache[cache_key]
                c["durata_ors"] = True
                ok += 1
                continue
            # Foloseste ORS gratuit
            d = durata_ors(c["pickup"], c["dropoff"])
            if d:
                c["durata"] = d
                c["durata_ors"] = True
                ok += 1
        except:
            pass
    print(f"[✅] Durate actualizate pentru {ok}/{len(curse)} curse.")

# ──────────────────────────────────────────
#  BACKGROUND LOOP
# ──────────────────────────────────────────

def do_refresh(citire_completa=True):
    state["se_incarca"] = True
    state["status"]     = "Se actualizează..."
    state["eroare"]     = None
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Actualizez cursele...")
    try:
        data  = fetch_rides(completa=citire_completa)
        curse = calculeaza(data) if data else []
        state["curse"]         = curse
        from zoneinfo import ZoneInfo
        state["ultima_update"] = datetime.now(ZoneInfo("Europe/Bucharest")).strftime("%H:%M:%S")
        state["status"]        = f"✅ {len(curse)} curse"
        print(f"[✅] {len(curse)} curse procesate.")
        # Verifica curse peste 100 lei si trimite push
        prag_push = state.get("prag_push", 100)
        # Exclude cursele deja rezervate din notificari Telegram
        # Nota: rezervate se stocheaza in browser, nu pe server
        # Folosim doar filtrul de categorie Comfort daca e relevant
        curse_top = [c for c in curse if c["pret_net"] >= prag_push and c.get("order_id") not in rezervate_server]
        # Filtrare curse noi - nu trimite notificare pentru aceeasi cursa de doua ori
        curse_noi = [c for c in curse_top if c.get("order_id") not in ultimele_notificari]
        if curse_noi:
            best = max(curse_noi, key=lambda x: x["pret_net"])
            ultimele_notificari.add(best.get("order_id"))
            # Curata notificarile vechi (curse care nu mai sunt in lista)
            order_ids_curente = {c.get("order_id") for c in curse}
            ultimele_notificari.intersection_update(order_ids_curente)
            emoji = "✈️ " if best.get("aeroport") else "🚕 "
            ilfov = " 📍 Ilfov" if best.get("ilfov") else ""
            lunga = " 🛣️ Cursă lungă" if best.get("lunga") else ""
            mesaj_tg = (
                f"🔥 <b>CURSĂ DISPONIBILĂ!</b>\n\n"
                f"{emoji}<b>{best['pret_net']} LEI net</b> ({best['pret']} brut)\n"
                f"⏰ <b>{best['ora']}</b> · {best['dist']}km · {best['lei_km_net']} lei/km net\n"
                f"📍 {best['pickup']}\n"
                f"🏁 {best['dropoff']}"
                f"{ilfov}{lunga}\n\n"
                f"👉 https://bolt-fleet-axp.onrender.com"
            )
            threading.Thread(target=trimite_telegram, args=(mesaj_tg,), daemon=True).start()
            threading.Thread(target=trimite_push, args=("🔥 Cursă mare disponibilă!", f"{emoji}{best['pret_net']} LEI · {best['ora']}"), daemon=True).start()
        # Actualizeaza duratele cu ORS in background
        threading.Thread(target=imbunatateste_durate, args=(curse,), daemon=True).start()
    except Exception as e:
        state["eroare"] = str(e)
        state["status"] = "❌ Eroare"
        print(f"[EROARE] {e}")
    state["se_incarca"] = False


def refresh_loop():
    while True:
        try:
            do_refresh(citire_completa=True)
        except Exception as e:
            print(f"[EROARE] refresh_loop: {e}")
        time.sleep(INTERVAL_MINUTE * 60)


# ──────────────────────────────────────────
#  HTML MOBIL
# ──────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="theme-color" content="#0a0a0f">
<title>Bolt Fleet AXP - Login</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');
  :root { --bg:#0a0a0f; --surface:#13131a; --border:#1e1e2e; --green:#00e676; --red:#ff3d57; --text:#e8e8f0; --muted:#6b6b80; --bolt:#34d186; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'DM Mono',monospace; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:24px; padding:36px 28px; width:100%; max-width:360px; }
  .logo { font-family:'Syne',sans-serif; font-size:22px; font-weight:800; background:linear-gradient(135deg,var(--bolt),#00b4d8); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin-bottom:6px; }
  .subtitle { font-size:12px; color:var(--muted); margin-bottom:28px; }
  label { font-size:11px; color:var(--muted); display:block; margin-bottom:6px; letter-spacing:.5px; }
  input { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:14px; color:var(--text); font-family:'DM Mono',monospace; font-size:15px; outline:none; transition:border-color .2s; margin-bottom:16px; }
  input:focus { border-color:var(--bolt); }
  .btn { width:100%; background:var(--bolt); color:#000; border:none; border-radius:12px; padding:14px; font-family:'DM Mono',monospace; font-size:15px; font-weight:600; cursor:pointer; transition:opacity .15s; }
  .btn:active { opacity:.7; }
  .error { background:rgba(255,61,87,.1); border:1px solid var(--red); color:var(--red); border-radius:10px; padding:10px 14px; font-size:12px; margin-bottom:16px; text-align:center; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">⚡ BOLT FLEET AXP</div>
  <div class="subtitle">Autentificare necesară</div>
  ERROR_PLACEHOLDER
  <label>PAROLĂ</label>
  <form method="POST" action="/login">
    <input type="password" name="password" placeholder="Introduceți parola..." autofocus>
    <button class="btn" type="submit">🔓 Intră</button>
  </form>
</div>
</body>
</html>"""

HTML = """<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="theme-color" content="#0a0a0f">
<meta name="apple-mobile-web-app-capable" content="yes">
<link rel="manifest" href="/manifest.json">
<title>Bolt Fleet AXP</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');
  :root {
    --bg:#0a0a0f; --surface:#13131a; --border:#1e1e2e;
    --green:#00e676; --yellow:#ffd600; --red:#ff3d57;
    --text:#e8e8f0; --muted:#6b6b80; --bolt:#34d186;
  }
  * { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
  body { background:var(--bg); color:var(--text); font-family:'DM Mono',monospace; min-height:100vh; padding-bottom:80px; }

  .header {
    position:sticky; top:0; z-index:100;
    background:rgba(10,10,15,0.96); backdrop-filter:blur(16px);
    border-bottom:1px solid var(--border); padding:14px 16px 10px;
  }
  .header-top { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
  .logo {
    font-family:'Syne',sans-serif; font-size:20px; font-weight:800;
    background:linear-gradient(135deg,var(--bolt),#00b4d8);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent;
  }
  .badge { font-size:11px; padding:4px 10px; border-radius:20px; border:1px solid var(--border); background:var(--surface); color:var(--muted); }
  .badge.ok { border-color:var(--green); color:var(--green); background:rgba(0,230,118,.08); }
  .badge.err { border-color:var(--red); color:var(--red); background:rgba(255,61,87,.08); }
  .badge.loading { animation:pulse 1.2s infinite; }
  .best-card { text-align:right; cursor:pointer; }
  .best-pret { font-family:'Syne',sans-serif; font-size:24px; font-weight:800; color:var(--green); line-height:1; }
  .best-info { font-size:16px; color:var(--muted); margin-top:4px; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .stats { display:grid; grid-template-columns:repeat(5,1fr); gap:6px; }
  .stat { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:8px 4px; text-align:center; }
  .stat-click { cursor:pointer; transition:all .15s; }
  .stat-click:active { transform:scale(.95); opacity:.7; }
  .stat-click.activ { border-color:var(--bolt); background:rgba(52,209,134,.08); }
  .stat-val { font-family:'Syne',sans-serif; font-size:17px; font-weight:700; line-height:1; }
  .stat-label { font-size:8px; color:var(--muted); margin-top:3px; }
  .g { color:var(--green); } .y { color:var(--yellow); } .r { color:var(--red); } .b { color:var(--bolt); }

  .controls { display:flex; gap:8px; padding:10px 12px 4px; align-items:center; }
  .btn-refresh {
    flex:1; background:var(--bolt); color:#000; border:none; border-radius:12px;
    padding:12px; font-family:'DM Mono',monospace; font-size:13px; font-weight:500;
    cursor:pointer; display:flex; align-items:center; justify-content:center; gap:6px;
    transition:opacity .15s,transform .1s;
  }
  .btn-refresh:active { opacity:.7; transform:scale(.98); }
  .btn-refresh:disabled { opacity:.4; cursor:not-allowed; }
  .filters { display:flex; gap:6px; }
  .fbtn {
    background:var(--surface); border:1px solid var(--border); color:var(--muted);
    border-radius:10px; padding:10px; font-size:11px; font-family:'DM Mono',monospace;
    cursor:pointer; transition:all .15s;
  }
  .fbtn.active { border-color:var(--bolt); color:var(--bolt); background:rgba(52,209,134,.1); }

  .update-info { text-align:center; font-size:10px; color:var(--muted); padding:5px 12px; }
  .err-banner { margin:12px; padding:14px; background:rgba(255,61,87,.1); border:1px solid var(--red); border-radius:12px; font-size:12px; color:var(--red); line-height:1.6; }

  .list { padding:0 12px; display:flex; flex-direction:column; gap:8px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:12px 14px; transition:transform .1s; border-left-width:3px; }
  .card:active { transform:scale(.99); }
  .card.bun { border-left-color:var(--green); }
  .card.ilfov { border-left-color:#a855f7; }
  .ilfov-row { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:8px; }
  .ilfov-badge { display:inline-block; background:rgba(168,85,247,.15); border:1px solid rgba(168,85,247,.5); color:#a855f7; border-radius:6px; padding:3px 9px; font-size:12px; }
  .ilfov-dest { background:rgba(251,146,60,.15); border-color:rgba(251,146,60,.5); color:#fb923c; }
  .lunga-badge { background:rgba(99,102,241,.15); border:1px solid rgba(99,102,241,.5); color:#818cf8; border-radius:6px; padding:3px 9px; font-size:12px; }
  .card.mediu { border-left-color:var(--yellow); }
  .card.slab { border-left-color:var(--red); }

  .card-top { display:flex; justify-content:space-between; align-items:flex-start; margin-bottom:8px; }
  .card-ora { font-family:'Syne',sans-serif; font-size:24px; font-weight:800; line-height:1; }
  .card-data { font-size:11px; color:var(--muted); margin-top:3px; letter-spacing:.3px; }
  .lk-val { font-family:'Syne',sans-serif; font-size:22px; font-weight:700; text-align:right; line-height:1; }
  .lk-unit { font-size:9px; color:var(--muted); text-align:right; }
  .card-ruta { font-size:12px; margin-bottom:10px; }
  .addr-line { color:var(--text); line-height:1.5; word-break:break-word; }
  .addr-arrow { color:var(--bolt); padding:1px 0; font-size:14px; }
  .tags { display:flex; gap:5px; flex-wrap:wrap; }
  .tag { background:rgba(255,255,255,.04); border:1px solid var(--border); border-radius:6px; padding:3px 7px; font-size:10px; color:var(--muted); }
  .tag.categ { color:var(--bolt); border-color:rgba(52,209,134,.3); }
  .card-pret-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .tag-km { background:rgba(0,180,216,.12); border:1px solid rgba(0,180,216,.4); border-radius:8px; padding:4px 10px; font-family:'Syne',sans-serif; font-size:16px; font-weight:800; color:#00b4d8; letter-spacing:-0.5px; }
  .km-unit { font-size:11px; font-weight:500; opacity:.8; }
  .pret-bloc { margin-left:auto; text-align:right; }
  .btn-rezervata { background:rgba(0,230,118,.1); border:1px solid rgba(0,230,118,.3); color:var(--green); border-radius:10px; padding:6px 12px; font-size:11px; cursor:pointer; font-family:'DM Mono',monospace; transition:all .15s; }
  .btn-rezervata.bifata { background:rgba(0,230,118,.25); border-color:var(--green); color:var(--green); }
  .card.rezervata { opacity:.45; filter:grayscale(.5); }
  .btn-atribuie {
    display:block; margin-top:10px;
    background:rgba(52,209,134,.12);
    border:1px solid rgba(52,209,134,.4);
    color:var(--bolt); border-radius:10px;
    padding:10px; text-align:center;
    font-size:14px; font-weight:600;
    text-decoration:none;
    transition:all .15s;
  }
  .btn-atribuie:active { background:rgba(52,209,134,.25); transform:scale(.98); }
  .pret-brut { font-family:'Syne',sans-serif; font-size:13px; font-weight:600; color:var(--muted); line-height:1.4; }
  .pret-net  { font-family:'Syne',sans-serif; font-size:22px; font-weight:800; line-height:1; }
  .pret-unit { font-size:10px; font-weight:500; opacity:.8; }

  .empty { text-align:center; padding:60px 20px; color:var(--muted); }
  .empty-icon { font-size:48px; margin-bottom:12px; }

  .toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:var(--green); color:#000; padding:10px 20px; border-radius:20px; font-size:13px; font-weight:500; opacity:0; pointer-events:none; transition:opacity .3s; z-index:999; white-space:nowrap; max-width:90vw; text-align:center; }
  .toast.show { opacity:1; }
</style>
</head>
<body>
<div class="header">
  <div class="header-top">
    <div class="logo">⚡ BOLT FLEET <span style="white-space:nowrap"><svg width="18" height="18" viewBox="0 0 100 100" style="display:inline-block;vertical-align:middle;margin:0 2px;" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="50" rx="48" ry="48" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="50" rx="30" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="32" rx="18" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/></svg>AXP<svg width="18" height="18" viewBox="0 0 100 100" style="display:inline-block;vertical-align:middle;margin:0 2px;" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="50" rx="48" ry="48" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="50" rx="30" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="32" rx="18" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/></svg></span></div>
    <div class="best-card" id="bestCard" onclick="gotoBest()" style="cursor:pointer">
      <div class="best-pret" id="bestPret">—</div>
      <div class="best-info" id="bestInfo">cea mai bună</div>
    </div>
  </div>
  <div class="stats">
    <div class="stat stat-click" onclick="setF('all')"><div class="stat-val b" id="sT">—</div><div class="stat-label">CURSE</div></div>
    <div class="stat stat-click" onclick="setF('bune')"><div class="stat-val g" id="sB">—</div><div class="stat-label">BUNE ✅</div></div>
    <div class="stat stat-click" onclick="setF('slabe')"><div class="stat-val r" id="sS">—</div><div class="stat-label">SLABE ⚠️</div></div>
    <div class="stat stat-click" onclick="setF('medii')"><div class="stat-val y" id="sM">—</div><div class="stat-label">MED</div></div>
    <div class="stat stat-click" onclick="gotoMax()"><div class="stat-val g" id="sX">—</div><div class="stat-label" id="sMaxLabel">MAX net</div></div>
  </div>
</div>

<div class="controls">
  <button class="btn-refresh" id="btnR" onclick="refresh()">
    <span id="rIcon">↺</span> Actualizează
  </button>
  <div class="filters">
    <button class="fbtn active" id="f-all"   onclick="setF('all')">Toate</button>
    <button class="fbtn"        id="f-bune"  onclick="gotoBest()">✅</button>
    <button class="fbtn"        id="f-slabe" onclick="setF('slabe')">⚠️</button>
  </div>
</div>

<div class="update-info" id="updInfo">—</div>
<div style="display:flex; justify-content:center; gap:12px; padding:4px 12px 8px; flex-wrap:wrap;">
  <a href="/token" style="font-size:11px; color:var(--muted); text-decoration:none; padding:4px 10px; border:1px solid var(--border); border-radius:8px; background:var(--surface);">🔑 Token</a>
  <button id="btnRezervate" onclick="toggleShowRezervate()" style="font-size:11px; color:var(--muted); padding:4px 10px; border:1px solid var(--border); border-radius:8px; background:var(--surface); cursor:pointer; font-family:'DM Mono',monospace;">✓ Rezervate (0)</button>
  <button id="btnComfort" onclick="toggleComfort()" style="font-size:11px; padding:4px 10px; border:1px solid rgba(0,230,118,.3); border-radius:8px; background:var(--surface); cursor:pointer; font-family:'DM Mono',monospace; color:var(--green);">🟢 Comfort</button>
  <button id="btnNotif" onclick="activeazaNotificari()" style="font-size:11px; color:var(--muted); padding:4px 10px; border:1px solid var(--border); border-radius:8px; background:var(--surface); cursor:pointer; font-family:'DM Mono',monospace;">🔕 Notificări</button>
  <select id="selPrag" onchange="setPragPush(this.value)" style="font-size:11px; color:var(--muted); padding:4px 8px; border:1px solid var(--border); border-radius:8px; background:var(--surface); cursor:pointer; font-family:'DM Mono',monospace;">
    <option value="10">🔔 &gt;10 lei</option>
    <option value="15">🔔 &gt;15 lei</option>
    <option value="20">🔔 &gt;20 lei</option>
    <option value="25" selected>🔔 &gt;25 lei</option>
    <option value="30">🔔 &gt;30 lei</option>
    <option value="50">🔔 &gt;50 lei</option>
    <option value="100">🔔 &gt;100 lei</option>
  </select>
  <a href="/logout" style="font-size:11px; color:var(--muted); text-decoration:none; padding:4px 10px; border:1px solid var(--border); border-radius:8px; background:var(--surface);">🚪 Logout</a>
</div>
<div id="errDiv"></div>
<div class="list" id="list"></div>
<div class="toast" id="toast"></div>

<div id="popup-overlay" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:1000; align-items:flex-end; justify-content:center;" onclick="inchidePopup()">
  <div id="popup-card" style="background:#13131a; border:1px solid #1e1e2e; border-radius:20px 20px 0 0; padding:24px 20px 36px; width:100%; max-width:480px; margin:0 auto;" onclick="event.stopPropagation()">
    <div style="font-family:'Syne',sans-serif; font-size:13px; color:#6b6b80; margin-bottom:12px; text-align:center;">REZERVĂ CURSA ÎN PORTAL</div>
    <div id="popup-content"></div>
    <div id="gmaps-result" style="display:none; margin-top:10px; padding:10px; background:rgba(66,133,244,.1); border:1px solid rgba(66,133,244,.4); border-radius:10px; font-size:14px; color:#4285f4; text-align:center;"></div>
    <div style="display:flex; gap:10px; margin-top:16px; flex-wrap:wrap;">
      <button onclick="inchidePopup()" style="flex:1; min-width:80px; background:#1e1e2e; color:#e8e8f0; border:1px solid #1e1e2e; border-radius:12px; padding:12px; font-size:13px; cursor:pointer;">✕</button>
      <button id="btn-gmaps" onclick="calculeazaGmaps()" style="flex:1; min-width:100px; background:rgba(66,133,244,.15); color:#4285f4; border:1px solid rgba(66,133,244,.4); border-radius:12px; padding:12px; font-size:13px; cursor:pointer;">🗺 Google Maps</button>
      <a id="popup-link" href="" target="_blank" onclick="inchidePopup()" style="flex:2; min-width:120px; background:#34d186; color:#000; border:none; border-radius:12px; padding:12px; font-size:13px; font-weight:700; text-align:center; text-decoration:none; display:flex; align-items:center; justify-content:center; gap:6px;">🚕 Rezervă</a>
    </div>
  </div>
</div>

<script>
let data=[], filtru='all';
const PRAG=PRAG_VAL, INTERVAL=INTERVAL_VAL;
let lastNotif=null;
let showRezervate = false;

function toggleShowRezervate() {
  showRezervate = !showRezervate;
  const btn = document.getElementById('btnRezervate');
  btn.style.color = showRezervate ? 'var(--green)' : 'var(--muted)';
  btn.style.borderColor = showRezervate ? 'rgba(0,230,118,.3)' : 'var(--border)';
  render();
  updateBestCard();
}

function updateRezervateBtn() {
  const btn = document.getElementById('btnRezervate');
  if(!btn) return;
  btn.textContent = '✓ Rezervate (' + rezervate.size + ')';
  btn.style.color = showRezervate ? 'var(--green)' : 'var(--muted)';
  btn.style.borderColor = showRezervate ? 'rgba(0,230,118,.3)' : 'var(--border)';
}
let showComfort = localStorage.getItem('showComfort') !== 'false';

function updateComfortBtn() {
  const btn = document.getElementById('btnComfort');
  if(!btn) return;
  const nrComfort = data.filter(c=>c.categ==='Comfort').length;
  btn.textContent = (showComfort ? '🟢' : '🔴') + ' Comfort' + (nrComfort > 0 ? ' ('+nrComfort+')' : '');
  btn.style.color = showComfort ? 'var(--green)' : 'var(--red)';
  btn.style.borderColor = showComfort ? 'rgba(0,230,118,.3)' : 'rgba(255,61,87,.3)';
}

function toggleComfort() {
  showComfort = !showComfort;
  localStorage.setItem('showComfort', showComfort);
  updateComfortBtn();
  render();
}
const rezervate = new Set(JSON.parse(localStorage.getItem('rezervate')||'[]'));

function toggleRezervata(orderId) {
  if(rezervate.has(orderId)) {
    rezervate.delete(orderId);
  } else {
    rezervate.add(orderId);
  }
  localStorage.setItem('rezervate', JSON.stringify([...rezervate]));
  fetch('/api/rezervate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ids: [...rezervate]})
  }).catch(()=>{});
  render();
  updateBestCard();
  updateComfortBtn();
  updateRezervateBtn();
}

function updateBestCard() {
  // Exclude rezervate si Comfort daca e ascuns
  let disponibile = data.filter(c => !rezervate.has(c.order_id));
  if(!showComfort) disponibile = disponibile.filter(c => c.categ !== 'Comfort');
  const sorted = [...disponibile].sort((a,b) => b.pret_net - a.pret_net);
  if(sorted.length) {
    const best = sorted[0];
    const avion = best.aeroport ? '✈️ ' : '';
    document.getElementById('bestPret').textContent = avion + best.pret_net + ' LEI';
    const pickup = best.pickup.length > 22 ? best.pickup.slice(0,22)+'…' : best.pickup;
    document.getElementById('bestInfo').textContent = best.ora + ' · ' + best.dist + 'km · ' + pickup;
  } else {
    document.getElementById('bestPret').textContent = '—';
    document.getElementById('bestInfo').textContent = 'nicio cursă';
  }
}
let lastUpdate='';
let countdown=INTERVAL*60;

function formatCountdown(sec){
  const m=Math.floor(sec/60);
  const s=sec%60;
  return m+':'+(s<10?'0':'')+s+' ⏳';
}

// Cronometru invers
setInterval(()=>{
  if(countdown>0){
    countdown--;
    if(lastUpdate)
      document.getElementById('updInfo').textContent=
        `Actualizat: ${lastUpdate} · Următor: ${formatCountdown(countdown)}`;
  }
}, 1000);

function setF(f){
  filtru=f;
  ['all','bune','slabe'].forEach(x=>document.getElementById('f-'+x).classList.toggle('active',x===f));
  render();
}

function render(){
  const list=document.getElementById('list');
  let curse=data;
  if(filtru==='bune')  curse=curse.filter(c=>c.lei_km_net>=PRAG*1.5);
  if(filtru==='medii') curse=curse.filter(c=>c.lei_km_net>=PRAG && c.lei_km_net<PRAG*1.5);
  if(filtru==='slabe') curse=curse.filter(c=>c.lei_km_net<PRAG);
  if(showRezervate) curse=curse.filter(c=>rezervate.has(c.order_id));
  else curse=curse.filter(c=>!rezervate.has(c.order_id));
  if(!showComfort) curse=curse.filter(c=>c.categ!=='Comfort');
  if(!curse.length){
    list.innerHTML=`<div class="empty"><div class="empty-icon">🚕</div><div>Nicio cursă ${filtru!=='all'?'în această categorie':'disponibilă'}</div></div>`;
    return;
  }
  list.innerHTML=curse.map(c=>{
    const cls=c.lei_km_net>=PRAG*1.5?'bun':c.lei_km_net>=PRAG?'mediu':'slab';
    const ilfovCls=c.ilfov?' ilfov':'';
    const ilfovBadge=[
      c.pickup_ilfov ? '<span class="ilfov-badge">🚩 Preluare în afara Buc.</span>' : '',
      c.dropoff_ilfov ? '<span class="ilfov-badge ilfov-dest">🏁 Destinație în afara Buc.</span>' : '',
      c.lunga ? '<span class="lunga-badge">🛣️ Cursă lungă +20km</span>' : ''
    ].filter(Boolean).join('');
    const ilfovRow = ilfovBadge ? `<div class="ilfov-row">${ilfovBadge}</div>` : '';
    const col=c.lei_km_net>=PRAG*1.5?'var(--green)':c.lei_km_net>=PRAG?'var(--yellow)':'var(--red)';
    const data=c.prog.split(' ')[0];
    const eRezervata = rezervate.has(c.order_id);
    return `<div class="card ${cls}${ilfovCls}${eRezervata?' rezervata':''}" data-order="${c.order_id}">
      <div style="display:flex;justify-content:flex-end;margin-bottom:4px;">
        <button class="btn-rezervata${eRezervata?' bifata':''}" data-oid="${c.order_id}">
          ${eRezervata?'✓ Rezervată':'○ Rezervă'}
        </button>
      </div>
      ${ilfovRow}
      <div class="card-top">
        <div>
          <div class="card-ora">${c.ora} ${c.aeroport ? '✈️' : ''}</div>
          <div class="card-data">${data}</div>
        </div>
        <div><div class="lk-val" style="color:${col}">${c.lei_km_net}</div><div class="lk-unit">LEI/KM net</div></div>
      </div>
      <div class="card-ruta">
        <div class="addr-line">📍 ${c.pickup}</div>
        <div class="addr-arrow">↓</div>
        <div class="addr-line">🏁 ${c.dropoff}</div>
        <div class="durata-line">${c.durata_ors ? '🚗 → 📍' : '🕐'} ${c.durata_ors ? '' : 'aprox. '}${c.durata < 60 ? c.durata+' min' : Math.floor(c.durata/60)+'h '+(c.durata%60 ? (c.durata%60)+'min' : '')}</div>
      </div>
      <div class="card-pret-row">
        <span class="tag categ">${c.categ}</span>
        <span class="tag-km">${c.dist} <span class="km-unit">km</span></span>
        <span class="tag">${c.plata}</span>
        <div class="pret-bloc">
          <div class="pret-brut">${c.pret} <span class="pret-unit">LEI brut</span></div>
          <div class="pret-net" style="color:${col}">${c.pret_net} <span class="pret-unit">LEI net</span></div>
        </div>
      </div>
      <button class="btn-atribuie" onclick="rezerva(event, '${c.ora}', '${c.prog}', '${c.pickup.substring(0,40)}', '${c.dropoff.substring(0,40)}', '${c.dist}', '${c.pret_net}', '${c.durata}', ${c.durata_ors}, ${c.ts})">
        🚕 Rezervă cursa
      </button>
    </div>`;
  }).join('');
  // Re-aplica starea rezervate dupa render

}

function stats(curse){
  const b=curse.filter(c=>c.lei_km_net>=PRAG*1.5),med=curse.filter(c=>c.lei_km_net>=PRAG&&c.lei_km_net<PRAG*1.5),s=curse.filter(c=>c.lei_km_net<PRAG);
  const m=curse.length?(curse.reduce((a,c)=>a+c.lei_km_net,0)/curse.length).toFixed(1):'—';
  const x=curse.length?Math.max(...curse.map(c=>c.lei_km_net)).toFixed(1):'—';
  document.getElementById('sT').textContent=curse.length;
  document.getElementById('sB').textContent=b.length;
  document.getElementById('sS').textContent=s.length;
  document.getElementById('sM').textContent=med.length;
  document.getElementById('sMaxLabel') && (document.getElementById('sM').title='Click pentru curse medii');
  document.getElementById('sX').textContent=x;
  document.getElementById('sMaxLabel').textContent='MAX net';
}

function toast(msg,bg='var(--green)'){
  const t=document.getElementById('toast');
  t.textContent=msg; t.style.background=bg;
  t.style.color=bg==='var(--green)'?'#000':'#fff';
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3500);
}

async function load(){
  // Sincronizeaza rezervatele cu serverul la primul load
  if(rezervate.size > 0) {
    fetch('/api/rezervate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: [...rezervate]})
    }).catch(()=>{});
  }
  // Initializeaza butonul Comfort
  const btn = document.getElementById('btnComfort');
  if(btn) {
    updateComfortBtn();
  }
  try{
    const r=await fetch('/api/curse');
    const d=await r.json();
    const errDiv=document.getElementById('errDiv');
    if(d.eroare){
      errDiv.innerHTML=`<div class="err-banner">⚠️ ${d.eroare}</div>`;
    } else {
      errDiv.innerHTML='';
    }
    data=d.curse||[];
    // Curata rezervarile pentru curse care nu mai exista
    const orderIds = new Set(data.map(c=>c.order_id));
    for(const id of [...rezervate]) {
      if(!orderIds.has(id)) rezervate.delete(id);
    }
    localStorage.setItem('rezervate', JSON.stringify([...rezervate]));
    stats(data); render();
    updateBestCard();
    updateComfortBtn();
    updateRezervateBtn();
    if(d.prag_push) syncPragSelector(d.prag_push);
    if(d.ultima_update && d.ultima_update !== lastUpdate){
      lastUpdate = d.ultima_update;
      countdown = INTERVAL * 60;
    }
    if(lastUpdate){
      document.getElementById('updInfo').textContent=`Actualizat: ${lastUpdate} · Următor: ${formatCountdown(countdown)}`;
    }
    const top=data.filter(c=>c.lei_km_net>=PRAG*1.5).sort((a,b)=>b.lei_km_net-a.lei_km_net);
    if(top.length){
      const key=top[0].order_id+''+top[0].lei_km_net;
      if(key!==lastNotif){ lastNotif=key; toast(`🔥 ${top[0].lei_km_net} lei/km net • ${top[0].ora} • ${top[0].dist}km`); }
    }
  }catch(e){console.error(e);}
}

function rezerva(event, ora, prog, pickup, dropoff, dist, pret_net, durata, durata_ors, ts) {
  event.stopPropagation();
  _popupData = {pickup: pickup, dropoff: dropoff, ts: ts || 0};
  const data = prog.split(' ')[0];
  const durata_min = parseInt(durata) || 0;
  const durata_text = durata_min < 60 ? durata_min + ' min' : Math.floor(durata_min/60) + 'h ' + (durata_min%60 ? (durata_min%60)+'min' : '');
  const durata_icon = durata_ors ? '🚗 → 📍' : '🕐 aprox.';
  document.getElementById('popup-content').innerHTML = `
    <div style="font-family:'Syne',sans-serif; font-size:28px; font-weight:800; color:#e8e8f0; margin-bottom:4px;">${ora} <span style="font-size:14px; color:#6b6b80;">${data}</span></div>
    <div style="font-size:15px; color:#e8e8f0; margin:10px 0 4px;">📍 ${pickup}</div>
    <div style="font-size:13px; color:#34d186; margin-bottom:4px;">↓</div>
    <div style="font-size:15px; color:#e8e8f0; margin-bottom:12px;">🏁 ${dropoff}</div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
      <span style="background:rgba(52,209,134,.1); border:1px solid rgba(52,209,134,.3); color:#34d186; border-radius:8px; padding:4px 10px; font-size:13px;">${dist} km</span>
      <span style="background:rgba(52,209,134,.1); border:1px solid rgba(52,209,134,.3); color:#34d186; border-radius:8px; padding:4px 10px; font-size:13px;">${pret_net} LEI net</span>
      <span style="background:rgba(255,214,0,.1); border:1px solid rgba(255,214,0,.3); color:#ffd600; border-radius:8px; padding:4px 10px; font-size:15px; font-weight:700;">${durata_icon} ${durata_text}</span>
    </div>
    <div style="font-size:12px; color:#6b6b80; text-align:center;">Caută cursa după ora <strong style="color:#ffd600;">${ora}</strong> în portal</div>
  `;
  document.getElementById('popup-link').href = 'https://fleets.bolt.eu/108961/operations/manage?tab=dispatching';
  const overlay = document.getElementById('popup-overlay');
  overlay.style.display = 'flex';
}

let _popupData = {};

function calculeazaGmaps() {
  const btn = document.getElementById('btn-gmaps');
  const result = document.getElementById('gmaps-result');
  btn.textContent = '⏳ Se calculează...';
  btn.disabled = true;
  result.style.display = 'none';

  fetch('/api/gmaps-durata', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      pickup: _popupData.pickup,
      dropoff: _popupData.dropoff,
      ts: _popupData.ts
    })
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      const min = d.durata;
      const text = min < 60 ? min + ' min' : Math.floor(min/60) + 'h ' + (min%60 ? (min%60)+'min' : '');
      const sursa = d.din_cache ? ' • din cache' : ` • cerere ${d.cereri_azi}/${d.limita} azi`;
      result.textContent = `🗺 Google Maps: ${text}${sursa}`;
      result.style.display = 'block';
    } else {
      result.textContent = '❌ ' + d.error;
      result.style.display = 'block';
      result.style.color = 'var(--red)';
    }
    btn.textContent = '🗺 Google Maps';
    btn.disabled = false;
  })
  .catch(e => {
    result.textContent = '❌ Eroare conexiune';
    result.style.display = 'block';
    btn.textContent = '🗺 Google Maps';
    btn.disabled = false;
  });
}

function inchidePopup() {
  document.getElementById('popup-overlay').style.display = 'none';
  const r = document.getElementById('gmaps-result');
  if(r){ r.style.display='none'; r.style.color='#4285f4'; }
  const btn = document.getElementById('btn-gmaps');
  if(btn){ btn.textContent='🗺 Google Maps'; btn.disabled=false; }
}

function copiazaOra(ora, prog, event) {
  const data = prog.split(' ')[0];
  const text = ora + '  ' + data;
  navigator.clipboard.writeText(text).catch(()=>{});
  toast('📋 ' + text + ' copiat! Lipește în căutare', 'var(--bolt)');
}

function atribuie(orderId, event) {
  event.stopPropagation();
  // Deschide portalul Bolt direct la tab-ul de dispatching
  const url = 'https://fleets.bolt.eu/108961/operations/manage?tab=dispatching';
  window.open(url, '_blank');
  // Copiaza order_id in clipboard ca referinta
  if(orderId && orderId !== 'N/A') {
    navigator.clipboard.writeText(orderId).catch(()=>{});
    toast('📋 ID copiat: ' + orderId.slice(0,8) + '... · Portalul Bolt s-a deschis!', 'var(--bolt)');
  } else {
    toast('📋 Portalul Bolt s-a deschis!', 'var(--bolt)');
  }
}

function gotoMax(){
  if(!data.length) return;
  const best=[...data].sort((a,b)=>b.lei_km_net-a.lei_km_net)[0];
  setF('all');
  setTimeout(()=>{
    const cards=document.querySelectorAll('.card');
    for(const card of cards){
      const lkVal=card.querySelector('.lk-val');
      if(lkVal && lkVal.textContent==String(best.lei_km_net)){
        card.scrollIntoView({behavior:'smooth', block:'center'});
        card.style.outline='2px solid var(--green)';
        setTimeout(()=>card.style.outline='',2000);
        break;
      }
    }
  }, 100);
}

function gotoBest(){
  let disponibile = data.filter(c=>!rezervate.has(c.order_id));
  if(!showComfort) disponibile = disponibile.filter(c=>c.categ!=='Comfort');
  const sorted=[...disponibile].sort((a,b)=>b.pret_net-a.pret_net);
  if(!sorted.length) return;
  const best=sorted[0];
  if(filtru==='rezervate') setF('all');
  setTimeout(()=>{
    const card=document.querySelector('[data-order="'+best.order_id+'"]');
    if(card){
      card.scrollIntoView({behavior:'smooth', block:'center'});
      card.style.outline='2px solid var(--green)';
      setTimeout(()=>card.style.outline='',2500);
    }
  }, 100);
}

async function refresh(){
  const btn=document.getElementById('btnR'),icon=document.getElementById('rIcon');
  btn.disabled=true; icon.textContent='⟳';
  toast('Se actualizează...','var(--bolt)');
  try{
    await fetch('/api/refresh',{method:'POST'});
    setTimeout(load,2000);
    setTimeout(()=>{btn.disabled=false;icon.textContent='↺';},8000);
  }catch(e){btn.disabled=false;icon.textContent='↺';}
}

load();
setInterval(load,12000);

// ── NOTIFICARI PUSH ──
async function activeazaNotificari() {
  if(!('serviceWorker' in navigator) || !('PushManager' in navigator)){
    toast('❌ Browserul nu suportă notificări push', 'var(--red)');
    return;
  }
  try {
    const perm = await Notification.requestPermission();
    if(perm !== 'granted'){
      toast('❌ Notificările au fost blocate!', 'var(--red)');
      return;
    }
    const reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    const existing = await reg.pushManager.getSubscription();
    if(existing){
      toast('✅ Notificări deja active!', 'var(--green)');
      document.getElementById('btnNotif').textContent = '🔔 Notificări ON';
      document.getElementById('btnNotif').style.color = 'var(--green)';
      return;
    }
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array('BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjZJkHm28D614kKTQfNNHfW1IXmHQ')
    });
    await fetch('/api/push-subscriptions', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(sub.toJSON())
    });
    document.getElementById('btnNotif').textContent = '🔔 Notificări ON';
    document.getElementById('btnNotif').style.color = 'var(--green)';
    toast('✅ Notificări activate!', 'var(--green)');
  } catch(e) {
    console.error(e);
    toast('❌ ' + e.message, 'var(--red)');
  }
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
  return outputArray;
}

async function setPragPush(prag) {
  try {
    const r = await fetch('/api/set-prag-push', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({prag: parseInt(prag)})
    });
    const d = await r.json();
    if(d.ok) toast('✅ Notificări pentru curse peste ' + prag + ' lei', 'var(--bolt)');
  } catch(e) { console.error(e); }
}

function syncPragSelector(prag){
  const sel = document.getElementById('selPrag');
  if(!sel) return;
  const opts = ['30','50','100'];
  let best = '100';
  for(const o of opts){ if(prag <= parseInt(o)){ best=o; break; } }
  sel.value = best;
}

// Verifica daca notificarile sunt deja active la incarcare
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js').then(reg => {
    reg.pushManager.getSubscription().then(sub => {
      if(sub){
        document.getElementById('btnNotif').textContent = '🔔 Notificări ON';
        document.getElementById('btnNotif').style.color = 'var(--green)';
      }
    });
  });
}
</script>
</body>
</html>"""

MANIFEST = """{
  "name": "Bolt Fleet",
  "short_name": "Bolt Fleet",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#0a0a0f",
  "theme_color": "#0a0a0f",
  "icons": [
    {"src": "https://www.bolt.eu/favicon.ico", "sizes": "64x64", "type": "image/x-icon"}
  ]
}"""

# ──────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logat"):
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["logat"] = True
            return redirect("/")
        error = '<div class="error">❌ Parolă greșită!</div>'
    return LOGIN_HTML.replace("ERROR_PLACEHOLDER", error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

@app.before_request
def before_first_request():
    start_background()

@app.route("/")
@login_required
def index():
    html = HTML.replace("PRAG_VAL", str(PRAG_MINIM_LEI_KM))
    html = html.replace("INTERVAL_VAL", str(INTERVAL_MINUTE))
    return html

@app.route("/sw.js")
def service_worker():
    sw_code = """
self.addEventListener('push', function(e) {
  const data = e.data ? e.data.json() : {};
  self.registration.showNotification(data.title || 'Bolt Fleet AXP', {
    body: data.body || '',
    icon: '/static/icon.png',
    badge: '/static/icon.png',
    vibrate: [200, 100, 200],
    tag: 'bolt-fleet',
    renotify: true
  });
});
self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  e.waitUntil(clients.openWindow('/'));
});
"""
    from flask import Response
    return Response(sw_code, mimetype='application/javascript')

@app.route("/api/push-subscriptions", methods=["POST"])
@login_required
def save_subscription():
    global push_subscriptions
    sub = request.get_json()
    if sub and sub not in push_subscriptions:
        push_subscriptions.append(sub)
    return jsonify({"ok": True})

@app.route("/manifest.json")
@login_required
def manifest():
    return app.response_class(MANIFEST, mimetype="application/json")

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ro">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bolt Fleet - Token</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap');
  :root { --bg:#0a0a0f; --surface:#13131a; --border:#1e1e2e; --green:#00e676; --red:#ff3d57; --text:#e8e8f0; --muted:#6b6b80; --bolt:#34d186; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'DM Mono',monospace; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:20px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:20px; padding:28px 24px; width:100%; max-width:480px; }
  .logo { font-family:'Syne',sans-serif; font-size:22px; font-weight:800; background:linear-gradient(135deg,var(--bolt),#00b4d8); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin-bottom:6px; }
  .subtitle { font-size:12px; color:var(--muted); margin-bottom:24px; line-height:1.6; }
  .steps { background:rgba(52,209,134,.06); border:1px solid rgba(52,209,134,.2); border-radius:12px; padding:16px; margin-bottom:20px; font-size:12px; line-height:2; color:var(--muted); }
  .steps b { color:var(--bolt); }
  label { font-size:11px; color:var(--muted); display:block; margin-bottom:6px; }
  textarea { width:100%; background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:12px; color:var(--text); font-family:'DM Mono',monospace; font-size:11px; height:100px; resize:none; outline:none; transition:border-color .2s; }
  textarea:focus { border-color:var(--bolt); }
  .btn { width:100%; background:var(--bolt); color:#000; border:none; border-radius:12px; padding:14px; font-family:'DM Mono',monospace; font-size:14px; font-weight:500; cursor:pointer; margin-top:14px; transition:opacity .15s; }
  .btn:active { opacity:.7; }
  .msg { margin-top:14px; padding:12px; border-radius:10px; font-size:12px; text-align:center; display:none; }
  .msg.ok  { background:rgba(0,230,118,.1); border:1px solid var(--green); color:var(--green); display:block; }
  .msg.err { background:rgba(255,61,87,.1);  border:1px solid var(--red);   color:var(--red);   display:block; }
  .back { display:block; text-align:center; margin-top:16px; color:var(--muted); font-size:12px; text-decoration:none; }
  .back:hover { color:var(--bolt); }
</style>
</head>
<body>
<div class="card">
  <div class="logo">⚡ BOLT FLEET <span style="white-space:nowrap"><svg width="18" height="18" viewBox="0 0 100 100" style="display:inline-block;vertical-align:middle;margin:0 2px;" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="50" rx="48" ry="48" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="50" rx="30" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="32" rx="18" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/></svg>AXP<svg width="18" height="18" viewBox="0 0 100 100" style="display:inline-block;vertical-align:middle;margin:0 2px;" xmlns="http://www.w3.org/2000/svg"><ellipse cx="50" cy="50" rx="48" ry="48" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="50" rx="30" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/><ellipse cx="50" cy="32" rx="18" ry="18" fill="none" stroke="#e8e8f0" stroke-width="5"/></svg></span></div>
  <div class="subtitle">Actualizare Refresh Token</div>

  <!-- BUTON AUTOMAT -->
  <div style="background:rgba(52,209,134,.08); border:1px solid rgba(52,209,134,.3); border-radius:12px; padding:16px; margin-bottom:16px; text-align:center;">
    <div style="font-size:13px; color:var(--bolt); font-weight:600; margin-bottom:8px;">⚡ Extrage automat</div>
    <div style="font-size:11px; color:var(--muted); margin-bottom:12px;">Apasă butonul de mai jos când ești logat pe fleets.bolt.eu</div>
    <a href="javascript:(function(){var t=localStorage.getItem('taxifyFleetOwnerPortal_refresh_token');if(t){fetch('https://bolt-fleet-axp.onrender.com/api/set-token-auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:t})}).then(r=>r.json()).then(d=>alert(d.ok?'Token actualizat!':'Eroare: '+d.error));}else{alert('Nu am gasit token! Esti logat pe fleets.bolt.eu?');}})();"
       onclick="window.open('https://fleets.bolt.eu/108961/operations/manage?tab=dispatching', '_blank'); setTimeout(()=>alert('Pasul 2: Dupa ce s-a deschis Bolt Fleet, apasa OK si copiaza linkul de mai jos in bara de adrese a tabului nou deschis'), 500); return false;"
       style="display:block; background:var(--bolt); color:#000; border-radius:10px; padding:12px; font-size:14px; font-weight:700; text-decoration:none; cursor:pointer;">
      🔑 Deschide Bolt Fleet și extrage token
    </a>
  </div>

  <div style="background:rgba(255,214,0,.06); border:1px solid rgba(255,214,0,.2); border-radius:12px; padding:12px; margin-bottom:16px; font-size:11px; color:var(--muted); line-height:1.8;">
    <b style="color:#ffd600;">Sau manual (pe calculator):</b><br>
    <b>1.</b> Deschide <a href="https://fleets.bolt.eu" target="_blank" style="color:var(--bolt);">fleets.bolt.eu</a> și loghează-te<br>
    <b>2.</b> Apasă <b style="color:#ffd600;">F12</b> → <b style="color:#ffd600;">Network</b> → <b style="color:#ffd600;">F5</b><br>
    <b>3.</b> Caută <b style="color:#ffd600;">getAccessToken</b> → <b style="color:#ffd600;">Payload</b><br>
    <b>4.</b> Copiază <b style="color:#ffd600;">refresh_token</b> și lipește jos
  </div>

  <label>Sau lipește manual refresh_token-ul:</label>
  <div style="position:relative;">
    <textarea id="token" placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." style="width:100%; background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:12px; color:var(--text); font-family:'DM Mono',monospace; font-size:11px; height:100px; resize:none; outline:none;"></textarea>
    <button onclick="paste()" style="position:absolute; bottom:8px; right:8px; background:var(--surface); border:1px solid var(--border); color:var(--bolt); border-radius:8px; padding:4px 10px; font-size:11px; cursor:pointer;">📋 Paste</button>
  </div>
  <button class="btn" onclick="salveaza()">✅ Salvează și reconectează</button>
  <div class="msg" id="msg"></div>
  <a href="/" class="back">← Înapoi la curse</a>
</div>
<script>
async function paste() {
  try {
    const text = await navigator.clipboard.readText();
    document.getElementById('token').value = text;
  } catch(e) {
    alert('Nu am putut citi clipboard-ul. Foloseste Ctrl+V manual.');
  }
}

async function salveaza() {
  const token = document.getElementById('token').value.trim();
  const msg = document.getElementById('msg');
  msg.className = 'msg';
  if (!token || !token.startsWith('eyJ')) {
    msg.textContent = '❌ Token invalid — trebuie să înceapă cu eyJ...';
    msg.className = 'msg err'; return;
  }
  try {
    const r = await fetch('/api/set-token', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({refresh_token: token})
    });
    const d = await r.json();
    if (d.ok) {
      msg.textContent = '✅ Token salvat! Se reconectează la Bolt...';
      msg.className = 'msg ok';
      setTimeout(() => window.location.href = '/', 2500);
    } else {
      msg.textContent = '❌ Eroare: ' + d.error;
      msg.className = 'msg err';
    }
  } catch(e) {
    msg.textContent = '❌ Eroare conexiune';
    msg.className = 'msg err';
  }
}
</script>
</body>
</html>"""

@app.route("/token")
@login_required
def token_page():
    return ADMIN_HTML

@app.route("/extrage-token")
def extrage_token_page():
    """Pagina care se deschide pe fleets.bolt.eu pentru a extrage token-ul automat."""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Extrage Token Bolt</title>
<style>
  body { background:#0a0a0f; color:#e8e8f0; font-family:monospace; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; padding:20px; box-sizing:border-box; }
  .card { background:#13131a; border:1px solid #1e1e2e; border-radius:20px; padding:28px; max-width:400px; width:100%; text-align:center; }
  .logo { font-size:20px; font-weight:800; color:#34d186; margin-bottom:8px; }
  .msg { font-size:13px; color:#6b6b80; margin-bottom:20px; }
  .btn { background:#34d186; color:#000; border:none; border-radius:12px; padding:14px 20px; font-size:15px; font-weight:700; cursor:pointer; width:100%; margin-bottom:10px; }
  .status { margin-top:16px; padding:12px; border-radius:10px; font-size:13px; display:none; }
  .ok { background:rgba(0,230,118,.1); border:1px solid #00e676; color:#00e676; display:block; }
  .err { background:rgba(255,61,87,.1); border:1px solid #ff3d57; color:#ff3d57; display:block; }
  .steps { background:rgba(52,209,134,.06); border:1px solid rgba(52,209,134,.2); border-radius:12px; padding:14px; margin-bottom:16px; font-size:12px; color:#6b6b80; line-height:2; text-align:left; }
  .steps b { color:#34d186; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">⚡ BOLT FLEET AXP</div>
  <div class="msg">Extragere automată token</div>
  <div class="steps" id="steps">
    <b>Pasul 1:</b> Salvează acest link în favorite<br>
    <b>Pasul 2:</b> Deschide <b>fleets.bolt.eu</b><br>
    <b>Pasul 3:</b> Apasă butonul de mai jos
  </div>
  <button class="btn" onclick="extrage()">🔑 Extrage Token Acum</button>
  <div class="status" id="status"></div>
</div>
<script>
function extrage() {
  var t = null;
  
  // Incearca din localStorage curent
  try { t = localStorage.getItem('taxifyFleetOwnerPortal_refresh_token'); } catch(e) {}
  
  // Incearca si alte chei posibile
  if (!t) {
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (key && key.toLowerCase().includes('refresh')) {
          var val = localStorage.getItem(key);
          if (val && val.startsWith('eyJ')) { t = val; break; }
        }
      }
    } catch(e) {}
  }

  var status = document.getElementById('status');
  
  if (!t) {
    status.textContent = '❌ Token negăsit! Asigură-te că ești logat pe fleets.bolt.eu și rulezi această pagină de pe acel site.';
    status.className = 'status err';
    return;
  }

  status.textContent = '⏳ Se trimite token-ul...';
  status.className = 'status ok';
  
  fetch('https://bolt-fleet-axp.onrender.com/api/set-token-auto', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({refresh_token: t})
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      status.textContent = '✅ Token actualizat cu succes! Aplicația s-a reconectat.';
      status.className = 'status ok';
      document.getElementById('steps').style.display = 'none';
    } else {
      status.textContent = '❌ Eroare: ' + d.error;
      status.className = 'status err';
    }
  })
  .catch(e => {
    status.textContent = '❌ Eroare conexiune. Verifică internetul.';
    status.className = 'status err';
  });
}

// Incearca automat la incarcare
window.onload = function() { extrage(); };
</script>
</body>
</html>"""

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Primeste mesaje de la Telegram si inregistreaza utilizatori noi."""
    try:
        data = request.get_json()
        message = data.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))
        first_name = message.get("chat", {}).get("first_name", "utilizator")

        if text == "/start" and chat_id:
            if chat_id not in TELEGRAM_CHAT_IDS:
                TELEGRAM_CHAT_IDS.append(chat_id)
                save_telegram_ids()
                print(f"[✅] Telegram nou: {first_name} ({chat_id})")
                # Trimite mesaj de confirmare
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"✅ Salut {first_name}! Ești înregistrat pentru notificări Bolt Fleet AXP. Vei primi alerte când apar curse importante! 🚕"
                    }, timeout=5
                )
            else:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"✅ {first_name}, ești deja înregistrat! Vei primi notificări automat. 🔔"
                    }, timeout=5
                )
        elif text == "/stop" and chat_id:
            if chat_id in TELEGRAM_CHAT_IDS:
                TELEGRAM_CHAT_IDS.remove(chat_id)
                save_telegram_ids()
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": "❌ Dezabonat. Nu vei mai primi notificări."}, timeout=5
                )
    except Exception as e:
        print(f"[WARN] Webhook Telegram: {e}")
    return jsonify({"ok": True})

@app.route("/api/add-telegram", methods=["POST"])
@login_required
def add_telegram():
    try:
        data = request.get_json()
        chat_id = str(data.get("chat_id", "")).strip()
        if chat_id and chat_id not in TELEGRAM_CHAT_IDS:
            TELEGRAM_CHAT_IDS.append(chat_id)
            print(f"[✅] Telegram chat ID adaugat: {chat_id}")
        return jsonify({"ok": True, "ids": TELEGRAM_CHAT_IDS})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/gmaps-durata", methods=["POST"])
@login_required
def api_gmaps_durata():
    """Calculeaza durata cu Google Maps la cerere pentru o cursa specifica."""
    try:
        data = request.get_json()
        pickup = data.get("pickup", "")
        dropoff = data.get("dropoff", "")
        ts = data.get("ts", 0)
        if not pickup or not dropoff:
            return jsonify({"ok": False, "error": "Adrese lipsă"})
        d = durata_gmaps(pickup, dropoff, ts)
        cache_key = f"gmaps|{pickup}|{dropoff}|{ts//3600}"
        din_cache = cache_key in _durata_cache
        if d:
            return jsonify({"ok": True, "durata": d, "cereri_azi": _gmaps_azi["count"], "limita": GMAPS_LIMIT_ZI, "din_cache": din_cache})
        else:
            return jsonify({"ok": False, "error": f"Limită atinsă ({_gmaps_azi['count']}/{GMAPS_LIMIT_ZI} cereri azi) sau eroare API"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/rezervate", methods=["POST"])
@login_required
def sync_rezervate():
    """Sincronizeaza cursele rezervate de pe telefon."""
    try:
        data = request.get_json()
        ids = data.get("ids", [])
        rezervate_server.clear()
        rezervate_server.update(ids)
        return jsonify({"ok": True, "count": len(rezervate_server)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/set-prag-push", methods=["POST"])
@login_required
def set_prag_push():
    try:
        data = request.get_json()
        prag = int(data.get("prag", 100))
        state["prag_push"] = prag
        print(f"[✅] Prag push setat la {prag} lei")
        return jsonify({"ok": True, "prag": prag})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/set-token-auto", methods=["POST"])
def set_token_auto():
    """Endpoint pentru bookmarklet - nu necesita sesiune activa."""
    try:
        data = request.get_json()
        new_token = data.get("refresh_token", "").strip()
        if not new_token or not new_token.startswith("eyJ"):
            return jsonify({"ok": False, "error": "Token invalid"})
        auth["refresh_token"] = new_token
        auth["access_token"]  = None
        auth["access_expiry"] = None
        state["eroare"]       = None
        save_token(new_token)
        print(f"[✅] Token actualizat via bookmarklet!")
        threading.Thread(target=do_refresh, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/set-token", methods=["POST"])
@login_required
def set_token():
    try:
        data = request.get_json()
        new_token = data.get("refresh_token", "").strip()
        if not new_token or not new_token.startswith("eyJ"):
            return jsonify({"ok": False, "error": "Token invalid"})
        auth["refresh_token"] = new_token
        auth["access_token"]  = None
        auth["access_expiry"] = None
        state["eroare"]       = None
        save_token(new_token)
        print(f"[✅] Refresh token actualizat manual!")
        threading.Thread(target=do_refresh, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/curse")
@login_required
def api_curse():
    return jsonify({
        "curse":         state["curse"],
        "ultima_update": state["ultima_update"],
        "status":        state["status"],
        "eroare":        state["eroare"],
        "se_incarca":    state["se_incarca"],
        "prag_push":     state["prag_push"],
    })

@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    if not state["se_incarca"]:
        threading.Thread(target=do_refresh, daemon=True).start()
    return jsonify({"ok": True})


# ──────────────────────────────────────────
#  START
# ──────────────────────────────────────────

# Pornire pentru gunicorn (Render) - la nivel de modul
threading.Thread(target=start_background, daemon=True).start()

if __name__ == "__main__":
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except:
        ip = "127.0.0.1"

    print("=" * 60)
    print("  BOLT FLEET — FINAL (fără Selenium)")
    print(f"  Local    : http://localhost:{PORT}")
    print(f"  Rețea    : http://{ip}:{PORT}")
    print(f"  Prag     : {PRAG_MINIM_LEI_KM} lei/km")
    print(f"  Interval : {INTERVAL_MINUTE} minute")
    print("=" * 60 + "\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)
