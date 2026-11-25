# Kostendeler

Deel boodschappen en kleine huishoudkosten met je huisgenoot. De app draait als add-on op je eigen Home Assistant. Je data staat in een SQLite-database op de Pi (`/data/kostendeler.db`), gaat mee in de gewone Home Assistant-back-ups en wordt nergens anders heen gestuurd.

## Installeren (lokale add-on)

1. Installeer in Home Assistant de add-on **Samba share** of **Advanced SSH & Web Terminal** (als je die nog niet hebt).
2. Kopieer de map `kostendeler` naar de map `addons` op je Home Assistant. Het pad wordt dan `/addons/kostendeler/config.yaml`.
3. Ga naar **Instellingen → Add-ons → Add-on Store**. Tik rechtsboven op **⋮ → Controleren op updates** en ververs de pagina.
4. Onder **Lokale add-ons** staat nu **Kostendeler**. Open hem en tik op **Installeren**. Het bouwen duurt op een Raspberry Pi een paar minuten.
5. Zet **Tonen in zijbalk** aan en tik op **Starten**.

## Je huisgenoot toevoegen

1. Ga naar **Instellingen → Personen → Persoon toevoegen** en maak een account aan met **Mag inloggen** aan. Adminrechten zijn niet nodig.
2. Laat je huisgenoot de **Home Assistant Companion-app** installeren (iOS of Android) en inloggen.
3. In de zijbalk staat **Kosten**. Na de eerste keer openen verschijnt je huisgenoot ook in de app, en kun je uitgaven met elkaar delen.

Tip: wil je huisgenoot verder weinig van Home Assistant zien? Die kan via **Profiel → Zijbalk wijzigen** alle andere items verbergen.

## Gebruik

- **+** om een uitgave toe te voegen: bedrag, omschrijving, categorie, wie betaalde en voor wie.
- **Voor wie**: vink mensen aan of uit om gelijk te verdelen, of tik op *Bedragen aanpassen* voor een eigen verdeling. Iets alleen voor je huisgenoot? Vink jezelf uit.
- **Afrekenen**: stuur een Tikkie, tik daarna op *Afrekenen* en het saldo staat weer op nul. Een deel afrekenen kan ook.
- **Vaste kosten** (onder Instellingen): huur, internet, energie enzovoort. Kies bedrag, wie betaalt, de verdeling en de dag van de maand; de app zet ze er op die dag automatisch bij. Een bedrag dat een keer afwijkt pas je gewoon aan in de lijst. Pauzeren kan ook.
- **Taken** (tabblad *Taken*): een gedeelde takenlijst. Typ bovenaan een taak en druk op enter, of tik op **+** voor meer opties:
  - **Herhalen**: elke dag, week, 2 weken of maand. Afvinken zet de taak op de eerstvolgende datum.
  - **Om de beurt** (bij een terugkerende taak): na elke keer afvinken is de ander aan de beurt. Bij *Nu aan de beurt* kies je wie begint.
  - **Overdragen**: tik op een taak. Bij om de beurt kun je **ruilen** (de ander doet nu, jij daarna de beurt van de ander) of **afkopen** (de ander doet het en jij betaalt een bedrag, dat als uitgave in het saldo komt). Bij andere taken kan het met of zonder vergoeding.
  - **Uitgave**: maakt van een taak meteen een uitgave en vinkt hem af.
  Vergist bij het afvinken? Tik binnen 6 seconden op **Ongedaan maken**. Met *Mijn* zie je alleen wat voor jou of voor iedereen is.
- **Boodschappen** (tabblad *Boodschappen*): typ wat er nodig is (eerdere boodschappen worden voorgesteld). Onder **Bijna op?** staan vaste spullen: één tik en het staat op de lijst; via *Bewerken* pas je ze aan. In de winkel vink je af wat je pakt; het gaat naar *In je mandje*. Bij de kassa tik je op **Bedrag invullen**: alles in je mandje wordt één uitgave. De lijst ververst vanzelf, dus je ziet wat je huisgenoot intussen toevoegt.
- **Weekmenu** (onder *Boodschappen*): tik op een dag en kies een recept en het aantal personen, of vul iets anders in (restjes, uit eten). Met **Boodschappen voor … op de lijst** gaan de ingrediënten van alle geplande dagen in één keer op de lijst; dezelfde ingrediënten uit meerdere recepten worden opgeteld. Dagen die al op de lijst staan krijgen een vinkje. Wat je vandaag eet staat ook in `sensor.kostendeler_eten_vandaag`.
- **Recepten** (onder *Boodschappen*): tik op *Nieuw* en plak een link van een receptensite en tik op **Ophalen**, of vul naam, personen en ingrediënten (één per regel) zelf in. Open een recept, kies voor hoeveel personen je kookt (de hoeveelheden rekenen mee) en vink aan wat je moet halen. Wat al op de lijst staat en dingen als zout, peper en olie staan standaard uit. Met **op de lijst zetten** gaat alles in één keer op de boodschappenlijst. Recepten die je vaak gebruikt staan vooraan. Sommige sites (zoals die van grote supermarkten) blokkeren het ophalen; vul de ingrediënten dan zelf in.
- **Cijfers**: per maand of per jaar het totaal, wie wat betaalde, uitgaven per categorie en een grafiek. In het jaaroverzicht zie je ook het gemiddelde per maand.
- **Instellingen** (op de telefoon via je avatar op het overzicht): categorieën toevoegen (met een eigen icoon), hernoemen of verbergen.

Tik op een uitgave om hem te bewerken of te verwijderen. Verwijderd per ongeluk? Tik binnen 6 seconden op **Ongedaan maken**.

## Betaalverzoek bij afrekenen

Vul bij **Instellingen → Betaalverzoek** je IBAN en de naam op je rekening in. Rekent je huisgenoot daarna met je af, dan staat er in het afrekenscherm een QR-code met jouw IBAN, het bedrag en een omschrijving. Die scan je met een bank-app die SEPA-QR-codes leest (onder meer ING, bunq en Knab; Rabobank en ABN AMRO lezen deze code niet). Werkt scannen niet, tik dan op IBAN, naam of bedrag om ze te kopiëren. Krijg jij geld, dan kun je met **Betaalverzoek delen** de gegevens als bericht sturen. De app maakt zelf niets over: markeer het als betaald zodra het geld binnen is.

## Op de computer

Op een breed scherm staat alles naast elkaar: op het overzicht zie je rechts de taken van vandaag, wat je eet en de boodschappenlijst. Sneltoetsen: **N** nieuw, **1**–**5** tabbladen, **/** snel toevoegen, **←** **→** vorige en volgende maand, **Esc** sluiten.

## Uitgave laten herhalen

Kies bij een nieuwe uitgave onder **Herhalen** voor *Elke maand*. De uitgave wordt opgeslagen en komt daarna elke maand op dezelfde dag vanzelf in de lijst. Aanpassen, pauzeren of stoppen doe je bij **Instellingen → Vaste kosten**.

## Meldingen

Ga in de app naar **Instellingen → Meldingen** en kies je telefoon (die verschijnt zodra je bent ingelogd in de Home Assistant Companion-app). Daarna kun je per soort melding kiezen:

- **Maandrapport op de 1e**: om 9:00 een overzicht van de vorige maand: totaal, jouw deel, de grootste kostenpost en het openstaande saldo.
- **Taken van vandaag**: elke dag op het tijdstip dat je kiest (standaard 8:00) een lijstje met taken voor jou of voor iedereen die vandaag of eerder aan de beurt zijn. Staat er niets op de lijst, dan krijg je ook geen melding.
- **Nieuwe taak voor jou**: zodra je huisgenoot een taak aan jou geeft. Taken die je aan jezelf of aan iedereen geeft, sturen geen melding.

Met **Test rapport** en **Test taken** kun je het meteen proberen. Je huisgenoot stelt dit in op zijn of haar eigen account.

## Home Assistant: sensoren en NFC

De app zet deze sensoren in Home Assistant en houdt ze bij: `sensor.kostendeler_saldo_<naam>` (per persoon), `sensor.kostendeler_taken_vandaag`, `sensor.kostendeler_taken_te_laat` en `sensor.kostendeler_boodschappen` (met de items als attribuut). Gebruik ze op je dashboard of in automatiseringen, bijvoorbeeld een lamp die oranje kleurt als `taken_te_laat` boven 0 komt. Na een herstart van Home Assistant zijn ze binnen een minuut terug.

Wil je met een **NFC-sticker** iets op de boodschappenlijst zetten of een taak afvinken? Ga naar **Instellingen → Home Assistant → NFC en automatiseringen instellen**. Daar staat kant-en-klare YAML met je eigen adres en sleutel: een `rest_command` voor `configuration.yaml` en twee voorbeeldautomatiseringen. De koppeling werkt alleen vanaf Home Assistant zelf en andere add-ons (zoals Node-RED), en alleen met de sleutel.

## Donkere modus

De app volgt automatisch de licht/donker-instelling van je telefoon of computer.

## Updaten

Vervang de bestanden in `/addons/kostendeler`, verhoog `version` in `config.yaml` en kies in de add-on **Opnieuw opbouwen**. De database blijft gewoon staan.
