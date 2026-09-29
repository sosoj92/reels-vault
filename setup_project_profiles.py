#!/usr/bin/env python3
"""
setup_project_profiles.py - Profils initiaux des projets (donnees, pas du code).

Fournis par Rosy le 2026-09-29, plus la migration des profils auparavant codes
dans mentor_project_links.py. Idempotent : un profil deja present dans vault.db
n'est JAMAIS ecrase (les corrections de Rosy priment), sauf --force.

Ajouts de Claude, signales : les `terms` d'un sujet sont des variantes d'une
MEME idee (pluriels, traductions directes) ; ils ne creent aucun nouveau sujet.

Usage :
    python setup_project_profiles.py            # cree les profils absents
    python setup_project_profiles.py --dry-run  # montre sans ecrire
"""

import argparse
import sys
from pathlib import Path

import mentor_project_profiles as profiles

ROOT = Path(__file__).resolve().parent

VARIANTS_NOTE = ("Variantes (pluriels, traductions) ajoutees par Claude dans `terms` ; "
                 "sujets, noms, outils et exclusions fournis par Rosy le 2026-09-29.")

PERSONAL_HEALTH_BLOCK = [
    {"term": t, "mode": "block", "note": "contenu sante personnel : jamais rattache"}
    for t in ("traitement", "treatment", "medicament", "medication", "diagnostic",
              "diagnosis", "symptomes", "symptoms")
]

NEURO_DESCRIPTION = (
    "Projet de creation de solution digitale pour neuropsychologues/cabinets : site, "
    "orientation, ateliers, gestion des demandes, automatisation et parcours patient/client."
)

INITIAL_PROFILES = {
    "activity:nexdoor_talent": {
        "description": ("Cabinet de recrutement specialise principalement Tech, IA, Data, "
                        "Cyber, Cloud, avec activite internationale."),
        "core_topics": [
            {"topic": "recrutement", "terms": ["recrutement", "recruter", "recruteur",
                                               "recruteurs", "recruiting", "recruiter",
                                               "recruiters", "hiring", "embauche"]},
            {"topic": "sourcing", "terms": ["sourcing", "sourcer"]},
            {"topic": "chasse de têtes", "terms": ["chasse de tetes", "chasseur de tetes",
                                                   "chasseurs de tetes"]},
            {"topic": "headhunting", "terms": ["headhunting", "headhunter", "headhunters"]},
            "talent acquisition",
            {"topic": "candidats", "terms": ["candidats", "candidat", "candidate",
                                             "candidates"]},
            {"topic": "qualification candidats", "terms": [
                "qualification candidats", "qualification des candidats",
                "qualifier les candidats"]},
            {"topic": "CV", "terms": ["cv", "cvs", "curriculum vitae"]},
            {"topic": "recrutement Tech", "terms": ["recrutement tech", "tech recruiting"]},
            {"topic": "recrutement IA", "terms": ["recrutement ia", "ai recruiting"]},
            {"topic": "recrutement Data", "terms": ["recrutement data", "data recruiting"]},
            {"topic": "recrutement Cyber", "terms": ["recrutement cyber", "cyber recruiting"]},
            {"topic": "recrutement Cloud", "terms": ["recrutement cloud", "cloud recruiting"]},
        ],
        "context_topics": [
            "LinkedIn", "prospection candidats", "ATS",
            {"topic": "entretiens", "terms": ["entretiens", "entretien", "interview",
                                              "interviews"]},
            "matching", "recrutement international",
            {"topic": "marché du travail", "terms": ["marche du travail", "job market"]},
            "talent pool",
        ],
        "named_entities": ["Nexdoor Talent"],
        "specific_tools": ["Manatal"],
        "advisors": ["Recruitment Specialist", "Growth Hacker", "Chief Financial Officer"],
        "excluded_topics": [
            {"term": t, "mode": "alone_insufficient", "note": "signal faible (Rosy)"}
            for t in ("emploi", "job", "linkedin", "ia", "automatisation")
        ],
        "profile_source": "rosy",
        "notes": VARIANTS_NOTE,
    },
    "activity:nexdoor_partners": {
        "description": (
            "Activite de creation de sites, systemes digitaux et automatisations pour "
            "entreprises et independants, avec amelioration du parcours client, CRM, "
            "formulaires, prise de rendez-vous et automatisation des processus."),
        "core_topics": [
            {"topic": "automatisation", "terms": ["automatisation", "automatisations",
                                                  "automatiser", "automation"]},
            {"topic": "workflow", "terms": ["workflow", "workflows"]},
            "CRM",
            {"topic": "création de site", "terms": ["creation de site", "creation de sites",
                                                    "creer un site", "creer son site"]},
            {"topic": "parcours client", "terms": ["parcours client", "customer journey"]},
            {"topic": "prise de rendez-vous", "terms": ["prise de rendez-vous",
                                                        "prise de rdv"]},
            {"topic": "qualification de leads", "terms": ["qualification de leads",
                                                          "qualifier les leads",
                                                          "lead qualification"]},
            {"topic": "formulaire", "terms": ["formulaire", "formulaires"]},
            {"topic": "chatbot", "terms": ["chatbot", "chatbots"]},
            {"topic": "digitalisation", "terms": ["digitalisation", "digitaliser",
                                                  "transformation digitale"]},
            "automatisation administrative",
        ],
        "context_topics": [
            "Zoho", "HubSpot", "WhatsApp",
            {"topic": "email", "terms": ["email", "emails", "e-mail", "e-mails"]},
            "API",
            {"topic": "intégration", "terms": ["integration", "integrations"]},
            "no-code", "conversion",
            {"topic": "lead generation", "terms": ["lead generation", "generation de leads"]},
        ],
        "named_entities": ["Nexdoor Partners"],
        "specific_tools": [
            "WordPress", "Elementor",
            # « Make » seul est un verbe anglais : seules ses formes non ambigues comptent.
            {"topic": "Make", "terms": ["make.com", "integromat"]},
            "Zapier",
        ],
        "excluded_topics": [
            {"term": t, "mode": "alone_insufficient", "note": "signal faible (Rosy)"}
            for t in ("ia", "chatgpt", "claude")
        ],
        "compound_rules": [{
            "name": "automatisation metier",
            "primary_any": [
                {"topic": "automatisation", "terms": ["automatisation", "automatisations",
                                                      "automatiser", "automation"]},
                {"topic": "workflow", "terms": ["workflow", "workflows"]},
                {"topic": "digitalisation", "terms": ["digitalisation", "digitaliser"]},
            ],
            "context_any": [
                {"topic": "leads", "terms": ["leads", "lead"]},
                {"topic": "generation de leads", "terms": ["generation de leads",
                                                           "lead generation"]},
                "CRM",
                {"topic": "email", "terms": ["email", "emails", "e-mail", "e-mails"]},
                {"topic": "qualification", "terms": ["qualification", "qualifier"]},
                {"topic": "formulaire", "terms": ["formulaire", "formulaires"]},
                {"topic": "prise de rendez-vous", "terms": ["prise de rendez-vous",
                                                            "prise de rdv"]},
                "prospection",
                {"topic": "client", "terms": ["client", "clients"]},
                {"topic": "chatbot", "terms": ["chatbot", "chatbots"]},
                {"topic": "integration", "terms": ["integration", "integrations"]},
                "API",
                "conversion",
            ],
            "context_min": 2,
            "level": "medium",
            "note": "Regle de Rosy du 2026-09-30.",
        }],
        "advisors": ["Workflow Architect", "Outbound Strategist", "Growth Hacker"],
        "profile_source": "rosy",
        "notes": VARIANTS_NOTE + " WordPress, Elementor, Make, Zapier : places en outils "
                 "specifiques uniquement (evite le double comptage sujet + outil). "
                 "Automatisation sans contexte metier : jamais suffisante seule (regle generique).",
    },
    "activity:recherche_emploi_remote": {
        "description": ("Projet visant a securiser un revenu remote stable, principalement "
                        "via emploi, missions ou plateformes, avec objectif de revenu mensuel."),
        "core_topics": [
            {"topic": "emploi remote", "terms": ["emploi remote", "emploi a distance",
                                                 "remote job", "remote jobs"]},
            {"topic": "full remote", "terms": ["full remote", "full-remote", "100% remote"]},
            {"topic": "recherche d'emploi", "terms": ["recherche d'emploi",
                                                      "chercher un emploi", "job search"]},
            {"topic": "candidature", "terms": ["candidature", "candidatures"]},
            "freelance remote",
            {"topic": "mission remote", "terms": ["mission remote", "missions remote",
                                                  "mission a distance"]},
            {"topic": "job board", "terms": ["job board", "job boards"]},
            "recrutement remote", "revenu remote",
        ],
        "context_topics": [
            {"topic": "CV", "terms": ["cv", "cvs"]}, "portfolio", "LinkedIn",
            {"topic": "entretien", "terms": ["entretien", "entretiens"]},
            "candidature spontanée",
            {"topic": "plateforme freelance", "terms": ["plateforme freelance",
                                                        "plateformes freelance"]},
            "marketplace",
            {"topic": "salaire", "terms": ["salaire", "salaires"]}, "TJM",
        ],
        "named_entities": ["Recherche emploi remote"],
        "specific_tools": [],
        "advisors": ["Recruitment Specialist", "Chief of Staff", "Chief Financial Officer"],
        "excluded_topics": [
            {"term": "job", "mode": "alone_insufficient", "note": "souvent dans un autre sens"},
            {"term": "certification", "mode": "alone_insufficient", "note": "Rosy"},
            {"term": "freelance", "mode": "alone_insufficient",
             "note": "sans objectif professionnel (Rosy)"},
            {"term": "remote", "mode": "alone_insufficient",
             "note": "sans rapport avec un emploi ou une mission (Rosy)"},
        ],
        "profile_source": "rosy",
        "notes": VARIANTS_NOTE,
    },
    "vertical:nexdoor_partners:neuropsychologues": {
        "description": NEURO_DESCRIPTION,
        "core_topics": [
            {"topic": "neuropsychologie", "terms": ["neuropsychologie", "neuropsychologique",
                                                    "neuropsychologiques"]},
            {"topic": "neuropsychologue", "terms": ["neuropsychologue", "neuropsychologues",
                                                    "neuropsychologist"]},
            "bilan neuropsychologique",
            {"topic": "TDAH", "terms": ["tdah", "adhd"]},
            {"topic": "TSA", "terms": ["tsa", "trouble du spectre de l'autisme"]},
            {"topic": "ateliers", "terms": ["ateliers", "atelier"]},
            "cabinet", "orientation patient", "formulaire d'orientation",
            "prise de rendez-vous neuropsy",
        ],
        "context_topics": [
            "automatisation cabinet",
            {"topic": "gestion demandes", "terms": ["gestion demandes",
                                                    "gestion des demandes"]},
            "parcours patient", "site cabinet", "agenda", "Doctolib",
        ],
        "named_entities": ["Verticale Neuropsychologues"],
        "specific_tools": [],
        "advisors": ["Healthcare Innovation Strategist", "Workflow Architect",
                     "Growth Hacker"],
        "excluded_topics": PERSONAL_HEALTH_BLOCK,
        "profile_source": "rosy (NEUROPSYCHOLOGUES / NEUROSOLUTIONS)",
        "notes": VARIANTS_NOTE + " Profil commun fourni pour la verticale et NeuroSolutions.",
    },
    "prospect:nexdoor_partners:neuropsychologues:neurosolutions_center": {
        "description": NEURO_DESCRIPTION,
        "named_entities": ["NeuroSolutions", "NeuroSolutions Center"],
        "advisors": ["Healthcare Innovation Strategist", "Workflow Architect"],
        "excluded_topics": PERSONAL_HEALTH_BLOCK,
        "profile_status": "complete",
        "profile_source": "rosy (NEUROPSYCHOLOGUES / NEUROSOLUTIONS)",
        "notes": "Prospect : designe par son nom ; les sujets sont portes par la verticale.",
    },
    "vertical:nexdoor_partners:logistique": {
        "description": ("Projet de digitalisation d'une activite import/export/logistique avec "
                        "site, CRM, qualification des demandes et automatisations."),
        "core_topics": [
            {"topic": "import export", "terms": ["import export", "import-export",
                                                 "import/export"]},
            {"topic": "logistique", "terms": ["logistique", "logistics"]},
            "transport international",
            {"topic": "freight", "terms": ["freight", "fret"]},
            "supply chain",
            {"topic": "expédition", "terms": ["expedition", "expeditions", "shipping"]},
            {"topic": "douane", "terms": ["douane", "douanes"]},
            {"topic": "transit", "terms": ["transit", "transitaire"]},
        ],
        "context_topics": [
            "CRM", {"topic": "devis", "terms": ["devis"]}, "leads", "automatisation",
            "chatbot", {"topic": "demandes clients", "terms": ["demandes clients",
                                                               "demande client"]},
        ],
        "named_entities": ["Verticale Logistique"],
        "specific_tools": [],
        "advisors": ["Workflow Architect", "Operations Manager", "Outbound Strategist"],
        "excluded_topics": [],
        "profile_source": "rosy (HALIDI / LOGISTIQUE) - applique a la verticale, a confirmer",
        "notes": VARIANTS_NOTE + " Sujets logistiques portes par la verticale ; Halidi "
                 "(client) n'est designe que par son nom.",
    },
    "client:nexdoor_partners:logistique:halidi_transport_international": {
        "description": ("Projet de digitalisation d'une activite import/export/logistique avec "
                        "site, CRM, qualification des demandes et automatisations."),
        "named_entities": ["Halidi", "Halidi Transport", "Halidi Transport International"],
        "advisors": ["Workflow Architect", "Operations Manager"],
        "profile_status": "complete",
        "profile_source": "rosy (HALIDI / LOGISTIQUE)",
        "notes": "Client : designe par son nom. Jamais sur de l'automatisation generique.",
    },
    "activity:side_businesses": {
        "description": (
            "Exploration d'idees de revenus, micro-business, produits, services ou "
            "opportunites commerciales non encore rattaches a un projet principal."),
        "core_topics": [
            {"topic": "business idea", "terms": ["business idea", "business ideas",
                                                 "idee de business", "idees de business"]},
            {"topic": "side business", "terms": ["side business", "side businesses"]},
            "side hustle",
            {"topic": "opportunite commerciale", "terms": ["opportunite commerciale",
                                                           "opportunites commerciales"]},
            "monetisation",
            {"topic": "nouveau service", "terms": ["nouveau service", "nouveaux services"]},
            {"topic": "nouveau produit", "terms": ["nouveau produit", "nouveaux produits"]},
            {"topic": "revenus complementaires", "terms": ["revenus complementaires",
                                                          "revenu complementaire"]},
            {"topic": "micro-SaaS", "terms": ["micro-saas", "micro saas", "microsaas"]},
            {"topic": "business model", "terms": ["business model", "modele economique"]},
        ],
        "context_topics": [
            "acquisition clients", "pricing",
            {"topic": "marge", "terms": ["marge", "marges"]},
            "recurring revenue", "marketplace", "niche", "lancement",
            {"topic": "validation marche", "terms": ["validation marche",
                                                     "valider le marche"]},
            "MVP",
        ],
        "named_entities": ["Side businesses"],
        "specific_tools": [],
        "excluded_topics": [
            {"term": "automatisation", "mode": "alone_insufficient",
             "note": "automatisation seule (Rosy)"},
            {"term": "prospection", "mode": "alone_insufficient",
             "note": "prospection seule (Rosy)"},
            {"term": "motivation", "mode": "block", "note": "developpement personnel (Rosy)"},
            {"term": "developpement personnel", "mode": "block", "note": "Rosy"},
            {"mode": "defer", "project": "activity:nexdoor_partners",
             "note": "contenu deja clairement rattache a Nexdoor Partners (Rosy)"},
            {"mode": "defer", "project": "activity:nexdoor_talent",
             "note": "contenu deja clairement rattache a Nexdoor Talent (Rosy)"},
        ],
        "advisors": ["Product Manager", "Trend Researcher", "Chief Financial Officer"],
        "profile_status": "complete",
        "profile_source": "rosy",
        "notes": VARIANTS_NOTE + " Jamais un fallback pour les sources non classees. "
                 "Outils IA generiques : exclus par la regle generique. « Simple actualite "
                 "business » : non detectable par mots-cles, couvert par l'exigence de "
                 "2 sujets + contexte.",
    },
}


def seed(conn, *, force=False, dry_run=False, update=()):
    """
    Cree les profils absents. `update` = cles a remplacer explicitement par la
    version fournie ici (decision de Rosy) ; les autres ne sont jamais ecrases.
    """
    result = {}
    for key, profile in INITIAL_PROFILES.items():
        row = conn.execute(
            "SELECT id FROM intelligence_entities WHERE canonical_key = ?", (key,)
        ).fetchone()
        if row is None:
            result[key] = "entity_missing"
            continue
        if dry_run:
            exists = profiles.schema_present(conn) and profiles.get_profile(conn, row[0])
            result[key] = "exists" if exists else "would_create"
            continue
        result[key] = profiles.upsert_profile(conn, row[0], profile,
                                              overwrite=force or key in update)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Profils initiaux des projets")
    parser.add_argument("--db", default=str(ROOT / "vault.db"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="ecrase les profils existants (corrections perdues)")
    parser.add_argument("--update", nargs="*", default=(),
                        help="cles canoniques a remplacer par la version de ce fichier")
    args = parser.parse_args(argv)
    from vault_db import connect

    conn = connect(args.db)
    try:
        for key, outcome in seed(conn, force=args.force, dry_run=args.dry_run,
                                     update=tuple(args.update)).items():
            print(f"{outcome:<15} {key}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
