#!/usr/bin/env python3
"""
vault_api.py - API locale et generique du Reels Vault (FastAPI).

Interface stable pour toute application qui consomme le Vault : l'interface
Reels Vault V2, Davy, des scripts, les connecteurs (Excel, Notion, ClickUp...).
L'API ne contient aucune logique propre a un client : elle expose vault.db.

Ecoute uniquement sur http://127.0.0.1:8765 (jamais sur le reseau).
Interface V2 :               http://127.0.0.1:8765/
Documentation interactive :  http://127.0.0.1:8765/docs

Lancement :
    .venv\\Scripts\\python.exe vault_api.py
Arret : Ctrl+C dans la fenetre du serveur.
"""

import os
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import connectors
import vault_db
import vault_intelligence as intelligence
import vault_intelligence_actions as intelligence_actions
import vault_intelligence_context as intelligence_context
import vault_intelligence_query as intelligence_query
import vault_intelligence_signals as intelligence_signals
import vault_voice_bridge as voice_bridge

HOST, PORT = "127.0.0.1", int(os.environ.get("REELS_VAULT_PORT", 8765))
API_VERSION = "2.2"
RACINE = Path(__file__).resolve().parent
UI_FILE = RACINE / "ui" / "index.html"
IMAGES_DIR = Path(os.environ.get("REELS_VAULT_IMAGES") or RACINE / "images")
EXPORT_DIR = connectors.base.EXPORT_DIR

app = FastAPI(
    title="Reels Vault API",
    version=API_VERSION,
    description=(
        "API locale du Reels Vault. vault.db est la source de verite. "
        "Description effective d'un contenu : description_manual > description_ai > description_original. "
        "Les champs modifies par l'utilisateur sont verrouilles contre les ecritures IA et les migrations. "
        "Les actions proposees par une IA sont creees au statut 'proposed' et ne sont jamais envoyees "
        "vers un outil externe sans validation. "
        "Life Intelligence V3 est exposee en lecture via /intelligence/*."
    ),
)

ActionStatus = Literal["proposed", "approved", "todo", "doing", "done", "dismissed", "archived"]
SourceStatus = Literal["available", "unavailable", "unknown"]
EntityType = Literal["content", "resource", "action", "review"]


def db():
    conn = vault_db.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.exception_handler(vault_db.VaultError)
def _vault_error(_, exc):
    statut = {"not_found": 404, "locked": 409, "not_validated": 409, "external_missing": 409}.get(exc.code, 422)
    return JSONResponse(status_code=statut, content={"error": exc.code, "detail": str(exc)})


@app.exception_handler(connectors.ConnectorError)
def _connector_error(_, exc):
    code = "not_configured" if isinstance(exc, connectors.NotConfigured) else "connector_error"
    return JSONResponse(status_code=501 if code == "not_configured" else 502,
                        content={"error": code, "detail": str(exc)})


@app.exception_handler(intelligence_query.IntelligenceQueryError)
def _intelligence_query_error(_, exc):
    return JSONResponse(status_code=422, content={"error": "invalid_query", "detail": str(exc)})


@app.exception_handler(intelligence_context.IntelligenceContextError)
def _intelligence_context_error(_, exc):
    return JSONResponse(status_code=422, content={"error": "invalid_context_query", "detail": str(exc)})


@app.exception_handler(intelligence_signals.IntelligenceSignalError)
def _intelligence_signal_error(_, exc):
    return JSONResponse(status_code=422, content={"error": "invalid_signal_query", "detail": str(exc)})


# ------------------------------------------------------------------ modeles

class ContentPatch(BaseModel):
    """Seuls les champs fournis sont modifies."""
    title: Optional[str] = None
    description_manual: Optional[str] = Field(None, description="Correction humaine, prioritaire sur tout")
    description_ai: Optional[str] = Field(None, description="Resume produit par une IA")
    category: Optional[str] = None
    subthemes: Optional[List[str]] = Field(None, max_length=3)
    tags: Optional[List[str]] = None
    source_status: Optional[SourceStatus] = None
    edited_by: Literal["user", "ai"] = Field(
        "user", description="'user' verrouille les champs modifies ; 'ai' ne peut pas ecraser un champ verrouille")


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1)


class ResourceIn(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field("autre", description=", ".join(vault_db.RESOURCE_TYPES))
    description: Optional[str] = None
    url: Optional[str] = None


class ResourcePatch(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None


class ActionIn(BaseModel):
    title: str = Field(..., min_length=1)
    content_id: Optional[int] = Field(None, description="Contenu source de l'action")
    resource_id: Optional[int] = Field(None, description="Ressource concernee (ex. outil a tester)")
    description: Optional[str] = None
    action_type: Optional[str] = Field(None, description=", ".join(vault_db.ACTION_TYPES))
    status: ActionStatus = "proposed"
    priority: Optional[int] = Field(None, ge=1, le=4, description="1 = haute ... 4 = basse")
    due_date: Optional[str] = Field(None, description="AAAA-MM-JJ")
    proposed_by: str = Field("user", description="'user', ou le nom de l'IA (ex. 'davy') -> statut force a 'proposed'")


class ActionPatch(BaseModel):
    title: Optional[str] = None
    content_id: Optional[int] = None
    resource_id: Optional[int] = None
    description: Optional[str] = None
    action_type: Optional[str] = None
    status: Optional[ActionStatus] = None
    priority: Optional[int] = Field(None, ge=1, le=4)
    due_date: Optional[str] = None


class ReviewIn(BaseModel):
    summary: Optional[str] = Field(None, description="Texte du bilan ; genere automatiquement si absent")


class SendIn(BaseModel):
    connector: Optional[str] = Field(
        None, description="Nom du connecteur (voir GET /connectors). Pour une action : TASK_PROVIDER si absent")


class TaskProviderIn(BaseModel):
    provider: Optional[str] = Field(None, description="Connecteur de taches par defaut (vide = aucun)")


class ExportIn(BaseModel):
    content_ids: Optional[List[int]] = Field(None, description="Selection ; absent = tout le Vault")
    destination: Optional[str] = Field(None, description="Chemin de sortie ; defaut : dossier exports/")


class VoiceAgentIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    agent: Optional[str] = Field(
        None,
        description="Nom explicite d'un Agency Agent ; absent = routage automatique",
    )
    max_agents: int = Field(2, ge=1, le=3)
    include_vault_context: bool = True


def _fournis(model):
    return model.model_dump(exclude_unset=True)


# ------------------------------------------------------------------ systeme

@app.get("/health", tags=["systeme"])
def health(conn=Depends(db)):
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "schema_version": vault_db.SCHEMA_VERSION,
        "database": str(vault_db.DB_PATH),
        "contents": conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0],
        "last_review_at": vault_db.last_review_at(conn),
    }


def _task_provider(conn):
    """Gestionnaire de taches par defaut, decrit de facon generique pour l'interface."""
    info = connectors.task_provider_info(conn)
    if info["name"]:
        c = connectors.get(info["name"])
        info.update(label=c.label, configured=c.is_configured())
    return info


@app.get("/config", tags=["systeme"], summary="Listes de valeurs et reglages (types, statuts, TASK_PROVIDER)")
def config(conn=Depends(db)):
    return {
        "action_statuses": list(vault_db.ACTION_STATUSES),
        "action_lifecycles": list(vault_db.LIFECYCLES),
        "action_types": list(vault_db.ACTION_TYPES),
        "resource_types": list(vault_db.RESOURCE_TYPES),
        "source_statuses": list(vault_db.SOURCE_STATUSES),
        "attention_statuses": list(intelligence.ATTENTION_STATUSES),
        "verification_statuses": list(intelligence.VERIFICATION_STATUSES),
        "task_provider": connectors.task_provider(conn),
        "task_provider_info": _task_provider(conn),
    }


@app.get("/settings/task-provider", tags=["systeme"], summary="Gestionnaire de taches par defaut")
def get_task_provider(conn=Depends(db)):
    return _task_provider(conn)


@app.put("/settings/task-provider", tags=["systeme"],
         summary="Choisir le gestionnaire de taches par defaut (sans effet si la variable TASK_PROVIDER est definie)")
def put_task_provider(body: TaskProviderIn, conn=Depends(db)):
    connectors.set_task_provider(conn, body.provider)
    return _task_provider(conn)


@app.get("/stats", tags=["systeme"], summary="Indicateurs du tableau de bord")
def stats(conn=Depends(db)):
    return vault_db.get_stats(conn)


# ------------------------------------------------------------------ Life Intelligence V3 (lecture)

@app.get(
    "/intelligence/overview",
    tags=["intelligence"],
    summary="Snapshot V3 : entites, sources, claims, relations et statuts epistemiques",
)
def intelligence_overview(conn=Depends(db)):
    return intelligence_query.overview(conn)


@app.get(
    "/intelligence/entities",
    tags=["intelligence"],
    summary="Recherche et filtrage des entites persistantes V3",
)
def intelligence_entities(
    q: Optional[str] = None,
    entity_type: Optional[str] = None,
    attention_status: Optional[str] = Query(
        None,
        description="now, later, in_place, dismissed",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn=Depends(db),
):
    return intelligence_query.list_entities(
        conn,
        q=q,
        entity_type=entity_type,
        attention_status=attention_status,
        limit=limit,
        offset=offset,
    )


@app.get(
    "/intelligence/entities/{entity_id}",
    tags=["intelligence"],
    summary="Vue probante d'une entite : sources, analyses actives, claims et relations",
)
def intelligence_entity(entity_id: int, conn=Depends(db)):
    item = intelligence_query.get_entity(conn, entity_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"intelligence entity {entity_id} not found")
    return item


@app.get(
    "/intelligence/context",
    tags=["intelligence"],
    summary="Paquet de contexte borne et probant pour l'Assistant IA",
)
def intelligence_context_pack(
    q: str = Query(..., min_length=1, max_length=500),
    entity_limit: int = Query(6, ge=1, le=20),
    claims_per_entity: int = Query(6, ge=1, le=20),
    sources_per_entity: int = Query(4, ge=1, le=20),
    relations_per_entity: int = Query(6, ge=1, le=20),
    conn=Depends(db),
):
    return intelligence_context.build_context_pack(
        conn,
        q,
        entity_limit=entity_limit,
        claims_per_entity=claims_per_entity,
        sources_per_entity=sources_per_entity,
        relations_per_entity=relations_per_entity,
    )


@app.get(
    "/intelligence/signals",
    tags=["intelligence"],
    summary="File proactive derivee : contradictions, verification, identite et suites manquantes",
)
def intelligence_signal_queue(
    priority: Optional[int] = Query(None, ge=1, le=4),
    limit: int = Query(100, ge=1, le=500),
    conn=Depends(db),
):
    return intelligence_signals.collect_signals(
        conn,
        priority=priority,
        limit=limit,
    )


@app.get(
    "/intelligence/action-proposals",
    tags=["intelligence"],
    summary="Actions V2 proposees a partir des signaux V3, jamais executees automatiquement",
)
def intelligence_action_proposals(conn=Depends(db)):
    return intelligence_actions.list_signal_actions(conn)


@app.post(
    "/intelligence/action-proposals/refresh",
    tags=["intelligence"],
    summary="Materialiser les signaux prioritaires en actions au statut proposed",
)
def intelligence_refresh_action_proposals(conn=Depends(db)):
    return intelligence_actions.propose_priority_actions(
        conn,
        max_priority=2,
    )


@app.post(
    "/intelligence/voice/route",
    tags=["intelligence"],
    summary="Router une demande vocale vers Agency Agents + contexte Vault, sans executer d'action",
)
def intelligence_voice_route(body: VoiceAgentIn, conn=Depends(db)):
    try:
        return voice_bridge.route_voice_request(
            conn,
            body.text,
            requested_agent=body.agent,
            max_agents=body.max_agents,
            include_vault_context=body.include_vault_context,
        )
    except voice_bridge.VoiceBridgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ------------------------------------------------------------------ contenus

@app.get("/contents", tags=["contenus"])
def list_contents(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                  category: Optional[str] = None, subtheme: Optional[str] = None,
                  tag: Optional[str] = None, platform: Optional[str] = None, conn=Depends(db)):
    return vault_db.list_contents(conn, limit, offset, category, subtheme, tag, platform)


@app.get("/contents/recent", tags=["contenus"])
def recent_contents(limit: int = Query(5, ge=1, le=100), conn=Depends(db)):
    return vault_db.list_contents(conn, limit=limit)


@app.get("/contents/unreviewed", tags=["contenus"], summary="Contenus ajoutes depuis le dernier bilan")
def unreviewed_contents(limit: int = Query(100, ge=1, le=500), conn=Depends(db)):
    return vault_db.unreviewed_contents(conn, limit)


@app.get("/contents/without-action", tags=["contenus"], summary="Contenus sans aucune action")
def contents_without_action(limit: int = Query(20, ge=1, le=500), conn=Depends(db)):
    return vault_db.list_contents_without_action(conn, limit)


@app.get("/contents/{content_id}", tags=["contenus"],
         summary="Fiche complete : transcription, ressources, notes, actions, medias, historique, liens externes")
def get_content(content_id: int, conn=Depends(db)):
    return vault_db.get_content(conn, content_id)


@app.patch("/contents/{content_id}", tags=["contenus"])
def patch_content(content_id: int, patch: ContentPatch, conn=Depends(db)):
    changes = _fournis(patch)
    edited_by = changes.pop("edited_by", "user")
    return vault_db.update_content(conn, content_id, changes, edited_by)


@app.post("/contents/{content_id}/description/revert", tags=["contenus"],
          summary="Revenir a la description IA (la version manuelle reste dans l'historique)")
def revert_description(content_id: int, conn=Depends(db)):
    return vault_db.revert_description(conn, content_id)


@app.post("/contents/{content_id}/resources", tags=["ressources"], status_code=201)
def add_resource(content_id: int, res: ResourceIn, conn=Depends(db)):
    return vault_db.add_resource(conn, content_id, res.model_dump())


@app.post("/contents/{content_id}/notes", tags=["notes"], status_code=201)
def add_note(content_id: int, note: NoteIn, conn=Depends(db)):
    return vault_db.add_note(conn, content_id, note.text)


@app.patch("/notes/{note_id}", tags=["notes"])
def patch_note(note_id: int, note: NoteIn, conn=Depends(db)):
    return vault_db.update_note(conn, note_id, note.text)


@app.delete("/notes/{note_id}", tags=["notes"])
def delete_note(note_id: int, conn=Depends(db)):
    return vault_db.delete_note(conn, note_id)


@app.post("/contents/{content_id}/send", tags=["connecteurs"],
          summary="Envoyer un contenu vers un service (Notion, Google Sheets, Davy...)")
def send_content(content_id: int, body: SendIn, conn=Depends(db)):
    return connectors.send(conn, "content", content_id, body.connector)


@app.get("/search", tags=["contenus"],
         summary="Recherche dans titres, descriptions, transcriptions, tags, ressources, notes")
def search(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=500), conn=Depends(db)):
    return vault_db.search(conn, q, limit)


# ------------------------------------------------------------------ taxonomie

@app.get("/taxonomy", tags=["taxonomie"])
def taxonomy(conn=Depends(db)):
    return vault_db.get_taxonomy(conn)


@app.get("/tags", tags=["taxonomie"])
def tags(conn=Depends(db)):
    return {"items": vault_db.get_taxonomy(conn)["tags"]}


@app.get("/subthemes", tags=["taxonomie"])
def subthemes(category: Optional[str] = None, conn=Depends(db)):
    items = [{**s, "category": c["name"]} for c in vault_db.get_taxonomy(conn)["categories"]
             for s in c["subthemes"] if not category or c["name"] == category]
    return {"items": items}


# ------------------------------------------------------------------ ressources

@app.get("/resources", tags=["ressources"])
def list_resources(type: Optional[str] = None, content_id: Optional[int] = None,
                   q: Optional[str] = None, limit: int = Query(200, ge=1, le=1000), conn=Depends(db)):
    return vault_db.list_resources(conn, type, content_id, q, limit)


@app.get("/resources/catalog", tags=["ressources"],
         summary="Ressources regroupees par nom : mentions, contenus sources, actions associees")
def resource_catalog(type: Optional[str] = None, q: Optional[str] = None, conn=Depends(db)):
    return vault_db.resource_catalog(conn, type, q)


@app.get("/resources/{resource_id}", tags=["ressources"])
def get_resource(resource_id: int, conn=Depends(db)):
    return vault_db.get_resource(conn, resource_id)


@app.patch("/resources/{resource_id}", tags=["ressources"])
def patch_resource(resource_id: int, patch: ResourcePatch, conn=Depends(db)):
    return vault_db.update_resource(conn, resource_id, _fournis(patch))


# ------------------------------------------------------------------ actions

@app.get("/actions", tags=["actions"])
def list_actions(status: Optional[str] = Query(None, description="Un ou plusieurs statuts separes par des virgules"),
                 lifecycle: Optional[str] = Query(None, description="proposed, approved, sent, completed, ignored, archived"),
                 content_id: Optional[int] = None, resource_id: Optional[int] = None,
                 limit: int = Query(200, ge=1, le=1000), conn=Depends(db)):
    res = vault_db.list_actions(conn, status, content_id, limit=limit, resource_id=resource_id, lifecycle=lifecycle)
    res["counts"] = vault_db.lifecycle_counts(conn)
    return res


@app.get("/actions/since-last-review", tags=["actions"])
def actions_since_last_review(conn=Depends(db)):
    return vault_db.list_actions(conn, since=vault_db.last_review_at(conn))


@app.post("/actions", tags=["actions"], status_code=201)
def create_action(action: ActionIn, conn=Depends(db)):
    return vault_db.create_action(conn, action.model_dump())


@app.get("/actions/{action_id}", tags=["actions"])
def get_action(action_id: int, conn=Depends(db)):
    return vault_db.get_action(conn, action_id)


@app.patch("/actions/{action_id}", tags=["actions"])
def patch_action(action_id: int, patch: ActionPatch, conn=Depends(db)):
    return vault_db.update_action(conn, action_id, _fournis(patch))


@app.post("/actions/{action_id}/approve", tags=["actions"], summary="Valider une action (aucun envoi externe)")
def approve_action(action_id: int, conn=Depends(db)):
    return vault_db.set_action_status(conn, action_id, "approved")


@app.post("/actions/{action_id}/ignore", tags=["actions"], summary="Ignorer une action")
def ignore_action(action_id: int, conn=Depends(db)):
    return vault_db.set_action_status(conn, action_id, "dismissed")


@app.post("/actions/{action_id}/complete", tags=["actions"], summary="Marquer une action comme terminee")
def complete_action(action_id: int, conn=Depends(db)):
    return vault_db.set_action_status(conn, action_id, "done")


@app.post("/actions/{action_id}/send", tags=["connecteurs"],
          summary="Envoyer UNE action VALIDEE vers le gestionnaire de taches (connecteur, sinon TASK_PROVIDER)")
def send_action(action_id: int, body: Optional[SendIn] = None, conn=Depends(db)):
    res = connectors.send(conn, "action", action_id, body.connector if body else None)
    return {**res, "action": vault_db.get_action(conn, action_id)}


@app.get("/actions/{action_id}/sync", tags=["connecteurs"],
         summary="Etat de synchronisation d'une action : lien externe (external_url) et journal")
def action_sync(action_id: int, conn=Depends(db)):
    a = vault_db.get_action(conn, action_id)
    links = [dict(r) for r in conn.execute(
        "SELECT provider, external_id, external_url, synced_at FROM external_links "
        "WHERE entity_type = 'action' AND entity_id = ?", (action_id,))]
    return {"action_id": action_id, "lifecycle": a["lifecycle"], "external_provider": a["external_provider"],
            "external_task_id": a["external_task_id"], "external_url": a["external_url"],
            "links": links, "log": vault_db.list_sync_log(conn, "action", action_id)}


# ------------------------------------------------------------------ bilans

@app.get("/review/since-last", tags=["bilans"], summary="Bilan calcule depuis le dernier bilan (non enregistre)")
def review_since_last(conn=Depends(db)):
    return vault_db.review_since_last(conn)


@app.post("/reviews", tags=["bilans"], status_code=201, summary="Enregistrer un bilan (clot la periode)")
def create_review(body: ReviewIn, conn=Depends(db)):
    return vault_db.create_review(conn, body.summary)


@app.get("/reviews", tags=["bilans"])
def list_reviews(details: bool = False, conn=Depends(db)):
    return {"items": vault_db.list_reviews(conn, details)}


@app.get("/reviews/{review_id}", tags=["bilans"])
def get_review(review_id: int, conn=Depends(db)):
    return vault_db.get_review(conn, review_id)


# ------------------------------------------------------------------ connecteurs / exports

@app.get("/connectors", tags=["connecteurs"],
         summary="Connecteurs : capacites, configuration, derniere synchronisation, TASK_PROVIDER")
def list_connectors(conn=Depends(db)):
    return {"task_provider": connectors.task_provider(conn), "task_provider_info": _task_provider(conn),
            "items": connectors.list_connectors(conn)}


@app.get("/sync/status", tags=["connecteurs"])
def sync_status(conn=Depends(db)):
    return vault_db.sync_status(conn)


@app.post("/send/{entity_type}/{entity_id}", tags=["connecteurs"],
          summary="Envoi generique (content, resource, action validee, review) vers un connecteur")
def send_generic(entity_type: EntityType, entity_id: int, body: SendIn, conn=Depends(db)):
    return connectors.send(conn, entity_type, entity_id, body.connector)


@app.post("/exports/{connector}", tags=["connecteurs"], summary="Exporter le Vault ou une selection (excel, csv...)")
def export(connector: str, body: ExportIn, conn=Depends(db)):
    if body.content_ids:
        res = connectors.export_contents(conn, connector, body.content_ids, body.destination)
    else:
        res = connectors.export_vault(conn, connector, body.destination)
    dossier = EXPORT_DIR.resolve()
    return {"connector": connector, **res,
            "downloads": [f"/exports/files/{Path(f).name}" for f in res.get("files", [])
                          if Path(f).resolve().parent == dossier]}


@app.get("/exports/files/{filename}", tags=["connecteurs"], summary="Telecharger un fichier du dossier exports/")
def download_export(filename: str):
    chemin = (EXPORT_DIR / filename).resolve()
    if chemin.parent != EXPORT_DIR.resolve() or not chemin.is_file():
        raise vault_db.VaultError(f"export {filename} introuvable", "not_found")
    return FileResponse(chemin, filename=chemin.name)


# ------------------------------------------------------------------ interface V2

@app.get("/", include_in_schema=False)
def racine():
    return RedirectResponse("/app")


@app.get("/app", include_in_schema=False)
def interface():
    return FileResponse(UI_FILE, headers={"Cache-Control": "no-store"})


# Captures locales (images/), affichees dans l'interface. Lecture seule.
app.mount("/images", StaticFiles(directory=IMAGES_DIR, check_dir=False), name="images")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
