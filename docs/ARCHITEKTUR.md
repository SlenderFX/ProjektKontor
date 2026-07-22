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
- **HTTPS:** Caddy mit automatischer Zertifikatsverwaltung

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

- Geschäftsführung: globaler Zugriff und globale Verwaltung
- Gesamtprojektleitung: Vollzugriff innerhalb des jeweiligen Projekts
- Teamleitung: Teamorganisation, Aufgabenanlage und Teamprüfung
- Teammitglied: eigene Teamaufgaben, Ergebnisse, Status, Kommentare, Uploads

Die Geschäftsführung kann die selbstständige Schülerorganisation pro Projekt
aktivieren. Schülerseitig angelegte Teams bleiben bis zur Freigabe durch eine
Hauptrolle im Status „vorgeschlagen“. Fristverlängerungen ändern die Aufgabe
erst nach einer Entscheidung der Gesamtprojektleitung oder Geschäftsführung.

Eine lernende Gesamtprojektleitung erhält keine Klassen-, Konto-, Backup- oder
Serververwaltung.

## Zugangsdaten

Das Lehrkraftkennwort wird nicht auslesbar als Scrypt-Hash gespeichert.
Schüler-Zugangscodes werden aufgrund der bewussten Produktentscheidung
verschlüsselt und sind für die Geschäftsführung auslesbar. Der Schlüssel liegt
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
