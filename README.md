# PS5 voorraad-alert (playstation.com België)

Controleert om de paar minuten, 24/7, of deze producten op de PlayStation Direct-shop
(playstation.com/nl-be) weer bestelbaar zijn, en stuurt dan een pushmelding via de app **ntfy**:

- PS5 Digitale Editie 825 GB (€599,99)
- PS5 Digitale Editie 825 GB + 2 DualSense-controllers (€649,99)

## Hoe het werkt
- `.github/workflows/check.yml` start elke 5 minuten op GitHub Actions (gratis voor openbare repo's)
  en controleert twee keer per run (om de ±2 minuten).
- `monitor.py` leest op de productpagina de status van de koopknop (`activeCtaId`).
  `UNAVAILABLE` = uitverkocht; elke andere status = melding met link om meteen te bestellen.
- Opent PlayStation een wachtrij, dan krijg je daar ook een melding van.
- Elke dag rond 10u een stille melding "monitor draait nog".
- Kan de checker de pagina niet lezen (blokkade of gewijzigde site), dan krijg je hooguit om de
  3 uur een waarschuwing.

## Instellingen
- Repo-secret `NTFY_TOPIC` = het onderwerp waarop je in de ntfy-app geabonneerd bent.
- Testmelding: Actions → *PS5 voorraad-check* → *Run workflow*.
- Stoppen (bv. na je bestelling): Actions → *PS5 voorraad-check* → ⋯ → *Disable workflow*.
- Producten toevoegen: lijst `PRODUCTS` bovenaan in `monitor.py`.

Alleen de Python-standaardbibliotheek; er wordt niets geïnstalleerd.
