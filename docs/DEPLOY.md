# Deployment-Anleitung: GAEB → Easyjob

> Ziel: Die App läuft in einem Docker-Container hinter Caddy (HTTPS).  
> Updates werden mit zwei Befehlen eingespielt, gelernte Mappings bleiben dabei immer erhalten.

---

## 1. Git + GitHub einrichten

Git ist ein **Versionsverwaltungs-Werkzeug** — jede Änderung am Code wird gespeichert, du kannst
jederzeit zurückgehen und Updates mit einem Befehl auf den Server übertragen.

**GitHub** ist die Plattform im Internet, auf der dein Code privat gespeichert wird.
Der Ablauf ist: `dein PC → GitHub (privat) → Server`.

---

### 1.1 GitHub-Account anlegen

Gehe auf [github.com](https://github.com) und lege einen kostenlosen Account an.
Private Repositories (nur du kannst sie sehen) sind kostenlos.

---

### 1.2 SSH-Schlüssel – einmalige Einrichtung

SSH-Schlüssel sind wie ein digitales Türschloss: du erzeugst ein Schlüsselpaar,
der **öffentliche Schlüssel** kommt zu GitHub, der **private Schlüssel** bleibt auf deinem Rechner.
Du musst danach nie wieder ein Passwort eingeben.

**Das machst du auf JEDEM Rechner der auf GitHub zugreifen soll**
(einmal auf deinem Entwicklungs-PC, einmal auf dem Server):

```bash
# 1. Schlüssel erzeugen (Enter drücken bei allen Fragen, kein Passwort nötig)
ssh-keygen -t ed25519 -C "deine@email.de"

# 2. Öffentlichen Schlüssel anzeigen und kopieren
cat ~/.ssh/id_ed25519.pub
```

Den angezeigten Text (beginnt mit `ssh-ed25519 ...`) kopieren, dann:

1. Auf GitHub: Oben rechts auf dein Profilbild klicken
2. **Settings → SSH and GPG keys → New SSH key**
3. Titel: z.B. `Entwicklungs-PC` oder `Server`
4. Den kopierten Schlüssel einfügen → **Add SSH key**

**Verbindung testen:**
```bash
ssh -T git@github.com
# Erwartet: "Hi dein-username! You've successfully authenticated..."
```

---

### 1.3 Privates Repository auf GitHub anlegen

1. Auf GitHub: **+** oben rechts → **New repository**
2. Name: z.B. `gaeb-ej`
3. **Private** auswählen ← wichtig!
4. **Kein** README, **keine** .gitignore anlegen (kommt von deinem PC)
5. **Create repository**

GitHub zeigt dir danach eine URL, die so aussieht:
```
git@github.com:dein-username/gaeb-ej.git
```
Diese URL brauchst du gleich.

---

### 1.4 .gitignore anlegen (was NICHT zu GitHub soll)

Erstelle im Projektordner eine Datei `.gitignore`:

```
__pycache__/
*.pyc
*.pyo
.env
data/
```

> **Warum:** `.env` enthält Passwörter — darf niemals ins Repository.
> `data/` enthält die Datenbank mit gelernten Mappings — ist laufzeitspezifisch.

---

### 1.5 Projekt das erste Mal zu GitHub hochladen

```bash
# Im Projektordner (c:\Users\ngrossmann\Documents\python\d84)
git init                          # Repository initialisieren
git add .                         # Alle Dateien vormerken
git commit -m "Initial commit"   # Ersten Snapshot speichern

# Mit GitHub verbinden (URL von Schritt 1.3 einsetzen)
git remote add origin git@github.com:dein-username/gaeb-ej.git

# Hochladen
git push -u origin main
```

Danach ist der Code auf GitHub — nur für dich sichtbar.

---

### 1.6 Auf dem Server: Repository klonen

Zuerst SSH-Schlüssel auf dem Server einrichten (Schritt 1.2 wiederholen für den Server).
Dann:


### 1.7 Wenn du Änderungen machst (normaler Workflow)

**Auf deinem Entwicklungs-PC** nach jeder Änderung:

```bash
git add .
git commit -m "Kurze Beschreibung was du geändert hast"
git push
```

**Auf dem Server** zum Einspielen:

```bash
cd ~/gaeb-ej
git pull
docker compose up -d --build
```

---

### Die wichtigsten Git-Befehle auf einen Blick

| Befehl | Was er macht |
|--------|-------------|
| `git init` | Neues Repository anlegen (einmalig) |
| `git add .` | Alle Änderungen vormerken |
| `git commit -m "..."` | Snapshot mit Nachricht speichern |
| `git push` | Zu GitHub hochladen |
| `git pull` | Neueste Version vom Server holen |
| `git status` | Zeigt was sich geändert hat |
| `git log --oneline -10` | Letzte 10 Änderungen anzeigen |

---

## 2. Ordnerstruktur auf dem Server

```
~/gaeb-ej/              ← Haupt-Ordner (Git-Repository)
├── data/                  ← Persistente Daten (KEIN Git, bleiben bei Updates erhalten)
│   ├── mappings_gui.json  ← Gelernte Zuordnungen (wird zur Laufzeit verändert)
│   ├── mappings.json      ← Basis-Mappings
│   └── infos/
│       ├── artikel.json
│       └── personal.json
├── .env                   ← Zugangsdaten (KEIN Git, nie committen!)
├── docker-compose.yml
├── Dockerfile
└── ... (restlicher Code)
```

> **Faustregel:** Alles in `data/` und `.env` liegt **außerhalb von Git** und überlebt jeden Update.

---

## 3. Erstmalige Einrichtung

### 3.1 Repository klonen

```bash
# Dorthin wechseln wo deine anderen docker-compose.yml Dateien liegen
cd ~   # oder z.B. cd ~/docker falls du sie dort sammelst

# Repository herunterladen – du bekommst einen Ordner "gaeb-ej"
git clone https://github.com/DEIN-REPO/gaeb-ej.git
cd gaeb-ej
```

### 3.2 Persistente Daten anlegen

```bash
# data/-Ordner erstellen (hier liegt die SQLite-DB + JSON-Backups)
mkdir -p data/infos

# JSON-Dateien als Ausgangsdaten hineinkopieren
# Beim ersten Start migriert die App automatisch JSON → gaeb.db
cp mappings_gui.json data/
cp mappings.json     data/
cp infos/artikel.json  data/infos/
cp infos/personal.json data/infos/
```

> **Hinweis:** Die App legt `data/gaeb.db` automatisch an und befüllt sie  
> beim ersten Start aus den JSON-Dateien. Danach ist die DB die Quelle der Wahrheit.  
> Die JSON-Dateien bleiben als Backup erhalten.

### 3.3 Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
nano .env   # oder: vim .env
```

Die `.env`-Datei befüllen:

```env
# Easyjob API
EJ_BASE_URL=http://192.168.1.xxx:8008    # IP statt Hostname empfohlen (siehe Netzwerk-Hinweis)
EJ_USERNAME=dein_benutzername
EJ_PASSWORD=dein_passwort

# Easyjob Datenbank (für D83-Import)
EJ_DB_SERVER=192.168.1.xxx\SQLEXPRESS
EJ_DB_NAME=easyjob
EJ_DB_UID=sa
EJ_DB_PWD=dein_db_passwort

# Session-Sicherheit – langen Zufallsstring erzeugen:
# python3 -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET=hier-langen-zufallsstring-eintragen
```

> **Netzwerk-Hinweis:** Docker-Container können Windows-Hostnamen wie `EASYJOB-TEST` oft nicht auflösen.  
> Trag stattdessen die **IP-Adresse** des EJ-Servers ein.  
> IP herausfinden (auf dem EJ-Server): `ipconfig`

---

## 4. Docker Compose

Die `docker-compose.yml` ist schon im Projekt. Hier nochmal mit Erklärungen:

```yaml
services:
  gaeb-ej:
    build: .                      # Image lokal bauen (aus dem Dockerfile)
    container_name: gaeb-ej
    restart: unless-stopped
    environment:
      - TZ=Europe/Berlin
    env_file: .env                # Zugangsdaten aus der .env laden
    volumes:
      # data/ auf dem Host → App-Verzeichnis im Container
      # Dadurch bleiben Mappings bei Updates erhalten
      - ./data/mappings_gui.json:/app/mappings_gui.json
      - ./data/mappings.json:/app/mappings.json
      - ./data/infos:/app/infos
    networks:
      - proxy                     # Damit Caddy den Container erreicht

networks:
  proxy:
    external: true                # Das Netzwerk existiert bereits (dein Caddy-Netzwerk)
```

---

## 5. Caddy konfigurieren

In deiner `Caddyfile` (oder dem entsprechenden Caddy-Config-Block) hinzufügen:

```caddyfile
gaeb.deine-domain.de {
    reverse_proxy gaeb-ej:8000
}
```

> Caddy und der Container müssen im selben Docker-Netzwerk (`proxy`) sein – das ist durch `networks: proxy: external: true` bereits sichergestellt.

---

## 6. Starten

```bash
# Container bauen und starten
docker compose up -d --build

# Logs anschauen (Strg+C zum Beenden)
docker compose logs -f

# Prüfen ob der Container läuft
docker compose ps
```

Die App ist jetzt unter `https://gaeb.deine-domain.de` erreichbar.

---

## 7. Updates einspielen

Das ist der normale Ablauf wenn es eine neue Version gibt:

```bash
cd ~/gaeb-ej

# 1. Neuesten Code holen
git pull

# 2. Container neu bauen und starten
docker compose up -d --build
```

Das war's. Die `data/`-Dateien werden nicht angefasst – alle gelernten Zuordnungen bleiben erhalten.

### Was passiert dabei im Hintergrund?

1. `git pull` holt die Änderungen (neue Python-Dateien, Templates, etc.)
2. `docker compose up -d --build` baut ein neues Image mit dem neuen Code
3. Der alte Container wird gestoppt, der neue gestartet
4. Die Volumes (`data/`) bleiben unangetastet

> **Kurze Downtime:** Während des Neustarts (~10–30 Sek.) ist die App kurz nicht erreichbar.  
> Für ein internes Tool ist das in der Regel kein Problem.

---

## 8. Nützliche Befehle im Alltag

```bash
# Container-Status
docker compose ps

# Live-Logs (letzte 50 Zeilen + weiter mitschauen)
docker compose logs -f --tail=50

# Container neu starten (ohne neu zu bauen – z.B. nach .env-Änderung)
docker compose restart

# Komplett stoppen
docker compose down

# Was hat sich im letzten Update geändert?
git log --oneline -10
```

---

## 9. Troubleshooting

### Container startet nicht
```bash
docker compose logs gaeb-ej
```
Meistens steht der Fehler direkt in den Logs.

### EJ-Verbindung schlägt fehl
- IP statt Hostname in `.env` eintragen (siehe Abschnitt 3.3)
- Prüfen ob der EJ-Server vom Docker-Host aus erreichbar ist: `curl http://192.168.1.xxx:8008`

### Mappings wurden versehentlich gelöscht
Die Dateien in `data/` liegen direkt auf dem Server – einfach ein Backup einspielen oder aus Git die Original-Version holen:
```bash
git show HEAD:mappings.json > data/mappings.json
```

### Nach git pull gibt es Konflikte
Das passiert wenn du eine Datei lokal geändert hast, die auch im Update geändert wurde.  
Da `.env` und `data/` in `.gitignore` stehen, betrifft das fast nie diese Dateien.  
Im Zweifelsfall:
```bash
git status          # zeigt welche Dateien betroffen sind
git diff            # zeigt die konkreten Unterschiede
```
