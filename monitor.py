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
  ROUNDS       aantal controles per run (standaard 2)
  INTERVAL     seconden tussen controles binnen één run (standaard 120)
  HEARTBEAT    "true" -> stuur een korte 'monitor draait nog'-melding
  TEST_NOTIFY  "true" -> stuur een testmelding (om de app te controleren)
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
    result = parse(html, final_url, product["product_id"]) if html else (ONBEKEND, f"HTTP {status}")
    if result[0] == ONBEKEND:
        log(f"  gewone HTTP gaf {result[1]} -> probeer headless Chrome")
        status, final_url, html = fetch_chrome(url)
        result = parse(html, final_url, product["product_id"]) if html else (ONBEKEND, "Chrome gaf geen pagina")
    return result


def notify(title: str, message: str, priority: int = 3, tags: list[str] | None = None,
           click: str | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("  (geen NTFY_TOPIC ingesteld; melding niet verstuurd)")
        return
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
                return
        except Exception as e:  # netwerkfout: kort wachten en opnieuw
            log(f"  melding mislukt ({e}), poging {poging + 1}/3")
            time.sleep(3)


def env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def main() -> int:
    rounds = max(1, int(os.environ.get("ROUNDS", "2")))
    interval = max(10, int(os.environ.get("INTERVAL", "120")))

    if env_true("TEST_NOTIFY"):
        notify("Testmelding PS5-monitor", "Werkt! Zo ziet een melding eruit. Tik om de PS5-pagina te openen.",
               priority=4, tags=["white_check_mark"], click=PRODUCTS[0]["url"])

    laatste: dict[str, tuple[str, str]] = {}
    for ronde in range(1, rounds + 1):
        log(f"Controle {ronde}/{rounds}")
        for p in PRODUCTS:
            st, detail = check(p)
            laatste[p["naam"]] = (st, detail)
            log(f"  {p['naam']}: {st} ({detail})")
            if st == BESCHIKBAAR:
                notify(f"🚨 OP VOORRAAD: {p['naam']}",
                       f"{p['prijs']} op playstation.com. Tik NU om te bestellen (aanmelden nodig, max. 1 per bestelling).",
                       priority=5, tags=["rotating_light"], click=p["url"])
            elif st == WACHTRIJ:
                notify(f"⏳ Wachtrij open: {p['naam']}",
                       "PlayStation Direct heeft een wachtrij geopend; de verkoop start waarschijnlijk. Tik om in de rij te gaan.",
                       priority=5, tags=["hourglass"], click=p["url"])
        if ronde < rounds:
            time.sleep(interval)

    nu = datetime.now(timezone.utc)
    blind = [n for n, (st, _) in laatste.items() if st == ONBEKEND]
    # Waarschuwing als de monitor 'blind' is, hooguit één keer per ~3 uur (zonder opgeslagen status).
    if blind and nu.hour % 3 == 0 and nu.minute < 10:
        notify("⚠️ PS5-monitor ziet de pagina niet",
               "Kon de status niet lezen voor: " + ", ".join(blind) +
               ". Mogelijk blokkeert playstation.com de checker of is de pagina veranderd. Vraag Claude om te kijken.",
               priority=3, tags=["warning"])

    if env_true("HEARTBEAT"):
        samenvatting = "\n".join(f"• {n}: {st.lower()}" for n, (st, _) in laatste.items())
        notify("✅ PS5-monitor draait nog", samenvatting, priority=2, tags=["eyes"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
