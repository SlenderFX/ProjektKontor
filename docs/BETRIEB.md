# ProjektKontor betreiben

## Empfohlene Adresse

Eine Subdomain der Schule ist sinnvoll, beispielsweise
`projektkontor.org`. Der DNS-Eintrag muss auf den gemieteten Server
zeigen. Der vorgeschaltete Reverse Proxy übernimmt HTTPS und die automatische
Erneuerung des TLS-Zertifikats.

## Installation mit Docker Compose

```bash
cp .env.example .env
# Domain, Backup-Pfad, Geheimschlüssel und Admin-Einrichtungscode eintragen
docker compose up -d --build
```

Die Datei `.env` enthält den Schlüssel für die verschlüsselten Zugangscodes.
Sie muss ausschließlich für den Serverbenutzer lesbar sein (`chmod 600 .env`)
und getrennt vom normalen ProjektKontor-Backup sicher verwahrt werden. Ohne
diesen Schlüssel können die Zugangscodes nach einer Wiederherstellung nicht
mehr ausgelesen werden.

`PK_ADMIN_SETUP_TOKEN` schützt ausschließlich die einmalige Anlage des ersten
Administrationskontos. Verwenden Sie dafür einen langen Zufallswert, bewahren
Sie ihn privat auf und geben Sie ihn nur in der Admin-Ersteinrichtung ein. Das
eigentliche Admin-Kennwort wird dabei von Ihnen festgelegt und niemals im
Quellcode hinterlegt. Das bisherige Lehrkraftkonto bleibt davon unberührt.

Vor der produktiven Freigabe müssen in `.env` außerdem Name, Anschrift und
Kontakt des datenschutzrechtlich Verantwortlichen, der Kontakt zur
datenschutzbeauftragten Person sowie die schulisch bestätigte Rechtsgrundlage
eingetragen werden. Die mitgelieferten Platzhalter sind für einen
Produktivbetrieb nicht ausreichend.

Auf der Firewall dürfen öffentlich nur TCP 80 und 443 freigegeben sein. Der
Anwendungsport 8080 und die Docker-Dienste werden nicht direkt veröffentlicht.
SSH sollte ausschließlich per Schlüssel und möglichst nur über ein VPN oder
einen festen Administrationszugang erreichbar sein.

Der Backup-Pfad sollte auf einem getrennten Speichersystem oder wenigstens
einem separat gesicherten Datenträger liegen. Ein Verzeichnis auf derselben
Serverplatte schützt nicht vor einem vollständigen Serverausfall.

## Backup

Der Backup-Dienst erzeugt täglich ein komprimiertes Archiv aus:

- konsistenter SQLite-Sicherung,
- Original-Uploads,
- PDF-Projektberichten.

Backups werden 14 Tage aufbewahrt. Monatlich sollte ein Archiv testweise in
eine getrennte Umgebung zurückgespielt werden.

Manuelle Sicherung:

```bash
docker compose exec app python -m projektkontor.backup
```

## Wiederherstellung

1. ProjektKontor stoppen.
2. Sicherungsarchiv in einen leeren Arbeitsordner entpacken.
3. Datenbank, `uploads/` und `reports/` in das Datenvolume zurückspielen.
4. Besitzer auf UID `10001` setzen.
5. Anwendung starten und Login, Projektansicht, Upload und Bericht prüfen.

## Updates

Vor jedem Update:

1. Backup erzeugen und Dateigröße prüfen.
2. Neues Image bauen.
3. Anwendung starten und Healthcheck abwarten.
4. Kernablauf in einem Testprojekt prüfen.

Größere Versionswechsel sollten nicht unbeaufsichtigt installiert werden.

## Abnahme vor der Freigabe

- HTTPS und `PK_COOKIE_SECURE=1` sind aktiv.
- Der Anwendungsport 8080 ist von außen nicht erreichbar.
- `.env` besitzt restriktive Dateirechte und wurde getrennt gesichert.
- Betriebssystem, Docker-Images und Python-Abhängigkeiten sind aktuell.
- Die Datenschutzinformation nennt keine Platzhalter mehr und wurde durch die
  verantwortliche Schule beziehungsweise den Schulträger freigegeben.
- Öffentliche Produktinformationen sind indexierbar; `/api/` und geschützte
  Downloads tragen `noindex` und sind ohne Anmeldung nicht zugänglich.
- Lehrkraft, Gesamtprojektleitung, Teamleitung, Teammitglied und unbeteiligter
  Schüler wurden mit getrennten Testkonten geprüft.
- Fremde Projekte lassen sich als Schüler nicht öffnen.
- Uploadgrenze und erlaubte Dateitypen wurden geprüft.
- Ein vollständiger Projektbericht wurde geöffnet und kontrolliert.
- Archivierung wurde erst nach Berichtserstellung zugelassen.
- Automatische Sicherung und Wiederherstellung wurden praktisch getestet.
- Auftragsverarbeitung, Löschfristen und schulische Datenschutzinformation sind
  organisatorisch freigegeben.

## Datenschutz-Hinweise

Vor einem produktiven Schuleinsatz müssen Verantwortlichkeit, Rechtsgrundlage,
Auftragsverarbeitung mit dem Hoster, Löschfristen, Datenschutzinformation und
technisch-organisatorische Maßnahmen schulisch geprüft und dokumentiert sein.
ProjektKontor ersetzt diese organisatorische Freigabe nicht.
