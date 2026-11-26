# PS5 voorraad-alert (playstation.com België)

Controleert om de paar minuten, 24/7, of deze producten op de PlayStation Direct-shop
(playstation.com/nl-be) weer bestelbaar zijn, en stuurt dan een pushmelding via de app **ntfy**:

- PS5 Digitale Editie 825 GB (€599,99)
- PS5 Digitale Editie 825 GB + 2 DualSense-controllers (€649,99)
- PS5 Console 1 TB met disc (€649,99)
- PS5 Console 1 TB met disc + 2 DualSense-controllers (€699,99)

De bestaande EA SPORTS FC 26-, Fortnite- en Wolverine-bundels worden niet bewaakt (oud).
Nieuw ontdekte bundels worden wel automatisch mee bewaakt (om beurten, één per ronde).

**Nieuwe producten:** om de ±5 min kijkt de monitor of er een nieuwe PS5-bundel of een nieuw
PS5-product in de shop verschijnt (bv. een GTA-bundel). Nieuw = niet in `bekende_producten.json`.
Je krijgt dan één melding; een nieuwe bundel wordt vanaf dan ook op voorraad bewaakt, en de lijst
wordt automatisch bijgewerkt in de repo.

## Hoe het werkt
- `.github/workflows/check.yml` draait doorlopend op GitHub Actions (gratis voor openbare repo's):
  één run controleert **elke 30 seconden**, ±5u40 lang, en start daarna zelf de volgende run.
  Een cron om de 15 minuten is het vangnet als die keten ooit breekt.
- `monitor.py` leest op de productpagina de status van de koopknop (`activeCtaId`).
  `UNAVAILABLE` = uitverkocht; elke andere status = melding met link om meteen te bestellen.
  Zolang het bestelbaar blijft: melding om de 3 minuten; daarna "weer uitverkocht".
- Opent PlayStation een wachtrij, dan krijg je daar ook een melding van.
- Neemt ntfy een voorraadmelding niet aan, dan 'mislukt' de run bewust: GitHub mailt je
  dan meteen, en de volgende run start direct.
- Elke dag rond 10u een stille melding "monitor draait nog".
- Is de pagina 5 minuten onleesbaar (blokkade of gewijzigde site), dan een waarschuwing
  (hooguit om de 3 uur) en een melding zodra het weer werkt.
- **Dodemansknop (healthchecks.io):** de checker pingt elke 30 s zolang hij alles kan lezen, en
  pingt `/fail` als een melding niet doorkomt. Blijven de pings 10 min weg (checker gestopt of
  pagina onleesbaar) of komt er een `/fail`, dan meldt healthchecks.io dat zelf via ntfy en mail.
- Stopt vanzelf na `EINDDATUM` in de workflow (31-12-2026).

## Instellingen
- Repo-secret `NTFY_TOPIC` = het onderwerp waarop je in de ntfy-app geabonneerd bent.
- Repo-secret `HC_PING_URL` = ping-URL van de check op healthchecks.io (period 1 min, grace 10 min,
  met ntfy-integratie op hetzelfde onderwerp).
- Testmelding: Actions → *PS5 voorraad-check* → *Run workflow*.
- Stoppen (bv. na je bestelling): Actions → *PS5 voorraad-check* → ⋯ → *Disable workflow*
  (een lopende run annuleren alleen is niet genoeg: die herstart zichzelf).
- Producten toevoegen: lijst `PRODUCTS` bovenaan in `monitor.py`.

Alleen de Python-standaardbibliotheek; er wordt niets geïnstalleerd.
