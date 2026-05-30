# Filename Patterns

`m3` uses a **universal parser** (`app/parser.py`) to decode media filenames into structured tokens before routing to a plugin. This document is the canonical grammar reference.

The raw source note this was derived from is at `NOTE_filename_grammar_source.md`.

---

## Top-Level Forms

Every filename is one of two forms:

| Form | Trigger | Example |
|---|---|---|
| **Manual Add** | Filename starts with keyword `Add` | `Add Jane Doe In My Scene At MyStudio` |
| **General** | Everything else | `Jane Doe with drama % mysite - 12345` |

---

## Form 1 — Manual Add

Used when the studio is not yet supported by any plugin. The result is written as a pending entry in the run report for manual resolution.

### Grammar

```
Add [<Date>] <Actor> [And <Actor> ...] [In <Title>] [At <Studio>] [With <Genre>, ...]
```

### Keywords (case-insensitive)

| Keyword | Role |
|---|---|
| `Add` | Signals manual-add form; must be the first token |
| `And` | Separates additional actors |
| `In` | Introduces the scene title |
| `At` | Introduces the studio name |
| `With` | Introduces comma-separated genres |

### Fields

| Field | Required | Notes |
|---|---|---|
| `actors` | Yes (at least one) | First actor follows `Add` (and optional date); subsequent actors follow `And` |
| `date` | No | Immediately after `Add`, before first actor. Formats: `YYYY-MM-DD`, `YY-MM-DD`, `YYYY.MM.DD` |
| `title` | No | Follows `In` keyword |
| `studio` | No | Follows `At` keyword |
| `genres` | No | Follow `With` keyword; comma-separated |

### Examples

```
Add Jane Doe At MyStudio
Add 2019-01-01 Jane Doe At MyStudio
Add Jane Doe And Mary Smith In My Scene At MyStudio
Add 2019-01-01 Jane Doe And Mary Smith In My Scene At MyStudio With Drama, Comedy
```

---

## Form 2 — General

The standard form for files with a known or registered site plugin.

### Grammar

```
<Actors> [with <Genres>] % <Match Payload>
```

- `<Actors>` — comma-separated actor names
- `with <Genres>` — optional; comma-separated genres after the keyword `with`
- `%` — required separator; splits actors/genres from the site match payload
- `<Match Payload>` — site name/shorthand followed by `-`-delimited fields (see subtypes below)

### Example top-level parse

```
Jane Doe, Mary Smith with Drama, Comedy % mysite - 2019-01-01 - 12345
│                    │            │       │         │            │
│                    │            │       │         │            └─ SceneID
│                    │            │       │         └─ Date
│                    │            │       └─ Site
│                    │            └─ Genres
│                    └─ "with" separator
└─ Actors (comma-separated)
```

---

## Match Payload Subtypes

The parser determines which subtype applies based on the structure of the payload after the site name. Plugins receive a `match_subtype` field so they know which search strategy to use.

### Subtype 1 — Enhanced Search

**Capabilities:** Title, Actor(s), Date, SceneID (any combination).

```
<Site> [- <Date>] [- <SceneID>] [- <Actor>] [- <Title>]
```

- Date and SceneID (if present) appear before Actor/Title
- Supports multi-search: both Actor and Title can be present
- SceneID is purely numeric

**Examples:**
```
mysite - 19-06-15 - 98765 - Jane Doe - An Interesting Plot   ← full
mysite - 98765 - Jane Doe                                      ← with SceneID, no title
mysite - 19-06-15 - An Interesting Plot                        ← date + title only
SN - An Interesting Plot                                       ← shorthand + title only
```

### Subtype 2 — Limited Search

**Capabilities:** Title and/or Actor only. No date or SceneID.

```
<Site> - <Title or Actor> [- <Title or Actor>]
```

**Examples:**
```
mysite - Jane Doe - An Interesting Plot
mysite - Jane Doe
SN - An Interesting Plot
```

### Subtype 3 — Exact Match

**Capabilities:** StudioID, ActressID, SceneID, or Direct URL slug. No free-text search.

```
<Site> [- <Date>] - <ID or DirectURL>
```

ID types:
- **SceneID** — purely numeric (`12345`)
- **StudioID** — numeric, used when the site hosts many small studios
- **ActressID** — alphanumeric, from the actress's page URL (e.g., `jane-doe`)
- **Direct URL slug** — the path suffix of a scene URL (may contain letters, numbers, hyphens, spaces)

Date may precede the ID. Additional actor/title terms after a StudioID or ActressID act as a search hint. **Do not add actor/title terms after a Direct URL slug** — they break matching.

> **Note:** `studio_id` and `actress_id` are **reserved fields** — the parser never populates them. These fields describe *conceptual* token types for plugin author guidance only. In practice, a StudioID token will appear as `scene_id` (if numeric) or `direct_url` (if a hyphenated slug). Plugins that need the studio or actress ID must parse it from `scene_id`, `direct_url`, or `raw_match_payload`.

**Examples:**
```
mysite - 12345                                              ← SceneID only
SN - 12345                                                  ← shorthand + SceneID
mysite - 19-01-01 - 12345                                   ← date + SceneID
mysite - eager-hands                                        ← Direct URL slug
mysite - 2019.01.01 - eager-hands                           ← date + Direct URL slug
mysite - 2019.10.10 - Stranger-Than-Fiction 77675           ← slug with embedded ID
mysite - 2019.10.10 - Stranger Than Fiction Scene 1 167063  ← full URL-derived slug
```

---

## ParsedFilename Dataclass

`app/parser.py` returns a `ParsedFilename` with these fields:

```python
@dataclass
class ParsedFilename:
    form: Literal["add", "general"]

    # General form fields
    actors: list[str] = field(default_factory=list)   # from both forms
    genres: list[str] = field(default_factory=list)   # from both forms
    site: str | None = None                           # site_id or shorthand
    match_subtype: MatchSubtype = "exact"  # never None after parsing

    # Match payload fields (general form)
    date: str | None = None                           # normalised to YYYY-MM-DD
    scene_id: str | None = None                       # numeric string
    studio_id: str | None = None    # reserved; parser never populates this
    actress_id: str | None = None   # reserved; parser never populates this
    direct_url: str | None = None
    title: str | None = None
    extra_actors: list[str] = field(default_factory=list)  # actor tokens in payload

    # Manual Add fields
    studio: str | None = None                         # from "At <Studio>"

    # Raw
    raw_match_payload: str | None = None              # everything after `%`
```

---

## Date Formats

Accepted anywhere a `<Date>` field appears:

| Format | Example |
|---|---|
| `YYYY-MM-DD` | `2019-01-15` |
| `YY-MM-DD` | `19-01-15` |
| `YYYY.MM.DD` | `2019.01.15` |
| `YY.MM.DD` | `19.01.15` |

All dates are normalised to `YYYY-MM-DD` in the parsed output.

---

## Parser Disambiguation Rules

Since subtypes share structural overlap, the parser applies these rules in order:

1. If filename starts with `Add` (case-insensitive) → **Form 1 (Manual Add)**
2. If `%` is present, split on first `%` → left side is actors/genres, right side is match payload
3. First token in payload (before first ` - `) is the **site**
4. After site, if next token matches a date pattern → extract as `date`
5. After optional date, if next token is purely numeric → likely `scene_id` (Enhanced or Exact)
6. Presence of free-text tokens (non-numeric, non-date) after site → Enhanced or Limited
7. Only a Direct URL slug with no free-text → Exact Match
8. Plugin receives `match_subtype` and uses it to decide which API call to make

Subtype resolution is deliberately **advisory** — plugins may override the subtype inference if they know their site's API better. For example, if your API always uses numeric IDs:

```python
def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
    # Ignore match_subtype and dispatch entirely by what fields are present
    if parsed.scene_id:
        return self._fetch_by_id(parsed)
    ...
```

---

## Delimiter Conventions

- Fields within the match payload are separated by ` - ` (space-hyphen-space)
- Actors in the left-hand side are separated by `, ` (comma-space)
- Genres in the left-hand side are separated by `, ` (comma-space)
- `And` separates actors in the Manual Add form (not commas)
- Keywords (`Add`, `And`, `In`, `At`, `With`) are case-insensitive
