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
- **Taken** (tabblad *Taken*): een gedeelde takenlijst voor jullie allebei. Typ bovenaan een taak en druk op enter voor snel toevoegen, bijvoorbeeld een boodschappenlijstje. Met **+** kies je ook voor wie de taak is, een deadline en of hij terugkomt (elke dag, week, 2 weken of maand). Vink een taak af met het rondje; vergist? Tik binnen 6 seconden op **Ongedaan maken**. Een terugkerende taak blijft staan met de eerstvolgende datum en laat zien wie hem de vorige keer deed. Boodschap gedaan? Tik op de taak en kies **Uitgave van maken**: de omschrijving is al ingevuld en de taak wordt na opslaan afgevinkt. Met *Mijn* zie je alleen wat voor jou of voor iedereen is. Afgevinkte taken staan nog 90 dagen onder *Toon afgerond*.
- **Cijfers**: per maand of per jaar het totaal, wie wat betaalde, uitgaven per categorie en een grafiek. In het jaaroverzicht zie je ook het gemiddelde per maand.
- **Instellingen**: categorieën toevoegen (met een eigen icoon), hernoemen of verbergen.

Tik op een uitgave om hem te bewerken of te verwijderen. Verwijderd per ongeluk? Tik binnen 6 seconden op **Ongedaan maken**.

## Meldingen

Ga in de app naar **Instellingen → Meldingen** en kies je telefoon (die verschijnt zodra je bent ingelogd in de Home Assistant Companion-app). Daarna kun je per soort melding kiezen:

- **Maandrapport op de 1e**: om 9:00 een overzicht van de vorige maand: totaal, jouw deel, de grootste kostenpost en het openstaande saldo.
- **Taken van vandaag**: elke dag op het tijdstip dat je kiest (standaard 8:00) een lijstje met taken voor jou of voor iedereen die vandaag of eerder aan de beurt zijn. Staat er niets op de lijst, dan krijg je ook geen melding.
- **Nieuwe taak voor jou**: zodra je huisgenoot een taak aan jou geeft. Taken die je aan jezelf of aan iedereen geeft, sturen geen melding.

Met **Test rapport** en **Test taken** kun je het meteen proberen. Je huisgenoot stelt dit in op zijn of haar eigen account.

## Donkere modus

De app volgt automatisch de licht/donker-instelling van je telefoon of computer.

## Updaten

Vervang de bestanden in `/addons/kostendeler`, verhoog `version` in `config.yaml` en kies in de add-on **Opnieuw opbouwen**. De database blijft gewoon staan.
