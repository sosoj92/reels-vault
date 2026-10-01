# Prompt maître — Diagnostiquer sans casser

Tu diagnostiques un LIS existant. Ne modifie rien au départ.

1. Lis FINAL_ARCHITECTURE.md et le module deployment-kit correspondant au symptôme.
2. Capture branche, SHA, git status, processus actifs, tâches Windows, logs pertinents, état providers.
3. Reproduis le problème de la manière la moins destructive.
4. Sépare configuration / environnement / donnée / réseau-provider / bug code.
5. Cherche une cause observable, pas une hypothèse.
6. Ne lis/affiche aucun secret.
7. Si le problème est configuration/environnement, corrige uniquement après mon accord.
8. Si le code doit changer : arrête-toi, explique le bug et son test, crée une branche dédiée après validation, ajoute/répare le test, rien directement sur main.
9. Après correction : tests ciblés, suite pertinente, test réel, rapport.
10. Ne merge pas sans autorisation explicite.

Rapport :
CAUSE
PREUVE
IMPACT
CORRECTION PROPOSÉE
RISQUE
TEST DE VALIDATION
