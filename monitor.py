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


HERHAAL_ALARM_S = 180        # zolang iets bestelbaar is: melding herhalen om de 3 min
BLIND_NA_S = 300             # pas waarschuwen als de pagina 5 min onleesbaar blijft
BLIND_HERHAAL_S = 3 * 3600   # blind-waarschuwing hooguit om de 3 uur


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
    onbekend_sinds: dict[str, float] = {}
    laatste_detail: dict[str, str] = {}
    laatste_blind = -1e9  # niet 0: monotonic() begint bij de opstarttijd van de VM
    blind_gemeld = False
    heartbeat_dag = ""

    begin = time.monotonic()
    ronde = 0
    while True:
        ronde += 1
        t0 = time.monotonic()
        log(f"Controle {ronde}")
        huidig: dict[str, str] = {}
        for p in PRODUCTS:
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
                if vorig != st or nu - laatste_alarm.get(naam, -1e9) >= HERHAAL_ALARM_S:
                    print(f"::error title=PS5 {st}::{naam} -> {p['url']}", flush=True)
                    if st == BESCHIKBAAR:
                        ok = notify(f"🚨 OP VOORRAAD: {naam}",
                                    f"{p['prijs']} op playstation.com. Tik NU om te bestellen "
                                    "(aanmelden nodig, max. 1 per bestelling).",
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
                if vorig in (BESCHIKBAAR, WACHTRIJ):
                    notify(f"Weer uitverkocht: {naam}", "Ik blijf verder kijken.", priority=3, tags=["x"])
            else:  # ONBEKEND
                onbekend_sinds.setdefault(naam, nu)

        st_vorig.update(huidig)

        # Alleen 'ik leef' melden als alles leesbaar was: blijft de pagina onleesbaar of stopt de
        # checker, dan vallen de pings weg en waarschuwt healthchecks.io je na de grace-tijd.
        if ONBEKEND not in huidig.values():
            ping()

        # Blind: pagina al even onleesbaar -> waarschuwen (beperkt), en melden als het hersteld is.
        nu = time.monotonic()
        blind = [n for n, t in onbekend_sinds.items() if nu - t >= BLIND_NA_S]
        if blind and nu - laatste_blind >= BLIND_HERHAAL_S:
            serverfout = any(re.search(r"HTTP 5\d\d", laatste_detail.get(n, "")) for n in blind)
            uitleg = ("playstation.com geeft serverfouten (overbelast of onderhoud). Dat kan ook "
                      "drukte door een nieuwe voorraad zijn: kijk zelf even op de site."
                      if serverfout else
                      "Mogelijk blokkeert playstation.com de checker of is de pagina veranderd. "
                      "Vraag Claude om te kijken.")
            notify("⚠️ PS5-monitor ziet de pagina niet",
                   "Kon de status niet lezen voor: " + ", ".join(blind) + ". " + uitleg,
                   priority=4 if serverfout else 3, tags=["warning"],
                   click=PRODUCTS[0]["url"] if serverfout else None)
            laatste_blind = nu
            blind_gemeld = True
        elif not onbekend_sinds and blind_gemeld:
            notify("PS5-monitor ziet de pagina weer", "Alles werkt opnieuw.", priority=2, tags=["white_check_mark"])
            blind_gemeld = False

        # Dagelijks rond 10u Belgische tijd (9u UTC in de winter): 'monitor draait nog'.
        utc = datetime.now(timezone.utc)
        if loop_s and utc.hour == 9 and utc.minute < 2 and heartbeat_dag != utc.date().isoformat():
            notify("✅ PS5-monitor draait nog",
                   "\n".join(f"• {n}: {s.lower()}" for n, s in huidig.items()), priority=2, tags=["eyes"])
            heartbeat_dag = utc.date().isoformat()

        klaar = (time.monotonic() - begin + interval > loop_s) if loop_s else (ronde >= rounds)
        if klaar:
            log(f"Klaar na {ronde} controles.")
            return 0
        time.sleep(max(0.0, interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
