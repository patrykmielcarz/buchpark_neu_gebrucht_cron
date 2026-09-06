#!/usr/bin/env python3
"""
bph_condition_candidates.py
===========================

Wöchentlicher Cron für die /start-Sektion „Neu oder gebraucht" (bph-cond).

Was der Job tut
---------------
1. Sucht je Hauptkategorie Titel, die es NEU und GEBRAUCHT gibt
   (Paar-Kandidaten, identifiziert über die parentId).
2. Sucht zusätzlich einzelne neue und einzelne (günstige) gebrauchte
   Exemplare als Fallback, falls kein Paar mehr gültig ist.
3. Validiert beides und schreibt das Ergebnis in das Custom Field
   `buchpark_condition_candidates` der jeweiligen Hauptkategorie.

Warum überhaupt ein Cron
------------------------
Das Suchen ist langsam: der Listing-Request mit Property-Filter „Neu"
braucht live gemessen 6,2 s (Bücher, auch bei Wiederholung 3,5 s),
1,7 s (Musik), 0,6 s (Film). Das Auslesen der Geschwister zu bekannten
parentIds dagegen nur ~200 ms. Deshalb: Suche einmal pro Woche hier,
Preise live im Widget.

Warum Store API zum Lesen
-------------------------
Der Property-Filter („nur Artikelzustand = Neu") existiert so nur am
Listing-Endpoint der Store API. Ein Filter auf optionIds am normalen
/product-Endpoint lief in der Messung ins Timeout (>45 s), ein Filter
auf `ean` brauchte 44 s für eine einzige EAN. Geschrieben wird über die
Admin API, weil Custom Fields nur dort schreibbar sind.

Bekannte Datenfallen (alle unten im Code behandelt)
---------------------------------------------------
* Manche Kind-Exemplare tragen ZWEI Artikelzustand-Optionen gleichzeitig
  (z. B. „Neu" + „Wie neu"). Sie sind nicht eindeutig -> überspringen.
  Betrifft vor allem Musik und ist auch der Grund, warum der
  Artikelzustand-Filter im Shop dort unzuverlässig ist.
* Preise sind nicht immer monoton über die Zustände (Akzeptabel teurer
  als Gut). Nur absteigende Paare kommen durch.
* Frisch importierte Artikel tragen ein generiertes Platzhalter-Cover
  (500x500, metaData.type == 3). Gilt als „kein Bild".
* calculatedPrice.unitPrice kann 0 sein -> als „kein Preis" behandeln.

Environment
-----------
SW_SHOP_URL            z. B. https://buchpark.de
SW_STORE_API_KEY       sw-access-key des Verkaufskanals (lesen)
SW_ADMIN_CLIENT_ID     Integration aus dem Admin (schreiben)
SW_ADMIN_CLIENT_SECRET
BPH_DRY_RUN            "1" = nur ausgeben, nichts schreiben
"""

import json
import os
import sys
import time
from typing import Dict, List, Optional

import requests

# ──────────────────────────────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────────────────────────────

SHOP_URL = os.environ.get("SW_SHOP_URL", "https://buchpark.de").rstrip("/")
STORE_KEY = os.environ.get("SW_STORE_API_KEY", "")
ADMIN_ID = os.environ.get("SW_ADMIN_CLIENT_ID", "")
ADMIN_SECRET = os.environ.get("SW_ADMIN_CLIENT_SECRET", "")
DRY_RUN = os.environ.get("BPH_DRY_RUN") == "1"

CUSTOM_FIELD = "buchpark_condition_candidates"

CATEGORIES = {
    "buch":  "5ad62c021e3ff311321a1c5b357e43dd",   # Bücher
    "musik": "547da3f90ec7bf92c7dd96d16c80ca01",   # Musik-CDs & Vinyl
    "film":  "6b9248b51d9f829b12533046ac8fdc91",   # DVD & Blu-ray
}

# property_group_option-IDs der Gruppe „Artikelzustand"
OPTION_NEU = "01952253885d72fbbde5dbdaff169068"
OPTION_SEHR_GUT = "0194bba0b25b7b22bc42c9240c739fb5"

# bester Zustand zuerst — bestimmt, welches gebrauchte Exemplar gezeigt wird
USED_ORDER = ["Wie neu", "Sehr gut", "Gut", "Akzeptabel"]

# Ausschlüsse (identisch zum Widget, das zusätzlich nochmal prüft)
EXCLUDE_CATEGORY_IDS = {
    "af91697f2b08944de1386b6cf66a801f",  # Schule & Lernen
    "241d50c58a7e8df8f72567b1331562e7",  # Kalender
    "381b7c84435577ecf0425400af1d948b",  # Erotik
}
EXCLUDE_TITLE_WORDS = [
    "arbeitsheft", "lösungen", "schuljahr", "klausur",
    "prüfungstraining", "lehrerband", "kalender",
]

MIN_SAVE_PCT = 30          # Mindestersparnis im Paar-Modus
MIN_PRICE = 1.0            # Cent-Artikel raus
PAIRS_PER_CATEGORY = 8     # so viele Paar-Kandidaten pro Kategorie speichern
FALLBACK_PER_SIDE = 5      # so viele Einzel-Exemplare je Seite speichern
SCAN_PAGES = 3             # Listing-Seiten à 100, die durchsucht werden
SIBLING_BATCH = 10         # parentIds pro Geschwister-Request

PRODUCT_INCLUDES = {
    "product": ["id", "parentId", "name", "translated", "cover",
                "calculatedPrice", "options", "categoryIds"],
    "property_group_option": ["name", "group"],
    "property_group": ["name"],
    "media": ["url", "metaData"],
    "product_media": ["media"],
    "calculated_price": ["unitPrice"],
}


# ──────────────────────────────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────────────────────────────

session = requests.Session()


def store_post(path: str, payload: dict, tries: int = 3) -> dict:
    url = f"{SHOP_URL}/store-api{path}"
    headers = {"Content-Type": "application/json", "sw-access-key": STORE_KEY}
    for attempt in range(1, tries + 1):
        try:
            r = session.post(url, headers=headers, json=payload, timeout=90)
            r.raise_for_status()
            return r.json()
        except Exception as exc:                       # noqa: BLE001
            if attempt == tries:
                raise
            log(f"  ! Store API {path} fehlgeschlagen ({exc}), Versuch {attempt + 1}")
            time.sleep(3 * attempt)
    return {}


def admin_token() -> str:
    r = session.post(
        f"{SHOP_URL}/api/oauth/token",
        json={
            "grant_type": "client_credentials",
            "client_id": ADMIN_ID,
            "client_secret": ADMIN_SECRET,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ──────────────────────────────────────────────────────────────────────
# Produkt-Helfer
# ──────────────────────────────────────────────────────────────────────

def product_name(p: dict) -> str:
    return (p.get("translated") or {}).get("name") or p.get("name") or ""


def product_price(p: dict) -> float:
    price = (p.get("calculatedPrice") or {}).get("unitPrice") or 0
    return float(price)


def has_real_cover(p: dict) -> bool:
    media = (p.get("cover") or {}).get("media") or {}
    if not media.get("url"):
        return False
    meta = media.get("metaData") or {}
    # generiertes Platzhalter-Cover
    if meta.get("type") == 3 and meta.get("width") == 500 and meta.get("height") == 500:
        return False
    return True


def condition(p: dict) -> Optional[str]:
    """Zustand des Exemplars — None, wenn keiner oder mehrdeutig (mehrere Optionen)."""
    opts = [
        o for o in (p.get("options") or [])
        if ((o.get("group") or {}).get("name")) == "Artikelzustand"
    ]
    return opts[0].get("name") if len(opts) == 1 else None


def is_excluded(p: dict) -> bool:
    name = product_name(p).lower()
    if any(word in name for word in EXCLUDE_TITLE_WORDS):
        return True
    if EXCLUDE_CATEGORY_IDS & set(p.get("categoryIds") or []):
        return True
    return False


def usable(p: dict) -> bool:
    return (
        product_price(p) >= MIN_PRICE
        and condition(p) is not None
        and has_real_cover(p)
        and not is_excluded(p)
    )


# ──────────────────────────────────────────────────────────────────────
# Schritt 1: Paar-Kandidaten
# ──────────────────────────────────────────────────────────────────────

def scan_parent_ids(category_id: str) -> List[str]:
    """parentIds aller Titel, die ein NEUES Exemplar in dieser Kategorie haben."""
    parents: List[str] = []
    seen = set()
    for page in range(1, SCAN_PAGES + 1):
        data = store_post(
            f"/product-listing/{category_id}",
            {
                "limit": 100,
                "p": page,
                "total-count-mode": 0,
                "properties": OPTION_NEU,
                "reduce-aggregations": True,
                "includes": {"product": ["id", "parentId"]},
            },
        )
        elements = data.get("elements") or []
        if not elements:
            break
        for el in elements:
            pid = el.get("parentId")
            if pid and pid not in seen:
                seen.add(pid)
                parents.append(pid)
    return parents


def fetch_siblings(parent_ids: List[str]) -> Dict[str, List[dict]]:
    """Alle Kind-Exemplare zu den parentIds, gruppiert nach parentId (~200 ms je Batch)."""
    by_parent: Dict[str, List[dict]] = {}
    for i in range(0, len(parent_ids), SIBLING_BATCH):
        batch = parent_ids[i:i + SIBLING_BATCH]
        data = store_post(
            "/product",
            {
                "limit": 100,
                "total-count-mode": 0,
                "filter": [{"type": "equalsAny", "field": "parentId", "value": batch}],
                "associations": {"options": {"associations": {"group": {}}}},
                "includes": PRODUCT_INCLUDES,
            },
        )
        for el in data.get("elements") or []:
            by_parent.setdefault(el.get("parentId"), []).append(el)
    return by_parent


def evaluate_pair(children: List[dict]) -> Optional[dict]:
    """Bestes Neu/Gebraucht-Paar eines Titels — oder None, wenn es keins gibt."""
    new_copy = None
    used_copy = None
    for child in children:
        if not usable(child):
            continue
        grade = condition(child)
        price = product_price(child)
        if grade == "Neu":
            if new_copy is None or price < product_price(new_copy):
                new_copy = child
            continue
        if grade not in USED_ORDER:
            continue
        # bester Zustand gewinnt, nicht der billigste
        if used_copy is None or USED_ORDER.index(grade) < USED_ORDER.index(condition(used_copy)):
            used_copy = child

    if not new_copy or not used_copy:
        return None

    new_price = product_price(new_copy)
    used_price = product_price(used_copy)
    if used_price >= new_price:
        return None
    save = round((1 - used_price / new_price) * 100)
    if save < MIN_SAVE_PCT:
        return None

    return {
        "parentId": new_copy.get("parentId"),
        "name": product_name(new_copy),
        "neu": new_price,
        "used": used_price,
        "grade": condition(used_copy),
        "save": save,
    }


def collect_pairs(category_id: str) -> List[dict]:
    parents = scan_parent_ids(category_id)
    log(f"  {len(parents)} Titel mit Neu-Exemplar gefunden")
    by_parent = fetch_siblings(parents)
    pairs = [p for p in (evaluate_pair(kids) for kids in by_parent.values()) if p]
    pairs.sort(key=lambda x: x["save"], reverse=True)
    return pairs[:PAIRS_PER_CATEGORY]


# ──────────────────────────────────────────────────────────────────────
# Schritt 2: Fallback-Einzelprodukte
# ──────────────────────────────────────────────────────────────────────

def collect_singles(category_id: str, option_id: str, want_grade: str,
                    order: Optional[str]) -> List[dict]:
    """
    Einzelne Exemplare eines Zustands.

    Gebrauchte werden nach Preis aufsteigend geholt: die erste Listing-Seite
    lieferte sonst teure Vinyl-Pressungen (22–28 €), gegen die jedes neue
    Exemplar günstig aussah — genau umgekehrt zur Aussage der Sektion.
    """
    out: List[dict] = []
    for page in range(1, SCAN_PAGES + 1):
        if len(out) >= FALLBACK_PER_SIDE:
            break
        payload = {
            "limit": 60,
            "p": page,
            "total-count-mode": 0,
            "properties": option_id,
            "reduce-aggregations": True,
            "associations": {"options": {"associations": {"group": {}}}},
            "includes": PRODUCT_INCLUDES,
        }
        if order:
            payload["order"] = order
        data = store_post(f"/product-listing/{category_id}", payload)
        for el in data.get("elements") or []:
            if len(out) >= FALLBACK_PER_SIDE:
                break
            if condition(el) != want_grade or not usable(el):
                continue
            out.append({"id": el["id"], "name": product_name(el), "price": product_price(el)})
    return out


# ──────────────────────────────────────────────────────────────────────
# Schritt 3: Schreiben
# ──────────────────────────────────────────────────────────────────────

def write_category(token: str, category_id: str, payload: dict) -> None:
    """
    Custom Field der Kategorie aktualisieren.

    Erst lesen, dann mergen, dann schreiben — damit andere Felder
    (z. B. buchpark_category_product_count_ aus dem Zähler-Cron)
    unangetastet bleiben.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = session.get(f"{SHOP_URL}/api/category/{category_id}", headers=headers, timeout=30)
    r.raise_for_status()
    existing = (r.json().get("data") or {}).get("attributes") or {}
    custom_fields = dict(existing.get("customFields") or {})
    custom_fields[CUSTOM_FIELD] = json.dumps(payload, ensure_ascii=False)

    r = session.patch(
        f"{SHOP_URL}/api/category/{category_id}",
        headers=headers,
        json={"customFields": custom_fields},
        timeout=30,
    )
    r.raise_for_status()


# ──────────────────────────────────────────────────────────────────────
# Ablauf
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if not STORE_KEY:
        log("SW_STORE_API_KEY fehlt — Abbruch.")
        return 1
    if not DRY_RUN and not (ADMIN_ID and ADMIN_SECRET):
        log("Admin-Zugangsdaten fehlen — Abbruch (oder BPH_DRY_RUN=1 setzen).")
        return 1

    results = {}
    for key, category_id in CATEGORIES.items():
        log(f"\n=== {key} ===")
        started = time.time()

        pairs = collect_pairs(category_id)
        log(f"  {len(pairs)} gültige Paare")
        for p in pairs:
            log(f"    {p['name'][:44]} | Neu {p['neu']:.2f} -> {p['grade']} {p['used']:.2f} (-{p['save']}%)")

        fb_new = collect_singles(category_id, OPTION_NEU, "Neu", None)
        fb_used = collect_singles(category_id, OPTION_SEHR_GUT, "Sehr gut", "price-asc")
        log(f"  Fallback: {len(fb_new)} neu / {len(fb_used)} gebraucht")

        results[key] = {
            "payload": {
                "updated": time.strftime("%Y-%m-%d"),
                "pairs": [p["parentId"] for p in pairs],
                "fallbackNeu": [p["id"] for p in fb_new],
                "fallbackUsed": [p["id"] for p in fb_used],
            },
            "categoryId": category_id,
        }
        log(f"  {time.time() - started:.1f}s")

    # Sicherung: keine Kategorie leer schreiben — lieber alten Stand behalten
    empty = [k for k, v in results.items()
             if not v["payload"]["pairs"] and not (v["payload"]["fallbackNeu"] and v["payload"]["fallbackUsed"])]
    for key in empty:
        log(f"\n! {key}: keine verwertbaren Kandidaten — Kategorie wird NICHT überschrieben")
        results.pop(key)

    if DRY_RUN:
        log("\n--- DRY RUN, es wird nichts geschrieben ---")
        log(json.dumps({k: v["payload"] for k, v in results.items()}, indent=1, ensure_ascii=False))
        return 0

    if not results:
        log("\nNichts zu schreiben.")
        return 1

    token = admin_token()
    for key, entry in results.items():
        write_category(token, entry["categoryId"], entry["payload"])
        log(f"\n{key}: geschrieben "
            f"({len(entry['payload']['pairs'])} Paare, "
            f"{len(entry['payload']['fallbackNeu'])}/{len(entry['payload']['fallbackUsed'])} Fallback)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
