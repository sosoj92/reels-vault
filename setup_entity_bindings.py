import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

# Chemin absolu : jamais de base vide creee dans le repertoire courant.
DB = Path(__file__).resolve().parent / "vault.db"
NOW = datetime.now(timezone.utc).isoformat()

entities = [
    {
        "key": "activity:nexdoor_partners",
        "type": "activity",
        "name": "Nexdoor Partners",
        "parent": None,
        "notion_id": "3e89afcbcabd81be89bff0f87663579f",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81be89bff0f87663579f",
        "drive_id": "1G3B3QEk9k0mVgF0yEozmkO6DVV5P4DVx",
        "drive_url": "https://drive.google.com/drive/folders/1G3B3QEk9k0mVgF0yEozmkO6DVV5P4DVx",
        "path": "Nexdoor Partners"
    },
    {
        "key": "activity:nexdoor_talent",
        "type": "activity",
        "name": "Nexdoor Talent",
        "parent": None,
        "notion_id": "3e89afcbcabd81cbaeedec6c57f3ac46",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81cbaeedec6c57f3ac46",
        "drive_id": "1l1Z3xICvFBZbv7JlUth_k6xnHXPMEEJy",
        "drive_url": "https://drive.google.com/drive/folders/1l1Z3xICvFBZbv7JlUth_k6xnHXPMEEJy",
        "path": "Nexdoor Talent"
    },
    {
        "key": "activity:recherche_emploi_remote",
        "type": "activity",
        "name": "Recherche emploi remote",
        "parent": None,
        "notion_id": "3e89afcbcabd81e6af71cfce246c7149",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81e6af71cfce246c7149",
        "drive_id": "1uM_74SxpQwbiz1mnXDRExx2xuPa4NcIj",
        "drive_url": "https://drive.google.com/drive/folders/1uM_74SxpQwbiz1mnXDRExx2xuPa4NcIj",
        "path": "Recherche emploi remote"
    },
    {
        "key": "activity:side_businesses",
        "type": "activity",
        "name": "Side businesses / opportunités",
        "parent": None,
        "notion_id": "3e89afcbcabd811aa56aff75a74232ee",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd811aa56aff75a74232ee",
        "drive_id": "1AsfQRu5sDDZVJzmJJcWlDgtt8byPoRGJ",
        "drive_url": "https://drive.google.com/drive/folders/1AsfQRu5sDDZVJzmJJcWlDgtt8byPoRGJ",
        "path": "Side businesses / opportunités"
    },
    {
        "key": "vertical:nexdoor_partners:logistique",
        "type": "vertical",
        "name": "Verticale Logistique",
        "parent": "activity:nexdoor_partners",
        "notion_id": "3e89afcbcabd8122b2acf891eb7dda42",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd8122b2acf891eb7dda42",
        "drive_id": "1Vska445QcfbwDJToUQ5DTbb0IVZNqyz4",
        "drive_url": "https://drive.google.com/drive/folders/1Vska445QcfbwDJToUQ5DTbb0IVZNqyz4",
        "path": "Nexdoor Partners / Verticale Logistique"
    },
    {
        "key": "client:nexdoor_partners:logistique:halidi_transport_international",
        "type": "client",
        "name": "Halidi Transport International",
        "parent": "vertical:nexdoor_partners:logistique",
        "notion_id": "3e89afcbcabd81ef933ed104332827f2",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81ef933ed104332827f2",
        "drive_id": "1XFgBx4G8AuF6VG_JuWFQJVTHuBmeOa_E",
        "drive_url": "https://drive.google.com/drive/folders/1XFgBx4G8AuF6VG_JuWFQJVTHuBmeOa_E",
        "path": "Nexdoor Partners / Verticale Logistique / Clients / Halidi Transport International"
    },
    {
        "key": "vertical:nexdoor_partners:neuropsychologues",
        "type": "vertical",
        "name": "Verticale Neuropsychologues",
        "parent": "activity:nexdoor_partners",
        "notion_id": "3e89afcbcabd81a28547c2bc2e8f29ae",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81a28547c2bc2e8f29ae",
        "drive_id": "1YqkP1uEth_f1yv8yliQUMS_BsjqNxvFo",
        "drive_url": "https://drive.google.com/drive/folders/1YqkP1uEth_f1yv8yliQUMS_BsjqNxvFo",
        "path": "Nexdoor Partners / Verticale Neuropsychologues"
    },
    {
        "key": "prospect:nexdoor_partners:neuropsychologues:neurosolutions_center",
        "type": "prospect",
        "name": "NeuroSolutions Center",
        "parent": "vertical:nexdoor_partners:neuropsychologues",
        "notion_id": "3e89afcbcabd81d7b746e37ba50e38d5",
        "notion_url": "https://app.notion.com/p/3e89afcbcabd81d7b746e37ba50e38d5",
        "drive_id": "1hkAaB531hpRsNSh4yj3UDl3pL1qu3vzl",
        "drive_url": "https://drive.google.com/drive/folders/1hkAaB531hpRsNSh4yj3UDl3pL1qu3vzl",
        "path": "Nexdoor Partners / Verticale Neuropsychologues / Prospects / NeuroSolutions Center"
    }
]

conn = sqlite3.connect(DB)
conn.execute("PRAGMA foreign_keys = ON")

try:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intelligence_entity_bindings (
            entity_id INTEGER PRIMARY KEY
                REFERENCES intelligence_entities(id) ON DELETE CASCADE,
            parent_entity_id INTEGER
                REFERENCES intelligence_entities(id) ON DELETE SET NULL,
            notion_page_id TEXT,
            notion_url TEXT,
            drive_folder_id TEXT,
            drive_url TEXT,
            canonical_path TEXT NOT NULL,
            synced_at TEXT NOT NULL
        )
    """)

    metadata = json.dumps(
        {
            "identity_basis": "workspace_binding",
            "managed_by": "life_intelligence"
        },
        ensure_ascii=False
    )

    for e in entities:
        conn.execute("""
            INSERT INTO intelligence_entities (
                entity_type,
                canonical_key,
                name,
                summary,
                metadata,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                entity_type = excluded.entity_type,
                name = excluded.name,
                updated_at = excluded.updated_at
        """, (
            e["type"],
            e["key"],
            e["name"],
            "Entité canonique du Life Intelligence System",
            metadata,
            NOW,
            NOW
        ))

    ids = dict(conn.execute("""
        SELECT canonical_key, id
        FROM intelligence_entities
        WHERE canonical_key IN (
            'activity:nexdoor_partners',
            'activity:nexdoor_talent',
            'activity:recherche_emploi_remote',
            'activity:side_businesses',
            'vertical:nexdoor_partners:logistique',
            'client:nexdoor_partners:logistique:halidi_transport_international',
            'vertical:nexdoor_partners:neuropsychologues',
            'prospect:nexdoor_partners:neuropsychologues:neurosolutions_center'
        )
    """).fetchall())

    for e in entities:
        entity_id = ids[e["key"]]
        parent_id = ids.get(e["parent"]) if e["parent"] else None

        conn.execute("""
            INSERT INTO intelligence_entity_bindings (
                entity_id,
                parent_entity_id,
                notion_page_id,
                notion_url,
                drive_folder_id,
                drive_url,
                canonical_path,
                synced_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(entity_id) DO UPDATE SET
                parent_entity_id = excluded.parent_entity_id,
                notion_page_id = excluded.notion_page_id,
                notion_url = excluded.notion_url,
                drive_folder_id = excluded.drive_folder_id,
                drive_url = excluded.drive_url,
                canonical_path = excluded.canonical_path,
                synced_at = excluded.synced_at
        """, (
            entity_id,
            parent_id,
            e["notion_id"],
            e["notion_url"],
            e["drive_id"],
            e["drive_url"],
            e["path"],
            NOW
        ))

    conn.commit()

    print("\n=== ENTITY BINDINGS OK ===")
    for key, entity_id in sorted(ids.items(), key=lambda x: x[1]):
        print(entity_id, "->", key)

    count = conn.execute(
        "SELECT COUNT(*) FROM intelligence_entity_bindings"
    ).fetchone()[0]

    print("\nBindings total:", count)

except Exception:
    conn.rollback()
    raise

finally:
    conn.close()
