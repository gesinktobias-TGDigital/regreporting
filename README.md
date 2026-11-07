# weekly-regulatory-digest

Cloud Function die wekelijks een Regulatory Intelligence Update genereert
(Gemini + Google Search grounding, Vertex AI) en per e-mail verstuurt via Gmail API.

## Snelste pad: deploy.sh

Alle onderstaande stappen (APIs enablen, Firestore + index, IAM-rechten, drie
functies deployen, Cloud Run invoker-rechten, twee scheduler-jobs) staan ook in
`deploy.sh`. Veilig om opnieuw te draaien.

```bash
cd weekly-digest
chmod +x deploy.sh
./deploy.sh
```

Vereist: `gcloud` al ingelogd en secrets `gmail-client-id`, `gmail-client-secret`,
`gmail-refresh-token` moeten al in Secret Manager staan (zie sectie "Gmail
refresh token" onderaan als je die nog moet aanmaken).

De rest van dit document beschrijft dezelfde stappen los, voor het geval je
handmatig wilt deployen of wilt begrijpen wat het script doet.

## Vereisten (eenmalig, al ingesteld)

Secrets in Secret Manager (project `project-5ce9338e-bc20-4b9f-88f`):
- `gmail-client-id`
- `gmail-client-secret`
- `gmail-refresh-token`

Service account `project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com` heeft nodig:
- `roles/secretmanager.secretAccessor` op de drie secrets hierboven
- `roles/aiplatform.user` op het project
- `roles/run.invoker` op deze Cloud Run service (voor de Cloud Scheduler trigger)

## Deployen

```bash
gcloud functions deploy weekly-regulatory-digest \
  --gen2 \
  --runtime=python312 \
  --region=us-west1 \
  --source=. \
  --entry-point=weekly_digest \
  --trigger-http \
  --no-allow-unauthenticated \
  --timeout=900 \
  --memory=512MB \
  --service-account=project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com \
  --build-service-account=projects/project-5ce9338e-bc20-4b9f-88f/serviceAccounts/project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com
```

## Testen

```bash
FUNCTION_URL="https://weekly-regulatory-digest-2sgcw2vfrq-uw.a.run.app"
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" $FUNCTION_URL
```

## Scheduler (wekelijks, maandag 8:00 Europe/Amsterdam)

```bash
gcloud scheduler jobs create http regulatory-digest-weekly \
  --schedule="0 8 * * 1" \
  --uri="$FUNCTION_URL" \
  --http-method=POST \
  --oidc-service-account-email="project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com" \
  --location=us-west1 \
  --time-zone="Europe/Amsterdam" \
  --attempt-deadline=1800s
```

## Let op: Gmail refresh token

De OAuth-app (`client_secret.json` gegenereerd voor `gmail-client-id`/`gmail-client-secret`)
moet in "In production" staan in het OAuth consent screen. Staat de app nog op "Testing",
dan verloopt de refresh token na 7 dagen en moet je 'm handmatig opnieuw genereren en
bijwerken via:

```bash
echo -n "NIEUWE_REFRESH_TOKEN" | gcloud secrets versions add gmail-refresh-token --data-file=-
```

## Prompt aanpassen

Pas de `PROMPT`-variabele in `main.py` aan en deploy opnieuw. Geen Agent Builder/UI nodig.

---

## Sub-agent: Last-Mile Horizon Scanner

Tweede entry point in hetzelfde bestand (`last_mile_digest`), gericht op proactieve
horizon scanning van naderende/finaliserende verplichtingen (binnen 90 dagen), met
een change-trail opgeslagen in Firestore zodat elke mail ook laat zien: nieuw
gevonden items én de volledige historie van eerder gesignaleerde items.

### Eenmalig: Firestore inschakelen

```bash
gcloud services enable firestore.googleapis.com
gcloud firestore databases create --location=us-west1 --type=firestore-native
```

### Rechten

```bash
gcloud projects add-iam-policy-binding project-5ce9338e-bc20-4b9f-88f \
  --member="serviceAccount:project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com" \
  --role="roles/datastore.user"
```

### Deployen (aparte Cloud Function, zelfde source)

```bash
gcloud functions deploy last-mile-horizon-scan \
  --gen2 \
  --runtime=python312 \
  --region=us-west1 \
  --source=. \
  --entry-point=last_mile_digest \
  --trigger-http \
  --no-allow-unauthenticated \
  --timeout=900 \
  --memory=512MB \
  --service-account=project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com \
  --build-service-account=projects/project-5ce9338e-bc20-4b9f-88f/serviceAccounts/project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com
```

### Invoker-rechten + Scheduler (maandag + donderdag 07:00)

```bash
gcloud run services add-iam-policy-binding last-mile-horizon-scan \
  --region=us-west1 \
  --member="serviceAccount:project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com" \
  --role="roles/run.invoker"

LAST_MILE_URL=$(gcloud functions describe last-mile-horizon-scan --region=us-west1 --gen2 --format="value(serviceConfig.uri)")

gcloud scheduler jobs create http last-mile-horizon-scan-biweekly \
  --schedule="0 7 * * 1,4" \
  --uri="$LAST_MILE_URL" \
  --http-method=POST \
  --oidc-service-account-email="project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com" \
  --location=us-west1 \
  --time-zone="Europe/Amsterdam" \
  --attempt-deadline=1800s
```

### Testen

```bash
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" $LAST_MILE_URL
```

### Firestore data-model

Collectie `last_mile_tracked_items`, één document per gevolgd item (ID = hash van
`body::topic`). Elk document bevat `first_detected`, `last_seen`, `times_seen`,
huidige `urgency`/`implementation_date`, een `vendor_impact`-blok (zie hieronder),
en een `history`-array met een snapshot per keer dat het item opnieuw is
gesignaleerd — dat is de basis voor de trail-tabel in de mail.

### Vendor Impact / Horizon Scanning

Elk item krijgt naast de bestaande relevantie-beoordeling ook een classificatie
tegen het scope van de regulatory reporting vendor (COREP, FINREP, AnaCredit,
ALMM/LCR/NSFR). Deze vendor-relevante items worden opgeslagen in een **aparte**
Firestore-collectie `vendor_impact_items`, met een levenscyclus-schema:

```
id                 auto (deterministische hash van source::title, i.p.v. random
                   Firestore-ID — nodig om dezelfde regeling bij een volgende
                   scan te herkennen in plaats van als duplicaat op te slaan)
title              string
source             EBA | DNB | SRB | ECB
source_url         string
change_type        validation_rule | template_change | new_taxonomy_dpm |
                   new_report_obligation | technical_standard_amendment |
                   qa_clarification | other
lead_time_class    short | medium | long
published_date     string (YYYY-MM-DD)
effective_date     string | null
reference_date     string | null   (eerste rapportage-referentieperiode)
impact_summary     string
action_needed      string
status             new | vendor_notified | vendor_confirmed | testing |
                   parallel_run | closed
vendor_sla_date    string | null   (handmatig ingevuld, wanneer vendor levering toezegt)
test_window_start  string | null   (handmatig ingevuld)
go_live_date       string | null   (handmatig ingevuld)
last_updated       ISO-timestamp
detected_in_digest YYYY-MM-DD      (welke scan dit item vond)
```

**Belangrijk gedrag:** `status`, `vendor_sla_date`, `test_window_start` en
`go_live_date` zijn mens-beheerde velden. Ze worden alleen bij eerste detectie
op hun default gezet (`status: "new"`, rest `null`) en daarna **nooit** door een
volgende scan overschreven — alleen de feitelijke velden (titel, datums, impact,
actie) worden bij elke run ververst. Status handmatig bijwerken kan direct in de
Firestore Console, of via een eigen script/UI die je later toevoegt.

De mail toont een persistente "Vendor Impact" tabel op basis van deze collectie
(niet alleen de items van de huidige scan), zodat je in één oogopslag de status
van alle lopende vendor-trajecten ziet.

### Extra Firestore composite index

De vendor-trail query (`order_by last_updated`) werkt zonder extra index. Mocht
je later filteren op `status` toevoegen (bijvoorbeeld alleen niet-`closed` items
tonen), dan vraagt Firestore bij de eerste run om een composite index — volg dan
de link die in de foutmelding verschijnt.

---

## DNB-categorisering + baseline-mechanisme

Elk gevonden item krijgt een `reporting_category`-veld dat exact de indeling
volgt van DNB's eigen Statistical Reporting-pagina voor banken
(dnb.nl/login/dlr/statistical-reporting/banks): AnaCredit reports, Balance Sheet
Items (BSI), Commercial Real Estate (CRE), MFI Interest Rates (MIR),
Macroeconomic Statistics Reporting (MESREP), Monthly securities reporting (MSR),
OTC derivatives reporting, Payment Statistics, Residential Real Estate (RRE),
Special Purpose Vehicles (SPV) — aangevuld met "EBA Prudential Reporting
(COREP/FINREP/P3DH/iREF)" voor het EBA-deel dat niet in DNB's eigen indeling
voorkomt maar wel binnen de scope van de agent valt.

### Hoe de baseline werkt

Nieuwe Firestore-collectie `category_baselines` — één document per categorie
(`category_slug()` = hash van de categorienaam als document-ID).

- **Eerste run**: voor elke categorie zonder bestaand baseline-document wordt
  een gerichte, grounded zoekopdracht gedaan (`establish_baseline()`) om de
  huidige situatie vast te leggen (actuele taxonomie-versie, drempels,
  cadans). Dit gebeurt per categorie **eenmalig**.
- **Elke volgende run**: `ensure_all_baselines()` slaat categorieën met een
  bestaande baseline over — geen extra zoekkosten. De hoofd-scan krijgt de
  bestaande baselines als context (`format_baselines_for_prompt()`) met de
  expliciete instructie: rapporteer alleen wat hiervan **afwijkt**, niet de
  baseline zelf opnieuw.

### E-mailweergave

De mail toont bevindingen gegroepeerd per DNB-categorie (in dezelfde volgorde
als de DNB-pagina), met onder elke categorie-kop een regel "Baseline
vastgesteld op [datum]: [samenvatting]" ter referentie, gevolgd door de nieuwe
bevindingen en getrackte vendor-items voor die categorie. Categorieën zonder
nieuwe bevindingen of getrackte items worden overgeslagen in de mail.

### Baseline resetten

Wil je een categorie opnieuw laten vaststellen (bijvoorbeeld na een grote
regelgevingswijziging die de baseline-tekst achterhaald maakt), verwijder dan
handmatig het bijbehorende document in `category_baselines` via de Firestore
Console — de eerstvolgende run stelt 'm dan opnieuw vast.

---

## EBA-categorisering (Reporting Framework 4.2)

Naast de DNB-indeling is er een tweede, parallelle categorie-set gebaseerd op
EBA Reporting Framework 4.2
(eba.europa.eu/risk-and-data-analysis/reporting-frameworks/reporting-framework-42):
de volledige module-lijst onder dit framework (AE, ALMM, FRTB, Large Exposures,
LCR Delegated Act, Leverage Ratio, Own Funds, NSFR, FINREP, G-SII, IRRBB,
Resolution Planning/MREL, Instant Payments), aangevuld met P3DH/iREF omdat dat
niet strikt in 4.2's modulelijst staat maar wel binnen de scope van de agent valt.

`ALL_CATEGORIES = DNB_CATEGORIES + EBA_CATEGORIES` wordt gebruikt voor de
baseline-vaststelling (`ensure_all_baselines()`) — dus bij de eerste run worden
baselines voor **beide** taxonomieën vastgesteld (in totaal ~24 gerichte
zoekopdrachten), en bij elke volgende run alleen de ontbrekende.

De mail toont twee gescheiden kopjes: "Bevindingen per DNB-rapportagecategorie"
en "Bevindingen per EBA-rapportagecategorie (Reporting Framework 4.2)", elk met
dezelfde opbouw (baseline-referentie, nieuwe bevindingen, getrackte vendor-items).

**Onderhoud:** EBA's frameworks worden periodiek opgevolgd door 4.3 (verwacht
Q4 2026) en 4.4. Wanneer dat zover is, werk je `EBA_CATEGORIES` bij met de dan
geldende module-indeling — de rest van de code (baseline-mechanisme, e-mail-
rendering) hoeft niet aangepast te worden.

---

## Bronlink-validatie

Elke `source_url` die het model teruggeeft wordt vóór opslag/verzending getest
(`verify_source_url()` — een GET-request met browser-achtige User-Agent).
Alleen een harde `404`, DNS-fout, timeout of connectie-fout telt als "kapot";
een `403` (veel toezichthouder-sites blokkeren scripted requests, ook voor
geldige pagina's) wordt niet als kapot beschouwd. Bij een kapotte/verzonnen
link valt het systeem terug op de bekende reporting-frameworks-hoofdpagina van
de betreffende toezichthouder (`FALLBACK_URLS`). De prompt is ook aangescherpt:
het model mag alleen URL's gebruiken die daadwerkelijk uit de zoekresultaten
kwamen, nooit een pad verzinnen of raden.

Dit voegt latency toe aan elke run (één HTTP-check per gevonden item), maar
voorkomt dode/verzonnen links in de mail. De "volledige trail"-tabel toont nu
ook een bronlink per item, en die link wordt bij elke herbevestiging ververst
(zodat een eerder foutieve link zichzelf corrigeert zodra een schone versie
wordt gevonden).

---

## Zelf vendor-status bijwerken

Elke rij in de "Vendor Impact"-tabellen heeft nu een link **"status
bijwerken"** die naar een klein webformulier gaat (vierde Cloud Function,
`vendor-status-update`, zelfde publieke opzet als het feedback-formulier —
noodzakelijk omdat een e-mail-link geen Authorization-header kan meesturen).

Op dat formulier kun je:
- de `status` wijzigen (new / vendor_notified / vendor_confirmed / testing /
  parallel_run / closed)
- optioneel `vendor_sla_date`, `test_window_start`, `go_live_date` invullen of
  bijwerken

Alleen ingevulde datumvelden worden overschreven; lege velden laten de
bestaande waarde in Firestore ongemoeid. Na opslaan toont het formulier een
bevestiging.

**Zelfde beveiligingskanttekening als bij het feedback-formulier:** dit
endpoint is bewust onbeveiligd (iedereen met de link kan de status van een
item wijzigen). Voor puur intern gebruik is dat een acceptabele afweging tegen
het gemak van direct vanuit de mail kunnen bijwerken, zonder in de Firestore
Console te hoeven klikken.

### Deployen

```bash
gcloud functions deploy vendor-status-update \
  --gen2 \
  --runtime=python312 \
  --region=us-west1 \
  --source=. \
  --entry-point=vendor_status_update \
  --trigger-http \
  --allow-unauthenticated \
  --timeout=60 \
  --memory=256MB \
  --service-account=project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com \
  --build-service-account=projects/project-5ce9338e-bc20-4b9f-88f/serviceAccounts/project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com
```

Let op: `VENDOR_STATUS_URL` in `main.py` verwacht dat deze functie exact
`vendor-status-update` heet en in `us-west1` draait. Wijk je hiervan af, pas de
constante aan.

---

## Opschonen: cleanup_firestore.py

Los script (geen Cloud Function, gewoon lokaal of in Cloud Shell draaien) dat
verouderde data verwijdert uit de drie collecties:

- `vendor_impact_items` — items met `status: closed` ouder dan 180 dagen
- `last_mile_tracked_items` — items die > 90 dagen niet meer in een scan zijn
  teruggekomen (waarschijnlijk niet langer relevant)
- `report_feedback` — feedback die al is verwerkt (`addressed: true`) en ouder
  is dan 30 dagen

Standaard een **dry-run** — toont wat verwijderd zou worden zonder iets te
wissen:

```bash
python3 cleanup_firestore.py
```

Echt uitvoeren:

```bash
python3 cleanup_firestore.py --apply
```

Drempels aanpassen:

```bash
python3 cleanup_firestore.py --apply \
  --vendor-closed-days 90 \
  --stale-tracked-days 60 \
  --feedback-days 14
```

Voor gebruik in een geautomatiseerde/geplande run (geen interactieve prompt):

```bash
python3 cleanup_firestore.py --apply --yes
```

Vereist dat `google-cloud-firestore` beschikbaar is (`pip install --user google-cloud-firestore --break-system-packages`
in Cloud Shell indien nog niet aanwezig) en dat je bent ingelogd met rechten
op de Firestore-database (`roles/datastore.user` of hoger).

---

## Feedback-loop (zelflerend)

Onderaan elke mail (weekly + last-mile) staat een link "Geef hier feedback". Die
opent een klein webformulier (categorie + toelichting), gehost door een derde
Cloud Function. Feedback wordt opgeslagen in Firestore-collectie `report_feedback`.

**Zelf-aanpassend gedrag:** bij elke run haalt de functie de meest recente,
nog niet "afgehandelde" feedback op voor het betreffende rapporttype en voegt dit
als expliciete instructie toe aan de prompt ("readers flagged the following gaps —
check whether these are now covered"). Feedback wordt automatisch als afgehandeld
gemarkeerd nadat die 3x is meegenomen in een run, zodat de prompt niet blijft
groeien met oude issues.

### Belangrijk: publiek toegankelijk endpoint

De feedback-functie moet **zonder authenticatie** bereikbaar zijn omdat een link
in een e-mail geen Authorization-header kan meesturen. Dat betekent dat iedereen
met de URL feedback kan insturen (geen gevoelige data wordt prijsgegeven, alleen
invoer opgeslagen — maar wel iets om je bewust van te zijn).

### Firestore composite index

De feedback-query (`report_type == X AND addressed == False`, gesorteerd op
`submitted_at`) vereist een composite index. Bij de eerste keer testen geeft
Firestore een foutmelding met een kant-en-klare link om de index aan te maken —
klik die link, of maak 'm handmatig aan:

```bash
gcloud firestore indexes composite create \
  --collection-group=report_feedback \
  --field-config field-path=report_type,order=ascending \
  --field-config field-path=addressed,order=ascending \
  --field-config field-path=submitted_at,order=descending
```

### Deployen (publiek toegankelijk, dus met --allow-unauthenticated)

```bash
gcloud functions deploy report-feedback \
  --gen2 \
  --runtime=python312 \
  --region=us-west1 \
  --source=. \
  --entry-point=report_feedback \
  --trigger-http \
  --allow-unauthenticated \
  --timeout=60 \
  --memory=256MB \
  --service-account=project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com \
  --build-service-account=projects/project-5ce9338e-bc20-4b9f-88f/serviceAccounts/project-5ce9338e-bc20-4b9f-88f@appspot.gserviceaccount.com
```

Let op: `FEEDBACK_FORM_URL` in `main.py` verwacht dat deze functie exact
`report-feedback` heet en in `us-west1` draait (voor de voorspelbare
`cloudfunctions.net` URL). Wijk je hiervan af, pas de constante aan.

### Testen

```bash
curl "https://us-west1-project-5ce9338e-bc20-4b9f-88f.cloudfunctions.net/report-feedback?report=weekly"
```

Moet een HTML-formulier teruggeven. Vul 'm ook een keer in via de browser om te
checken dat de POST goed wegschrijft naar Firestore.

### Bestaande functies opnieuw deployen

Omdat `main.py` is aangepast (nieuwe imports/functies), moet je **weekly-regulatory-digest**
en **last-mile-horizon-scan** ook opnieuw deployen met dezelfde deploy-commando's
als eerder in dit bestand — de entry-points (`weekly_digest`, `last_mile_digest`)
zijn ongewijzigd, alleen de onderliggende `main.py` is bijgewerkt.
