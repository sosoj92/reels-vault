"""
Fixtures controlees pour les tests Life Intelligence (jamais le vrai vault.db).

- life_db()        : base temporaire avec les projets du workspace, leurs profils,
                     3 sources liees a Nexdoor Talent (auteurs differents) et un insight ;
- FakeBackend      : Qwen simule (reponses JSON scriptees, compteur d'appels) ;
- FakeTelegram     : API Bot simulee (journal des envois, aucune requete reseau) ;
- FakeNotion       : cockpit Notion simule (pages en memoire, colonne Decision).
"""

import json
import uuid
from datetime import datetime, timezone

import _isolation

import mentor_council
import setup_project_profiles
import vault_intelligence as intelligence

STAMP = "2026-09-29T12:00:00+00:00"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

BINDINGS = """
CREATE TABLE IF NOT EXISTS intelligence_entity_bindings (
    entity_id INTEGER PRIMARY KEY, parent_entity_id INTEGER, notion_page_id TEXT,
    notion_url TEXT, drive_folder_id TEXT, drive_url TEXT,
    canonical_path TEXT NOT NULL, synced_at TEXT NOT NULL
)
"""

PROJECTS = (
    ("activity", "activity:nexdoor_partners", "Nexdoor Partners", None),
    ("activity", "activity:nexdoor_talent", "Nexdoor Talent", None),
    ("activity", "activity:recherche_emploi_remote", "Recherche emploi remote", None),
    ("activity", "activity:side_businesses", "Side businesses / opportunités", None),
)


def life_db(*, talent_sources=3, path=None):
    path = path or (_isolation.TMP / f"life-{uuid.uuid4().hex[:8]}.db")
    conn = intelligence.connect(str(path))
    conn.execute(BINDINGS)
    ids = {}
    for entity_type, key, name, parent in PROJECTS:
        ids[key] = intelligence.upsert_entity(conn, entity_type=entity_type,
                                              canonical_key=key, name=name)
        conn.execute("INSERT INTO intelligence_entity_bindings (entity_id, parent_entity_id, "
                     "canonical_path, synced_at) VALUES (?, ?, ?, ?)",
                     (ids[key], ids.get(parent), name, STAMP))
    conn.commit()
    setup_project_profiles.seed(conn)
    import mentor_worker

    mentor_worker.ensure_schema(conn)
    talent = ids["activity:nexdoor_talent"]
    sources = []
    topics = ("Sourcing de profils data sur LinkedIn", "Qualifier les candidats cyber",
              "Recrutement cloud : pieges des ATS", "Chasse de tetes IA", "Talent pool tech")
    for index in range(talent_sources):
        url = f"https://example.invalid/life/{index}"
        conn.execute("INSERT INTO contents (source_url, title, description_original, "
                     "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (url, topics[index], f"{topics[index]} pour un cabinet de recrutement.",
                      STAMP, STAMP))
        content_id = conn.execute("SELECT MAX(id) FROM contents").fetchone()[0]
        source_id = intelligence.upsert_source(conn, source_url=url, title=topics[index],
                                               author=f"auteur{index}", content_id=content_id,
                                               captured_at="2026-09-27")
        intelligence.link_entity_source(conn, entity_id=talent, source_id=source_id,
                                        role="relevant_to", notes="fixture")
        sources.append(source_id)
    conn.execute(
        "INSERT INTO intelligence_mentor_insights_local (source_id, insight_type, observation, "
        "relevance_score, status, created_at, updated_at) VALUES (?, 'opportunity', "
        "'Sourcing data', 0.8, 'proposed', ?, ?)", (sources[0], STAMP, STAMP))
    conn.commit()
    return path, conn, ids, sources


class FakeBackend:
    name = "fake-qwen"

    def __init__(self, verdicts=None, *, available=True, hypothesis=True, panel=True):
        self.verdicts = list(verdicts or [])
        self._available = available
        self.hypothesis = hypothesis
        self.panel = panel
        self.calls = []
        self.prompts = []

    def available(self):
        return self._available

    def ensure_ready(self, **kwargs):
        return self._available

    def chat_json(self, system, user, schema, *, num_predict=260):
        self.calls.append(schema)
        self.prompts.append((schema, user))
        if schema is mentor_council.HYPOTHESIS_SCHEMA:
            if not self.hypothesis:
                return None
            return {"title": "Sourcing data a industrialiser",
                    "summary": "Trois sources convergent sur le sourcing data et cyber.",
                    "hypothesis": "Un process de sourcing data reutilisable ferait gagner du temps.",
                    "actions": ["Formaliser une grille de qualification candidats data"]}
        if schema is mentor_council.VERDICT_SCHEMA:
            if self.verdicts:
                return self.verdicts.pop(0)
            return verdict()
        if schema is mentor_council.PANEL_SCHEMA:
            if not self.panel:
                return None
            data = json.loads(user)
            return {"opinions": [{"agent": s["agent"], "opinion": f"Avis de {s['agent']}."}
                                 for s in data["specialistes"]],
                    "synthesis": "Synthese des specialistes."}
        return None


def verdict(score=4, stance="support", blocking=False, objection="", refs=("source:1",)):
    return {"verdict": stance, "value_score": score, "proposed_action": "Tester sur 5 profils",
            "main_objection": objection, "blocking_objection": blocking, "confidence": 0.7,
            "evidence_refs": list(refs)}


class FakeTelegram:
    def __init__(self, updates=None):
        self.sent = []
        self.edits = []
        self.callbacks = []
        self.actions = []
        self.updates = list(updates or [])
        self.files = {}
        self.next_id = 100

    def configured(self):
        return True

    def send_message(self, chat_id, text, *, buttons=None, reply_to=None):
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons,
                          "reply_to": reply_to, "message_id": self.next_id})
        return {"message_id": self.next_id}

    def edit_message(self, chat_id, message_id, text, *, buttons=None):
        self.edits.append({"message_id": message_id, "text": text, "buttons": buttons})
        return {}

    def answer_callback(self, callback_id, text=None):
        self.callbacks.append((callback_id, text))

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(action)

    def download_file(self, file_id):
        return self.files[file_id]

    def get_updates(self, offset=None, timeout=25):
        batch = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return batch

    def buttons_of(self, index=-1):
        return [b["callback_data"] for row in (self.sent[index]["buttons"] or []) for b in row]


class FakeNotion:
    data_source_id = "fake-ds"

    def __init__(self):
        self.pages = {}
        self.created = []
        self.updated = []
        self.options = {"Décision": ["✅ Approuver", "⛔ Ignorer"],
                        "Kind": ["Entity", "Signal", "Action"]}
        self.patches = []

    def configured(self):
        return True

    def create_page(self, properties):
        page_id = f"page-{len(self.pages) + 1}"
        self.pages[page_id] = {"id": page_id, "properties": dict(properties)}
        self.created.append(page_id)
        return {"id": page_id, "url": f"https://notion.invalid/{page_id}"}

    def update_page(self, page_id, properties):
        self.pages[page_id]["properties"].update(properties)
        self.updated.append(page_id)
        return {"id": page_id, "url": f"https://notion.invalid/{page_id}"}

    def get_page(self, page_id):
        return self.pages[page_id]

    def clear_decision(self, page_id):
        self.pages[page_id]["properties"]["Décision"] = {"select": None}

    def decide(self, page_id, name):
        self.pages[page_id]["properties"]["Décision"] = {"select": {"name": name}}

    def _request(self, method, path, body=None):
        if method == "GET":
            return {"properties": {
                prop: {"select": {"options": [{"id": f"id-{n}", "name": n} for n in names]}}
                for prop, names in self.options.items()}}
        self.patches.append(body)
        for prop, spec in body["properties"].items():
            self.options[prop] = [o["name"] for o in spec["select"]["options"]]
        return {}

    def page_for(self, vault_ref, conn):
        row = conn.execute("SELECT notion_page_id FROM intelligence_cockpit_links "
                           "WHERE vault_ref = ?", (vault_ref,)).fetchone()
        return row[0] if row else None
