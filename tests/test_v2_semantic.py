import _isolation  # noqa: F401  (EN PREMIER : isole le vrai Vault)

import os
import time
import unittest
from unittest import mock

import numpy as np

import vault_ask
import vault_embeddings as embeddings
import vault_intelligence_context as context
from _v2_fixtures import add_source, make_db

CORPUS = [
    ("chauffage", "Faire baisser sa facture d'électricité à la maison",
     "Isoler les combles, régler le thermostat à 19 degrés et profiter des heures creuses "
     "permet de diminuer nettement la facture d'énergie du foyer en hiver."),
    ("rtk", "RTK pour les agents de code",
     "RTK évite à l'IA de lire des textes inutiles et réduit la consommation de tokens."),
    ("linkedin", "Prospection LinkedIn automatisée",
     "Un agent qui trouve des leads sur LinkedIn et rédige des messages personnalisés."),
    ("hebergeur", "Héberger un site web gratuitement",
     "Sept hébergeurs gratuits pour mettre un site statique en ligne, avec leurs limites."),
    ("pentest", "Un pentester automatique en open source",
     "Un outil de tests d'intrusion qui scanne les failles de sécurité d'une application."),
    ("seo", "Rédiger des articles optimisés pour Google",
     "Un agent qui écrit des articles de blog et trouve des backlinks pour le référencement."),
    ("aws", "Démarrer sur Amazon Web Services",
     "Les ressources gratuites et les certifications pour apprendre le cloud AWS."),
    ("minto", "La pyramide de Minto pour écrire clairement",
     "Commencer par la conclusion, puis les arguments, pour des textes de consultant."),
    ("git", "Transformer un dépôt GitHub en texte",
     "Gitingest convertit un repository de code en un fichier lisible par un modèle."),
    ("cv", "Injections de prompt cachées dans des CV",
     "Des candidats cachent du texte blanc pour tromper les logiciels de tri des recruteurs."),
    ("motion", "Motion design avec une IA : le résultat",
     "Générer une animation vidéo à partir d'un seul prompt, sans logiciel de montage. "
     "J'ai testé, voilà le résultat."),
    ("immo", "Acheter une villa via une société",
     "Détenir un bien immobilier par une SPV et publier son plan de financement."),
    ("contexte", "Reprendre une conversation IA ailleurs",
     "Un skill qui résume l'échange pour le continuer dans une nouvelle fenêtre de chat."),
    ("vibe", "Recréer un SaaS payant avec l'IA",
     "Un site estime si un logiciel payant peut être recodé soi-même en un week-end."),
    ("formation", "Formations gratuites en intelligence artificielle",
     "Des cours certifiants et gratuits pour comprendre l'IA générative."),
    ("tokenscode", "Coder avec un agent : huit habitudes",
     "Planifier, découper les tâches et relire le code produit par l'agent."),
    ("grok", "Tester un nouveau chatbot gratuitement",
     "Un nouveau bot conversationnel est disponible sans abonnement pendant un temps."),
    ("assurance", "L'assureur profite de ta flemme",
     "Comparer les contrats chaque année évite de payer trop cher son assurance."),
    ("claudeos", "Organiser ses projets avec Claude",
     "Un système de dossiers et de mémoire pour piloter plusieurs projets avec un assistant."),
    ("agence", "Lancer son agence d'automatisation",
     "Trouver ses premiers clients et vendre des automatisations aux PME."),
    ("threed", "Des rendus 3D sur son site",
     "Une bibliothèque pour intégrer des scènes 3D interactives dans une page web."),
    ("skills", "Des compétences pour agents de code",
     "Un dépôt de skills réutilisables pour spécialiser un assistant de programmation."),
    ("tri", "Classer ses emails automatiquement",
     "Des règles et un assistant pour trier la boîte de réception chaque matin."),
    ("design", "Des interfaces moins banales avec l'IA",
     "Trois astuces pour que les maquettes générées aient un style plus original."),
]


def build_corpus():
    conn = make_db()
    ids = {}
    for index, (key, title, transcript) in enumerate(CORPUS):
        ids[key] = add_source(conn, f"https://example.test/{key}", title,
                              author=f"auteur{index}", transcript=transcript)
    return conn, ids


@unittest.skipUnless(embeddings.OnnxE5Backend().available(), "modele d'embedding local absent")
class SemanticSearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = embeddings.OnnxE5Backend()
        cls.conn, cls.ids = build_corpus()
        started = time.perf_counter()
        cls.stats = embeddings.index_sources(cls.conn, cls.backend)
        cls.index_seconds = time.perf_counter() - started

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # V2-10 : question formulee differemment du texte source -> source retrouvee.
    def test_paraphrase_finds_source_without_shared_words(self):
        question = "comment réduire ce que coûte le chauffage de mon appartement"
        self.assertEqual(context.search_saved_sources(self.conn, question), [],
                         "le lexical seul ne doit rien trouver (sinon le test ne prouve rien)")
        results, info = context.search_sources_hybrid(self.conn, question, backend=self.backend)
        self.assertEqual(info["mode"], "hybrid")
        self.assertTrue(results, info)
        top = results[0]
        self.assertEqual(top["source_id"], self.ids["chauffage"])
        self.assertEqual(top["retrieval"]["match_type"], "semantic")
        self.assertGreaterEqual(top["retrieval"]["semantic_z"], embeddings.SEMANTIC_ONLY_MIN_Z)
        # Provenance conservee.
        self.assertEqual(top["url"], "https://example.test/chauffage")
        self.assertTrue(top["snippet"])

    def test_unrelated_question_returns_nothing(self):
        for question in ("résultats du match de football hier soir",
                         "recette de la tarte aux pommes de ma grand-mère"):
            results, _ = context.search_sources_hybrid(self.conn, question, backend=self.backend)
            self.assertEqual(results, [], question)

    def test_semantic_vetoes_weak_unrelated_lexical_match(self):
        question = "résultats du match de football hier soir"
        lexical = context.search_saved_sources(self.conn, question)
        self.assertEqual([r["source_id"] for r in lexical], [self.ids["motion"]],
                         "le mot generique 'resultat' doit matcher en lexical seul")
        results, info = context.search_sources_hybrid(self.conn, question, backend=self.backend)
        self.assertEqual(results, [])
        self.assertEqual(info["semantic_vetoed"], [self.ids["motion"]])

    def test_lexical_ranking_is_preserved(self):
        results, info = context.search_sources_hybrid(
            self.conn, "économiser des tokens avec RTK", backend=self.backend)
        self.assertEqual(results[0]["source_id"], self.ids["rtk"])
        self.assertIn(results[0]["retrieval"]["match_type"], ("lexical", "hybrid"))

    def test_ask_presents_semantic_only_results_as_to_verify(self):
        result = vault_ask.ask(self.conn, "comment réduire ce que coûte le chauffage de mon appartement",
                               llm="off", semantic_backend=self.backend)
        self.assertIn("Rapprochement de sens uniquement", result["answer"])
        self.assertIn("à vérifier", result["answer"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["related_sources"][0]["source_id"], self.ids["chauffage"])

    def test_threshold_is_calibrated_above_offtopic_noise(self):
        calibration = self.stats["calibration"]
        self.assertGreaterEqual(calibration["threshold_z"], embeddings.SEMANTIC_ONLY_MIN_Z)
        self.assertAlmostEqual(
            calibration["threshold_z"],
            max(embeddings.SEMANTIC_ONLY_MIN_Z,
                calibration["probe_max_z"] + embeddings.CALIBRATION_MARGIN_Z),
            places=2,
        )
        self.assertEqual(embeddings.semantic_threshold(self.conn), calibration["threshold_z"])

    def test_query_speed(self):
        started = time.perf_counter()
        context.search_sources_hybrid(self.conn, "sécurité des applications", backend=self.backend)
        self.assertLess(time.perf_counter() - started, 2.0)


@unittest.skipUnless(embeddings.OnnxE5Backend().available(), "modele d'embedding local absent")
class IndexMaintenanceTests(unittest.TestCase):
    def test_incremental_detection_and_rebuild(self):
        conn, ids = build_corpus()
        try:
            backend = embeddings.OnnxE5Backend()
            first = embeddings.index_sources(conn, backend)
            self.assertEqual(first["indexed"], len(CORPUS))
            second = embeddings.index_sources(conn, backend)
            self.assertEqual((second["indexed"], second["unchanged"]), (0, len(CORPUS)))
            conn.execute("UPDATE contents SET transcript = transcript || ' Mise à jour.' "
                         "WHERE id = (SELECT content_id FROM intelligence_sources WHERE id = ?)",
                         (ids["rtk"],))
            conn.commit()
            third = embeddings.index_sources(conn, backend)
            self.assertEqual(third["indexed"], 1)
            rebuilt = embeddings.index_sources(conn, backend, rebuild=True)
            self.assertEqual(rebuilt["indexed"], len(CORPUS))
            self.assertEqual(embeddings.index_status(conn)["indexed_sources"], len(CORPUS))
        finally:
            conn.close()


class FakeBackend:
    """Vecteurs deterministes, pour tester la plomberie sans modele."""

    model = embeddings.MODEL_ID

    def embed_passage(self, text):
        rng = np.random.default_rng(abs(hash(text)) % (2 ** 32))
        vector = rng.normal(size=8).astype("float32")
        return vector / np.linalg.norm(vector)

    embed_query = embed_passage


class BrokenBackend(FakeBackend):
    def embed_query(self, text):
        raise RuntimeError("onnxruntime a plante")


class SlowBackend(FakeBackend):
    def embed_query(self, text):
        time.sleep(3)
        return super().embed_query(text)


class FallbackTests(unittest.TestCase):
    """V2-11 : embeddings indisponibles -> la recherche lexicale fonctionne."""

    def setUp(self):
        self.conn, self.ids = build_corpus()

    def tearDown(self):
        self.conn.close()

    def check_lexical(self, results, info, expected_mode):
        self.assertEqual(info["mode"], expected_mode, info)
        self.assertEqual(results[0]["source_id"], self.ids["rtk"])

    def test_no_index_falls_back_to_lexical(self):
        results, info = context.search_sources_hybrid(
            self.conn, "économiser des tokens avec RTK", backend=FakeBackend())
        self.check_lexical(results, info, "lexical_fallback")
        self.assertEqual(info["semantic_status"], "unavailable")

    def test_broken_backend_falls_back_to_lexical(self):
        embeddings.index_sources(self.conn, FakeBackend())
        results, info = context.search_sources_hybrid(
            self.conn, "économiser des tokens avec RTK", backend=BrokenBackend())
        self.check_lexical(results, info, "lexical_fallback")
        self.assertTrue(info["semantic_status"].startswith("error"))

    def test_slow_backend_times_out_to_lexical(self):
        embeddings.index_sources(self.conn, FakeBackend())
        started = time.perf_counter()
        results, info = context.search_sources_hybrid(
            self.conn, "économiser des tokens avec RTK", backend=SlowBackend(),
            semantic_timeout=0.5)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.check_lexical(results, info, "lexical_fallback")
        self.assertEqual(info["semantic_status"], "timeout")

    def test_model_missing_or_disabled(self):
        missing = embeddings.OnnxE5Backend(model_dir=_isolation.TMP / "pas-de-modele")
        self.assertFalse(missing.available())
        with self.assertRaises(embeddings.EmbeddingUnavailable):
            missing.embed_query("x")
        embeddings.index_sources(self.conn, FakeBackend())
        with mock.patch.dict(os.environ, {"VAULT_SEMANTIC": "off"}), \
                mock.patch.object(embeddings, "_DEFAULT_BACKEND", None):
            self.assertIsNone(embeddings.default_backend())
            result = vault_ask.ask(self.conn, "économiser des tokens avec RTK", llm="off")
        self.assertEqual(result["retrieval"]["mode"], "lexical_fallback")
        self.assertIn("RTK", result["answer"])


if __name__ == "__main__":
    unittest.main()
