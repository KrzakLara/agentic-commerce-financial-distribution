# TRUSTEQ — Implementacijski plan v2 (vertical slice first)

**Projekt:** Secure Agentic Depot Onboarding  
**Arhitekturni izvor istine:** `TRUSTEQ_ADR_v1.1_FINAL.docx` (Architecture Frozen — ADR-001)  
**Lokalni folder:** `C:\Users\larak\Desktop\Projektici\Trusteq`  
**Formalni rok predaje:** 9. 11. 2026. do 16:45; **interni cilj:** 9. 11. ujutro  
**Intervju:** 10. 11. 2026.; izlaganje i demonstracija ukupno **najviše 10 minuta**  
**Jezik:** PDF case studyja kaže njemački; personalizirani email dopušta engleski. Sačuvati tu potvrdu ili po potrebi pisano potvrditi s recruiterom.

## Što se promijenilo u odnosu na v1?

1. **Ist-Analyse postaje zaseban obvezni paralelni radni tok** od prvog dana, ne zadnja stavka u dokumentaciji.
2. **Vertical slice odmah:** 4. 11. mora raditi cjelovit simulirani happy path (scripted agent → REST → nacrt → autorizirani customer handoff → mock KYC/contract/depot → `OPENED`).
3. **Scripted agent je dio jezgre od početka**, a ne dodatak na kraju.
4. **Slajdovi nastaju paralelno s kodom.** Kostur 3. 11.; gotovi sadržajni slajdovi 7. 11.; 8. 11. proba i backup.
5. **Dva freezea:** P0 core freeze 5. 11. navečer (tri demo ishoda rade); završni feature freeze 6. 11. navečer. Poslije samo ispravci, testovi, dokumentacija i prezentacija.
6. Svaka faza završava **dokazom i kratkim učenjem**: objasni komponentu svojim riječima, napravi jednu malu izmjenu ili test sama, zapiši 2–3 Q&A pitanja.

## Nemijenjajuća arhitekturna pravila

- **L3 controlled autonomy:** vanjski AI agent koordinira, ali nije bankarski autoritet. *LLM proposes; policy decides; customer authorizes; bank executes.*
- Odvojiti identitet agenta/operatera, identitet korisnika i kratkotrajan mandat/opseg ovlasti. Čak i u mock modu: jasne granice aktera i scenarija.
- **Secure handoff:** korisnik autentificiran izravno na bank-controlled kanalu, jednokratni challenge s istekom, vezan uz application, customer i digest odobrene akcije. QR/link sam nije dokaz identiteta.
- Policy i state machine su deterministički; sustav **fail-closed** ako authorization/policy/callback ne radi. Samo potvrda mock depot providera dopušta `OPENED`.
- **Samo sintetski podaci.** Mock KYC/AML, ugovor, wallet i depot jasno označeni; nula pravih financijskih operacija.
- Agent dobiva **generičke statuse** bez privatnih AML razloga, ID dokumenata i osjetljivih compliance detalja. Mandat prestaje nakon završnog stanja.
- REST je kanonski API. MCP kao P1 u najviše 0,5 dana i bez dupliciranja sigurnosne logike. Bez full A2A, FAPI certifikacije ili pravih eID/KYC integracija.
- Modularni monolit Python 3.11/3.12, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite + repository abstraction, pytest, httpx, ruff. **Scripted mode radi bez API ključa.**
- Prije izmjena pregledati postojeće datoteke i `git status`. `.claude/` (uključujući `.claude/skills/`) ne smije biti kreiran/trackan; tajne, `.env`, lokalna baza, `.venv/` i cache ne idu u git. Bez GitHub pushanja bez odobrenja.

---

## Paralelni tok R — Ist-Analyse i slajdovi (obvezno od 3. 11.)

**Cilj:** riješiti Korak 1 TRUSTEQ case studyja, ne čekati posljednji dan.

**3. 11. — istražiti konkretan njemački primjer** (prednost Union Investment/Deka, ako se iz službenih izvora može razdvojiti fondovska kompanija, distribucijska banka i depot provider). Izraditi 1 stranicu o: korisnikovim koracima, zahtijevanim dokazima, identity/KYC i poreznim podacima, appropriateness pitanjima kada su relevantna, ugovoru, uključenim pravnim osobama, uočenim friction points i ograničenjima. Ne predstavljati pretpostavke kao provjerenu činjenicu. Zabilježiti službene URL-ove i datum pristupa; ne pokušavati potpunu pravnu analizu.

**3. 11. — PowerPoint kostur u TRUSTEQ masteru:** naslov; današnji proces i problem; pet razina autonomije 0–4; ciljna arhitektura; trust/handoff; trade-offs; roadmap/staffing; demo; ograničenja. Od početka voditi 10-minutni budžet, demo oko 2–3 minute.

**5. 11. — prvi kompletni draft slajdova:** obavezno uključiti verified discovery, agent-facing brand/product information, Hausbank/channel attribution, razdvojene pravne odgovornosti, otvaranje samo uz potvrdu partnera te jasne mock-vs-real tvrdnje.

**6. 11. — ugraditi stvarne testove i evaluaciju**, bez izmišljenih metrika; pripremiti rezervne slajdove za Q&A i izvore.

**7. 11. — sadržaj i izgled prezentacije gotovi;** 8. 11. samo proba, snimka, korekcije.

**Dokaz završetka toka R:** matrica `zahtjev case studyja → slajd / demo / test`, najmanje jedan primarni izvor za postojeći proces i jasno navedene pretpostavke. **Učenje:** Lara u 60 sekundi prepriča današnji proces i zašto je AI kanal drugačiji.

---

## Faza 0 — Temelji projekta i provedbeni ugovori (3. 11.)

**Implementacija:**
- Pregledati lokalnu mapu, git status, ADR i postojeće datoteke, bez brisanja korisničkih promjena.
- `pyproject.toml`, `.env.example`, `.gitignore`, `README.md`, FastAPI app, `GET /health`, pytest/httpx, ruff; točne Windows PowerShell naredbe.
- Definirati tipizirane modele/ugovore: `AgentActor`, `Customer`, `Mandate`, `Application`, `Provider`, `Product`, `Handoff`, `ConsentReceipt`, `DisclosureReceipt`, `AuditEvent`, `ManualReviewCase`.
- Pripremiti stub/mock sučelja za KYC, compliance, contract i depot provider, s eksplicitnim `MOCK` oznakama.
- Dokumentirati T0a/T0b/T1/T2/T3 granice povjerenja i API ugovore iz ADR-a.

**Acceptance:** `GET /health` → HTTP 200; prvi pytest prolazi; startup reproducibilan; `.claude/` nije u Git indeksu; sve tajne izvan repozitorija.

**Učenje:** objasni FastAPI, Pydantic i modularni monolit; sama izmijeni jedan mali endpoint test; napiši 2–3 Q&A pitanja.

## Faza 1 — Persistence, workflow + minimalni scripted agent (3. 11.)

**Implementacija:**
- SQLite + SQLAlchemy 2 i repository interface; jasni datotečni modeli/migracijski koraci.
- State machine bez proizvoljnog `set_status`: `DRAFT → AWAITING_AUTHORIZATION → AUTHORIZED → KYC_PENDING → COMPLIANCE_PENDING → CONTRACT_PENDING → SUBMITTED_TO_PROVIDER → OPENED`; bočna stanja `ACTION_REQUIRED`, `MANUAL_REVIEW`, `REJECTED`, `CANCELLED`, `FAILED` uz izričito dopuštene prijelaze.
- `POST /v1/applications`, `GET /v1/applications/{id}` i generički status; testni identiteti aktera, ne javno otvoren pristup.
- **`ScriptedCustomerAgent` odmah**: poziva stvarne REST endpoint-e ili isti validirani API klijentski ugovor, nikada privatnu metodu za `set_opened`.
- Audit zapis za svaku promjenu stanja; `correlation_id`; DB-level UNIQUE/idempotency key po actoru + operaciji + body hash; isti ključ/isti body → isti rezultat, različit body → 409.

**Acceptance:** podaci ostaju nakon ponovnog pokretanja; nedopušteni prijelaz blokiran; dupli draft ne nastaje; agent može napraviti nacrt i vidjeti dopušteni status.

**Učenje:** nacrtaj prijelaze stanja; sama dodaj jedan test nedopuštenog prijelaza; odgovori zašto stanje ne smije biti samo u LLM razgovoru.

## Faza 2 — Rani vertical slice: cjeloviti simulirani happy path (4. 11.)

**Redoslijed implementacije (end-to-end, bez čekanja svih naprednih funkcionalnosti):**
1. Scripted agent otkriva **unaprijed dopuštenog testnog providera** i proizvod iz lokalnih fixturea; učitava samo validirane podatke.
2. Agent otvara draft i zatraži handoff.
3. U minimalnom bank-controlled customer prikazu (mali HTML/Jinja ili ekvivalent) simulirani autentificirani korisnik vidi provider, proizvod, kanal, document version/digest i radnju; potvrđuje konkretnu akciju.
4. Backend provjerava zaseban customer identitet, jednokratni/kratkotrajni izazov i neizmijenjeni digest; nastavlja isključivo kroz determinističke prijelaze.
5. Mock KYC/compliance daje `PASS`; mock ugovor bilježi korisnikovu potvrdu; mock depot provider šalje potvrdu kroz kontrolirani callback.
6. Tek nakon potvrde i svih preduvjeta backend postavlja `OPENED`, sprema audit i opoziva mandat; agent dobiva samo generički status.

**Važno:** za prvi slice dopuštena je **minimalna testna** autentifikacija/registrirani fixture i lokalne mock provjere. Takve prečace dokumentirati kao privremene, isključivo unutar demo okruženja. **Nema** bypassa policyja ili izravnog `OPENED` čak ni u prvoj verziji.

**Acceptance:** iz čiste baze `python -m scripts.demo_happy` (ili ekvivalent) pokazuje stvarni slijed REST zahtjeva i `OPENED`; isto prolazi automatiziranim integracijskim testom bez API ključa; status ostaje nakon restarta.

**Učenje:** Lara može objasniti svaki korak demoa i sama promijeniti jedan mock odgovor `PASS → REVIEW_REQUIRED` te predvidjeti ishod.

## Faza 3 — Sigurnosno očvršćivanje: autorizacija i trusted handoff (5. 11.)

**Implementacija:**
- Odvojeni `agent_actor_id`, `customer_id`, `mandate_id`; kratkotrajni scoped mandat sa `aud`, `exp`, `jti`, revocation i provjerom vlasništva svakog zahtjeva; token/claimovi su **mock**, ne FAPI certifikacija.
- Potvrda u customer-controlled kanalu; nonce single-use/TTL i vezivanje uz `application_id + customer_id + provider/product/channel + document digest`.
- Promjena digesta, tuđi korisnik, istekao mandat, neispravna audience, replay ili zamijenjena sesija → fail closed i audit, bez osjetljivih detalja agentu.
- `POST /v1/customer/applications/{id}/request-human`; stanja za kontroliranu manual review; korisnički request-human bez zaobilaženja KYC-a.

**Acceptance:** `denied` demo radi, kao i sigurnosni testovi handoff substitution/IDOR/replay/expiry; ne dolazi do poslovnog prijelaza kad ovlast nedostaje. **Ne smije oslabiti happy path.**

**Učenje:** sama izazovi 401/403 testom bez autorizacije; objasni razliku između agent identity i customer approval.

## Faza 4 — Discovery, kanal, disclosures i treći demo ishod (5. 11.)

**Implementacija:**
- `GET /v1/providers/{id}/manifest` i `GET /v1/products`; fixture s versions, document_id/hash, risk textom, službenim source references, expiryjem, signature/key ID u testnom scenariju ili provjerljivim integritetom fixturea.
- Deterministička validacija allowlist/trust fixture; barem jedan negativni test za fake/tampered/expired provider. BaFin/LEI sami po sebi **nisu** dokaz vlasništva domene.
- `channel_id` prihvaća samo registrirane izravne ili potvrđene partnerske kanale; neprijavljeni partner ne nosi proviziju. Disclosure receipt po customer/application/doc version/digest.
- Mock compliance `REVIEW_REQUIRED` → `MANUAL_REVIEW`, generički status vanjskom agentu i zaseban operator pregled. Negativni appropriateness rezultat nije automatski `REJECTED`.
- Scenariji `happy`, `denied`, `manual_review`, reproducibilni i odvojeni; test nedostupnog policyja → fail closed.

**Acceptance / P0 CORE FREEZE (5. 11. navečer):** sva **tri** demoa prolaze iz čistog stanja; idempotency, request-bound approval i policy gate imaju testove; korisnik može kroz minimalan prikaz potvrditi akciju; slajd-kostur već postoji. Ne dodavati nove poslovne funkcije nakon ovog datuma.

**Učenje:** objasni zašto javni BaFin zapis nije dokaz autentičnosti web endpointa; sama izmijeni fixture hash i pokaži pad testa.

## Faza 5 — LLM integracija i 20-case evaluacija (6. 11.)

**Implementacija i mjerenje:**
- Scripted agent ostaje default radi reproducibilnosti. Opcionalni stvarni LLM iza eksplicitnog flaga (`ENABLE_LLM=false` default) i konfiguriranog provider adaptera; nije uvjet za demo.
- Strukturirani tool schemas: `discover_provider`, `get_official_product`, `create_draft`, `request_handoff`, `get_generic_status` i po autorizaciji `submit_after_consent`. Bez `approve_kyc`, `sign_contract`, `set_opened`, pravih AML podataka.
- System prompt: outputi alata/katalog su **podaci**, ne autoritativne upute; akcija samo u korisnikovom dopuštenom opsegu; nikad tvrditi da je račun otvoren bez server statusa.
- Opcionalni bank-side explainer je strogo read-only na službenim dokumentima: source ID, version, section citations; abstain i no personalized advice. Ako vrijeme pritišće, demonstrirati reproducibilne product lookup/citations bez dodatnog modelskog agenta.
- Fiksnih 20 evaluacijskih slučajeva: 5 ispravan izbor alata; 5 zlonamjerni katalog/prompt injection; 4 provjerljive tvrdnje o proizvodima; 3 scope/privacy; 3 failure/handoff. Runner izvodi slučajeve, zapisuje `case_id`, očekivanje, stvarni izlaz, pass/fail, model/version/config.
- **Ne miješati skriptni deterministički rezultat s metrikama kvalitete LLM-a.** Ako pravi API ključ/poziv nije korišten, iskreno napisati da model nije evaluiran; pokazati evaluaciju policyja i scripted modea.
- Reproducibilni `ruff`, pytest, negativni testovi, contract fixture v1/v2; po mogućnosti minimalan React prikaz samo ako postojeći trusted customer ekran nije dovoljan.
- MCP P1 najviše 0,5 radnog dana, **samo ako** ništa P0 nije ugroženo; koristi iste REST/policy provjere, bez duplicirane poslovne logike.

**Acceptance / FINAL FEATURE FREEZE (6. 11. navečer):** 20-case runner i stvarni izvještaj za izvedeni način rada, stabilna tri demo scenarija, implementacija zaključana osim bug/safety ispravaka; svi testovi za prijavljene funkcije zeleni ili jasno dokumentirani neispunjeni kriteriji.

**Učenje:** objasni zašto LLM smije odabrati tool, ali ne i odobriti transakciju; sama dodaj jednu prompt-injection evaluacijsku stavku.

## Faza 6 — Stabilizacija, testni dokazi, sigurnost (7. 11.)

**Bez novih featurea.** Pokrenuti testove iz čistog okruženja, popravljati samo bugove i sigurnosne propuste:
- State transitions, consent binding, replay, IDOR, policy outage, callback duplicate/out-of-order, depot timeout, no protected AML disclosure, post-open revocation, fake provider i malicious catalog, API version v1/v2.
- Idempotency na DB sloju, concurrency/callback test gdje izvedivo; audit/event timeline iz workflowa; generički status agentu.
- Pokrenuti testove na Windowsu, sačuvati stvarne izlaze naredbi i verzije. Napraviti `docs/test_results.md` i `docs/evaluation_results.md` s mjerljivim rezultatima.
- Ako se kritičan kriterij ne može dovršiti, eksplicitno ga staviti u Known Limitations i ne prikazivati kao implementirano.

**Acceptance:** fresh install + README → sva 3 demoa; exact pytest summary; svi blokirajući sigurnosni testovi prolaze; zaključena verzija demoa za snimanje.

**Učenje:** Lara objasni 2 sigurnosna testa i ručno reproducira barem jedan negativni slučaj.

## Faza 7 — Finalni PowerPoint i demo materijali (7. 11.)

- Izraditi u danom TRUSTEQ masteru (bez dodatnih frameworka/slika samo za ukras). Prezentacija **na engleskom prema dopuštenju u emailu**; obavezno potvrditi ako postoji dvojba zbog njemačkog teksta PDF-a.
- 8–9 slajdova i demo ukupno do 10 minuta: 1) problem/aktéri 2) real Ist-Analyse 3) pet autonomy levels + L3 4) architecture + trust boundaries 5) security, identity and approval 6) trade-offs/channel conflict/roadmap/staffing 7) demo storyboard 8) real measurements/results 9) assumptions/limitations/next steps (po potrebi spajati slajdove).
- Na dijagramu: korisnik odvojen od vanjskog AI; bank-controlled channel; audit obuhvaća cijeli workflow; fond, partneri i depot provider zasebni.
- Slide „Implemented vs Mocked vs Target 2030“, 2–3 dodatna Q&A slajda (testovi, API, regulators). Ne prikazivati fake provider verification kao stvarni BaFin provjeren proizvod.
- Demo script 2–3 minute s jasnim `happy → denied → review` ishodima i čistim testnim podacima.

**Acceptance:** slajdovi i mjerljive brojke odgovaraju stvarnoj aplikaciji, video i sve screenshotove moguće je reproducirati. Izlaganje mjerljivo ispod 10 minuta.

**Učenje:** Lara govori cijeli pitch bez čitanja ADR-a; za pet odluka zna objasniti i odbačenu alternativu.

## Faza 8 — Proba, rezerva i predaja (8.–9. 11.)

**8. 11.:** full dress rehearsal s mjerenjem vremena; snimka radnog demoa kao backup (nikada fake/demo animacija predstavljena kao stvaran izvršeni kod), završni README/ADR i Q&A podsjetnik; freeze svega osim kritičnog fixa. Provjeriti repo za `.claude/`, tajne, testne baze, privatne podatke i nenamjerne Git datoteke.

**9. 11. ujutro:** završno smoke pokretanje; pripremiti/poslati finalni PowerPoint, pristup prototipu ili arhivu prema zahtjevima recruitera, jasne upute za pokretanje i eventualni link samo uz odobrenje. Ne ostavljati slanje za zadnji sat prije roka 16:45.

**Acceptance:** predani traženi artefakti; demo može raditi offline u scripted modu; rezervna snimka je dostupna; projekt demonstrira stvarne izmjerene rezultate, a ne tvrdnje o budućoj certifikaciji.

---

## Mini-matrica: zahtjev → dokaz

| TRUSTEQ zahtjev | Dokaz u sustavu / prezentaciji |
|---|---|
| Ist-Analyse | Research slajd + službeni izvori + friction points |
| Stufen 0–4 | Autonomy slajd s L3 odabirom i odbacivanjem L4 |
| Verified discovery / brand | Fixture catalog/manifest, fake-provider test, risk receipt |
| Agentic depot onboarding | Scripted agent + REST happy path |
| Identity/authorization/contracts | Scoped mandate, bank-controlled handoff, mock contract + tests |
| Compliance + human | Mock compliance + manual_review, privacy gate |
| Channel conflicts | Deterministički partner attribution + test |
| Security/deprecation | Negative tests, v1/v2 contract, fail-closed |
| AI models/prompts/data | Tool schemas, prompt contract, 20-case eval i jasno navedeni model settings |
| Technical concept / staffing | Architecture + ADR decisions + roadmap/team slide |
| Live demo with fallback | 3 demo outcomes + pre-recorded real run backup |

## Jedan prompt za Claude Code — revidirani plan

```text
Project: C:\Users\larak\Desktop\Projektici\Trusteq

The frozen TRUSTEQ ADR v1.1 is the architecture source of truth.
Read TRUSTEQ_IMPLEMENTATION_PHASES_v2.md and ADR v1.1 thoroughly.
The implementation schedule has changed: parallel Ist-Analyse and slides from day 1,
a working scripted-agent vertical slice by November 4, all three P0 demo paths by
November 5, final feature freeze November 6, final slides November 7, rehearsal
November 8, submission November 9 in the morning.

Do not redesign the architecture. Do not implement all phases at once.
First inspect existing files, Git status and runtime. Work ONLY on Phase 0 now,
and initialize the Ist-Analyse/slide skeleton in the parallel R track if it fits
without delaying Phase 0. Verify startup and tests with exact commands.
Report changed files, real test results, blockers and acceptance criteria.
Include a short learning checkpoint: explain what was built in simple Croatian,
ask me to make one tiny change, and propose 2-3 technical interview questions.
Stop after this phase for my approval.

Never create/commit .claude/ (including .claude/skills), secrets, .env or local
DB files. No GitHub push without my approval. Do not invent results or claim
mock KYC, signing, identity or bank integrations are production compliant.
```

**Plan v2 mijenja redoslijed implementacije, ne zaključanu arhitekturu ADR v1.1.**
