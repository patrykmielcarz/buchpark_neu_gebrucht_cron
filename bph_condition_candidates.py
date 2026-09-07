#!/usr/bin/env python3
"""
bph_condition_candidates.py
===========================

Wöchentlicher Cron für die /start-Sektion „Neu oder gebraucht" (bph-cond).

NUR BÜCHER
----------
Die Sektion zeigt drei Kacheln, und alle drei sind Bücher-Unterkategorien.
Musik und DVD & Blu-ray sind bewusst raus.

Rotation
--------
Jede Woche werden drei Unterkategorien gezogen — zufällig, aber ohne
Wiederholung: bereits gezeigte Kategorien werden übersprungen, bis der
Pool leer ist. Läuft er MITTEN in einer Ziehung leer, beginnt ein neuer
Zyklus, wobei die in dieser Runde schon gezogenen Kategorien ausgenommen
bleiben — sonst stünde dieselbe Kategorie zweimal nebeneinander.

Der Rotationsstand steht im selben Custom Field wie die Kacheln
(Schlüssel "used"). Ein eigener Speicher ist damit unnötig, und ein
fehlgeschlagener Lauf „verbraucht" keine Kategorie: geschrieben wird
erst, wenn Kandidaten wirklich gefunden wurden.

Was der Job tut
---------------
1. Zieht drei Bücher-Unterkategorien (siehe oben).
2. Sucht je Kategorie Titel, die es NEU und GEBRAUCHT gibt
   (Paar-Kandidaten, identifiziert über die parentId).
3. Sucht zusätzlich einzelne neue und einzelne (günstige) gebrauchte
   Exemplare als Fallback, falls kein Paar mehr gültig ist.
4. Schreibt alles zusammen in EIN Custom Field
   `buchpark_condition_candidates` an der Kategorie „Bücher".
   Das Widget liest genau dieses Feld und rendert, was drinsteht —
   es kennt die Kategorien nicht mehr selbst.

Warum überhaupt ein Cron
------------------------
Das Suchen ist langsam: der Listing-Request mit Property-Filter „Neu"
brauchte live gemessen mehrere Sekunden. Das Auslesen der Geschwister zu
bekannten parentIds dagegen nur ~200 ms. Deshalb: Suche einmal pro Woche
hier, Preise live im Widget.

Warum Store API zum Lesen
-------------------------
Der Property-Filter („nur Artikelzustand = Neu") existiert so nur am
Listing-Endpoint der Store API. Ein Filter auf optionIds am normalen
/product-Endpoint lief in der Messung ins Timeout (>45 s). Geschrieben
wird über die Admin API, weil Custom Fields nur dort schreibbar sind.

Bekannte Datenfallen (alle unten im Code behandelt)
---------------------------------------------------
* In den Bücher-Kategorien liegen auch Nicht-Bücher. Maßgeblich ist
  nicht die Kategorie, sondern book_details_media_type am Produkt.
* Manche Kind-Exemplare tragen ZWEI Artikelzustand-Optionen gleichzeitig
  (z. B. „Neu" + „Wie neu"). Sie sind nicht eindeutig -> überspringen.
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
BPH_RESET_ROTATION     "1" = Rotationsstand verwerfen und neu beginnen
"""

import json
import os
import random
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

# ──────────────────────────────────────────────────────────────────────
# Konfiguration
# ──────────────────────────────────────────────────────────────────────

SHOP_URL = os.environ.get("SW_SHOP_URL", "https://buchpark.de").rstrip("/")
STORE_KEY = os.environ.get("SW_STORE_API_KEY", "")
ADMIN_ID = os.environ.get("SW_ADMIN_CLIENT_ID", "")
ADMIN_SECRET = os.environ.get("SW_ADMIN_CLIENT_SECRET", "")
DRY_RUN = os.environ.get("BPH_DRY_RUN") == "1"
RESET_ROTATION = os.environ.get("BPH_RESET_ROTATION") == "1"

CUSTOM_FIELD = "buchpark_condition_candidates"

# Trägerkategorie: hier stehen Kacheln UND Rotationsstand.
CONFIG_CATEGORY_ID = "5ad62c021e3ff311321a1c5b357e43dd"   # Bücher

TILE_COUNT = 3

# Pool der Bücher-Unterkategorien: (categoryId, Anzeigename).
# Schule & Lernen, Kalender und Erotik sind bewusst nicht dabei — sie
# stehen ohnehin in EXCLUDE_CATEGORY_IDS.
CATEGORY_POOL: List[Tuple[str, str]] = [
    ("19c8b5436914ace027d19139f048d0e7", "Biografien & Erinnerungen"),
    ("544d3a4f60daf8282a00b16d7d64eab9", "Börse & Geld"),
    ("c4a2f5742a5b8d1c0bf9b730abd85831", "Business & Karriere"),
    ("a7f588b600962e7f0748ddb12a0b0aa2", "Comics & Mangas"),
    ("b31d85348e8d3850ed8d950b387edac2", "Computer & Internet"),
    ("e54dc19adfe2255cee1898676f0a999c", "Esoterik"),
    ("144982acbefab767d0b862eb2080f53d", "Fachbücher"),
    ("7bd2727970f3145d4f8ee23b6a55afbd", "Fantasy & Science Fiction"),
    ("29ef598fe119dd4f77401634ae3bbb51", "Film, Kunst & Kultur"),
    ("427b9567dd2219535348abfb4f112c1f", "Freizeit, Haus & Garten"),
    ("43fad17d2032f59a9fe40bedfc699591", "Geschenkbücher"),
    ("725b0927d9d616a362fc4b957e2ce1ab", "Jugendbücher"),
    ("b310f92273f5f3d46600bf0336eeddef", "Kinderbücher"),
    ("513f6114563ce94e1417f4a60397ccb0", "Kochen & Genießen"),
    ("5c38a83946937ad372eb51c1bdf684d7", "Krimis & Thriller"),
    ("4652e4aa57836e42dbea13c404002941", "Liebesromane"),
    ("06cb4b87054afa0b3fc7b1817f81ed45", "Literatur & Fiktion"),
    ("1b1e265a25d71f126ea13e9f236d022d", "Medizin"),
    ("b5c43b3d6aadf01b2b113065e279ab34", "Naturwissenschaften & Technik"),
    ("379e443421f0e4f069225dbdafdc4839", "Politik & Geschichte"),
    ("13887e0cafdf2e1240191c2f3d6b863c", "Ratgeber"),
    ("4bf23d0a436024d1273bcc0a1c263523", "Recht"),
    ("2e93d15da06a595e2715b8e1ed59f3ab", "Reise & Abenteuer"),
    ("1fbaf5f60dcdab6b6fc0ecef6d253dba", "Religion & Glaube"),
    ("e59ba735f508f65a13df1badf00e6f31", "Sozialwissenschaft"),
    ("4f4a852fa4098ea8b37118c6dbd3cad1", "Sport & Fitness"),
]

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
PAIRS_PER_CATEGORY = 8     # so viele Paar-Kandidaten pro Kachel speichern
FALLBACK_PER_SIDE = 5      # so viele Einzel-Exemplare je Seite speichern
SCAN_PAGES = 3             # Listing-Seiten à 100, die durchsucht werden
SIBLING_BATCH = 10         # parentIds pro Geschwister-Request
MAX_DRAWS = 8              # Reißleine: so viele Kategorien maximal probieren

# Ein PATCH auf eine Kategorie invalidiert Caches und kann deutlich länger
# dauern als ein Lesezugriff — mit 30 s lief der erste Live-Lauf in einen
# ReadTimeout, obwohl der Shop den Schreibvorgang danach ausgeführt hatte.
ADMIN_TIMEOUT = 120

PRODUCT_INCLUDES = {
    "product": ["id", "parentId", "name", "translated", "cover",
                "calculatedPrice", "options", "categoryIds", "customFields"],
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


def log(msg: str) -> None:
    print(msg, flush=True)


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


def is_book(p: dict) -> bool:
    """
    Tatsächliches Medium laut Custom Fields — NICHT laut Kategorie.

    Im Katalog liegen Bücher in Musik und DVD & Blu-ray und umgekehrt.
    Seit die Sektion nur noch Bücher zeigt, ist das ein reines
    Ausschlusskriterium: ohne book_details_* kein Kandidat.
    """
    return bool((p.get("customFields") or {}).get("book_details_media_type"))


def usable(p: dict) -> bool:
    return (
        product_price(p) >= MIN_PRICE
        and condition(p) is not None
        and has_real_cover(p)
        and not is_excluded(p)
        and is_book(p)
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

def collect_singles(category_id: str, option_id: str,
                    want_grade: str, order: Optional[str]) -> List[dict]:
    """
    Einzelne Exemplare eines Zustands.

    Gebrauchte werden nach Preis aufsteigend geholt: die erste Listing-Seite
    lieferte sonst teure Exemplare, gegen die jedes neue günstig aussah —
    genau umgekehrt zur Aussage der Sektion.
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


def category_href(token: str, category_id: str) -> Optional[str]:
    """
    SEO-Pfad der Kategorie fuer den „Alle … ansehen"-Link.

    Ueber die Admin API, nicht ueber die Store API: der Versuch, seoUrls
    an /store-api/category mitzuladen, lieferte im Testlauf durchgehend
    null. Der Link ist Kuer — schlaegt es fehl, rendert das Widget die
    Kachel einfach ohne Fusszeile.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = session.post(
            f"{SHOP_URL}/api/search/seo-url",
            headers=headers,
            json={
                "limit": 1,
                "filter": [
                    {"type": "equals", "field": "foreignKey", "value": category_id},
                    {"type": "equals", "field": "routeName", "value": "frontend.navigation.page"},
                    {"type": "equals", "field": "isCanonical", "value": True},
                ],
            },
            timeout=ADMIN_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
        path = (rows[0].get("attributes") or {}).get("seoPathInfo") if rows else None
        return "/" + path if path else None
    except Exception:                                  # noqa: BLE001
        return None


# ──────────────────────────────────────────────────────────────────────
# Rotation
# ──────────────────────────────────────────────────────────────────────

def pick_next(available: List[Tuple[str, str]],
              picked: List[Tuple[str, str]]) -> Optional[Tuple[str, str]]:
    """
    Nächste Kategorie ziehen. Ist der Pool leer, beginnt ein neuer Zyklus —
    ohne die in dieser Runde bereits gezogenen Kategorien, damit keine
    Kachel doppelt erscheint.
    """
    if not available:
        chosen = {c[0] for c in picked}
        available.extend(c for c in CATEGORY_POOL if c[0] not in chosen)
        log("  ~ Pool erschöpft — neuer Zyklus (laufende Auswahl bleibt ausgenommen)")
    if not available:
        return None
    entry = random.choice(available)
    available.remove(entry)
    return entry


# ──────────────────────────────────────────────────────────────────────
# Schritt 3: Schreiben
# ──────────────────────────────────────────────────────────────────────

def read_custom_fields(token: str, category_id: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = session.get(f"{SHOP_URL}/api/category/{category_id}", headers=headers,
                    timeout=ADMIN_TIMEOUT)
    r.raise_for_status()
    attributes = (r.json().get("data") or {}).get("attributes") or {}
    return dict(attributes.get("customFields") or {})


def previous_payload(custom_fields: dict) -> dict:
    raw = custom_fields.get(CUSTOM_FIELD)
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return {}


def fill_up_from_previous(tiles: List[dict], previous: dict) -> List[dict]:
    """
    Kamen weniger als TILE_COUNT Kacheln zusammen, werden Kacheln des
    letzten Laufs aufgefüllt — aber nur solche, deren Kategorie diesmal
    nicht ohnehin schon dabei ist. Lieber eine Kachel der Vorwoche als
    eine Lücke im Dreier-Grid.
    """
    if len(tiles) >= TILE_COUNT:
        return tiles[:TILE_COUNT]
    have = {t.get("categoryId") for t in tiles}
    for old in previous.get("tiles") or []:
        if len(tiles) >= TILE_COUNT:
            break
        if old.get("categoryId") in have:
            continue
        old = dict(old)
        old["carriedOver"] = True
        tiles.append(old)
        log(f"  ~ Kachel aus dem letzten Lauf übernommen: {old.get('label')}")
    return tiles


def write_category(token: str, category_id: str, serialized: str, tries: int = 3) -> None:
    """
    Custom Field der Kategorie aktualisieren.

    Erst lesen, dann mergen, dann schreiben — damit andere Felder
    (z. B. buchpark_category_product_count_ aus dem Zähler-Cron)
    unangetastet bleiben.

    Ein Timeout heißt hier NICHT, dass nichts passiert ist: beim ersten
    Live-Lauf lief ein PATCH in einen ReadTimeout, der Wert stand danach
    trotzdem in der Kategorie. Deshalb wird nach einem Timeout erst
    gegengelesen und nur dann erneut geschrieben, wenn der Wert fehlt.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for attempt in range(1, tries + 1):
        custom_fields = read_custom_fields(token, category_id)
        if custom_fields.get(CUSTOM_FIELD) == serialized:
            return                                    # steht schon drin
        custom_fields[CUSTOM_FIELD] = serialized
        try:
            r = session.patch(
                f"{SHOP_URL}/api/category/{category_id}",
                headers=headers,
                json={"customFields": custom_fields},
                timeout=ADMIN_TIMEOUT,
            )
            r.raise_for_status()
            return
        except requests.exceptions.ReadTimeout:
            log("  ! PATCH-Timeout — prüfe, ob der Wert trotzdem ankam")
            time.sleep(5)
            if read_custom_fields(token, category_id).get(CUSTOM_FIELD) == serialized:
                log("  -> Wert ist gesetzt, Timeout war nur die Antwort")
                return
            if attempt == tries:
                raise
        except Exception as exc:                      # noqa: BLE001
            if attempt == tries:
                raise
            log(f"  ! Schreiben fehlgeschlagen ({exc}), neuer Versuch")
            time.sleep(5 * attempt)


# ──────────────────────────────────────────────────────────────────────
# Ablauf
# ──────────────────────────────────────────────────────────────────────

def main() -> int:
    if not STORE_KEY:
        log("SW_STORE_API_KEY fehlt — Abbruch.")
        return 1
    if not (ADMIN_ID and ADMIN_SECRET):
        log("Admin-Zugangsdaten fehlen — Abbruch (Rotationsstand liegt im Custom Field).")
        return 1

    token = admin_token()
    previous = previous_payload(read_custom_fields(token, CONFIG_CATEGORY_ID))
    used: List[str] = [] if RESET_ROTATION else list(previous.get("used") or [])
    if RESET_ROTATION:
        log("Rotationsstand wird zurückgesetzt (BPH_RESET_ROTATION=1).")

    pool_ids = {c[0] for c in CATEGORY_POOL}
    used = [c for c in used if c in pool_ids]          # entfernte Kategorien aufräumen
    available = [c for c in CATEGORY_POOL if c[0] not in used]

    tiles: List[dict] = []
    picked: List[Tuple[str, str]] = []
    # Produkte haengen in mehreren Kategorien: im Testlauf tauchte
    # „365 Kreuzwortraetsel" sowohl unter Business & Karriere als auch
    # unter Geschenkbuecher auf. Ohne diese Sperre stuende derselbe Titel
    # in zwei Kacheln nebeneinander.
    seen_products: set = set()
    draws = 0

    while len(tiles) < TILE_COUNT and draws < MAX_DRAWS:
        entry = pick_next(available, picked)
        if entry is None:
            break
        draws += 1
        category_id, label = entry
        log(f"\n=== {label} ===")
        started = time.time()

        pairs = collect_pairs(category_id)
        log(f"  {len(pairs)} gültige Paare")
        for p in pairs:
            log(f"    {p['name'][:40]} | Neu {p['neu']:.2f} -> "
                f"{p['grade']} {p['used']:.2f} (-{p['save']}%)")

        fb_new = collect_singles(category_id, OPTION_NEU, "Neu", None)
        fb_used = collect_singles(category_id, OPTION_SEHR_GUT, "Sehr gut", "price-asc")
        log(f"  Fallback: {len(fb_new)} neu / {len(fb_used)} gebraucht")
        log(f"  {time.time() - started:.1f}s")

        # Dubletten gegen die bereits vergebenen Kacheln herausnehmen …
        pair_ids = [p["parentId"] for p in pairs if p["parentId"] not in seen_products]
        neu_ids = [p["id"] for p in fb_new if p["id"] not in seen_products]
        used_ids = [p["id"] for p in fb_used if p["id"] not in seen_products]

        # … und erst danach pruefen: eine Kachel muss entweder ein Paar
        # oder ein vollstaendiges Fallback-Duo tragen, sonst bliebe sie
        # im Widget leer.
        if not pair_ids and not (neu_ids and used_ids):
            log("  -> keine verwertbaren Kandidaten, nächste Kategorie")
            continue

        seen_products.update(pair_ids + neu_ids + used_ids)
        picked.append(entry)
        tiles.append({
            "categoryId": category_id,
            "label": label,
            "href": category_href(token, category_id),
            "pairs": pair_ids,
            "fallbackNeu": neu_ids,
            "fallbackUsed": used_ids,
        })

    tiles = fill_up_from_previous(tiles, previous)

    if not tiles:
        log("\nKeine Kachel zustande gekommen — Custom Field bleibt unverändert.")
        return 1

    payload = {
        "updated": time.strftime("%Y-%m-%d"),
        "tiles": tiles,
        # Nur frisch gezogene Kategorien verbrauchen den Rotationsstand;
        # übernommene Kacheln zählen nicht als „gezeigt".
        "used": used + [c[0] for c in picked],
    }

    if DRY_RUN:
        log("\n--- DRY RUN, es wird nichts geschrieben ---")
        log(json.dumps(payload, indent=1, ensure_ascii=False))
        return 0

    serialized = json.dumps(payload, ensure_ascii=False)
    try:
        write_category(token, CONFIG_CATEGORY_ID, serialized)
    except Exception as exc:                          # noqa: BLE001
        log(f"\nFEHLER beim Schreiben — {exc} (alter Stand bleibt stehen)")
        return 1

    log("\ngeschrieben: " + ", ".join(
        f"{t['label']} ({len(t['pairs'])} Paare)" for t in tiles))
    log(f"Rotationsstand: {len(payload['used'])}/{len(CATEGORY_POOL)} Kategorien verbraucht")
    return 0


if __name__ == "__main__":
    sys.exit(main())
