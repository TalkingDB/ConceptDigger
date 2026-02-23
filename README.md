# Concept Digger

A REST API service that traverses the DBpedia knowledge graph to extract entities and their surface-text synonyms, organised by category — primarily used to generate training data for NER (Named Entity Recognition) models.

## How it works

1. Accepts one or more DBpedia category names as input (e.g. `Waterproofing`, `Machine_learning`)
2. Traverses the category hierarchy in DBpedia via live SPARQL queries up to a configurable depth
3. For each entity found, collects all known surface-text synonyms from multiple sources — labels, redirects, disambiguations, aliases and alternate names
4. Returns structured JSON grouped by entity, and persists results to a local JSON store for future retrieval without re-querying

## Running the application

Requires Docker. Run the bootstrap script:

```bash
chmod +x start.sh
./start.sh
```

The API will be available at `http://localhost:5007`.  
Interactive API documentation (Swagger UI) is at `http://localhost:5007/docs`.

## Project structure

```
conceptdigger/
├── app/
│   ├── main.py       # FastAPI application, endpoints, SPARQL logic
│   └── store.py      # JSON file persistence layer
├── data/
│   └── digger_store.json   # initially it might not be present, generated at runtime, persists dig results
├── Dockerfile
├── docker-compose.yml
├── LICENCE-AGPL-3.0.txt
├── requirements.txt
└── start.sh          # bootstrap script — builds and starts the container
```

## Key endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/dig/grouped/multi` | Dig multiple categories, returns grouped entity+synonym list |
| `POST` | `/dig/grouped` | Dig a single category, grouped format |
| `POST` | `/dig` | Dig a single category, full flat record format |
| `GET` | `/entities` | Return all stored entities (optional `?category=` filter) |
| `GET` | `/entities/meta/stats` | Total entities, synonyms and known categories in store |
| `GET` | `/entities/meta/categories` | List all seed categories ever dug and stored |
| `GET` | `/subcategories` | Preview the category hierarchy before digging |

## Data source

Queries the live [DBpedia SPARQL endpoint](https://dbpedia.org/sparql) — no local dump files required. DBpedia is a structured knowledge graph derived from Wikipedia, with category hierarchies, entity labels, redirects and disambiguation pages already parsed and queryable. This makes it directly suitable for synonym extraction without needing to parse raw Wikipedia article content.

## License

This program has been made available under the terms of the GNU Affero General Public License (AGPL). See individual files for details.