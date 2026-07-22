# Sicherheitsprüfung ProjektKontor

Stand: 22. Juli 2026

## Umgesetzte Schutzmaßnahmen

- serverseitige Rollen-, Projekt- und Teamprüfung; Inhalte fremder Teams sind
  für gewöhnliche Teammitglieder nicht abrufbar,
- zufällige, ausschließlich gehasht gespeicherte Sitzungsschlüssel sowie
  CSRF-Schutz für schreibende Anfragen,
- zeitliche Sitzungsbegrenzung und nur eine aktive Sitzung je Schülerkonto,
- Begrenzung fehlgeschlagener Anmeldungen nach Konto und Clientadresse,
- verstärkte Scrypt-Ableitung für das Lehrkraftkennwort; bestehende Hashes
  werden nach erfolgreicher Anmeldung automatisch aktualisiert,
- getrennte Schüler-, Lehrkraft- und Adminanmeldung sowie ein durch einen
  privaten Einrichtungscode geschütztes einmaliges Admin-Setup,
- verpflichtender Wechsel des vom Admin erzeugten Lehrkraft-Initialkennworts;
  nach der ersten Anmeldung ist kein Admin-Reset mehr möglich,
- kein pauschaler Adminzugriff auf Unterrichtsdaten: Supportfreigaben benötigen
  einen gehasht gespeicherten, 24 Stunden gültigen Code, laufen nach zwei
  Stunden ab und können von der Lehrkraft sofort widerrufen werden,
- verschlüsselte Speicherung der vereinbarungsgemäß auslesbaren
  Schüler-Zugangscodes,
- Upload-Allowlist, Größenbegrenzung, zufällige Speichernamen, Signaturprüfung,
  sichere Downloadnamen und Prüfung von Bildern auf Defekte und übermäßige
  Auflösung,
- Sicherheitsheader, HTTPS-Konfiguration und gehärtete, nicht privilegierte
  Container,
- restriktive Dateirechte für Datenbank, Datendateien und Backups,
- validierte Projekt-, Aufgaben-, Mitgliedschafts- und Statuszuordnungen.
- dokumentierte Kenntnisnahme der aktuellen Datenschutzinformation vor der
  ersten Sitzung eines Kontos,
- Suchmaschinen dürfen öffentliche Produktinformationen erfassen; API-Daten
  und geschützte Downloads werden nicht indexiert und benötigen zusätzlich die
  fachliche Berechtigung.

## Verbleibende betriebliche Risiken

- PDF-Dateien werden nicht durch einen Virenscanner oder Content Disarm and
  Reconstruction verarbeitet. Sie werden nur berechtigten Personen und als
  Download bereitgestellt.
- Das Backup-Archiv ist dateiseitig geschützt, aber nicht zusätzlich
  verschlüsselt. Der Sicherungsort muss selbst verschlüsselt und zugriffssicher
  sein.
- `PK_SECRET_KEY` wird bewusst nicht in das normale Datenbackup aufgenommen.
  Die `.env`-Datei muss getrennt und sicher gesichert werden.
- Betriebssystem, Reverse Proxy, Docker und Python-Abhängigkeiten benötigen
  weiterhin regelmäßige Sicherheitsupdates.
- Die Prüfung ersetzt keinen externen Penetrationstest und keine schulische
  Datenschutz- oder Freigabeprüfung.
- Bei einem Betrieb im Heimnetz bleiben Routerkonfiguration, Firewall,
  Netzwerksegmentierung und physische Verfügbarkeit in der Verantwortung des
  Betreibers.

## Empfohlener Prüfzyklus

- vor jedem produktiven Einsatz: vollständiger Funktionstest mit Lehrkraft,
  Projektleitung, zwei getrennten Teams und einem unbeteiligten Konto,
- monatlich: verfügbare Sicherheitsupdates und Backup-Erfolg prüfen,
- halbjährlich sowie vor größeren Versionswechseln: erneute Code- und
  Berechtigungsprüfung,
- mindestens einmal pro Halbjahr: Wiederherstellung eines Backups in einer
  getrennten Testumgebung.
