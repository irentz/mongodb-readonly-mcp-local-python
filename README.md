# mongodb-readonly-mcp

Lokaler MCP-Server (stdio) mit **ausschließlich lesendem** Zugriff auf MongoDB.
Gebaut mit dem offiziellen MCP Python SDK (1.x und 2.x) und PyMongo.

## Tools

| Tool | Zweck |
|---|---|
| `list_databases` | Datenbanken auflisten (beachtet Allowlist, System-DBs standardmäßig ausgeblendet) |
| `list_collections` | Collections und Views einer Datenbank |
| `collection_schema` | Feldpfade, BSON-Typen und Vorkommensquote aus einer Stichprobe (`$sample`, max. 500) |
| `collection_indexes` | Indexe einer Collection |
| `find` | Abfrage mit `filter`, `projection`, `sort`, `limit`, `skip` |
| `count` | Dokumente zählen (ohne Filter: schnelle Schätzung) |
| `distinct` | Eindeutige Werte eines Feldes |
| `aggregate` | Aggregation-Pipeline (`$limit` wird automatisch angehängt) |
| `explain` | Query-Plan für `find` oder `aggregate` (`queryPlanner` / `executionStats`) |
| `collection_stats` | Größe, Dokumentanzahl, Indexgrößen |
| `db_stats` | Kennzahlen einer Datenbank |
| `readonly_status` | Zeigt den verbundenen User, seine Rollen und ob er Schreibrechte hat |

Alle Tools sind mit `readOnlyHint=true` annotiert. Filter und Pipelines akzeptieren
MongoDB Extended JSON, z. B. `{"_id": {"$oid": "…"}}` oder
`{"createdAt": {"$gte": {"$date": "2026-01-01T00:00:00Z"}}}`.

## Wie „read-only“ abgesichert ist

1. **Kein Schreibcode:** Der Server ruft nur lesende Treiberfunktionen auf (`find`, `aggregate`,
   `count_documents`, `distinct`, `list_*`, `explain`, `dbStats`, `$collStats`).
   Es gibt kein generisches „run command“-Tool.
2. **Pipeline-Prüfung:** `$out` und `$merge` werden überall abgelehnt – auch verschachtelt in
   `$facet`, `$lookup.pipeline` oder `$unionWith`.
3. **Kein serverseitiges JavaScript:** `$where`, `$function`, `$accumulator` werden abgelehnt
   (abschaltbar mit `MONGODB_ALLOW_JS=true`; `$out`/`$merge` bleiben trotzdem verboten).
4. **Rechteprüfung beim Start:** Über `connectionStatus` wird geprüft, ob der DB-User
   Schreibrechte hat. Standard: Warnung auf stderr. Mit `MONGODB_STRICT_READONLY=true`
   startet der Server nur mit einem nachweislich lesenden User.
5. **Limits:** Dokumentanzahl, Antwortgröße und `maxTimeMS` sind begrenzt.

**Empfehlung:** Zusätzlich immer einen eigenen DB-User mit Rolle `read` verwenden – das ist die
eigentliche Garantie auf Serverseite:

```js
db.getSiblingDB("admin").createUser({
  user: "mcp_readonly",
  pwd: passwordPrompt(),
  roles: [{ role: "read", db: "meineDatenbank" }]
})
```

## Installation

Voraussetzung: Python ≥ 3.10, empfohlen [uv](https://docs.astral.sh/uv/).

```bash
cd ~/PycharmProjects/mongodb-readonly-mcp
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest -q          # Tests (laufen ohne echte MongoDB, via mongomock)
```

## Konfiguration (Umgebungsvariablen)

| Variable | Standard | Bedeutung |
|---|---|---|
| `MONGODB_URI` | `mongodb://localhost:27017` | Connection-String (inkl. User/Passwort, `readPreference` usw.) |
| `MONGODB_ALLOWED_DATABASES` | *(leer = alle)* | Kommagetrennte Allowlist, z. B. `jef,hercules` |
| `MONGODB_INCLUDE_SYSTEM_DBS` | `false` | `admin`, `local`, `config` sichtbar machen |
| `MONGODB_DEFAULT_LIMIT` | `20` | Standard-Limit für `find`/`aggregate` |
| `MONGODB_MAX_DOCUMENTS` | `100` | Obergrenze für Dokumente pro Antwort |
| `MONGODB_MAX_RESULT_CHARS` | `100000` | Obergrenze für die Antwortgröße (Zeichen) |
| `MONGODB_MAX_TIME_MS` | `15000` | Server-Timeout pro Abfrage |
| `MONGODB_ALLOW_JS` | `false` | `$where`/`$function`/`$accumulator` erlauben |
| `MONGODB_STRICT_READONLY` | `false` | Start verweigern, wenn der User Schreibrechte hat |

## Einbinden

### Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "mongodb-readonly-local": {
      "command": "/Users/rentzi/PycharmProjects/mongodb-readonly-mcp/.venv/bin/mongodb-readonly-mcp",
      "env": {
        "MONGODB_URI": "mongodb://mcp_readonly:<passwort>@localhost:27017/?authSource=admin",
        "MONGODB_STRICT_READONLY": "true"
      }
    }
  }
}
```

Alternativ ohne venv: `"command": "uv"`, `"args": ["run", "--directory", "/Users/rentzi/PycharmProjects/mongodb-readonly-mcp", "mongodb-readonly-mcp"]`.

### Claude Code

```bash
claude mcp add mongodb-readonly-local \
  -e MONGODB_URI="mongodb://mcp_readonly:<passwort>@localhost:27017/?authSource=admin" \
  -- /Users/rentzi/PycharmProjects/mongodb-readonly-mcp/.venv/bin/mongodb-readonly-mcp
```

### Testen mit dem MCP Inspector

```bash
npx @modelcontextprotocol/inspector .venv/bin/mongodb-readonly-mcp
```

## Datenschutz

Der Server speichert und protokolliert keine Abfrageergebnisse (Logs nur auf stderr, ohne
Dokumentinhalte). Abfrageergebnisse werden aber an den MCP-Client bzw. das Sprachmodell
übergeben. Für Collections mit personenbezogenen Daten daher mit `MONGODB_ALLOWED_DATABASES`,
einem eingeschränkten DB-User (ggf. auf Views ohne personenbezogene Felder) und Projektionen arbeiten.
Passwörter nicht im Repo ablegen.
