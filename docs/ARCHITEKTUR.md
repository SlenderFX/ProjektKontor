# ProjektKontor – MVP-Architektur

## Ziel

ProjektKontor ist für eine Schule mit mehreren gespeicherten Klassen und in der
Regel einer gleichzeitig arbeitenden Klasse ausgelegt. Die Architektur hält den
Betrieb bewusst überschaubar und vermeidet doppelte Datenhaltung.

## Bausteine

- **WSGI-Webanwendung:** Python 3.12, im Serverbetrieb über Gunicorn
- **Oberfläche:** responsives HTML, CSS und JavaScript ohne Build-Schritt
- **Datenbank:** SQLite im WAL-Modus
- **Dateien:** geschützter Datenordner außerhalb des Webverzeichnisses
- **Excel:** OpenPyXL
- **PDF:** ReportLab
- **HTTPS:** vorgeschalteter Reverse Proxy mit automatischer
  Zertifikatsverwaltung

SQLite genügt für den beschriebenen MVP mit einer gleichzeitig aktiven Klasse.
Bei deutlich höherer Parallelität sollte vor einer Ausweitung auf mehrere
Schulen oder viele gleichzeitig aktive Klassen auf PostgreSQL migriert werden.

## Eine Aufgabe, mehrere Ansichten

Aufgabenliste, „Meine Aufgaben“, Kanban, Gantt, Fortschritt und PDF-Bericht
lesen dieselben Datensätze. Das Verschieben einer Kanban-Karte ändert daher den
Status der Aufgabe selbst. Es gibt keine Board-Kopie.

Mehrteam-Aufgaben bestehen aus einer gemeinsamen Hauptaufgabe und je einem
Teambeitrag. Der Gesamtstand ergibt sich aus den Teambeiträgen.

## Berechtigungsgrenzen

- Admin: Verwaltung der erworbenen Lehrkraftzugänge; kein dauerhafter globaler
  Zugriff auf Klassen-, Schüler- oder Projektdaten
- Lehrkraft: selbstständige Verwaltung der eigenen Klassen, Schülerzugänge und
  zugehörigen Projekte
- Supportzugriff: nur nach Einlösung eines von der Lehrkraft erzeugten Codes,
  höchstens zwei Stunden und jederzeit durch die Lehrkraft widerrufbar
- Gesamtprojektleitung: Vollzugriff innerhalb des jeweiligen Projekts
- Teamleitung: Teamorganisation, Aufgabenanlage und Teamprüfung
- Teammitglied: eigene Teamaufgaben, Ergebnisse, Status, Kommentare, Uploads

Die Lehrkraft kann die selbstständige Schülerorganisation pro Projekt
aktivieren. Schülerseitig angelegte Teams bleiben bis zur Freigabe durch eine
Hauptrolle im Status „vorgeschlagen“. Fristverlängerungen ändern die Aufgabe
erst nach einer Entscheidung der Gesamtprojektleitung oder Lehrkraft.

Eine lernende Gesamtprojektleitung erhält keine Klassen-, Konto-, Backup- oder
Serververwaltung.

## Zugangsdaten

Die Anmeldung trennt Schüler-, Lehrkraft- und Adminzugänge. Das persönliche
Administrationskonto dient ausschließlich der Verwaltung von Lehrkraftzugängen
und wird bei einer neuen Installation durch `PK_ADMIN_SETUP_TOKEN` geschützt
eingerichtet. Neue Lehrkraft-Benutzernamen beginnen mit `lehrkraft_`.
Lehrkraftkonten werden ausschließlich durch den Admin angelegt. Das erzeugte
Initialkennwort wird nur unmittelbar nach Anlage angezeigt und muss bei der
ersten Anmeldung durch ein persönliches Kennwort ersetzt werden. Danach kann
es der Admin nicht zurücksetzen. Lehrkraftkennwörter werden ausschließlich als
Scrypt-Hash gespeichert.

Fordert eine Lehrkraft Hilfe an, werden Telefonnummer und Problembeschreibung
an den privaten Supportkontakt übermittelt. Ein achtstelliger, nur gehasht
gespeicherter Unterstützungscode ist 24 Stunden einlösbar. Erst seine Eingabe
durch den Admin gewährt für höchstens zwei Stunden Zugriff auf die Klassen der
betreffenden Lehrkraft. Die Lehrkraft kann die Freigabe sofort widerrufen.

Schüler-Zugangscodes werden aufgrund der bewussten Produktentscheidung
verschlüsselt und sind nur für die zuständige Lehrkraft sowie während eines
aktiven Supportzugriffs für den Admin auslesbar. Der Schlüssel liegt
außerhalb der Datenbank in `PK_SECRET_KEY` beziehungsweise der lokalen
Schlüsseldatei.

Schülersitzungen enden spätestens sechs Zeitstunden nach der Anmeldung.
Lehrkraftsitzungen enden nach zwölf Stunden. Ein Serverneustart beendet gültige
Sitzungen nicht vorzeitig, weil sie kontrolliert in der Datenbank gespeichert
werden. Pro Schülerkonto ist nur eine Sitzung gleichzeitig zulässig; eine neue
Anmeldung beendet automatisch alle bisherigen Sitzungen dieses Kontos.

## Archivierung

Vor der Bereinigung ist ein PDF-Endbericht erforderlich. Bei der Archivierung
werden Aufgaben, Kommentare, Uploads, Teams, Phasen, Hinweise und Verläufe aus
dem laufenden System entfernt. Sichtbar bleiben die Projekt-Hülle und der
verknüpfte Endbericht. Backups enthalten gelöschte Daten bis zum Ablauf ihrer
14-tägigen Rotation.
