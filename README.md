# Warenwirtschaft Extravaganza

Kleines Python-basiertes Warenwirtschaftssystem fuer Material, Nutzer, Orte/Baustellen und Verleih.

## Starten

```bash
python3 app.py
```

Danach im Browser oeffnen:

```text
http://127.0.0.1:8000
```

Wenn Port `8000` schon belegt ist, sucht die App automatisch den naechsten freien Port.
Beim Start zeigt die App ausserdem eine Handy-Adresse fuer das lokale Netzwerk an, zum
Beispiel:

```text
http://192.168.178.42:8000/mobile
```

Fuer den Live-Scanner mit der Handy-Kamera muss die App per HTTPS laufen:

```bash
python3 app.py --https
```

Dann die angezeigte `https://.../mobile` Adresse auf dem Handy oeffnen. Beim ersten
Start erzeugt die App ein lokales Zertifikat unter `https_zertifikate/`. Wenn der
Handy-Browser dazu eine Warnung zeigt, die Verbindung fuer dieses lokale Geraet
akzeptieren.

Wenn die Adresse ohne Port funktionieren soll, die App auf dem Standard-HTTPS-Port starten:

```bash
python3 app.py --https --port 443
```

Dann reicht im Browser:

```text
https://warenwirtschaft.test/
```

Das Handy muss dafuer im gleichen WLAN sein. Wenn die App nur auf dem Rechner selbst
erreichbar sein soll:

```bash
python3 app.py --host 127.0.0.1
```

Hinweis: Barcode-Eingabe per Feld oder Bluetooth-Scanner funktioniert auch ohne HTTPS
ueber die Handy-Ansicht. Der direkte Live-Zugriff auf die Handy-Kamera verlangt HTTPS
oder localhost.

## Funktionen

- Material mit Art, Kategorie, Besitzer, Bestimmungsort und Anzahl einbuchen
- Fuer neue Artikel automatisch einen Barcode erzeugen
- Fuer neue Nutzer automatisch einen scanbaren Code erzeugen
- Fuer neue Orte bzw. Baustellen automatisch einen scanbaren Code erzeugen
- Einzelne oder alle Barcode-Labels drucken
- Einzelne oder alle Nutzer-Labels drucken
- Einzelne oder alle Ort-Labels drucken
- Material aus dem frei verfuegbaren Bestand ausbuchen
- Material per Barcode-Scanner ein- oder ausbuchen
- Nutzer anlegen
- Orte bzw. Baustellen anlegen
- Material per Dropdown an Nutzer und Ort/Baustelle verleihen oder Verbrauchsmaterial ausgeben
- Material, Nutzer und Ort per Code in beliebiger Reihenfolge scannen und danach verleihen oder ausgeben
- Mehrere Materialcodes in einem Stapel scannen und gemeinsam einbuchen, ausbuchen, verleihen oder ausgeben
- Material per interner oder angeschlossener Webcam scannen und danach Verleih, Einbuchen oder Ausbuchen waehlen
- Kamera-Scanner mit Browser-BarcodeDetector oder lokalem Code-128-Fallback fuer App-Labels
- Handy-Ansicht unter `/mobile` mit Bestandssuche, Scanner, Buchungen, Verleih, Rueckgaben und Stammdaten
- Nutzer und Orte/Baustellen direkt im Verleih-Wizard neu anlegen
- Aktive Ausleihen zurueckbuchen
- Materialliste als CSV exportieren

Die Daten werden lokal in `warenwirtschaft.sqlite3` gespeichert. Beim Start prueft die App
die Datenbank kurz auf Integritaet und legt hoechstens einmal pro Tag eine konsistente
Sicherung unter `datenbank_backups/` an. Solange die App laeuft, erstellt sie danach
automatisch alle 24 Stunden eine weitere Sicherung. Die letzten 30 Sicherungen bleiben
erhalten.

Das Intervall kann beim Start angepasst werden:

```bash
python3 app.py --backup-interval-hours 6
```

Damit wird alle 6 Stunden gesichert. Fuer Wartungsfaelle lassen sich die laufenden
Sicherungen mit `--no-scheduled-backups` abschalten; die Start-Sicherung bleibt aktiv.

Die SQLite-Datenbank laeuft im WAL-Modus mit Foreign-Key-Pruefung, Busy-Timeout und
atomaren Schreibtransaktionen. Dadurch koennen Lesezugriffe weiterlaufen, waehrend
Buchungen gespeichert werden, und parallele Scanner- oder Browseraktionen ueberschreiben
keine Bestaende.

Fuer groessere Datenmengen legt die App zusaetzliche Indexe fuer Materialsuche,
Sortierung, Ausleihen und Buchungsverlauf an. Trotzdem sollte die Datenbankdatei
regelmaessig extern mitgesichert werden, besonders wenn sie auf einem Arbeitsgeraet liegt.

## Scanner

Die Scanner-Felder erwarten normale Tastatureingaben. Die meisten USB- oder Bluetooth-Scanner funktionieren damit direkt, wenn sie nach dem Scan ein Enter senden. Materialcodes beginnen mit `WX`, Nutzer-Codes mit `WU`, Ort-Codes mit `WO`.

Im Scanner-Bereich kann ausserdem eine interne oder angeschlossene Webcam ausgewaehlt werden. Wenn der Browser die Barcode-Erkennung fuer Code 128 unterstuetzt, oeffnet ein erkannter Material-Barcode automatisch eine Auswahl fuer Verleih, Einbuchen oder Ausbuchen.

Im Stapel-Scan koennen mehrere Materialcodes nacheinander gescannt werden. Wiederholte Scans desselben Materials erhoehen die Menge. Beim Verleih werden zusaetzlich Nutzer-Code und Ort-Code gescannt; ob zuerst Material, Nutzer oder Ort gescannt wird, ist egal. Ort bzw. Baustelle kann alternativ weiterhin einmal fuer den Stapel gewaehlt werden.
