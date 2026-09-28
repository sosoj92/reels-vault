#!/usr/bin/env python3
"""
generate_vault.py - Construit vault.html (dashboard hors ligne) a partir de index.md.

Ce script ne fait AUCUN appel a une IA ni a internet : il lit index.md et
taxonomy.md, extrait les entrees et ecrit un vault.html autonome (donnees
integrees, aucune dependance externe).

Il ne modifie jamais index.md, taxonomy.md ni progression.txt : il ne fait
que les lire. Relancable apres chaque mise a jour de index.md.

Usage :
    python generate_vault.py
    python generate_vault.py --from-db     (option : lire vault.db, corrections manuelles incluses)
"""

import argparse
import json
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parent

CHAMPS = {
    "lien": "lien",
    "plateforme": "plateforme",
    "auteur": "auteur",
    "catégorie": "categorie",
    "sous_thèmes": "sous_themes",
    "tags": "tags",
    "contenu": "contenu",
    "outils_ressources": "outils",
}
CHAMPS_LISTE = {"sous_themes", "tags"}


def decouper(valeur):
    """Separe une liste 'a, b, c' en ignorant les virgules entre parentheses ou guillemets."""
    morceaux, courant, profondeur = [], "", 0
    for c in valeur:
        if c in "(«":
            profondeur += 1
        elif c in ")»":
            profondeur = max(0, profondeur - 1)
        if c == "," and profondeur == 0:
            morceaux.append(courant.strip())
            courant = ""
        else:
            courant += c
    morceaux.append(courant.strip())
    return [m for m in morceaux if m]


def lire_index(chemin):
    entrees, courante = [], None
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if ligne.startswith("### "):
            courante = {"titre": ligne[4:].strip()}
            entrees.append(courante)
            continue
        if courante is None:
            continue
        m = re.match(r"^-\s*([^:]+?)\s*:\s*(.*)$", ligne)
        if not m:
            continue
        cle = CHAMPS.get(m.group(1).strip())
        if cle:
            valeur = m.group(2).strip()
            courante[cle] = decouper(valeur) if cle in CHAMPS_LISTE else valeur
    for e in entrees:
        for cle in CHAMPS.values():
            e.setdefault(cle, [] if cle in CHAMPS_LISTE else "")
    return entrees


def lire_taxonomie(chemin):
    """Retourne {categorie: [sous-themes]} et la liste des tags frequents."""
    categories, tags = {}, []
    if not chemin.exists():
        return categories, tags
    section, cat = None, None
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        if ligne.startswith("## "):
            titre = ligne[3:].lower()
            section = "cat" if "catégorie" in titre else "tags" if "tag" in titre else None
            cat = None
        elif ligne.startswith("### ") and section == "cat":
            cat = ligne[4:].strip()
            categories.setdefault(cat, [])
        elif ligne.startswith("- "):
            terme = ligne[2:].strip()
            if section == "cat" and cat:
                categories[cat].append(terme)
            elif section == "tags":
                tags.append(terme)
    return categories, tags


def fusionner(categories, entrees):
    """Ajoute les categories/sous-themes presents dans index.md mais absents de la taxonomie."""
    arbre = {c: list(s) for c, s in categories.items()}
    for e in entrees:
        if not e["categorie"]:
            continue
        sous = arbre.setdefault(e["categorie"], [])
        for s in e["sous_themes"]:
            if s not in sous:
                sous.append(s)
    return [{"nom": c, "sous_themes": s} for c, s in arbre.items()]


def lire_db(chemin):
    """Memes donnees, lues dans vault.db : les corrections manuelles y sont prises en compte."""
    import vault_db

    conn = vault_db.connect(chemin)
    entrees = []
    for c in vault_db.dump(conn)["contents"]:
        entrees.append({
            "titre": c["title"] or "", "lien": c["source_url"], "plateforme": c["platform"] or "",
            "auteur": c["author"] or "", "categorie": c["category"] or "",
            "sous_themes": c["subthemes"], "tags": c["tags"], "contenu": c["description"] or "",
            "outils": ", ".join(r["name"] + (f" ({r['description']})" if r["description"] else "")
                                for r in c["resources"]),
        })
    categories = {cat["name"]: [s["name"] for s in cat["subthemes"]]
                  for cat in vault_db.get_taxonomy(conn)["categories"] if cat["name"]}
    conn.close()
    return entrees, categories


def json_html(donnees):
    """JSON sur a integrer dans une balise <script>."""
    return json.dumps(donnees, ensure_ascii=False).replace("</", "<\\/")


def main():
    parser = argparse.ArgumentParser(description="Genere vault.html a partir de index.md")
    parser.add_argument("--index", default=RACINE / "index.md", type=Path)
    parser.add_argument("--taxonomy", default=RACINE / "taxonomy.md", type=Path)
    parser.add_argument("--out", default=RACINE / "vault.html", type=Path)
    parser.add_argument("--from-db", nargs="?", const=RACINE / "vault.db", type=Path,
                        help="lire vault.db au lieu de index.md (optionnel)")
    args = parser.parse_args()

    categories, tags_frequents = lire_taxonomie(args.taxonomy)
    if args.from_db:
        entrees, categories = lire_db(args.from_db)
    else:
        entrees = lire_index(args.index)
    donnees = {
        "entrees": entrees,
        "categories": fusionner(categories, entrees),
        "tags_frequents": tags_frequents,
    }
    html = MODELE.replace("__DONNEES__", json_html(donnees))
    args.out.write_text(html, encoding="utf-8")
    print(f"{len(entrees)} contenu(s), {len(donnees['categories'])} categorie(s) -> {args.out}")


MODELE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reels Vault</title>
<style>
:root {
  --bg: #f6f5f2; --surface: #ffffff; --surface-2: #efede8; --text: #1d1c1a;
  --muted: #6b6860; --border: #e2dfd8; --accent: #3b5bdb; --accent-soft: #e7ecfd;
  --tiktok: #111111; --insta: #c13584; --tag: #f1efe9;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #151517; --surface: #1e1e21; --surface-2: #26262a; --text: #ecebe8;
    --muted: #9c9a94; --border: #333338; --accent: #8ea4ff; --accent-soft: #252c47;
    --tiktok: #e9e9e9; --insta: #e46bb0; --tag: #2a2a2e;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button { font: inherit; color: inherit; cursor: pointer; }
header { position: sticky; top: 0; z-index: 5; background: var(--bg);
  border-bottom: 1px solid var(--border); padding: 14px 20px; }
.top { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; max-width: 1400px; margin: 0 auto; }
h1 { margin: 0; font-size: 22px; letter-spacing: -0.01em; }
.total { color: var(--muted); font-size: 14px; }
#recherche { flex: 1; min-width: 200px; padding: 10px 14px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text); font: inherit; }
#recherche:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.layout { display: grid; grid-template-columns: 270px 1fr; gap: 24px; max-width: 1400px; margin: 0 auto; padding: 20px; }
aside { position: sticky; top: 84px; align-self: start; max-height: calc(100vh - 100px); overflow: auto; }
.bloc { margin-bottom: 22px; }
.bloc h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 8px; }
.nav-btn { display: flex; justify-content: space-between; align-items: center; width: 100%;
  text-align: left; background: none; border: 0; padding: 6px 10px; border-radius: 8px; }
.nav-btn:hover { background: var(--surface-2); }
.nav-btn.actif { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nav-btn.sous { padding-left: 26px; font-size: 14px; }
.nav-btn[disabled] { opacity: .4; cursor: default; }
.nb { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.nav-btn.actif .nb { color: inherit; }
.segments { display: flex; gap: 4px; background: var(--surface-2); padding: 3px; border-radius: 10px; }
.segments button { flex: 1; border: 0; background: none; padding: 6px 4px; border-radius: 8px; font-size: 13px; }
.segments button.actif { background: var(--surface); font-weight: 600; box-shadow: 0 1px 2px rgba(0,0,0,.08); }
.tags { display: flex; flex-wrap: wrap; gap: 6px; }
.tag { border: 1px solid var(--border); background: var(--tag); border-radius: 999px; padding: 3px 10px; font-size: 13px; }
.tag:hover { border-color: var(--accent); }
.tag.actif { background: var(--accent); border-color: var(--accent); color: #fff; }
.tag .nb { margin-left: 4px; }
.tag.actif .nb { color: #fff; }
.barre { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.resultats { font-weight: 600; }
.filtres-actifs { color: var(--muted); font-size: 14px; }
.reset { border: 1px solid var(--border); background: var(--surface); padding: 6px 12px; border-radius: 8px; font-size: 14px; }
.reset:hover { border-color: var(--accent); color: var(--accent); }
.grille { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.carte { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 18px;
  display: flex; flex-direction: column; gap: 10px; }
.carte h3 { margin: 0; font-size: 17px; line-height: 1.35; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; font-size: 13px; color: var(--muted); }
.plateforme { font-weight: 600; }
.plateforme.tiktok { color: var(--tiktok); }
.plateforme.instagram { color: var(--insta); }
.classif { display: flex; flex-wrap: wrap; gap: 6px; font-size: 13px; }
.pill { border: 0; border-radius: 6px; padding: 2px 8px; }
.pill.cat { background: var(--accent); color: #fff; font-weight: 600; }
.pill.sous { background: var(--accent-soft); color: var(--accent); }
.carte p { margin: 0; }
.outils { font-size: 13px; color: var(--muted); }
.outils strong { color: var(--text); font-weight: 600; }
.voir { margin-top: auto; align-self: flex-start; background: var(--text); color: var(--bg); text-decoration: none;
  padding: 8px 14px; border-radius: 8px; font-size: 14px; font-weight: 600; }
.voir:hover { opacity: .85; }
.vide { text-align: center; padding: 60px 20px; color: var(--muted); background: var(--surface);
  border: 1px dashed var(--border); border-radius: 14px; }
.vide strong { display: block; color: var(--text); font-size: 17px; margin-bottom: 6px; }
.vide .reset { margin-top: 14px; }
#toggle-filtres { display: none; }
@media (max-width: 800px) {
  header { padding: 12px 16px; }
  .layout { grid-template-columns: 1fr; padding: 16px; gap: 12px; }
  aside { position: static; max-height: none; display: none; }
  aside.ouvert { display: block; }
  #toggle-filtres { display: inline-block; }
  .grille { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<header>
  <div class="top">
    <h1>Reels Vault</h1>
    <span class="total" id="total"></span>
    <input id="recherche" type="search" placeholder="Rechercher : titre, auteur, outil, tag…" autocomplete="off">
    <button class="reset" id="toggle-filtres">Filtres</button>
  </div>
</header>
<div class="layout">
  <aside id="filtres">
    <div class="bloc"><h2>Plateforme</h2><div class="segments" id="plateformes"></div></div>
    <div class="bloc"><h2>Catégories</h2><div id="categories"></div></div>
    <div class="bloc"><h2>Tags</h2><div class="tags" id="tags"></div></div>
  </aside>
  <main>
    <div class="barre">
      <div><span class="resultats" id="nb-resultats"></span> <span class="filtres-actifs" id="filtres-actifs"></span></div>
      <button class="reset" id="reset">Réinitialiser les filtres</button>
    </div>
    <div class="grille" id="grille"></div>
  </main>
</div>
<script id="donnees" type="application/json">__DONNEES__</script>
<script>
const D = JSON.parse(document.getElementById("donnees").textContent);
const E = D.entrees;
const etat = { q: "", plateforme: "", categorie: "", sous: "", tags: new Set() };
const $ = id => document.getElementById(id);

const norm = s => (s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const plat = e => norm(e.plateforme).includes("insta") ? "Instagram" : norm(e.plateforme).includes("tiktok") ? "TikTok" : e.plateforme;
E.forEach(e => {
  e._plat = plat(e);
  e._texte = norm([e.titre, e.auteur, e.contenu, e.categorie, e.sous_themes.join(" "), e.tags.join(" "), e.outils].join(" \n "));
});

// Chaque filtre compte les elements en tenant compte de tous les AUTRES filtres (facettes).
function passe(e, sauf) {
  if (sauf !== "q" && etat.q && !norm(etat.q).split(/\s+/).every(m => e._texte.includes(m))) return false;
  if (sauf !== "plateforme" && etat.plateforme && e._plat !== etat.plateforme) return false;
  if (sauf !== "categorie" && sauf !== "sous" && etat.categorie && e.categorie !== etat.categorie) return false;
  if (sauf !== "sous" && etat.sous && !e.sous_themes.includes(etat.sous)) return false;
  if (sauf !== "tags") for (const t of etat.tags) if (!e.tags.includes(t)) return false;
  return true;
}
const compter = (sauf, test) => E.filter(e => passe(e, sauf) && test(e)).length;

function bouton(label, nb, actif, onclick, classe = "nav-btn") {
  const b = document.createElement("button");
  b.className = classe + (actif ? " actif" : "");
  b.innerHTML = `<span>${esc(label)}</span><span class="nb">${nb}</span>`;
  if (!nb && !actif) b.disabled = true;
  b.onclick = onclick;
  return b;
}

function rendrePlateformes() {
  const box = $("plateformes"); box.innerHTML = "";
  [["", "Tous"], ["TikTok", "TikTok"], ["Instagram", "Instagram"]].forEach(([v, l]) => {
    const b = document.createElement("button");
    const nb = compter("plateforme", e => !v || e._plat === v);
    b.className = etat.plateforme === v ? "actif" : "";
    b.innerHTML = `${l} <span class="nb">${nb}</span>`;
    b.onclick = () => { etat.plateforme = v; rendre(); };
    box.appendChild(b);
  });
}

function rendreCategories() {
  const box = $("categories"); box.innerHTML = "";
  box.appendChild(bouton("Tous", compter("categorie", () => true), !etat.categorie,
    () => { etat.categorie = ""; etat.sous = ""; rendre(); }));
  D.categories.forEach(c => {
    const nb = compter("categorie", e => e.categorie === c.nom);
    const ouverte = etat.categorie === c.nom;
    box.appendChild(bouton(c.nom, nb, ouverte && !etat.sous,
      () => { etat.categorie = c.nom; etat.sous = ""; rendre(); }));
    if (ouverte) c.sous_themes.forEach(s => {
      const n = compter("sous", e => e.categorie === c.nom && e.sous_themes.includes(s));
      box.appendChild(bouton(s, n, etat.sous === s,
        () => { etat.sous = etat.sous === s ? "" : s; rendre(); }, "nav-btn sous"));
    });
  });
}

function rendreTags() {
  const box = $("tags"); box.innerHTML = "";
  const freq = {};
  E.forEach(e => e.tags.forEach(t => freq[t] = (freq[t] || 0) + 1));
  D.tags_frequents.forEach(t => freq[t] ??= 0);
  const tous = Object.keys(freq).sort((a, b) => freq[b] - freq[a] || a.localeCompare(b, "fr"));
  tous.forEach(t => {
    const actif = etat.tags.has(t);
    const nb = compter("tags", e => [...etat.tags, t].every(x => e.tags.includes(x)));
    if (!nb && !actif) return;
    const b = document.createElement("button");
    b.className = "tag" + (actif ? " actif" : "");
    b.innerHTML = `${esc(t)}<span class="nb">${nb}</span>`;
    b.onclick = () => { actif ? etat.tags.delete(t) : etat.tags.add(t); rendre(); };
    box.appendChild(b);
  });
  if (!box.children.length) box.innerHTML = `<span class="nb">Aucun tag</span>`;
}

function carte(e) {
  const tags = e.tags.map(t => `<button class="tag${etat.tags.has(t) ? " actif" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("");
  const sous = e.sous_themes.map(s => `<span class="pill sous">${esc(s)}</span>`).join("");
  const lien = /^https?:\/\//i.test(e.lien) ? e.lien : "#";
  return `<article class="carte">
    <h3>${esc(e.titre)}</h3>
    <div class="meta"><span class="plateforme ${norm(e._plat)}">${esc(e._plat)}</span><span>@${esc(e.auteur.replace(/^@/, ""))}</span></div>
    <div class="classif">${e.categorie ? `<span class="pill cat">${esc(e.categorie)}</span>` : ""}${sous}</div>
    <p>${esc(e.contenu)}</p>
    ${e.outils ? `<div class="outils"><strong>Outils / ressources :</strong> ${esc(e.outils)}</div>` : ""}
    ${tags ? `<div class="tags">${tags}</div>` : ""}
    <a class="voir" href="${esc(lien)}" target="_blank" rel="noopener noreferrer">Voir la vidéo ↗</a>
  </article>`;
}

function rendre() {
  rendrePlateformes(); rendreCategories(); rendreTags();
  const res = E.filter(e => passe(e));
  $("nb-resultats").textContent = `${res.length} résultat${res.length > 1 ? "s" : ""}`;
  const actifs = [etat.plateforme, etat.categorie, etat.sous, ...etat.tags].filter(Boolean);
  if (etat.q) actifs.unshift(`« ${etat.q} »`);
  $("filtres-actifs").textContent = actifs.length ? "· " + actifs.join(" › ") : "";
  $("reset").style.visibility = actifs.length ? "visible" : "hidden";
  $("grille").innerHTML = res.length ? res.map(carte).join("") :
    `<div class="vide"><strong>Aucun contenu ne correspond à ces critères.</strong>
     Essaie un autre mot-clé ou retire un filtre.<br><button class="reset" onclick="reinit()">Réinitialiser les filtres</button></div>`;
}

function reinit() {
  Object.assign(etat, { q: "", plateforme: "", categorie: "", sous: "" }); etat.tags.clear();
  $("recherche").value = ""; rendre();
}

$("total").textContent = `${E.length} contenu${E.length > 1 ? "s" : ""} indexé${E.length > 1 ? "s" : ""}`;
$("recherche").addEventListener("input", ev => { etat.q = ev.target.value.trim(); rendre(); });
$("reset").onclick = reinit;
$("toggle-filtres").onclick = () => $("filtres").classList.toggle("ouvert");
$("grille").addEventListener("click", ev => {
  const t = ev.target.closest("[data-tag]"); if (!t) return;
  const v = t.dataset.tag; etat.tags.has(v) ? etat.tags.delete(v) : etat.tags.add(v); rendre();
});
rendre();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
