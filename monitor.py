#!/usr/bin/env python3
"""PS5 voorraad-monitor voor de PlayStation Direct-shop (België, nl-be).

Controleert de productpagina's op playstation.com en stuurt een pushmelding
via ntfy zodra een product bestelbaar wordt. Alleen de Python-standaardbibliotheek
(geen pip-installaties nodig).

Hoe de status wordt bepaald:
  De productpagina bevat (server-side gerenderd) de knopstatus van de winkel als
  "activeCtaId". Uitverkocht = "UNAVAILABLE:...". Elke andere waarde betekent dat
  de knop is veranderd (bv. toevoegen aan wagentje) -> melding.
  Let op: "stockLevelStatus" en schema.org "availability" op dezelfde pagina zeggen
  ook bij uitverkocht "IN_STOCK" en zijn daarom NIET bruikbaar.

Omgevingsvariabelen:
  NTFY_TOPIC   onderwerp in de ntfy-app (verplicht om meldingen te sturen)
  LOOP_SECONDS doorlopend controleren gedurende zoveel seconden (GitHub-modus; 0 = uit)
  ROUNDS       aantal controles als LOOP_SECONDS 0 is (lokaal testen, standaard 1)
  INTERVAL     seconden tussen controles (standaard 30)
  EINDDATUM    JJJJ-MM-DD; daarna stopt de monitor vanzelf
  TEST_NOTIFY  "true" -> stuur een testmelding (om de app te controleren)
  HC_PING_URL  ping-URL van healthchecks.io (dodemansknop; optioneel)
  ONTDEK_ELKE  om de hoeveel rondes naar nieuwe producten zoeken (standaard 10 = ±5 min)
  BEKEND_PAD   pad van bekende_producten.json (standaard naast dit script)
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PRODUCTS = [
    {
        "naam": "PS5 Digitale Editie 825 GB",
        "prijs": "€599,99",
        "url": "https://www.playstation.com/nl-be/ps5/digital-edition-825-gb/",
        "product_id": "PSD001-PSDE01916_79-PS5DIGITALKKBAEE",
    },
    {
        "naam": "PS5 Digitale Editie 825 GB + 2 DualSense",
        "prijs": "€649,99",
        "url": "https://www.playstation.com/nl-be/ps5/digital-edition-825-gb/product/digital-edition-825-gb-two-dualsense-bundle/",
        "product_id": "PSD001-PSDE01916_79-PS5DIGITAL2TALIE",
    },
    {
        "naam": "PS5 Console 1 TB (met disc)",
        "prijs": "€649,99",
        "url": "https://www.playstation.com/nl-be/ps5/1-tb-disc/",
        "product_id": "PSD001-PSDE01929_59-PS5CONSOLEHB2OYE",
    },
    {
        "naam": "PS5 Console 1 TB (met disc) + 2 DualSense",
        "prijs": "€699,99",
        "url": "https://www.playstation.com/nl-be/ps5/1-tb-disc/product/two-dualsense-wireless-controllers-bundle/",
        "product_id": "PSD001-PSDE01929_59-PS5CONSOLETAPQ3E",
    },
]

# Tweede lijn: extra bundels, per ronde één (om beurten).
EXTRA: list[dict] = [
    # Bewust leeg: de bestaande EA SPORTS FC 26-, Fortnite- en Wolverine-bundels hoeven niet
    # (oud, komen volgens Renzo niet meer terug). Ze staan wel in bekende_producten.json, dus
    # geen 'nieuw'-melding. Nieuw ontdekte bundels komen hier tijdens de run automatisch bij.
]

# Nieuwe producten ontdekken (bv. een GTA-bundel): de PS5-shoplijst (nieuwste eerst) en de
# pagina's met varianten van de Digitale Editie en de 1 TB-console. Bekende producten staan in
# bekende_producten.json; wat daar niet in staat, is nieuw -> melding (één keer).
SHOP_ALL = "https://www.playstation.com/nl-be/consoles/ps5/shop-all/"
VARIANT_PAGINAS = [
    "https://www.playstation.com/nl-be/ps5/digital-edition-825-gb/",
    "https://www.playstation.com/nl-be/ps5/1-tb-disc/",
]
BEKEND_PAD = os.environ.get(
    "BEKEND_PAD", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bekende_producten.json"))
GRID_RE = re.compile(r'\{\\?"__typename\\?":\\?"Product\\?",\\?"id\\?":\\?"(PSD001-[^"\\]+)\\?",'
                     r'\\?"name\\?":\\?"([^"\\]+)')
PRIJS_RE = re.compile(r'\\?"(?:basePrice|displayPrice)\\?":\\?"(€[^"\\]+)')
VARIANT_RE = re.compile(r'/nl-be/ps5/[a-z0-9\-]+/product/[a-z0-9\-]+/')

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Werkt zowel op gewone JSON als op de ge-escapete variant (\"activeCtaId\":\"...\") in de pagina.
CTA_RE = re.compile(r'\\?"activeCtaId\\?"\s*:\s*\\?"([^"\\]+)')

BESCHIKBAAR = "BESCHIKBAAR"
UITVERKOCHT = "UITVERKOCHT"
WACHTRIJ = "WACHTRIJ"
ONBEKEND = "ONBEKEND"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}Z] {msg}", flush=True)


def fetch_urllib(url: str) -> tuple[int, str, str]:
    """Haalt de pagina op met gewone HTTP. Geeft (status, eind-url, html)."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return resp.status, resp.geturl(), body.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:  # time-out, verbinding verbroken, ... mag de lus nooit laten crashen
        log(f"  netwerkfout bij ophalen: {type(e).__name__}: {e}")
        return 0, url, ""


def fetch_chrome(url: str) -> tuple[int, str, str]:
    """Reserve: echte (headless) Chrome, voor als gewone HTTP geblokkeerd wordt."""
    chrome = next(
        (p for p in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(p)),
        None,
    )
    if not chrome:
        return 0, url, ""
    cmd = [
        chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        f"--user-agent={UA}", "--lang=nl-BE", "--virtual-time-budget=15000", "--dump-dom", url,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        return (200 if out.stdout else 0), url, out.stdout
    except subprocess.TimeoutExpired:
        return 0, url, ""


def parse(html: str, final_url: str, product_id: str) -> tuple[str, str]:
    """Geeft (status, detail)."""
    if "queue-it" in final_url or "queue.playstation" in final_url:
        return WACHTRIJ, final_url
    ctas = CTA_RE.findall(html)
    # Neem de knop van dit product (de pagina kan ook andere producten tonen).
    eigen = [c for c in ctas if product_id in c] or ctas
    if not eigen:
        return ONBEKEND, "geen knopstatus gevonden (pagina veranderd of geblokkeerd)"
    if all(c.startswith("UNAVAILABLE") for c in eigen):
        return UITVERKOCHT, eigen[0].split(":")[0]
    beschikbaar = next(c for c in eigen if not c.startswith("UNAVAILABLE"))
    return BESCHIKBAAR, beschikbaar.split(":")[0]


def check(product: dict) -> tuple[str, str]:
    url = product["url"]
    status, final_url, html = fetch_urllib(url)
    result = parse(html, final_url, product["product_id"]) if html else (ONBEKEND, f"HTTP {status or 'geen antwoord'}")
    if result[0] == ONBEKEND:
        eerste = result[1]
        log(f"  gewone HTTP gaf {eerste} -> probeer headless Chrome")
        status, final_url, html = fetch_chrome(url)
        result = parse(html, final_url, product["product_id"]) if html else (ONBEKEND, "Chrome gaf geen pagina")
        if result[0] == ONBEKEND:
            result = (ONBEKEND, f"{eerste}; Chrome: {result[1]}")
    return result


def notify(title: str, message: str, priority: int = 3, tags: list[str] | None = None,
           click: str | None = None) -> bool:
    """Stuurt een pushmelding via ntfy. Geeft True als ntfy de melding aannam."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("  (geen NTFY_TOPIC ingesteld; melding niet verstuurd)")
        return False
    payload = {"topic": topic, "title": title, "message": message, "priority": priority,
               "tags": tags or []}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Nu bestellen", "url": click, "clear": True}]
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    for poging in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                log(f"  melding verstuurd (HTTP {resp.status}): {title}")
                return True
        except urllib.error.HTTPError as e:
            log(f"  melding mislukt (HTTP {e.code}), poging {poging + 1}/3")
            if e.code == 429:  # dagquotum van dit IP-adres op ntfy.sh; opnieuw proberen helpt niet
                print("::warning title=ntfy quotum::ntfy.sh weigerde de melding (429)", flush=True)
                break
            time.sleep(3)
        except Exception as e:  # netwerkfout: kort wachten en opnieuw
            log(f"  melding mislukt ({e}), poging {poging + 1}/3")
            time.sleep(3)
    ping("/fail")  # healthchecks.io laat je dan via een ander pad weten dat meldingen niet doorkomen
    return False


def env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def ping(suffix: str = "") -> None:
    """Dodemansknop: meldt aan healthchecks.io dat de checker leeft ('' ) of dat iets misging ('/fail').
    Blijven de pings 10 min weg, dan stuurt healthchecks.io zelf een melding (ntfy + mail)."""
    url = os.environ.get("HC_PING_URL", "").strip()
    if not url:
        return
    try:
        with urllib.request.urlopen(url.rstrip("/") + suffix, timeout=10) as resp:
            if suffix or not getattr(ping, "_ok", False):
                log(f"  healthchecks-ping{suffix or ''} ok (HTTP {resp.status})")
            ping._ok = True
    except Exception as e:  # een gemiste ping is niet erg; de volgende komt 30 s later
        log(f"  healthchecks-ping{suffix} mislukt ({e})")


def laad_bekend() -> dict:
    try:
        with open(BEKEND_PAD, encoding="utf-8") as f:
            b = json.load(f)
        b.setdefault("producten", {})
        b.setdefault("varianten", [])
        return b
    except (OSError, ValueError):
        return {"producten": {}, "varianten": []}


def bewaar_bekend(b: dict) -> None:
    b["varianten"] = sorted(set(b["varianten"]))
    with open(BEKEND_PAD, "w", encoding="utf-8") as f:
        json.dump(b, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def lees_shoplijst(html: str) -> list[tuple[str, str, str]]:
    """(product_id, naam, prijs) uit de PS5-shoplijst, in volgorde (nieuwste eerst)."""
    uit, gezien = [], set()
    for mm in GRID_RE.finditer(html):
        pid, naam = mm.group(1), mm.group(2).replace("\\t", "").strip()
        if pid in gezien:
            continue
        gezien.add(pid)
        pm = PRIJS_RE.search(html, mm.end(), mm.end() + 600)
        uit.append((pid, naam, pm.group(1) if pm else "?"))
    return uit


def variant_info(url: str) -> dict:
    """Haalt een (nieuwe) productpagina op: naam, prijs, product-id en voorraadstatus."""
    st_http, final_url, html = fetch_urllib(url)
    slug = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
    info = {"naam": f"PS5 {slug}", "prijs": "?", "url": url, "product_id": ""}
    if not html:
        info["status"] = ONBEKEND
        return info
    ctas = CTA_RE.findall(html)
    if ctas:
        info["product_id"] = ctas[0].split(":")[-1].rsplit("-", 1)[0]
        pid = re.escape(info["product_id"])
        nm = re.search(pid + r'\\?",\\?"__typename\\?":\\?"Product\\?"[^{}]*?\\?"name\\?":\\?"([^"\\]+)', html)
        if nm:
            info["naam"] = nm.group(1).replace("\\t", "").strip()
        pr = re.search(pid + r'-[A-Z0-9]+\\?",\\?"displayPrice\\?":\\?"([^"\\]+)', html)
        if pr:
            info["prijs"] = pr.group(1)
    info["status"] = parse(html, final_url, info["product_id"])[0]
    return info


def ontdek(bekend: dict, bewaakt: set[str], eerste_keer: bool) -> tuple[list[dict], list[dict]]:
    """Zoekt nieuwe PS5-producten. Geeft (nieuwe_varianten met info, nieuwe_shopproducten)."""
    nieuwe_var: list[dict] = []
    for pagina in VARIANT_PAGINAS:
        _, _, html = fetch_urllib(pagina)
        for pad in sorted(set(VARIANT_RE.findall(html))):
            url = "https://www.playstation.com" + pad
            if url in bekend["varianten"]:
                continue
            bekend["varianten"].append(url)
            if not eerste_keer and url not in bewaakt:
                nieuwe_var.append(variant_info(url))
    _, _, html = fetch_urllib(SHOP_ALL)
    var_ids = {v["product_id"] for v in nieuwe_var}
    nieuwe_shop: list[dict] = []
    for pid, naam, prijs in lees_shoplijst(html):
        if pid in bekend["producten"]:
            continue
        bekend["producten"][pid] = naam
        if not eerste_keer and pid not in var_ids:
            nieuwe_shop.append({"product_id": pid, "naam": naam, "prijs": prijs})
    return nieuwe_var, nieuwe_shop


GOLF_UITVERKOCHT_S = 180     # 3 min niet meer te koop -> 'weer uitverkocht'
GOLF_EINDE_S = 300           # 5 min niet meer te koop -> golf voorbij; daarna telt opduiken als nieuw
BLIND_NA_S = 300             # pas waarschuwen als de pagina 5 min onleesbaar blijft (korte storingen negeren)
BLOK_NA_S = 90               # bij HTTP 403 (blokkade, blijft meestal duren) al na 1,5 min waarschuwen
BLIND_HERHAAL_S = 30 * 60    # zolang de checker blind is: waarschuwing om de 30 min


def main() -> int:
    """Controleert in een lus. LOOP_SECONDS>0: doorlopend tot die tijd om is (GitHub-modus).
    Anders: ROUNDS controles (handig om lokaal te testen)."""
    loop_s = int(os.environ.get("LOOP_SECONDS", "0"))
    rounds = max(1, int(os.environ.get("ROUNDS", "1")))
    interval = max(10, int(os.environ.get("INTERVAL", "30")))

    einddatum = os.environ.get("EINDDATUM", "").strip()
    if einddatum and datetime.now(timezone.utc).date().isoformat() > einddatum:
        log(f"Einddatum {einddatum} voorbij: monitor gestopt.")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("stop=true\n")
        return 0

    if env_true("TEST_NOTIFY"):
        notify("Testmelding PS5-monitor", "Werkt! Zo ziet een melding eruit. Tik om de PS5-pagina te openen.",
               priority=4, tags=["white_check_mark"], click=PRODUCTS[0]["url"])

    st_vorig: dict[str, str] = {}
    laatste_alarm: dict[str, float] = {}
    golf: dict[str, dict] = {}                # lopende voorraadgolf per model
    laatst_beschikbaar: dict[str, float] = {}
    onbekend_sinds: dict[str, float] = {}
    laatste_detail: dict[str, str] = {}
    laatste_blind = -1e9  # niet 0: monotonic() begint bij de opstarttijd van de VM
    blind_gemeld = False
    heartbeat_dag = ""

    extras = [dict(p) for p in EXTRA]
    bekend = laad_bekend()
    eerste_keer = not bekend["producten"]  # geen lijst -> eerst stil vullen, niets melden
    ontdek_elke = max(1, int(os.environ.get("ONTDEK_ELKE", "10")))  # om de 10 rondes = ±5 min

    begin = time.monotonic()
    ronde = 0
    while True:
        ronde += 1
        t0 = time.monotonic()
        log(f"Controle {ronde}")
        huidig: dict[str, str] = {}
        te_checken = list(PRODUCTS)
        if extras:
            te_checken.append(extras[(ronde - 1) % len(extras)])
        for p in te_checken:
            naam = p["naam"]
            try:
                st, detail = check(p)
            except Exception as e:  # vangnet: één fout mag de doorlopende controle nooit stoppen
                st, detail = ONBEKEND, f"fout: {type(e).__name__}: {e}"
            laatste_detail[naam] = detail
            huidig[naam] = st
            vorig = st_vorig.get(naam)
            if st != vorig or ronde == 1:
                log(f"  {naam}: {st} ({detail})")
            nu = time.monotonic()

            if st in (BESCHIKBAAR, WACHTRIJ):
                onbekend_sinds.pop(naam, None)
                laatst_beschikbaar[naam] = nu
                # Eén melding per golf: zolang een model aan/uit flikkert, geen nieuwe melding.
                # Pas als 'weer uitverkocht' al gemeld is of de golf voorbij is, telt het als nieuw.
                if naam not in golf or golf[naam]["uitverkocht_gemeld"]:
                    golf[naam] = {"uitverkocht_gemeld": False}
                    print(f"::error title=PS5 {st}::{naam} -> {p['url']}", flush=True)
                    if st == BESCHIKBAAR:
                        ok = notify(f"🚨 OP VOORRAAD: {naam}",
                                    f"{p['prijs']} op playstation.com. Tik NU om te bestellen "
                                    "(aanmelden nodig, max. 1 per bestelling). Lijkt hij weg? Blijf even "
                                    "vernieuwen: voorraad kan een paar minuten aan en uit springen.",
                                    priority=5, tags=["rotating_light"], click=p["url"])
                    else:
                        ok = notify(f"⏳ Wachtrij open: {naam}",
                                    "PlayStation Direct heeft een wachtrij geopend; de verkoop start "
                                    "waarschijnlijk. Tik om in de rij te gaan.",
                                    priority=5, tags=["hourglass"], click=p["url"])
                    laatste_alarm[naam] = nu
                    if not ok:
                        # Reservekanaal: run laten mislukken -> GitHub mailt meteen; de herstartstap
                        # in de workflow start daarna direct een nieuwe controle.
                        print("::error title=Melding mislukt::ntfy nam de voorraadmelding niet aan", flush=True)
                        return 1
            elif st == UITVERKOCHT:
                onbekend_sinds.pop(naam, None)
            else:  # ONBEKEND
                onbekend_sinds.setdefault(naam, nu)

        st_vorig.update(huidig)

        # Golven afronden: 3 min niet meer gezien -> 'weer uitverkocht'; 5 min -> golf voorbij.
        nu = time.monotonic()
        for naam in list(golf):
            weg = nu - laatst_beschikbaar.get(naam, nu)
            if not golf[naam]["uitverkocht_gemeld"] and weg >= GOLF_UITVERKOCHT_S:
                notify(f"Weer uitverkocht: {naam}", "Al 3 minuten niet meer te koop. Ik blijf verder kijken.",
                       priority=3, tags=["x"])
                golf[naam]["uitverkocht_gemeld"] = True
            if weg >= GOLF_EINDE_S:
                del golf[naam]

        # Nieuwe PS5-producten of -bundels zoeken (om de ±5 min).
        if (ronde - 1) % ontdek_elke == 0:
            try:
                bewaakt = {x["url"] for x in PRODUCTS + extras}
                nieuwe_var, nieuwe_shop = ontdek(bekend, bewaakt, eerste_keer)
                for v in nieuwe_var:
                    log(f"  NIEUWE BUNDEL: {v['naam']} ({v['prijs']}) {v['status']} {v['url']}")
                    extras.append({k: v[k] for k in ("naam", "prijs", "url", "product_id")})
                    if v["status"] == BESCHIKBAAR:
                        notify(f"🚨 NIEUW & OP VOORRAAD: {v['naam']}",
                               f"{v['prijs']} op playstation.com. Tik NU om te bestellen.",
                               priority=5, tags=["rotating_light"], click=v["url"])
                        laatste_alarm[v["naam"]] = time.monotonic()
                        laatst_beschikbaar[v["naam"]] = time.monotonic()
                        golf[v["naam"]] = {"uitverkocht_gemeld": False}
                        st_vorig[v["naam"]] = BESCHIKBAAR
                    else:
                        notify(f"🆕 Nieuwe PS5-bundel: {v['naam']}",
                               f"{v['prijs']} · nu: {v['status'].lower()}. Ik bewaak hem vanaf nu ook "
                               "en meld het zodra hij bestelbaar is.",
                               priority=4, tags=["new"], click=v["url"])
                for n in nieuwe_shop:
                    log(f"  NIEUW PRODUCT IN SHOP: {n['naam']} ({n['prijs']}) {n['product_id']}")
                    notify(f"🆕 Nieuw bij PlayStation: {n['naam']}",
                           f"{n['prijs']} · verscheen net in de PS5-shop. Tik om te kijken.",
                           priority=4, tags=["new"], click=SHOP_ALL)
                if eerste_keer or nieuwe_var or nieuwe_shop:
                    bewaar_bekend(bekend)
                    eerste_keer = False
            except Exception as e:  # ontdekken is extra; mag de hoofdcontrole nooit storen
                log(f"  ontdekken mislukt: {type(e).__name__}: {e}")

        # Alleen 'ik leef' melden als alles leesbaar was: blijft de pagina onleesbaar of stopt de
        # checker, dan vallen de pings weg en waarschuwt healthchecks.io je na de grace-tijd.
        if ONBEKEND not in huidig.values():
            ping()

        # Blind: pagina al even onleesbaar -> waarschuwen (beperkt), en melden als het hersteld is.
        nu = time.monotonic()
        blind = [n for n, t in onbekend_sinds.items()
                 if nu - t >= (BLOK_NA_S if "HTTP 403" in laatste_detail.get(n, "") else BLIND_NA_S)]
        if blind and nu - laatste_blind >= BLIND_HERHAAL_S:
            serverfout = any(re.search(r"HTTP 5\d\d", laatste_detail.get(n, "")) for n in blind)
            geblokkeerd = any("HTTP 403" in laatste_detail.get(n, "") for n in blind)
            if serverfout:
                uitleg = ("playstation.com geeft serverfouten (overbelast of onderhoud). Dat kan ook "
                          "drukte door een nieuwe voorraad zijn: kijk zelf even op de site.")
            elif geblokkeerd:
                uitleg = ("playstation.com weigert de computer van de checker (HTTP 403). Jij kunt gewoon "
                          "bestellen; kijk zelf even op de site.")
            else:
                uitleg = "Mogelijk is de pagina veranderd. Kijk zelf even op de site en vraag Claude om te kijken."
            notify("⚠️ PS5-monitor ziet de pagina niet",
                   "Kon de status niet lezen voor: " + ", ".join(blind) + ". " + uitleg +
                   " Deze melding komt om de 30 min terug zolang het duurt.",
                   priority=4, tags=["warning"], click=PRODUCTS[0]["url"])
            laatste_blind = nu
            blind_gemeld = True
        elif not onbekend_sinds and blind_gemeld:
            notify("PS5-monitor ziet de pagina weer", "Alles werkt opnieuw.", priority=2, tags=["white_check_mark"])
            blind_gemeld = False

        # Dagelijks rond 10u Belgische tijd (9u UTC in de winter): 'monitor draait nog'.
        utc = datetime.now(timezone.utc)
        if loop_s and utc.hour == 9 and utc.minute < 2 and heartbeat_dag != utc.date().isoformat():
            hoofd = [p["naam"] for p in PRODUCTS]
            notify("✅ PS5-monitor draait nog",
                   "\n".join(f"• {n}: {st_vorig.get(n, '?').lower()}" for n in hoofd) +
                   (f"\n+ {len(extras)} nieuw ontdekte bundel(s)" if extras else "") +
                   "\n+ nieuwe PS5-producten in het oog",
                   priority=2, tags=["eyes"])
            heartbeat_dag = utc.date().isoformat()

        klaar = (time.monotonic() - begin + interval > loop_s) if loop_s else (ronde >= rounds)
        if klaar:
            log(f"Klaar na {ronde} controles.")
            return 0
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
