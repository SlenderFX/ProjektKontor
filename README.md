# ProjektKontor

ProjektKontor ist eine selbst gehostete Projektmanagement-Anwendung für den
Wirtschaftsunterricht an Berufsschulen. Das MVP bildet Klassen, verwaltete
Schülerzugänge, Projektteams, Aufgaben, Freigaben, ein synchronisiertes
Kanban-Board, eine Gantt-Übersicht sowie PDF-Projektberichte ab. Das zentrale
Dashboard ergänzt ein projektübergreifendes Portfolio, Kalender, Meilensteine,
Aufgabenabhängigkeiten, Auslastung, globale Suche und lokal gespeicherte
Aufgabenfilter. Teamleitungen erhalten eine eigene Verteilansicht;
Fristverlängerungen, moderierbare Kommentare und projektbezogene
Schülerorganisation sind im Aufgabenablauf integriert.

## Lokal starten

Voraussetzungen: Python 3.11 oder 3.12.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m projektkontor
```

Anschließend `http://127.0.0.1:8080` öffnen. Die Anmeldung unterscheidet klar
zwischen Schüler, Lehrkraft und Admin. Das bestehende ursprüngliche
Lehrkraftkonto bleibt ein Lehrkraftkonto. Zusätzlich wird einmalig ein
persönliches Administrationskonto eingerichtet. Eine Selbstregistrierung gibt
es nicht: Der Admin legt Lehrkraftkonten an; jede Lehrkraft verwaltet danach
ihre eigenen Klassen und Schülerzugänge.

Der Adminbereich enthält außerdem „Bestellungen & Lizenzen“. Dort werden Pilot-,
Einzel-, Fachbereichs- und Schullizenzen mit Preis, Zahlungsstatus, Laufzeit,
Platzanzahl und zugeordneten Lehrkraftkonten verwaltet. Eine Standardlizenz kann
bereits beim Anlegen oder Bearbeiten einer Lehrkraft erzeugt beziehungsweise
ausgewählt werden; die Zuordnung bleibt zusätzlich innerhalb der Lizenz
bearbeitbar. Für jede Lehrkraftanmeldung ist ausnahmslos eine aktive und aktuell
gültige Lizenz erforderlich. Eine fehlende Freigabe, Sperrung oder ein Ablauf
beendet den Zugang, löscht aber keine Unterrichtsdaten.

Rechnungsanschrift und Bestellreferenz können nachträglich an der Lizenz ergänzt
werden. Anschließend erzeugt der Admin eine fortlaufend nummerierte,
unveränderlich gespeicherte PDF-Rechnung im Namen von PRIMEAdvisory. Angaben zum
Rechnungssteller, Besteuerung, Zahlungsziel und Bankverbindung sind über
„Rechnungssteller“ konfigurierbar. Die PDF-Ausgabe ist noch keine strukturierte
XRechnung oder ZUGFeRD-Rechnung.

Für einen schnellen Test ohne Installation kann der von Codex bereitgestellte
Python verwendet werden, sofern die Abhängigkeiten dort vorhanden sind.

## Konfiguration

| Variable | Standard | Zweck |
|---|---|---|
| `PK_HOST` | `127.0.0.1` | Bind-Adresse |
| `PK_PORT` | `8080` | Port |
| `PK_DATA_DIR` | `./data` | Datenbank, Uploads und Berichte |
| `PK_SECRET_KEY` | automatisch lokal erzeugt | Sitzungen und Zugangscode-Verschlüsselung |
| `PK_ADMIN_SETUP_TOKEN` | lokal optional, auf dem Server erforderlich | Einmaliger privater Code, der die Einrichtung des ersten Administrationskontos schützt |
| `PK_MAX_UPLOAD_MB` | `25` | Maximale Dateigröße |
| `PK_COOKIE_SECURE` | `0` lokal, auf Server `1` | Sitzungscookie nur über HTTPS |
| `PK_TRUST_PROXY` | `0` lokal, in Docker `1` | Weitergeleitete Clientadresse ausschließlich hinter einem vertrauenswürdigen Reverse Proxy auswerten |
| `PK_PRIVACY_CONTROLLER_*` | PRIMEAdvisory | Verantwortlicher, Anschrift und Datenschutzkontakt der Installation; bei schulischer Verantwortlichkeit durch die Angaben von Schule oder Schulträger ersetzen |
| `PK_PRIVACY_LEGAL_BASIS` | Hinweis auf die installationsbezogene Festlegung | Von Schule oder Schulträger bestätigte konkrete Rechtsgrundlage ergänzen |
| `PK_CONTACT_RECIPIENT` | private Empfängeradresse | Nur serverseitiges Ziel des Kontaktformulars; wird nicht an den Browser ausgeliefert |
| `PK_SMTP_*` | nicht gesetzt | Mailserver, Port, Benutzername, Passwort, Absender und SSL für den Formularversand |
| `PK_TURNSTILE_SITEKEY`, `PK_TURNSTILE_SECRET` | lokale Testschlüssel | Cloudflare-Turnstile-Schlüssel für die Menschprüfung; produktiv zwingend ersetzen |

Auf einem Server müssen HTTPS, ein vertrauenswürdiger Reverse Proxy,
automatisierte Backups und ein separater Sicherungsort verwendet werden.
`compose.yaml` bindet ProjektKontor dafür an das externe Docker-Netzwerk
`proxy` an und enthält eine tägliche 14-Tage-Backuprotation.

Das öffentliche Kontaktformular schützt den Versand durch Cloudflare Turnstile,
ein Bot-Fangfeld und eine Begrenzung auf fünf Anfragen je IP-Adresse und Stunde.
Der Turnstile-Secret-Key, das SMTP-Passwort und die private Empfängeradresse
gehören ausschließlich in die serverseitige `.env` und niemals in HTML oder
JavaScript. Lokal verwendet ProjektKontor die offiziellen Turnstile-Testschlüssel;
der Mailversand bleibt bis zur SMTP-Konfiguration deaktiviert.

Weitere Dokumente:

- [MVP-Architektur](docs/ARCHITEKTUR.md)
- [Serverbetrieb, Backup und Wiederherstellung](docs/BETRIEB.md)
- [Sicherheitsprüfung und verbleibende Risiken](docs/SICHERHEIT.md)
- [SEO-Vorbereitung für den späteren Marktstart](docs/SEO-STRATEGIE.md)

## Sicherheitsmodell

- Lehrkraftkennwörter werden ausschließlich als Scrypt-Hash gespeichert.
- Der Admin verwaltet Lehrkraftkonten, erhält aber keinen pauschalen Zugriff
  auf deren Klassen, Schüler oder Projekte.
- Neue Lehrkraft-Benutzernamen beginnen verbindlich mit `lehrkraft_`. Ein
  erzeugtes Initialkennwort muss bei der ersten Anmeldung durch ein privates
  Kennwort ersetzt werden; anschließend kann der Admin es nicht zurücksetzen.
- Eine Lehrkraft kann mit Telefonnummer und Anliegen Hilfe anfordern. Erst ein
  von ihr telefonisch übermittelter Unterstützungscode öffnet dem Admin einen
  auf zwei Stunden begrenzten Zugriff auf ihre Unterrichtsdaten. Die Lehrkraft
  kann diesen Zugriff jederzeit sofort widerrufen.
- Schüler-Zugangscodes sind wie vereinbart durch die Lehrkraft auslesbar und
  werden deshalb verschlüsselt gespeichert.
- Schüler-Zugangscodes dürfen nicht für andere Dienste wiederverwendet werden.
- Zugriffe werden über serverseitige, zeitlich begrenzte Sitzungen geschützt.
- Uploads sind auf PDF, PNG, JPEG und WebP bis 25 MB beschränkt.
- Teambezogene Inhalte werden zusätzlich zur Oberfläche serverseitig gegen
  Zugriffe aus anderen Teams abgeschirmt.
- Sicherheitsheader, CSRF-Schutz, Anmeldebegrenzung und gehärtete Container
  sind in der Serverkonfiguration enthalten.
- Vor der ersten Sitzung muss jedes Konto die aktuelle Datenschutzinformation
  öffnen und ihre Kenntnisnahme bestätigen. Version und Zeitpunkt werden
  dokumentiert; eine neue Informationsversion kann erneut bestätigt werden.
- Öffentliche Seiten dürfen von Suchmaschinen erfasst werden. API-Antworten,
  Downloads und geschützte Unterrichtsdaten werden mit `noindex` ausgeliefert.

## Vor dem Unterrichtseinsatz

1. Lehrkraft- und Schüleranmeldung testen.
2. Testprojekt mit Teamleitung, Mitglied und unbeteiligtem Schüler durchspielen.
3. Aufgabe zuweisen, bearbeiten, freigeben und einen Fristantrag entscheiden.
4. Bild/PDF hochladen und einen Projektbericht erzeugen.
5. Projekt archivieren und kontrollieren, dass der Bericht erhalten bleibt.
6. Backup erzeugen und mindestens einmal in einer Testumgebung wiederherstellen.

## Offen vor der öffentlichen Bereitstellung

- Für `projektkontor.org` ein eigenes Cloudflare-Turnstile-Widget anlegen und
  `PK_TURNSTILE_SITEKEY` sowie `PK_TURNSTILE_SECRET` ausschließlich in der
  privaten Serverkonfiguration hinterlegen.
- Den externen Mailversand im GMX-Konto freischalten, das SMTP-Passwort nur als
  `PK_SMTP_PASSWORD` in der privaten `.env` hinterlegen und niemals versenden
  oder in den Quellcode aufnehmen.
- Das Kontaktformular anschließend mit einer echten Anfrage prüfen: Eingang an
  der privaten Empfängeradresse, Betreffpräfix `ProjektKontor Anfrage:`,
  Antwortadresse sowie Fehler- und Missbrauchsschutz kontrollieren.
- Die internen Halbzeit- und Abschlussmeldungen eines Pilotzugangs in der
  Lizenzverwaltung bearbeiten. Ein automatischer Versand an Lehrkräfte ist
  bewusst nicht aktiviert, damit Rückmeldungen persönlich abgestimmt werden.
