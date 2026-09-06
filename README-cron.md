# Cron „Neu oder gebraucht" — Einrichtung

Der Job füllt das Custom Field, aus dem die /start-Sektion `bph-cond`
ihre Kandidaten liest. Preise holt das Widget weiterhin live.

## 1. Custom Field anlegen (Admin, einmalig)

Einstellungen → System → Custom Fields → neues Set, z. B.
`buchpark_condition`, zugewiesen an **Kategorien**.

Darin ein Feld:

| | |
|---|---|
| technischer Name | `buchpark_condition_candidates` |
| Typ | Textfeld (der Job schreibt JSON als String) |
| Label | Zustandsvergleich-Kandidaten (Cron) |

Das Set muss den drei Hauptkategorien zugewiesen sein: Bücher,
Musik-CDs & Vinyl, DVD & Blu-ray.

## 2. Integration für die Admin API

Einstellungen → System → Integrationen → neue Integration mit
Schreibrechten auf Kategorien. Client-ID und Secret notieren.

## 3. Environment auf Render

```
SW_SHOP_URL            https://buchpark.de
SW_STORE_API_KEY       <sw-access-key des Verkaufskanals>
SW_ADMIN_CLIENT_ID     <Integration>
SW_ADMIN_CLIENT_SECRET <Integration>
BPH_DRY_RUN            1   (zum Testen, später entfernen)
```

`requirements.txt` braucht nur `requests`.

## 4. Erster Lauf

Mit `BPH_DRY_RUN=1` starten. Die Ausgabe listet je Kategorie die
gefundenen Paare mit Titel, Preisen und Ersparnis — daran lässt sich
ablesen, ob die Ausschlussfilter passen. Erst wenn die Titel gefallen,
`BPH_DRY_RUN` entfernen.

Laufzeit-Erwartung: die Suche ist der langsame Teil (Bücher ~3,5–6 s pro
Listing-Seite, Musik ~1,7 s, Film ~0,6 s), bei `SCAN_PAGES = 3` also
grob eine bis zwei Minuten für alles.

## 5. Schedule

Wöchentlich reicht — Exemplare rotieren, aber das Widget validiert bei
jedem Seitenaufruf neu und weicht selbstständig auf den nächsten
Kandidaten aus.

## 6. Widget umstellen

Im JS-Feld des Elements `bph-cond`:

```js
var USE_CATEGORY_CONFIG = true;
```

Danach liest das Widget die Listen aus dem Custom Field und benutzt die
im Code hinterlegte Liste nur noch als Notnagel, wenn das Feld leer ist
oder der Request scheitert.

## Stellschrauben im Skript

| Konstante | Bedeutung |
|---|---|
| `MIN_SAVE_PCT` | Mindestersparnis für ein Paar (Default 30) |
| `PAIRS_PER_CATEGORY` | wie viele Paar-Kandidaten gespeichert werden |
| `FALLBACK_PER_SIDE` | Einzel-Exemplare je Seite (neu / gebraucht) |
| `SCAN_PAGES` | Listing-Seiten à 100, die durchsucht werden |
| `EXCLUDE_CATEGORY_IDS` | ausgeschlossene Unterkategorien |
| `EXCLUDE_TITLE_WORDS` | ausgeschlossene Titel-Stichwörter |

Die Ausschlüsse stehen bewusst doppelt — hier und im Widget. Das Widget
prüft jeden Kandidaten vor dem Rendern noch einmal, damit auf der
Startseite auch dann nichts Unpassendes landet, wenn der Cron etwas
durchlässt.

## Sicherung gegen leere Läufe

Findet der Job für eine Kategorie weder ein Paar noch ein vollständiges
Fallback-Set, wird diese Kategorie **nicht** überschrieben. Der alte
Stand bleibt stehen, statt dass die Kachel verschwindet.
