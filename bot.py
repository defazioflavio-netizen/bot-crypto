"""
Bot crypto per Revolut X - esperimento personale.

Gira sul PC di casa ogni 45 secondi (GitHub Actions fa da riserva):
  1. legge i comandi Telegram
  2. ogni ora sceglie le crypto in euro più scambiate e prova centinaia di strategie sul loro storico
  3. scarica prezzi e candele da Revolut X e applica la strategia migliore (trend o rimbalzo, stop loss, trailing stop)
  4. compra/vende con ordini market e spiega per ogni crypto cosa vede e cosa manca per comprare
  5. salva tutto in data.json (letto dalla dashboard)

Le chiavi NON stanno in questo file: arrivano dai "Secrets" di GitHub o da impostazioni.json sul PC.
"""

import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# ---------------------------------------------------------------------------
# IMPOSTAZIONI DELLA STRATEGIA (puoi modificarle da GitHub)
# ---------------------------------------------------------------------------
CONFIG = {
    "valuta": "EUR",                 # valuta del conto su Revolut X
    "capitale_bot": 100.0,           # EUR di partenza. Il bot usa solo questi + i suoi guadagni:
                                     # eventuali altri soldi sul conto non vengono mai toccati
    # Strategia di base: "rimbalzo" su candele da 4 ore. Nelle simulazioni con i costi reali di Revolut X
    # (60 giorni, 3 periodi diversi, crypto liquide) è l'unica che è rimasta in positivo in tutti i periodi.
    # Il bot la sostituisce solo se ne trova una migliore sia in allenamento sia in verifica.
    "tipo": "rimbalzo",
    "asset": ["BTC", "ETH", "XRP"],  # crypto usate finché non fa la prima ottimizzazione
    "intervallo_candele": 240,       # minuti per candela (240 = 4 ore)
    "ema_veloce": 20,
    "ema_lenta": 50,
    "rsi_periodo": 14,
    "rsi_ingresso": 30,              # rimbalzo: compra quando l'RSI scende sotto 30 (crypto "ipervenduta")...
    "rsi_uscita": 55,                # ...e rivende quando risale sopra 55
    "take_profit": 0.06,             # ...o appena guadagna il 6%
    "rsi_max_ingresso": 70,          # trend: non compra se RSI sopra (mercato "surriscaldato")
    "stop_loss": 0.08,               # vende se scende dell'8% dal prezzo di acquisto
    "attiva_trailing_dopo": 0.03,    # trend: dopo +3% di guadagno attiva il trailing stop
    "trailing_stop": 0.03,           # ...e vende se scende del 3% dal massimo raggiunto
    "ordine_minimo": 5.0,            # EUR, sotto questa cifra non apre ordini

    # Apprendimento: ogni giorno riprova 108 varianti della strategia sugli ultimi 60 giorni
    # di ogni crypto e adotta la migliore, solo se regge anche sui giorni più recenti
    "asset_candidati": ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"],  # usate se la ricerca automatica non va
    "tutte_le_crypto": True,         # guarda tutte le crypto in euro di Revolut X (non solo l'elenco sopra)
    "max_candidati": 12,             # ...tenendo le più scambiate (le altre hanno prezzi poco affidabili)
    "volume_minimo_24h": 100_000,    # EUR scambiati nelle ultime 24 ore per essere presa in considerazione
    "spread_max": 0.005,             # esclude le crypto con differenza tra prezzo di acquisto e di vendita sopra lo 0,5%
                                     # (es. PEPE in euro: spread ~0,8% e prezzi "a scatti" per mancanza di scambi)
    "escluse": ["USDT", "USDC", "EURC", "EUROC", "DAI", "PYUSD", "FDUSD", "TUSD", "USDE", "RLUSD", "USD1", "EURT", "USDP", "GUSD"],
                                     # stablecoin: valgono sempre ~1 dollaro/euro, non ha senso farci trading
    "max_asset_attivi": 3,           # su quante crypto al massimo opera insieme
    "ottimizza_ogni_ore": 1,         # ogni ora rifà l'analisi del mercato
    "giorni_storico": 60,            # storico per le strategie su candele da 1 e 4 ore
    "giorni_storico_15m": 20,        # storico per le strategie veloci su candele da 15 minuti
    "strategie_veloci": False,       # candele da 15 minuti: con i costi reali di Revolut X perdevano sempre (spento)
    "min_operazioni_test": 12,       # una strategia vale solo se nelle simulazioni ha fatto almeno 12 operazioni (su tutte le crypto)...
    "min_operazioni_verifica": 4,    # ...di cui almeno 4 nel periodo di verifica: pochi colpi fortunati non bastano
    "costo_per_operazione": 0.0015,  # usato solo se lo spread di una crypto non è ancora stato misurato
    "commissione": 0.0009,           # commissione Revolut X per ordine market (0,09%)
    "slittamento": 0.0005,           # margine prudenziale per ordini eseguiti a prezzi un po' peggiori
    "stop_emergenza_x": 1.5,         # tra una candela e l'altra vende subito solo se il calo è 1,5 volte lo stop loss
    "peso_rischio": 0.3,             # quanto penalizza i cali (0 = cerca solo il massimo guadagno)
    "crollo_max_7g": 0.25,           # strategia "rimbalzo": non compra se la crypto è scesa più del 25% in 7 giorni
    "pausa_dopo_vendita_ore": 6,     # dopo una vendita aspetta prima di ricomprare la stessa crypto
    "pausa_dopo_vendita_ore_15m": 1, # ...ma con la strategia veloce (15 minuti) aspetta solo 1 ora
    "soglia_emergenza": 0.70,        # se il capitale scende sotto il 70% vende tutto e si ferma
    "ora_report": 21,                # ora (Italia) del resoconto giornaliero su Telegram
    "versione_logica": 2,            # se cambia, il bot ricalcola subito la strategia
}

BASE = "https://revx.revolut.com"
FUSO = ZoneInfo("Europe/Rome")
# sul PC di casa il file dei dati sta fuori dalla cartella del codice (BOT_DATA_FILE); su GitHub resta qui accanto
DATA_FILE = os.environ.get("BOT_DATA_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
MAX_PUNTI = 8000

API_KEY = os.environ.get("REVX_API_KEY", "").strip()
PRIVATE_PEM = os.environ.get("REVX_PRIVATE_KEY", "").strip()
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SOLO_TEST = os.environ.get("SOLO_TEST", "").strip().lower() in ("1", "true", "yes")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
PASSWORD_DATI = os.environ.get("DASHBOARD_PASSWORD", "")
ITERAZIONI = 200_000


def ora_ms():
    return int(time.time() * 1000)


def ora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def D(x):
    return Decimal(str(x))


def euro(x):
    return f"{float(x):,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def link_dashboard():
    if "/" not in REPO:
        return ""
    owner, repo = REPO.split("/", 1)
    return f"https://{owner.lower()}.github.io/{repo}/"


# ---------------------------------------------------------------------------
# CLIENT REVOLUT X
# ---------------------------------------------------------------------------
class RevX:
    def __init__(self, api_key, private_pem):
        if not api_key or not private_pem:
            raise RuntimeError("Mancano REVX_API_KEY o REVX_PRIVATE_KEY nei Secrets di GitHub.")
        pem = private_pem.replace("\\n", "\n")
        if "BEGIN" not in pem:
            raise RuntimeError("REVX_PRIVATE_KEY non sembra una chiave PEM valida.")
        self.key = serialization.load_pem_private_key(pem.encode(), password=None)
        self.api_key = api_key
        self.s = requests.Session()

    def _call(self, method, path, query="", body=None):
        full_path = "/api/1.0" + path
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        for tentativo in range(4):
            ts = str(ora_ms())
            msg = f"{ts}{method}{full_path}{query}{body_str}".encode()
            firma = base64.b64encode(self.key.sign(msg)).decode()
            headers = {
                "X-Revx-API-Key": self.api_key,
                "X-Revx-Timestamp": ts,
                "X-Revx-Signature": firma,
                "Accept": "application/json",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"
            url = BASE + full_path + (("?" + query) if query else "")
            r = self.s.request(method, url, headers=headers,
                               data=body_str if body is not None else None, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                attesa = int(r.headers.get("Retry-After", "2000")) / 1000 if r.status_code == 429 else 2
                time.sleep(min(attesa, 10))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Errore Revolut X {r.status_code} su {path}: {r.text[:300]}")
            if r.status_code == 204 or not r.text:
                return None
            return r.json()
        raise RuntimeError(f"Revolut X non risponde su {path}")

    def saldi(self):
        res = self._call("GET", "/balances")
        return {b["currency"]: b for b in res}

    def coppie(self):
        return self._call("GET", "/configuration/pairs")

    def ticker(self, simboli):
        """Prezzi attuali. A volte Revolut X manda un prezzo vuoto: quella crypto viene scartata
        (e si riprova un paio di volte), così un dato mancante non blocca tutto il giro."""
        for tentativo in range(3):
            res = self._call("GET", "/tickers", query="symbols=" + ",".join(simboli))
            prezzi = {}
            for t in res["data"]:
                try:
                    bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
                except (TypeError, ValueError):
                    continue
                if bid <= 0 or ask <= 0:
                    continue
                try:
                    float(t.get("mid") or "")
                except (TypeError, ValueError):
                    t["mid"] = str((bid + ask) / 2)
                prezzi[t["symbol"].replace("/", "-")] = t
            if all(s in prezzi for s in simboli) or tentativo == 2:
                return prezzi
            time.sleep(2)

    def candele(self, simbolo, intervallo, quante=99):
        adesso = ora_ms()
        since = adesso - quante * intervallo * 60_000
        q = f"interval={intervallo}&since={since}&until={adesso}"
        res = self._call("GET", f"/candles/{simbolo}", query=q)
        candele = sorted(res["data"], key=lambda c: c["start"])
        # scarta la candela ancora aperta
        return [c for c in candele if c["start"] + intervallo * 60_000 <= adesso]

    def storico(self, simbolo, ore, minuti=60, cache=None):
        """Candele da `minuti` (60 o 15) delle ultime `ore`, scaricate a blocchi da 100.
        Con `cache` (un dizionario) riscarica solo le candele nuove dall'ultima volta."""
        passo = minuti * 60_000
        fine = ora_ms() // passo * passo
        inizio_finestra = fine - (ore * 60 // minuti) * passo
        chiave = f"{simbolo}|{minuti}"
        tutte = {}
        if cache is not None:
            tutte = {int(k): v for k, v in cache.get(chiave, {}).items() if int(k) >= inizio_finestra}
        # dal punto in cui si era fermata la volta prima (riscaricando l'ultima candela, che poteva essere incompleta)
        da = max(inizio_finestra, max(tutte) - passo) if tutte else inizio_finestra
        while da < fine:
            a = min(fine, da + 100 * passo)
            res = self._call("GET", f"/candles/{simbolo}", query=f"interval={minuti}&since={da}&until={a}")
            for c in res["data"]:
                tutte[int(c["start"])] = {"start": int(c["start"]), "close": c["close"], "volume": c.get("volume")}
            da = a
        if cache is not None:
            cache[chiave] = {str(k): v for k, v in tutte.items()}
        adesso = ora_ms()
        return [tutte[k] for k in sorted(tutte) if k + passo <= adesso]

    def ordine_market(self, simbolo, lato, quote_size=None, base_size=None):
        conf = {"quote_size": quote_size} if quote_size else {"base_size": base_size}
        body = {
            "client_order_id": str(uuid.uuid4()),
            "symbol": simbolo,
            "side": lato,
            "order_configuration": {"market": conf},
        }
        res = self._call("POST", "/orders", body=body)
        oid = res["data"]["venue_order_id"]
        dettagli = None
        for _ in range(8):
            time.sleep(1)
            dettagli = self._call("GET", f"/orders/{oid}")["data"]
            if dettagli.get("status") in ("filled", "cancelled", "rejected"):
                break
        return dettagli


# ---------------------------------------------------------------------------
# INDICATORI
# ---------------------------------------------------------------------------
def ema(valori, periodo):
    k = 2 / (periodo + 1)
    e = sum(valori[:periodo]) / periodo
    for v in valori[periodo:]:
        e = v * k + e * (1 - k)
    return e


def rsi(valori, periodo=14):
    if len(valori) <= periodo:
        return 50.0
    guadagni, perdite = 0.0, 0.0
    for i in range(1, periodo + 1):
        d = valori[i] - valori[i - 1]
        guadagni += max(d, 0)
        perdite += max(-d, 0)
    mg, mp = guadagni / periodo, perdite / periodo
    for i in range(periodo + 1, len(valori)):
        d = valori[i] - valori[i - 1]
        mg = (mg * (periodo - 1) + max(d, 0)) / periodo
        mp = (mp * (periodo - 1) + max(-d, 0)) / periodo
    if mp == 0:
        return 100.0
    return 100 - 100 / (1 + mg / mp)


def chiusure(candele_orarie, minuti):
    """Chiusure alla risoluzione voluta (60 = 1h, 240 = 4h), solo candele complete."""
    if minuti == 60:
        return [float(c["close"]) for c in candele_orarie]
    per, gruppi = minuti // 60, {}
    for c in candele_orarie:
        gruppi.setdefault(c["start"] // (minuti * 60_000), []).append(c)
    return [float(sorted(g, key=lambda x: x["start"])[-1]["close"])
            for k, g in sorted(gruppi.items()) if len(g) == per]


def serie_ema(v, p):
    out = [None] * len(v)
    if len(v) < p:
        return out
    e = sum(v[:p]) / p
    out[p - 1] = e
    k = 2 / (p + 1)
    for i in range(p, len(v)):
        e = v[i] * k + e * (1 - k)
        out[i] = e
    return out


def serie_rsi(v, p=14):
    out = [None] * len(v)
    if len(v) <= p:
        return out
    g = sum(max(v[i] - v[i - 1], 0) for i in range(1, p + 1)) / p
    l = sum(max(v[i - 1] - v[i], 0) for i in range(1, p + 1)) / p
    out[p] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(p + 1, len(v)):
        d = v[i] - v[i - 1]
        g = (g * (p - 1) + max(d, 0)) / p
        l = (l * (p - 1) + max(-d, 0)) / p
        out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def tipo(P):
    return P.get("tipo", "trend")


def esce(P, p, entrata, picco, ef_i, el_i, rs_i):
    """Motivo di vendita (o None). Stesse regole per simulazione e trading reale."""
    if p <= entrata * (1 - P["stop_loss"]):
        return f"stop loss (-{P['stop_loss']*100:.1f}%)".replace(".0%", "%")
    if tipo(P) == "rimbalzo":
        if p >= entrata * (1 + P["take_profit"]):
            return f"rimbalzo incassato (+{P['take_profit']*100:.0f}%)"
        if rs_i > P["rsi_uscita"]:
            return f"rimbalzo finito (RSI sopra {P['rsi_uscita']})"
        return None
    if picco >= entrata * (1 + P["attiva_trailing_dopo"]) and p <= picco * (1 - P["trailing_stop"]):
        return "trailing stop: protegge il guadagno"
    if ef_i < el_i:
        return "trend girato al ribasso"
    return None


def entra(P, p, ef_i, el_i, rs_i, prezzo_7g_fa):
    """True se le regole dicono di comprare."""
    if tipo(P) == "rimbalzo":
        crollo = prezzo_7g_fa is not None and p < prezzo_7g_fa * (1 - CONFIG["crollo_max_7g"])
        return rs_i < P["rsi_ingresso"] and p < el_i and not crollo
    return ef_i > el_i and p > el_i and rs_i < P["rsi_max_ingresso"]


def backtest(c, ef, el, rs, P, da, a, costo, pausa_barre):
    """Simula la strategia sulle candele [da, a). Ritorna (rendimento, calo massimo, n. operazioni)."""
    cap, pos, entrata, picco = 1.0, False, 0.0, 0.0
    ultima_uscita, max_eq, max_calo, n = -10**9, 1.0, 0.0, 0
    barre_7g = 7 * 1440 // P["intervallo_candele"]
    for i in range(max(da, 1), a):
        if ef[i] is None or el[i] is None or rs[i] is None:
            continue
        p = c[i]
        if pos:
            picco = max(picco, p)
            if esce(P, p, entrata, picco, ef[i], el[i], rs[i]):
                cap *= p / entrata * (1 - costo)
                pos, ultima_uscita, n = False, i, n + 1
            eq = cap * p / entrata if pos else cap
        else:
            fa7 = c[i - barre_7g] if i >= barre_7g else None
            if i - ultima_uscita >= pausa_barre and entra(P, p, ef[i], el[i], rs[i], fa7):
                cap *= 1 - costo
                pos, entrata, picco = True, p, p
            eq = cap
        max_eq = max(max_eq, eq)
        max_calo = max(max_calo, 1 - eq / max_eq)
    if pos:
        cap *= c[a - 1] / entrata * (1 - costo)
        n += 1
    return cap - 1, max_calo, n


def intervalli():
    return (15, 60, 240) if CONFIG["strategie_veloci"] else (60, 240)


def griglia():
    yield from (P for P in griglia_trend() if P["intervallo_candele"] in intervalli())
    # strategie "rimbalzo": comprano quando una crypto è scesa troppo e rivendono alla risalita
    for intervallo in intervalli():
        for ri in (25, 30):
            for ru in (55, 65):
                for sl in (0.04, 0.08):
                    for tp in (0.03, 0.06):
                        yield {"tipo": "rimbalzo", "intervallo_candele": intervallo, "ema_veloce": 20, "ema_lenta": 50,
                               "rsi_ingresso": ri, "rsi_uscita": ru, "stop_loss": sl, "take_profit": tp,
                               "rsi_max_ingresso": 70, "trailing_stop": 0.03, "attiva_trailing_dopo": 0.03}


def griglia_trend():
    # strategie veloci su candele da 15 minuti: più operazioni al giorno, stop più stretti
    for ev, el in ((5, 13), (9, 21)):
        for rmax in (65, 75):
            for sl in (0.015, 0.025):
                for tr, att in ((0.01, 0.01), (0.015, 0.02)):
                    yield {"intervallo_candele": 15, "ema_veloce": ev, "ema_lenta": el,
                           "rsi_max_ingresso": rmax, "stop_loss": sl,
                           "trailing_stop": tr, "attiva_trailing_dopo": att}
    # strategie più calme su candele da 1 e 4 ore
    for intervallo in (60, 240):
        for ev, el in ((9, 21), (12, 26), (20, 50)):
            for rmax in (65, 75):
                for sl in (0.03, 0.05, 0.08):
                    for tr, att in ((0.02, 0.02), (0.03, 0.03), (0.04, 0.05)):
                        yield {"intervallo_candele": intervallo, "ema_veloce": ev, "ema_lenta": el,
                               "rsi_max_ingresso": rmax, "stop_loss": sl,
                               "trailing_stop": tr, "attiva_trailing_dopo": att}


def costo_reale(dati, a):
    """Costo di un acquisto o di una vendita per questa crypto: commissione + metà dello spread medio misurato + margine."""
    sp = (dati.get("spread") or {}).get(a)
    if sp is None:
        return CONFIG["costo_per_operazione"]
    return CONFIG["commissione"] + sp / 2 + CONFIG["slittamento"]


def pausa_ore(intervallo):
    return CONFIG["pausa_dopo_vendita_ore_15m"] if intervallo == 15 else CONFIG["pausa_dopo_vendita_ore"]


def chiave(P):
    return tuple(P.get(k) if k != "tipo" else tipo(P) for k in chiave_nomi())


def strategia_iniziale():
    P = {k: CONFIG[k] for k in ("tipo", "intervallo_candele", "ema_veloce", "ema_lenta", "rsi_max_ingresso",
                                "stop_loss", "trailing_stop", "attiva_trailing_dopo",
                                "rsi_ingresso", "rsi_uscita", "take_profit")}
    P.update({"asset": list(CONFIG["asset"]), "aggiornata": None, "origine": "impostazioni iniziali"})
    return P


CACHE_FILE = os.path.join(os.path.dirname(DATA_FILE), "cache_candele.json")


def carica_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salva_cache(cache):
    try:
        with open(CACHE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(CACHE_FILE + ".tmp", CACHE_FILE)
    except Exception as e:
        print("cache non salvata:", e)


def scegli_candidati(api, coppie, dati):
    """Le crypto in euro più scambiate nelle ultime 24 ore (escluse le stablecoin)."""
    valuta = CONFIG["valuta"]
    attive = lambda a: f"{a}/{valuta}" in coppie and coppie[f"{a}/{valuta}"].get("status", "active") == "active"
    predefinite = [a for a in CONFIG["asset_candidati"] if attive(a)]
    if not CONFIG.get("tutte_le_crypto"):
        return predefinite
    escluse = set(CONFIG["escluse"]) | {valuta}
    tutte = sorted({k.split("/")[0] for k in coppie if k.endswith("/" + valuta)} - escluse)
    volumi = {}
    for a in tutte:
        if not attive(a):
            continue
        try:
            cc = api.candele(f"{a}-{valuta}", 60, 25)
            volumi[a] = round(sum(float(c.get("volume") or 0) * float(c.get("close") or 0) for c in cc))
        except Exception as e:
            print("volume", a, e)
    liquide = sorted((a for a in volumi if volumi[a] >= CONFIG["volume_minimo_24h"]), key=volumi.get, reverse=True)
    # spread reale (differenza tra prezzo di acquisto e di vendita), media mobile delle misure orarie
    misura_spread(api, dati, liquide)
    spread = dati.get("spread", {})
    scartate = {a: round(spread[a] * 100, 2) for a in liquide if spread.get(a, 1) > CONFIG["spread_max"]}
    liquide = [a for a in liquide if spread.get(a, 1) <= CONFIG["spread_max"]]
    dati["mercato"] = {"t": ora_ms(), "crypto_in_euro": len(tutte), "volumi_24h": dict(sorted(volumi.items(), key=lambda x: -x[1])[:30]),
                       "spread_%": {a: round(v * 100, 3) for a, v in sorted(spread.items(), key=lambda x: x[1])},
                       "escluse_per_spread_%": scartate}
    if len(liquide) < 3:
        return [a for a in predefinite if spread.get(a, 0) <= CONFIG["spread_max"]] or predefinite
    return liquide[:CONFIG["max_candidati"]]


def misura_spread(api, dati, asset):
    valuta = CONFIG["valuta"]
    spread = dati.setdefault("spread", {})
    for i in range(0, len(asset), 20):
        blocco = asset[i:i + 20]
        try:
            prezzi = api.ticker([f"{a}-{valuta}" for a in blocco])
        except Exception as e:
            print("spread", e)
            continue
        for a in blocco:
            t = prezzi.get(f"{a}-{valuta}")
            if not t:
                continue
            bid, ask = float(t["bid"]), float(t["ask"])
            sp = (ask - bid) / ((ask + bid) / 2)
            spread[a] = sp if a not in spread else spread[a] * 0.7 + sp * 0.3


def ottimizza(api, dati, coppie):
    """Apprendimento: riprova tutte le varianti sugli ultimi giorni e tiene la migliore.
    Allenamento sui primi 2/3 del periodo, verifica sull'ultimo 1/3 (dati che la scelta non ha visto)."""
    valuta = CONFIG["valuta"]
    candidati = scegli_candidati(api, coppie, dati)
    # le crypto in mano vanno sempre analizzate, anche se sono uscite dalla lista
    candidati += [a for a in dati.get("posizioni", {}) if a not in candidati]
    cache = carica_cache()
    storici, storici15 = {}, {}
    for a in candidati:
        try:
            st = api.storico(f"{a}-{valuta}", CONFIG["giorni_storico"] * 24, cache=cache)
            st15 = api.storico(f"{a}-{valuta}", CONFIG["giorni_storico_15m"] * 24, 15, cache=cache) if 15 in intervalli() else []
            if len(st) >= 24 * 20 and (15 not in intervalli() or len(st15) >= 96 * 7):
                storici[a], storici15[a] = st, st15
        except Exception as e:
            print("storico", a, e)
        salva_cache(cache)  # salva man mano: se il giro venisse interrotto, la volta dopo riparte da qui
    # tiene in cache solo le crypto analizzate adesso
    salva_cache({k: v for k, v in cache.items() if k.split("-")[0] in candidati})
    if not storici:
        return None

    # indicatori calcolati una volta per crypto e risoluzione
    prep = {}
    for a in storici:
        for intervallo in intervalli():
            c = [float(x["close"]) for x in storici15[a]] if intervallo == 15 else chiusure(storici[a], intervallo)
            prep[(a, intervallo)] = {"c": c, "rsi": serie_rsi(c, CONFIG["rsi_periodo"]),
                                     "ema": {p: serie_ema(c, p) for p in (5, 9, 12, 13, 20, 21, 26, 50)}}

    def valuta_P(P):
        risultati = {}
        for a in storici:
            d = prep[(a, P["intervallo_candele"])]
            n = len(d["c"])
            taglio = n * 2 // 3
            giorni_tr = max(1.0, taglio * P["intervallo_candele"] / 1440)
            giorni_va = max(1.0, (n - taglio) * P["intervallo_candele"] / 1440)
            pausa = max(1, -(-pausa_ore(P["intervallo_candele"]) * 60 // P["intervallo_candele"]))
            args = (d["c"], d["ema"][P["ema_veloce"]], d["ema"][P["ema_lenta"]], d["rsi"], P)
            costo = costo_reale(dati, a)
            r_tr, dd_tr, n_tr = backtest(*args, 0, taglio, costo, pausa)
            r_va, dd_va, n_va = backtest(*args, taglio, n, costo, pausa)
            # punteggi riportati a "ogni 10 giorni", così strategie veloci e lente sono confrontabili
            risultati[a] = {"train": (r_tr - CONFIG["peso_rischio"] * dd_tr) * 10 / giorni_tr,
                            "verifica": (r_va - CONFIG["peso_rischio"] * dd_va) * 10 / giorni_va,
                            "rend_train": r_tr, "rend_verifica": r_va, "operazioni": n_tr + n_va,
                            "operazioni_verifica": n_va,
                            "operazioni_verifica_al_giorno": round(n_va / giorni_va, 1)}
        # le crypto migliori in allenamento, ma solo se guadagnano anche nella verifica
        # e con abbastanza operazioni da non essere solo un paio di colpi fortunati
        affidabile = lambda a: risultati[a]["operazioni"] >= 2 and risultati[a]["operazioni_verifica"] >= 1
        # le crypto con spread troppo alto (o non misurato) si analizzano solo per gestire quelle già in mano
        ammessa = lambda a: (dati.get("spread") or {}).get(a, 1) <= CONFIG["spread_max"]
        ordinate = sorted((a for a in risultati if ammessa(a)), key=lambda a: risultati[a]["train"], reverse=True)
        scelte = [a for a in ordinate if risultati[a]["train"] > 0 and risultati[a]["verifica"] > 0
                  and affidabile(a)][:CONFIG["max_asset_attivi"]]
        # punteggio sulla MEDIA di tutte le crypto analizzate, non solo sulle migliori:
        # una strategia vale se funziona in generale, non se ha avuto fortuna su una sola moneta
        tutte = ordinate or list(risultati)
        punteggio_train = sum(risultati[a]["train"] for a in tutte) / len(tutte)
        punteggio_ver = sum(risultati[a]["verifica"] for a in tutte) / len(tutte)
        abbastanza = (sum(risultati[a]["operazioni"] for a in tutte) >= CONFIG["min_operazioni_test"] and
                      sum(risultati[a]["operazioni_verifica"] for a in tutte) >= CONFIG["min_operazioni_verifica"])
        if not scelte or not abbastanza:
            punteggio_ver = -1
        return punteggio_train, punteggio_ver, scelte, risultati

    # punteggio: il PEGGIORE tra allenamento e verifica, così vince solo chi va bene in entrambi i periodi
    migliore = None
    for P in griglia():
        tr, ve, scelte, ris = valuta_P(P)
        if ve <= 0 or tr <= 0:
            continue
        if migliore is None or min(tr, ve) > min(migliore[0], migliore[1]):
            migliore = (tr, ve, scelte, ris, P)

    attuale = dati.get("strategia") or strategia_iniziale()
    if dati.get("versione_logica") != CONFIG["versione_logica"]:
        # nuova versione del bot: si riparte dalla strategia di base (quella vecchia era stata scelta con costi irreali)
        attuale = strategia_iniziale()
    if attuale.get("intervallo_candele") not in intervalli():
        # strategia attuale non più ammessa (es. veloci disattivate): va sostituita
        attuale = strategia_iniziale()
    tr_a, ve_a, scelte_a, ris_a = valuta_P(attuale)

    # cambia strategia solo se quella nuova è davvero migliore (margine minimo per non cambiare a caso)
    if migliore and min(migliore[0], migliore[1]) > min(tr_a, ve_a) + 0.002:
        tr, ve, scelte, ris, P = migliore
        origine = "ottimizzazione"
    else:
        tr, ve, scelte, ris, P = tr_a, ve_a, scelte_a, ris_a, {k: attuale.get(k) for k in chiave_nomi() if attuale.get(k) is not None}
        P["tipo"] = tipo(attuale)
        origine = attuale.get("origine", "impostazioni iniziali") if attuale.get("aggiornata") else "confermata"

    nuova = dict(P)
    nuova["tipo"] = tipo(P)
    nuova.update({
        "asset": scelte, "aggiornata": ora_ms(), "origine": origine,
        "rendimento_verifica": round(sum(ris[a]["rend_verifica"] for a in scelte) / len(scelte) * 100, 2) if scelte else None,
        "giorni_verifica": (CONFIG["giorni_storico_15m"] if P["intervallo_candele"] == 15 else CONFIG["giorni_storico"]) // 3,
        "operazioni_al_giorno": round(sum(ris[a]["operazioni_verifica_al_giorno"] for a in scelte) / len(scelte), 1) if scelte else 0,
        "classifica": {a: {"verifica_%": round(ris[a]["rend_verifica"] * 100, 2),
                           "allenamento_%": round(ris[a]["rend_train"] * 100, 2),
                           "operazioni": ris[a]["operazioni"]} for a in ris},
        "varianti_provate": sum(1 for _ in griglia()),
    })
    cambiata = chiave(nuova) != chiave(attuale) or nuova["asset"] != attuale.get("asset")
    dati["strategia"] = nuova
    dati["versione_logica"] = CONFIG["versione_logica"]
    dati.setdefault("storia_strategie", []).append({"t": ora_ms(), "asset": scelte, "parametri": list(chiave(nuova)),
                                                    "rendimento_verifica": nuova["rendimento_verifica"]})
    dati["storia_strategie"] = dati["storia_strategie"][-60:]
    return nuova, cambiata


def chiave_nomi():
    return ("tipo", "intervallo_candele", "ema_veloce", "ema_lenta", "rsi_max_ingresso",
            "stop_loss", "trailing_stop", "attiva_trailing_dopo", "rsi_ingresso", "rsi_uscita", "take_profit")


def descrivi_strategia(P):
    ore = {15: "15 minuti", 60: "1 ora", 240: "4 ore"}.get(P["intervallo_candele"], f"{P['intervallo_candele']} min")
    crypto = ", ".join(P["asset"]) if P["asset"] else "nessuna (mercato sfavorevole: resta in liquidità)"
    if tipo(P) == "rimbalzo":
        righe = [f"<b>Tipo:</b> rimbalzo (compra dopo i cali, rivende alla risalita)",
                 f"<b>Crypto:</b> {crypto}",
                 f"<b>Candele:</b> {ore} · compra con RSI sotto {P['rsi_ingresso']}, rivende sopra {P['rsi_uscita']}",
                 f"<b>Obiettivo:</b> +{P['take_profit']*100:.0f}% · <b>stop loss:</b> -{P['stop_loss']*100:.0f}% · "
                 f"mai se è scesa più del {CONFIG['crollo_max_7g']*100:.0f}% in 7 giorni"]
    else:
        righe = [f"<b>Tipo:</b> segue la tendenza (compra quando il prezzo risale)",
                 f"<b>Crypto:</b> {crypto}",
                 f"<b>Candele:</b> {ore} · EMA {P['ema_veloce']}/{P['ema_lenta']} · RSI max {P['rsi_max_ingresso']}",
                 f"<b>Stop loss:</b> -{P['stop_loss']*100:.1f}% · <b>trailing:</b> -{P['trailing_stop']*100:.1f}% dopo +{P['attiva_trailing_dopo']*100:.1f}%".replace(".0%", "%")]
    righe.append(f"<b>Costi:</b> commissione + spread reale di ogni crypto · escluse quelle con spread sopra "
                 f"{CONFIG['spread_max']*100:.1f}%".replace(".", ","))
    if P.get("classifica"):
        righe.insert(2, f"<b>Analizzate:</b> {len(P['classifica'])} crypto · {P.get('varianti_provate', '–')} strategie ciascuna · "
                        f"valide solo con almeno {CONFIG['min_operazioni_test']} operazioni simulate in totale")
    if P.get("rendimento_verifica") is not None:
        righe.append(f"<b>Test sugli ultimi {P.get('giorni_verifica', 20)} giorni:</b> {P['rendimento_verifica']:+.2f}% (simulazione al netto dei costi, non è una promessa)")
        righe.append(f"<b>Operazioni previste:</b> circa {P.get('operazioni_al_giorno', 0)} al giorno per crypto")
    return "\n".join(righe)


# ---------------------------------------------------------------------------
# SPIEGAZIONI: cosa vede il bot e cosa manca per comprare
# ---------------------------------------------------------------------------
def fmt_prezzo(x):
    """Prezzo leggibile anche per le crypto che valgono pochi centesimi."""
    x = float(x)
    decimali = 2 if x >= 10 else 4 if x >= 0.1 else 6
    return f"{x:,.{decimali}f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def prezzo_per_rsi(valori, periodo, soglia):
    """Stima: a che prezzo dovrebbe chiudere la prossima candela perché l'RSI scenda sotto `soglia`."""
    if len(valori) <= periodo:
        return None
    mg = sum(max(valori[i] - valori[i - 1], 0) for i in range(1, periodo + 1)) / periodo
    mp = sum(max(valori[i - 1] - valori[i], 0) for i in range(1, periodo + 1)) / periodo
    for i in range(periodo + 1, len(valori)):
        d = valori[i] - valori[i - 1]
        mg = (mg * (periodo - 1) + max(d, 0)) / periodo
        mp = (mp * (periodo - 1) + max(-d, 0)) / periodo
    mg_dopo = mg * (periodo - 1) / periodo
    calo = mg_dopo * (100 - soglia) / soglia * periodo - mp * (periodo - 1)
    return valori[-1] - calo if 0 <= calo < valori[-1] else None


def spiega(P, dati, a, ch, ef, el, r, fa7):
    """Una frase su cosa sta facendo il bot con questa crypto."""
    pos = dati["posizioni"].get(a)
    if pos:
        entrata = pos["prezzo_ingresso"]
        stop = entrata * (1 - P["stop_loss"])
        if tipo(P) == "rimbalzo":
            return (f"In mano da {fmt_prezzo(entrata)}: vende se scende a {fmt_prezzo(stop)} (stop loss), se sale a "
                    f"{fmt_prezzo(entrata * (1 + P['take_profit']))} (obiettivo) o se l'RSI supera {P['rsi_uscita']}")
        if pos["picco"] >= entrata * (1 + P["attiva_trailing_dopo"]):
            return f"In guadagno: vende se scende a {fmt_prezzo(pos['picco'] * (1 - P['trailing_stop']))} (protegge il guadagno) o se la tendenza gira"
        return (f"In mano da {fmt_prezzo(entrata)}: stop loss a {fmt_prezzo(stop)}; da {fmt_prezzo(entrata * (1 + P['attiva_trailing_dopo']))} "
                f"in su protegge il guadagno; vende anche se la tendenza gira")
    if dati["stato"] != "attivo":
        return f"Bot {dati['stato']}: niente nuovi acquisti"
    fine_pausa = dati["ultima_vendita"].get(a, 0) + pausa_ore(P["intervallo_candele"]) * 3_600_000
    if ora_ms() < fine_pausa:
        return f"Venduta da poco: può ricomprarla dalle {datetime.fromtimestamp(fine_pausa / 1000, FUSO):%H:%M}"
    mancano = []
    if tipo(P) == "rimbalzo":
        if r >= P["rsi_ingresso"]:
            soglia = prezzo_per_rsi(ch, CONFIG["rsi_periodo"], P["rsi_ingresso"])
            mancano.append(f"RSI {r:.0f}, deve scendere sotto {P['rsi_ingresso']}"
                           + (f" (una candela che chiude sotto ~{fmt_prezzo(soglia)})" if soglia else ""))
        if ch[-1] >= el:
            mancano.append(f"prezzo ancora sopra la sua media ({fmt_prezzo(el)})")
        if fa7 and ch[-1] < fa7 * (1 - CONFIG["crollo_max_7g"]):
            mancano.append(f"è crollata più del {CONFIG['crollo_max_7g']*100:.0f}% in 7 giorni: troppo rischioso")
    else:
        if ef <= el:
            mancano.append(f"tendenza al ribasso: deve tornare sopra {fmt_prezzo(el)} e restarci")
        elif ch[-1] <= el:
            mancano.append(f"prezzo sotto la media ({fmt_prezzo(el)}): deve risalire sopra")
        if r >= P["rsi_max_ingresso"]:
            soglia = prezzo_per_rsi(ch, CONFIG["rsi_periodo"], P["rsi_max_ingresso"])
            mancano.append(f"RSI {r:.0f} troppo alto (max {P['rsi_max_ingresso']}): mercato surriscaldato"
                           + (f", ok sotto ~{fmt_prezzo(soglia)}" if soglia else ""))
    return ("Aspetta: " + "; ".join(mancano)) if mancano else "Condizioni per comprare presenti"


def elenco_spiegazioni(dati):
    sg = dati.get("segnali") or {}
    if not sg:
        return "Nessuna crypto sotto osservazione in questo momento."
    righe = []
    for a, s in sg.items():
        freccia = "▲" if s.get("trend") == "rialzo" else "▼"
        righe.append(f"<b>{a}</b> {fmt_prezzo(s['prezzo'])} {freccia} · RSI {s.get('rsi')}\n{s.get('nota', '')}")
    return "\n\n".join(righe)


def arrotonda_giu(valore, passo):
    passo = D(passo)
    return (D(valore) / passo).to_integral_value(rounding=ROUND_DOWN) * passo


# ---------------------------------------------------------------------------
# DATI
# ---------------------------------------------------------------------------
def _chiave(salt):
    """Chiave AES-256 ricavata dalla password (stesso metodo usato dalla dashboard e dal widget)."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERAZIONI)
    return kdf.derive(PASSWORD_DATI.encode())


def carica_dati():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, encoding="utf-8") as f:
        contenuto = json.load(f)
    if not contenuto.get("cifrato"):
        return contenuto  # vecchio formato non cifrato
    if not PASSWORD_DATI:
        raise RuntimeError("I dati sono cifrati ma manca il Secret DASHBOARD_PASSWORD.")
    salt = base64.b64decode(contenuto["salt"])
    iv = base64.b64decode(contenuto["iv"])
    chiaro = AESGCM(_chiave(salt)).decrypt(iv, base64.b64decode(contenuto["dati"]), None)
    return json.loads(chiaro)


def salva_dati(d):
    d["aggiornato"] = ora_iso()
    if not PASSWORD_DATI:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        return
    # dati cifrati: senza la password chi apre il file vede solo caratteri a caso
    salt, iv = os.urandom(16), os.urandom(12)
    testo = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode()
    cifrato = AESGCM(_chiave(salt)).encrypt(iv, testo, None)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"cifrato": 1, "iterazioni": ITERAZIONI, "salt": base64.b64encode(salt).decode(),
                   "iv": base64.b64encode(iv).decode(), "dati": base64.b64encode(cifrato).decode()}, f)


def default_dati():
    return {
        "versione": 1,
        "valuta": CONFIG["valuta"],
        "inizio": None,
        "capitale_iniziale": None,
        "prezzo_btc_iniziale": None,
        "stato": "attivo",          # attivo | in pausa | fermato
        "base_emergenza": None,
        "posizioni": {},            # asset -> {qty, costo, prezzo_ingresso, picco, dal}
        "ultima_vendita": {},       # asset -> timestamp ms
        "operazioni": [],
        "equity": [],               # [t_ms, valore, benchmark_btc]
        "segnali": {},
        "capitale_bot": None,
        "strategia": None,          # parametri attuali, ricalcolati ogni giorno       # 100 € + guadagni - perdite: il massimo che il bot può usare
        "tg_offset": 0,
        "ultimo_report": None,
        "config": CONFIG,
        "errori": [],
    }


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------
def tg_invia(testo):
    if not TG_TOKEN or not TG_CHAT:
        print("[telegram]", testo)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": testo, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        print("Telegram non raggiungibile:", e)


def tg_comandi(dati):
    if not TG_TOKEN:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": dati.get("tg_offset", 0) + 1, "timeout": 0}, timeout=15)
        aggiornamenti = r.json().get("result", [])
    except Exception as e:
        print("Telegram non raggiungibile:", e)
        return []
    comandi = []
    for u in aggiornamenti:
        dati["tg_offset"] = max(dati.get("tg_offset", 0), u["update_id"])
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != TG_CHAT:
            continue
        testo = (m.get("text") or "").strip().split("@")[0].lower()
        if testo.startswith("/"):
            comandi.append(testo.split()[0])
    return comandi


# ---------------------------------------------------------------------------
# LOGICA PRINCIPALE
# ---------------------------------------------------------------------------
def valuta_portafoglio(saldi, prezzi, valuta):
    """Valore dell'intero conto Revolut X (usato solo all'avvio)."""
    totale = D(saldi.get(valuta, {}).get("total", "0"))
    for a in CONFIG["asset"]:
        qty = D(saldi.get(a, {}).get("total", "0"))
        sim = f"{a}-{valuta}"
        if qty > 0 and sim in prezzi:
            totale += qty * D(prezzi[sim]["bid"])
    return totale


def liquidita_bot(dati):
    """Euro del bot non investiti: capitale del bot (100 € + guadagni - perdite) meno il costo delle posizioni."""
    return max(0.0, dati["capitale_bot"] - sum(p["costo"] for p in dati["posizioni"].values()))


def valore_bot(dati, prezzi, valuta):
    """Valore dei soli soldi del bot: la sua liquidità + le crypto che ha comprato, ai prezzi attuali."""
    totale = liquidita_bot(dati)
    for a, p in dati["posizioni"].items():
        sim = f"{a}-{valuta}"
        if sim in prezzi:
            totale += float(p["qty"]) * float(prezzi[sim]["bid"])
    return D(round(totale, 2))


def riepilogo(dati, valore, dettagliato=False):
    ini = dati.get("capitale_iniziale") or 0
    diff = float(valore) - ini
    perc = (diff / ini * 100) if ini else 0
    segno = "+" if diff >= 0 else ""
    righe = [
        f"<b>Stato:</b> {dati['stato']}",
        f"<b>Valore attuale:</b> {euro(valore)}",
        f"<b>Capitale iniziale:</b> {euro(ini)}",
        f"<b>Risultato:</b> {segno}{euro(diff)} ({segno}{perc:.2f}%)",
    ]
    if dati["equity"]:
        bench = dati["equity"][-1][2]
        if bench:
            righe.append(f"<b>Se avessi solo tenuto BTC:</b> {euro(bench)}")
    if dati["posizioni"]:
        for a, p in dati["posizioni"].items():
            righe.append(f"• {a}: {p['qty']} (comprato a {euro(p['prezzo_ingresso'])})")
    else:
        righe.append("• Nessuna posizione aperta, tutto in liquidità")
    if dettagliato and dati.get("strategia"):
        righe.append("\n🧠 <b>Strategia</b>\n" + descrivi_strategia(dati["strategia"]))
    if dettagliato and dati.get("segnali"):
        righe.append("\n🔎 <b>Cosa vede il bot</b>\n" + elenco_spiegazioni(dati))
    if dettagliato and dati["operazioni"]:
        righe.append("\n<b>Ultime operazioni:</b>")
        for op in dati["operazioni"][-5:]:
            t = datetime.fromtimestamp(op["t"] / 1000, FUSO).strftime("%d/%m %H:%M")
            righe.append(f"{t} {op['lato'].upper()} {op['asset']} {euro(op['importo'])}")
    link = link_dashboard()
    if link:
        righe.append(f"\n📊 {link}")
    return "\n".join(righe)


def registra_operazione(dati, asset, lato, det, prezzo_rif, motivo):
    qty = D(det.get("filled_quantity") or "0")
    importo = D(det.get("filled_amount") or "0")
    prezzo = D(det.get("average_fill_price") or prezzo_rif)
    if importo == 0 and qty > 0:
        importo = qty * prezzo
    fee = D(det.get("total_fee") or "0")
    fee_val = det.get("fee_currency") or CONFIG["valuta"]
    fee_eur = fee if fee_val == CONFIG["valuta"] else fee * prezzo
    op = {
        "t": ora_ms(), "asset": asset, "lato": lato, "qty": str(qty),
        "prezzo": float(prezzo), "importo": float(importo), "commissione": float(fee_eur),
        "motivo": motivo, "stato_ordine": det.get("status"),
    }
    return op, qty, importo, prezzo, fee_eur


def compra(api, dati, asset, eur, prezzo_rif, coppie, motivo):
    sim = f"{asset}-{CONFIG['valuta']}"
    conf = coppie.get(f"{asset}/{CONFIG['valuta']}", {})
    quote = arrotonda_giu(eur, conf.get("quote_step", "0.01"))
    if quote < D(conf.get("min_order_size_quote", CONFIG["ordine_minimo"])) or quote < D(CONFIG["ordine_minimo"]):
        return None
    det = api.ordine_market(sim, "buy", quote_size=str(quote))
    if not det or det.get("status") == "rejected":
        tg_invia(f"⚠️ Acquisto {asset} rifiutato: {det.get('reject_reason') if det else 'nessuna risposta'}")
        return None
    op, qty, importo, prezzo, fee = registra_operazione(dati, asset, "compra", det, prezzo_rif, motivo)
    if qty <= 0:
        return None
    dati["operazioni"].append(op)
    dati["posizioni"][asset] = {
        "qty": str(qty), "costo": float(importo + (fee if det.get("fee_currency") == CONFIG["valuta"] else 0)),
        "prezzo_ingresso": float(prezzo), "picco": float(prezzo), "dal": ora_ms(),
    }
    tg_invia(f"🟢 <b>COMPRATO {asset}</b>\n{qty} a {euro(prezzo)}\nSpesa: {euro(importo)} · commissione {euro(fee)}\nMotivo: {motivo}")
    return op


def vendi(api, dati, asset, qty_disp, prezzo_rif, coppie, motivo):
    sim = f"{asset}-{CONFIG['valuta']}"
    conf = coppie.get(f"{asset}/{CONFIG['valuta']}", {})
    qty = arrotonda_giu(qty_disp, conf.get("base_step", "0.00000001"))
    if qty <= 0 or qty < D(conf.get("min_order_size", "0")):
        dati["posizioni"].pop(asset, None)
        return None
    det = api.ordine_market(sim, "sell", base_size=str(qty))
    if not det or det.get("status") == "rejected":
        tg_invia(f"⚠️ Vendita {asset} rifiutata: {det.get('reject_reason') if det else 'nessuna risposta'}")
        return None
    op, qty_v, importo, prezzo, fee = registra_operazione(dati, asset, "vendi", det, prezzo_rif, motivo)
    pos = dati["posizioni"].pop(asset, None)
    if pos:
        risultato = float(importo) - float(fee) - pos["costo"]
        op["risultato"] = round(risultato, 2)
        # guadagni e perdite cambiano il capitale che il bot può usare
        dati["capitale_bot"] = round(dati["capitale_bot"] + risultato, 2)
    dati["operazioni"].append(op)
    dati["ultima_vendita"][asset] = ora_ms()
    r = op.get("risultato")
    extra = f"\nRisultato: {'+' if r >= 0 else ''}{euro(r)}" if r is not None else ""
    tg_invia(f"🔴 <b>VENDUTO {asset}</b>\n{qty_v} a {euro(prezzo)}\nIncasso: {euro(importo)} · commissione {euro(fee)}{extra}\nMotivo: {motivo}")
    return op


def qty_vendibile(dati, saldi, a):
    """Vende solo le crypto comprate dal bot, mai altre che hai sul conto."""
    pos = dati["posizioni"].get(a)
    if not pos:
        return D("0")
    return min(D(saldi.get(a, {}).get("available", "0")), D(pos["qty"]))


def vendi_tutto(api, dati, saldi, prezzi, coppie, motivo):
    for a in list(dati["posizioni"].keys()):
        qty = qty_vendibile(dati, saldi, a)
        sim = f"{a}-{CONFIG['valuta']}"
        if qty > 0 and sim in prezzi:
            vendi(api, dati, a, qty, prezzi[sim]["bid"], coppie, motivo)


def main():
    dati = carica_dati() or default_dati()
    for k, v in default_dati().items():
        dati.setdefault(k, v)
    dati["config"] = CONFIG
    valuta = CONFIG["valuta"]
    if not dati.get("strategia"):
        dati["strategia"] = strategia_iniziale()

    def simboli_utili():
        nomi = set(dati["strategia"]["asset"]) | set(dati["posizioni"]) | set(CONFIG["asset"]) | {"BTC"}
        return sorted(f"{a}-{valuta}" for a in nomi)

    comandi = tg_comandi(dati)
    api = RevX(API_KEY, PRIVATE_PEM)

    saldi = api.saldi()
    coppie = api.coppie()
    prezzi = api.ticker(simboli_utili())
    valore = valuta_portafoglio(saldi, prezzi, valuta)
    prezzo_btc = float(prezzi[f"BTC-{valuta}"]["mid"])

    if SOLO_TEST:
        msg = "✅ <b>Connessione a Revolut X riuscita</b>\n"
        for c, b in saldi.items():
            if D(b.get("total", "0")) > 0:
                msg += f"• {c}: {b['total']}\n"
        msg += f"Valore totale del conto: {euro(valore)}\nIl bot userà al massimo: {euro(min(float(valore), CONFIG['capitale_bot']))}\nPrezzo BTC: {euro(prezzo_btc)}\n(test: nessun ordine eseguito)"
        tg_invia(msg)
        salva_dati(dati)
        return

    # inizializzazione al primo avvio con soldi sul conto
    if not dati.get("capitale_iniziale"):
        if valore < D(CONFIG["ordine_minimo"]):
            print("Conto vuoto: in attesa del deposito.")
            salva_dati(dati)
            return
        iniziale = round(min(float(D(saldi.get(valuta, {}).get("available", "0"))), CONFIG["capitale_bot"]), 2)
        if iniziale < CONFIG["ordine_minimo"]:
            print("Euro non sufficienti su Revolut X: in attesa del deposito.")
            salva_dati(dati)
            return
        dati["capitale_iniziale"] = iniziale
        dati["capitale_bot"] = iniziale
        dati["inizio"] = ora_ms()
        dati["prezzo_btc_iniziale"] = prezzo_btc
        dati["base_emergenza"] = iniziale
        tg_invia(f"🚀 <b>Bot avviato</b>\nCapitale del bot: {euro(iniziale)}\n"
                 "Userà solo questi soldi e i loro guadagni, mai altre ricariche.\nScrivi /aiuto per i comandi.")
    if dati.get("capitale_bot") is None:
        dati["capitale_bot"] = dati["capitale_iniziale"]

    # controlla che le crypto del bot ci siano ancora (se le vendi a mano, il bot lo registra)
    for a in list(dati["posizioni"].keys()):
        pos = dati["posizioni"][a]
        sim = f"{a}-{valuta}"
        saldo = D(saldi.get(a, {}).get("total", "0"))
        if saldo < D(pos["qty"]):
            if sim in prezzi and saldo * D(prezzi[sim]["bid"]) >= D("1"):
                pos["qty"] = str(saldo)
            else:
                # venduta fuori dal bot: chiude la posizione al prezzo attuale
                if sim in prezzi:
                    ricavo = float(pos["qty"]) * float(prezzi[sim]["bid"])
                    dati["capitale_bot"] = round(dati["capitale_bot"] + ricavo - pos["costo"], 2)
                dati["posizioni"].pop(a)

    # apprendimento: ricalcola la strategia una volta al giorno (o su comando /ottimizza)
    scaduta = (not dati["strategia"].get("aggiornata") or
               dati.get("versione_logica") != CONFIG["versione_logica"] or
               dati["strategia"].get("intervallo_candele") not in intervalli() or
               ora_ms() - dati["strategia"]["aggiornata"] > CONFIG["ottimizza_ogni_ore"] * 3_600_000)
    if scaduta or "/ottimizza" in comandi:
        try:
            esito = ottimizza(api, dati, coppie)
            if esito:
                nuova, cambiata = esito
                if cambiata or "/ottimizza" in comandi:
                    tg_invia(("🧠 <b>Strategia aggiornata</b>\n" if cambiata else "🧠 <b>Strategia confermata</b>\n")
                             + descrivi_strategia(nuova))
                prezzi = api.ticker(simboli_utili())
        except Exception as e:
            dati["errori"] = (dati["errori"] + [f"{ora_iso()} ottimizzazione: {e}"])[-20:]
    P = dati["strategia"]

    valore = valore_bot(dati, prezzi, valuta)

    # comandi telegram
    for c in comandi:
        if c in ("/start", "/aiuto", "/help"):
            tg_invia("<b>Comandi</b>\n/stato – situazione attuale\n/report – situazione + ultime operazioni\n"
                     "/pausa – niente nuovi acquisti (stop loss resta attivo)\n/riprendi – riattiva il bot\n"
                     "/vendi_tutto – vende tutto e mette in pausa\n/strategia – strategia attuale\n"
                     "/perche – cosa vede il bot e cosa aspetta per comprare\n"
                     "/ottimizza – ricalcola subito la strategia")
        elif c == "/strategia":
            tg_invia("🧠 <b>Strategia attuale</b>\n" + descrivi_strategia(P))
        elif c in ("/perche", "/perché"):
            tg_invia("🔎 <b>Cosa vede il bot</b>\n\n" + elenco_spiegazioni(dati))
        elif c == "/stato":
            tg_invia(riepilogo(dati, valore))
        elif c == "/report":
            tg_invia(riepilogo(dati, valore, dettagliato=True))
        elif c == "/pausa":
            dati["stato"] = "in pausa"
            tg_invia("⏸ Bot in pausa: nessun nuovo acquisto. Le protezioni sulle posizioni aperte restano attive.")
        elif c == "/riprendi":
            dati["stato"] = "attivo"
            dati["base_emergenza"] = float(valore)
            tg_invia("▶️ Bot riattivato.")
        elif c == "/vendi_tutto":
            vendi_tutto(api, dati, saldi, prezzi, coppie, "comando manuale")
            dati["stato"] = "in pausa"
            tg_invia("🛑 Venduto tutto. Bot in pausa: scrivi /riprendi per riattivarlo.")
            saldi = api.saldi()

    # freno di emergenza
    base = dati.get("base_emergenza") or dati["capitale_iniziale"]
    if dati["stato"] != "fermato" and float(valore) < base * CONFIG["soglia_emergenza"]:
        vendi_tutto(api, dati, saldi, prezzi, coppie, "freno di emergenza")
        dati["stato"] = "fermato"
        tg_invia(f"🚨 <b>Freno di emergenza</b>: il capitale è sceso sotto il {int(CONFIG['soglia_emergenza']*100)}%. "
                 "Ho venduto tutto e fermato il bot. Scrivi /riprendi se vuoi ripartire.")
        saldi = api.saldi()

    # strategia
    if dati["stato"] != "fermato":
        dati["segnali"] = {k: v for k, v in dati["segnali"].items() if k in set(P["asset"]) | set(dati["posizioni"])}
        for a in sorted(set(P["asset"]) | set(dati["posizioni"])):
            sim = f"{a}-{valuta}"
            if sim not in prezzi:
                continue
            try:
                if P["intervallo_candele"] == 15:
                    ch = [float(x["close"]) for x in api.storico(sim, (P["ema_lenta"] + 40) // 4 + 1, 15)]
                else:
                    ore = (P["ema_lenta"] + 40) * P["intervallo_candele"] // 60
                    ch = chiusure(api.storico(sim, ore), P["intervallo_candele"])
            except Exception as e:
                dati["errori"] = (dati["errori"] + [f"{ora_iso()} candele {a}: {e}"])[-20:]
                continue
            if len(ch) < P["ema_lenta"] + 5:
                continue
            ef, el = ema(ch, P["ema_veloce"]), ema(ch, P["ema_lenta"])
            r = rsi(ch, CONFIG["rsi_periodo"])
            bid, ask = float(prezzi[sim]["bid"]), float(prezzi[sim]["ask"])
            trend_su = ef > el and ch[-1] > el
            fa7 = None
            if tipo(P) == "rimbalzo":
                try:
                    orarie = api.storico(sim, 7 * 24 + 2)
                    if len(orarie) >= 7 * 24:
                        fa7 = float(orarie[-7 * 24]["close"])
                except Exception:
                    pass
            cifre = lambda x: float(f"{x:.6g}")  # anche per le crypto da pochi centesimi
            dati["segnali"][a] = {"prezzo": bid, "ema_veloce": cifre(ef), "ema_lenta": cifre(el),
                                  "rsi": round(r, 1), "trend": "rialzo" if trend_su else "ribasso", "t": ora_ms(),
                                  "nota": spiega(P, dati, a, ch, ef, el, r, fa7)}
            if fa7:
                dati["segnali"][a]["variazione_7g"] = round((bid / fa7 - 1) * 100, 1)

            pos = dati["posizioni"].get(a)
            if pos:
                # stesse regole della simulazione: si guarda la chiusura dell'ultima candela, non ogni oscillazione
                # del momento (su Revolut X il prezzo può fare scatti di qualche % per pochi secondi)
                entrata = pos["prezzo_ingresso"]
                pos["picco"] = max(pos["picco"], ch[-1])
                motivo = esce(P, ch[-1], entrata, pos["picco"], ef, el, r)
                if not motivo and bid <= entrata * (1 - P["stop_loss"] * CONFIG["stop_emergenza_x"]):
                    motivo = f"stop di emergenza (-{P['stop_loss'] * CONFIG['stop_emergenza_x'] * 100:.0f}%)"
                if not motivo and tipo(P) == "rimbalzo" and bid >= entrata * (1 + P["take_profit"]):
                    motivo = f"rimbalzo incassato (+{P['take_profit']*100:.0f}%)"
                if motivo:
                    qty = qty_vendibile(dati, saldi, a)
                    vendi(api, dati, a, qty, bid, coppie, motivo)
                    if a not in dati["posizioni"]:
                        dati["segnali"][a]["nota"] = "Appena venduta: " + motivo
                    saldi = api.saldi()
            elif a in P["asset"] and dati["stato"] == "attivo" and entra(P, ch[-1], ef, el, r, fa7):
                pausa = pausa_ore(P["intervallo_candele"]) * 3_600_000
                if ora_ms() - dati["ultima_vendita"].get(a, 0) < pausa:
                    continue
                # usa solo la liquidità del bot (100 € + guadagni), mai altri euro del conto
                disponibile = min(float(saldi.get(valuta, {}).get("available", "0")), liquidita_bot(dati))
                quota = 1 / max(1, len(P["asset"]))
                importo = min(disponibile, dati["capitale_bot"] * quota) * 0.995
                if importo >= CONFIG["ordine_minimo"]:
                    motivo = (f"rimbalzo: crypto scesa troppo (RSI {r:.0f} sotto {P['rsi_ingresso']})"
                              if tipo(P) == "rimbalzo" else
                              f"trend in rialzo (EMA{P['ema_veloce']} sopra EMA{P['ema_lenta']}), RSI {r:.0f}")
                    if compra(api, dati, a, importo, ask, coppie, motivo):
                        dati["segnali"][a]["nota"] = "Appena comprata: " + motivo
                    saldi = api.saldi()
                else:
                    dati["segnali"][a]["nota"] = ("Condizioni per comprare presenti, ma la liquidità libera del bot "
                                                  f"({euro(disponibile)}) è già impegnata sulle altre crypto")

    # valore finale e curva
    prezzi = api.ticker(list(prezzi.keys()))
    valore = valore_bot(dati, prezzi, valuta)
    prezzo_btc = float(prezzi[f"BTC-{valuta}"]["mid"])
    bench = dati["capitale_iniziale"] * prezzo_btc / dati["prezzo_btc_iniziale"]
    # un punto sulla curva ogni 5 minuti circa (il bot gira più spesso, ma basta così)
    if not dati["equity"] or ora_ms() - dati["equity"][-1][0] >= 4 * 60_000:
        dati["equity"].append([ora_ms(), round(float(valore), 2), round(bench, 2)])
    if len(dati["equity"]) > MAX_PUNTI:
        meta = len(dati["equity"]) // 2
        dati["equity"] = dati["equity"][:meta:2] + dati["equity"][meta:]
    dati["valore_attuale"] = round(float(valore), 2)
    dati["liquidita"] = round(liquidita_bot(dati), 2)

    # report serale
    adesso = datetime.now(FUSO)
    oggi = adesso.strftime("%Y-%m-%d")
    if adesso.hour >= CONFIG["ora_report"] and dati.get("ultimo_report") != oggi:
        dati["ultimo_report"] = oggi
        tg_invia("🌙 <b>Resoconto del giorno</b>\n" + riepilogo(dati, valore, dettagliato=True))

    salva_dati(dati)


def esegui_un_giro():
    """Un controllo completo. Ritorna False se c'è stato un errore."""
    try:
        main()
        return True
    except Exception as e:
        print("ERRORE:", e)
        avvisa = True
        try:
            d = carica_dati()
            if d:
                d["errori"] = (d.get("errori", []) + [f"{ora_iso()} {e}"])[-20:]
                # al massimo un avviso di errore ogni 3 ore, per non intasare Telegram
                avvisa = ora_ms() - d.get("ultimo_avviso_errore", 0) > 3 * 3_600_000
                if avvisa:
                    d["ultimo_avviso_errore"] = ora_ms()
                salva_dati(d)
        except Exception:
            pass
        if avvisa:
            tg_invia(f"⚠️ Errore del bot: {str(e)[:300]}")
        return False


if __name__ == "__main__":
    # GitHub avvia il bot ogni 5 minuti; dentro ogni avvio il bot ripete il controllo
    # ogni CONTROLLO_SECONDI per circa DURATA_GIRO secondi, così risponde ai comandi in meno di un minuto
    durata = int(os.environ.get("DURATA_GIRO", "0") or 0)
    pausa = int(os.environ.get("CONTROLLO_SECONDI", "45") or 45)
    fine = time.time() + durata
    while True:
        if not esegui_un_giro():
            sys.exit(1)
        if SOLO_TEST or time.time() + pausa > fine:
            break
        time.sleep(pausa)
