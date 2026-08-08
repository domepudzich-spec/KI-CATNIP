

# GEMINI FREE-TIER VERSION

Diese Version verwendet **Google Gemini statt Gemini**.

Standardmodell:

```text
gemini-2.5-flash-lite
```

## Railway-Umstellung

1. Erzeuge in Google AI Studio einen Gemini API-Key.
2. Öffne Railway -> KI-CATNIP -> Variables.
3. Lösche bzw. ignoriere:
   - `GEMINI_API_KEY`
   - `GEMINI_MODEL`
4. Füge hinzu:

```text
GEMINI_API_KEY=dein_key
GEMINI_MODEL=gemini-2.5-flash-lite
GEMINI_FREE_TIER=true
WEB_SEARCH=true
```

5. Ersetze auf GitHub mindestens:
   - `bot.py`
   - `requirements.txt`
   - `.env.example`
6. Committe die Änderungen. Railway deployt automatisch neu.

## Kostenloser Tier

Mit `GEMINI_FREE_TIER=true` blockiert KI-Catnip nicht wegen des alten
20-Euro-Limits. `/budget` zeigt stattdessen Token- und Anfrageverbrauch.

Wenn du später bewusst einen kostenpflichtigen Gemini-Tarif aktivierst, kannst
du `GEMINI_FREE_TIER=false` setzen. Dann wird das lokale 20-Euro-Sicherheitslimit
wieder verwendet.

## Enthaltene Funktionen

- normale Fragen mit `@KI-Catnip`
- `/ffxiv`, `/lore`, `/job`, `/kampf`, `/quest`
- `/mount`, `/minion`, `/markt`, `/charakter`
- `/quiz`, `/event`, `/boss`, `/raetsel`
- Event-Admin-Whitelist
- private User-Channels
- Begrüßung und Rückkehr-Begrüßung
- `/pdf` für FFXIV-Guides
- `/budget`


# Schattenflauscher — Eorzea-Enzyklopädie

# Sparsame Version

Diese Variante ist auf niedrige API-Kosten optimiert:

- nur die letzten 6 Chat-Nachrichten werden an die KI mitgeschickt
- maximal 10 Nachrichten werden lokal pro Channel gepuffert
- normale Antworten sind bewusst kompakter
- Websuche ist standardmäßig deaktiviert
- Spezialbefehle wie `/quest`, `/mount`, `/minion` und Charakter-/Marktabfragen können sie gezielt weiterhin nutzen
- Websuche verwendet einen kleineren Suchkontext


Ein spezialisierter Discord-Bot für **FINAL FANTASY XIV**.

Diese Version enthält ausdrücklich **keine automatische News-Funktion**
und **keinen Serverstatus**.

## Neue Hauptfunktion: @KI-Catnip

Du musst nicht mehr zwingend Slash-Commands benutzen.

Im normalen Discord-Chat reicht zum Beispiel:

```text
@KI-Catnip Wie funktioniert der Viper auf Level 100?
```

oder:

```text
@KI-Catnip Wo bekomme ich das Mount aus diesem Raid?
```

Der Bot merkt sich dabei den Gesprächskontext des Channels.

## Befehle

### Allgemeines Wissen
- `/ffxiv` – beliebige FFXIV-Frage
- `/lore` – Lore, Story, Charaktere und Orte
- `/job` – Jobs, Skills und Spielweise
- `/kampf` – Dungeon-/Trial-/Raidmechaniken
- `/quest` – Quest-Hilfe
- `/begriff` – Abkürzungen und Begriffe
- `/einsteiger` – besonders einfache Erklärungen

### Nachschlagen
- `/charakter` – sucht öffentliche Charakterinformationen auf dem Lodestone
- `/mount` – Herkunft und Freischaltung eines Mounts
- `/minion` – Herkunft und Freischaltung eines Minions
- `/markt` – Marktbrettpreise über Universalis

### Verwaltung
- `/reset` – Gesprächskontext des Channels löschen
- `/botinfo` – Funktionsübersicht

---

# Datenquellen

## Offizielle FFXIV-Seiten

Der KI-Prompt priorisiert:
- FINAL FANTASY XIV Lodestone
- offizielle Patch Notes
- offiziellen Job Guide
- Play Guide
- Eorzea Database
- Square Enix

## XIVAPI v2

Für die Item-Suche des Marktbrettbefehls wird XIVAPI v2 verwendet.

https://v2.xivapi.com/

XIVAPI v2 stellt Spieldaten aus den FFXIV-Clientdaten bereit.
Es stellt selbst keine aktuellen Spieler-/Lodestone-Profile bereit.

## Universalis

`/markt` verwendet:

https://universalis.app/

Universalis stellt von Spielern hochgeladene Marktbrettdaten bereit.
Diese können gegenüber dem echten Marktbrett im Spiel verzögert sein.

---

# Installation

## 1. Discord Developer Portal

https://discord.com/developers/applications

Erstelle eine Application und einen Bot.

## 2. WICHTIG: Message Content Intent

Damit `@KI-Catnip` funktioniert:

1. Developer Portal öffnen
2. Deine Application auswählen
3. **Bot**
4. Zu **Privileged Gateway Intents** scrollen
5. **MESSAGE CONTENT INTENT** aktivieren
6. Änderungen speichern

Ohne diese Einstellung funktionieren die Slash-Commands zwar weiterhin,
aber der Bot kann normale `@KI-Catnip`-Fragen nicht lesen.

## 3. Bot-Berechtigungen

Beim Einladen:

Scopes:
- `bot`
- `applications.commands`

Berechtigungen:
- View Channels
- Send Messages
- Embed Links
- Read Message History

## 4. Gemini API-Key

https://platform.openai.com/api-keys

## 5. `.env`

`.env.example` kopieren und die Kopie `.env` nennen:

```env
DISCORD_TOKEN=...
GEMINI_API_KEY=...
GEMINI_MODEL=gpt-5.6
BOT_NAME=KI-Catnip
DEFAULT_SPOILER_LEVEL=Dawntrail
WEB_SEARCH=false
```

## 6. Installieren

Unter Windows:

`INSTALLIEREN.bat`

oder:

```bash
python -m pip install -r requirements.txt
```

## 7. Starten

`START_BOT.bat`

oder:

```bash
python bot.py
```

---

# Beispiele

```text
@KI-Catnip Wer war Haurchefant?
```

```text
@KI-Catnip Erkläre mir die Mechaniken von diesem Savage-Fight.
```

```text
/job job: Viper frage: Wie funktioniert die Grundrotation?
```

```text
/charakter name: Beispiel Name welt: Shiva
```

```text
/mount name: Dein Mountname
```

```text
/minion name: Dein Minionname
```

```text
/markt item: Beispielgegenstand welt: Light
```

---

# Was bewusst NICHT eingebaut ist

- keine automatischen FFXIV-News
- kein Serverstatus
- keine Serverstatus-Benachrichtigungen
- kein News-Monitoring

Der Bot darf eine aktuelle Patchfrage beantworten, wenn ein Nutzer ihn direkt
danach fragt. Es gibt aber keinen automatischen Newsfeed.

---

# 20-Euro-Monatsbudget

Diese Version besitzt ein lokales Kostenlimit:

```env
MONTHLY_BUDGET_EUR=20.00
BUDGET_WARNING_EUR=15.00
```

Der Bot zählt nach jeder Gemini-Antwort:
- Input-Tokens
- davon erkannte Cached-Input-Tokens
- Output-Tokens
- erkannte Web-Search-Aufrufe

Die Werte werden in `budget_usage.json` gespeichert und beim Monatswechsel
automatisch auf einen neuen Monat umgestellt.

Mit:

```text
/budget
```

kannst du jederzeit die geschätzten bisherigen Kosten und das Restbudget sehen.

Ab 15 € erscheint eine Warnung. Sobald die lokale Schätzung 20 € erreicht,
werden weitere KI-Anfragen bis zum nächsten Kalendermonat blockiert.

## Wichtig

Das ist ein **lokaler Schutzmechanismus des Bots**. Er kann die offizielle
Gemini-Abrechnung nicht technisch ersetzen. Wechselkurse, Preisänderungen,
nicht erkannte Tool-Kosten oder andere Anwendungen desselben API-Projekts
können zu Abweichungen führen.

Für eine möglichst sichere Kostenkontrolle solltest du zusätzlich im
Gemini-Platform-Konto die dort verfügbaren Budget-/Usage-Einstellungen nutzen.

## Sparmodell

Standardmäßig verwendet diese Version:

```env
GEMINI_MODEL=gpt-5.6-luna
```

Dadurch ist sie erheblich günstiger als der Alias `gpt-5.6`, der derzeit
auf die Sol-Stufe führt.

## Sicherheitsreserve

Zusätzlich ist standardmäßig eine Reserve von 0,50 € eingestellt:

```env
BUDGET_SAFETY_RESERVE_EUR=0.50
```

Neue KI-Anfragen werden damit bereits bei ungefähr 19,50 € lokaler
Monatsschätzung gestoppt. Die Reserve reduziert das Risiko, dass eine letzte
größere Anfrage oder Wechselkursabweichung das gewünschte 20-€-Ziel überschreitet.


---

# Private Budgetwarnungen

Die Budgetwarnungen werden in dieser Version **nicht öffentlich in Discord-Channels angezeigt**.

Als Budget-Administrator ist konfiguriert:

```env
BUDGET_ADMIN_USER_ID=731192061294018641
```

Nur dieser Discord-Account:
- erhält Budgetwarnungen per Direktnachricht
- kann `/budget` verwenden
- sieht die konkreten Euro-Beträge

Warnstufen:
- ca. 15 € → erste DM-Warnung
- ca. 18 € → zweite DM-Warnung
- ca. 19,50 € → Limitwarnung

Normale Mitglieder sehen beim Erreichen des Limits lediglich, dass das
monatliche Nutzungslimit erreicht wurde.

Damit Direktnachrichten funktionieren, muss der Bot dem Administrator eine DM
senden dürfen. Falls Discord-DMs für den Server blockiert sind, erscheint die
Warnung nur in der Bot-Konsole.


---

# Automatische private FFXIV-Channels

Diese Version erstellt für neue Servermitglieder automatisch einen eigenen
privaten Textkanal.

Beispiel:

```text
Private FFXIV-Anfragen
└─ ffxiv-dominik-8641
```

Der Channel ist standardmäßig nur sichtbar für:
- das jeweilige Mitglied
- den Bot
- optional eine konfigurierte Admin-/Moderator-Rolle

Andere normale Mitglieder können den Channel weder sehen noch betreten.

## Benötigte Discord-Einstellung

Damit der Bot auf neue Mitglieder reagieren kann:

1. Discord Developer Portal öffnen
2. Application auswählen
3. **Bot**
4. **Privileged Gateway Intents**
5. **SERVER MEMBERS INTENT** aktivieren
6. **MESSAGE CONTENT INTENT** weiterhin aktiviert lassen

## Benötigte Bot-Berechtigungen

Zusätzlich zu den bisherigen Rechten benötigt der Bot:

- View Channels
- Send Messages
- Read Message History
- Embed Links
- **Manage Channels**

Ohne `Manage Channels` kann der Bot keine privaten Channels automatisch
erstellen oder löschen.

## Einstellungen in `.env`

```env
PRIVATE_CHANNELS_ENABLED=true
PRIVATE_CATEGORY_NAME=Private FFXIV-Anfragen
DELETE_PRIVATE_CHANNEL_ON_LEAVE=true
PRIVATE_ADMIN_ROLE_ID=0
```

### Admin-Rolle

Wenn Administratoren oder Moderatoren ebenfalls alle privaten FFXIV-Channels
sehen dürfen, trage die Discord-Rollen-ID ein:

```env
PRIVATE_ADMIN_ROLE_ID=123456789012345678
```

Bei `0` erhält keine zusätzliche Rolle Zugriff.

## Wenn ein Mitglied bereits auf dem Server ist

Automatisch wird der Channel beim **Beitritt** erstellt.

Bereits vorhandene Mitglieder können selbst:

```text
/privatchat
```

verwenden. Der Bot prüft vorher, ob bereits ein privater Channel existiert.

## Beim Verlassen des Servers

Mit:

```env
DELETE_PRIVATE_CHANNEL_ON_LEAVE=true
```

wird der private Channel automatisch gelöscht, sobald das Mitglied den Server
verlässt. Dabei wird auch der lokale KI-Gesprächsverlauf dieses Channels
entfernt.

Wenn du die Channels lieber behalten möchtest:

```env
DELETE_PRIVATE_CHANNEL_ON_LEAVE=false
```

## Datenschutz-Hinweis

Die Discord-Channel-Berechtigungen sorgen dafür, dass andere normale Mitglieder
den privaten Channel nicht sehen können. Discord-Serverinhaber und Personen
mit weitreichenden Administratorrechten können technisch trotzdem Zugriff auf
Serverinhalte haben.


---

# Bot-Name

Diese Version ist standardmäßig auf folgenden Namen eingestellt:

```env
BOT_NAME=KI-Catnip
```

Nach dem Umbenennen des Discord-Bot-Accounts im Developer Portal kannst du ihn
im Chat beispielsweise so ansprechen:

```text
@KI-Catnip Wie funktioniert der Viper?
```

Der Bot erkennt im KI-Kontext außerdem `KI-Catnip` und `Catnip` als seinen Namen.
Die eigentliche Discord-Erwähnung funktioniert über den Discord-Account des Bots.


---

# Begrüßung und Rückkehr

KI-Catnip begrüßt neue Mitglieder in ihrem automatisch erstellten privaten
FFXIV-Channel mit einer persönlichen Nachricht und fragt:

> Wie kann ich dir bei FINAL FANTASY XIV helfen?

Zusätzlich erkennt KI-Catnip eine Rückkehr nach längerer Inaktivität. Da
Discord Bots nicht erkennen können, dass ein Textchannel lediglich geöffnet
wurde, erscheint die Rückkehr-Begrüßung bei der **ersten neuen Nachricht**
des Channel-Besitzers nach der Pause.

Standard:

```env
RETURN_GREETING_ENABLED=true
RETURN_GREETING_HOURS=12
```

Nach mindestens 12 Stunden Pause erscheint beispielsweise:

> 🐱 Willkommen zurück, Dominik! Wie kann ich dir heute bei FINAL FANTASY XIV helfen?

Die Zeit kann frei angepasst werden, z. B. auf 24 Stunden:

```env
RETURN_GREETING_HOURS=24
```

Mit `RETURN_GREETING_ENABLED=false` lässt sich die Rückkehr-Begrüßung komplett
abschalten.


---

# Einmalige Befehlsübersicht bei der Channel-Erstellung

Sobald KI-Catnip einen privaten FFXIV-Channel für ein Mitglied erstellt,
erscheint dort einmalig eine ausführliche Befehlsübersicht.

Enthalten sind:

- `@KI-Catnip` – freie FFXIV-Fragen
- `/ffxiv` – allgemeine FFXIV-Frage
- `/lore` – Lore, Figuren und Orte
- `/job` – Jobs, Skills und Spielweise
- `/kampf` – Dungeon-, Trial- und Raidmechaniken
- `/quest` – Quest-Hilfe
- `/begriff` – Begriffe und Abkürzungen
- `/einsteiger` – einfache Erklärungen
- `/charakter` – Spielercharakter suchen
- `/mount` – Mount-Herkunft
- `/minion` – Minion-Herkunft
- `/markt` – Marktbrettdaten
- `/pdf` – FFXIV-Guide als PDF
- `/privatchat` – privaten Channel nachträglich erstellen
- `/reset` – Gesprächskontext löschen
- `/botinfo` – Funktionsübersicht

Die ausführliche Liste erscheint nur bei der erstmaligen Channel-Erstellung.
Die Rückkehr-Begrüßung bleibt bewusst kurz.


---

# Event-Funktionen

KI-Catnip kann jetzt wieder FFXIV-inspirierte Gildenevents und Spielleiter-Inhalte
erstellen.

## `/quiz`

Erstellt ein komplettes FFXIV-Quiz mit:
- 5 bis 30 Fragen
- vier Antwortmöglichkeiten pro Frage
- genau einer richtigen Antwort
- separater Spielleiter-Lösung
- Punktewertung
- optionaler Stichfrage

Beispiel:

```text
/quiz thema: Heavensward fragen: 15 schwierigkeit: Schwer
```

## `/event`

Plant ein vollständiges Gildenevent mit Intro, Ablauf, Rätsel/Quizmoment,
Gruppenentscheidung, Finale und Spielleiterhinweisen.

```text
/event thema: Der Schwarze Eid spieler: 8 dauer: 90 Minuten
```

## `/boss`

Erstellt einen mehrphasigen FFXIV-inspirierten Event-Bosskampf.

```text
/boss thema: Diabellstar im verfluchten Gridania phasen: 4 schwierigkeit: Brutal
```

## `/raetsel`

Erstellt ein einzelnes FFXIV-Rätsel mit drei Hinweisen und einer getrennten
Spielleiter-Lösung.

```text
/raetsel thema: Die Ruinen unter Gridania schwierigkeit: Extrem
```

## Lore-Regel

Offizielle FFXIV-Fakten werden von frei erfundener Event-Lore getrennt.
Quizfragen sollen nur auf ausreichend sicheren offiziellen Fakten basieren.

Die Event-Inhalte können anschließend auch als Grundlage für `/pdf` verwendet
werden.


---

# Event-Administratoren

Die Event-Funktionen `/quiz`, `/event`, `/boss` und `/raetsel` sind nur für
freigeschaltete Discord-User verfügbar.

Freigeschaltet sind die im Code hinterlegten Discord-User-IDs sowie automatisch
der bestehende Budget-Administrator.

Wenn einer dieser Nutzer später dem Server beitritt, erkennt KI-Catnip die
User-ID und weist automatisch die Rolle:

`KI-Catnip Event-Admin`

zu.

Die Rolle dient für KI-Catnip-interne Event-Rechte und Zugriff auf die von
KI-Catnip verwalteten privaten Channels. Sie erhält **bewusst nicht automatisch
die globale Discord-Berechtigung Administrator**, da diese vollständige Kontrolle
über den gesamten Discord-Server geben würde.

Wenn du diesen Personen zusätzlich echte Discord-Administratorrechte für den
gesamten Server geben möchtest, kannst du der Rolle später manuell in Discord
die Berechtigung `Administrator` geben. Das sollte nur bei vollständig
vertrauenswürdigen Personen erfolgen.

Der Bot benötigt `Manage Roles`, damit er die Event-Admin-Rolle automatisch
zuweisen kann. Seine eigene Bot-Rolle muss in der Discord-Rollenhierarchie
oberhalb von `KI-Catnip Event-Admin` stehen.

Mit `/eventadmins` können freigeschaltete Event-Admins die aktuelle Whitelist
prüfen. Nutzer, die noch nicht beigetreten sind, werden anhand ihrer ID als
noch nicht auf dem Server angezeigt.
