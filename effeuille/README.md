# 🌹 Effeuille — le jeu quotidien qui a du charme

**Concept** : un mot à deviner par jour (façon Sutom/Motus). Chaque essai défloute un peu
plus la photo de charme de la « muse du jour », une créatrice partenaire. Gagné → photo
dévoilée + fiche de la créatrice avec liens vers son MYM/OnlyFans/Instagram. Perdu → la
photo reste voilée… et le bouton « La découvrir en entier 🔥 » pointe vers son espace
(c'est le moment où la frustration convertit le mieux).

**Le modèle** : le site reste 100 % soft (lingerie/suggestif, jamais explicite), le contenu
explicite est chez les créatrices, derrière leurs propres paywalls. Toi tu apportes du
trafic, elles apportent les photos et leur audience. Tout le monde y gagne.

---

## Lancer le prototype

C'est un fichier statique unique, zéro build :

```bash
# en local
open effeuille/index.html        # ou python3 -m http.server dans le dossier

# en prod : nouveau site Netlify, publish directory = effeuille/
# (ne pas toucher au netlify.toml existant qui sert Synqio depuis frontend/)
```

### À configurer avant mise en ligne

1. **Les muses** : remplacer les entrées `PUZZLES` dans `index.html` (mots, noms, bios,
   liens). Les profils actuels sont **fictifs** (marqués « démo »).
2. **Les photos** : remplacer le placeholder dégradé (`.photo-art` + 🌹) par les vraies
   photos fournies par les créatrices (`<img>` dans `.photo`). Format portrait ~4:5,
   ≥ 1200 px de large.
3. **Les liens** : `mailto:contact@VOTRE-DOMAINE.fr` (2 occurrences), mentions légales,
   nom de domaine dans le partage.
4. **Le domaine** : effeuille.fr / effeuille.app / devoile.fr — vérifier la dispo.

---

## 💶 La monétisation, version honnête

| Canal | Comment ça paie | Réalité |
|---|---|---|
| **Parrainage plateforme** | OnlyFans reverse ~5 % des revenus des **créatrices que tu fais s'inscrire** via ton lien (12 mois). MYM a un programme de parrainage équivalent. | Ne paie que pour les créatrices *nouvelles* sur la plateforme. Intéressant si tu recrutes des modèles Instagram pas encore sur MYM. |
| **Feature payante** | Une fois l'audience installée : les créatrices paient pour être « muse du jour » (forfait ou enchère). C'est le modèle shoutout, standard dans le milieu. | Le vrai revenu à terme. 500-2000 visiteurs/jour = tu peux facturer. |
| **Affiliation lingerie / sextoys** | Programmes d'affiliation mainstream (marques de lingerie, Dorcel Store, Espace Plaisir… 10-25 % de commission), bandeau discret « la sélection de la muse ». | Compatible AdSense-free, paie correctement, thématiquement parfait. |
| **Archive premium** | Accès aux jours passés + mode « zen » sans flou : petit abonnement (2-3 €/mois). | Le site étant soft, **Stripe reste utilisable** — c'est tout l'intérêt de rester du bon côté de la ligne. |

À éviter : les régies pub adultes (CPM faibles, pubs dégradantes, ça tue l'image
« charme élégant » qui est ta différenciation).

## 📈 La boucle d'acquisition

1. Chaque muse **partage son jour** à son audience (« je suis la muse du jour sur
   Effeuille, viens me dévoiler 😏 ») — c'est gratuit pour toi et c'est dans son intérêt.
2. Son audience joue, met le site en favori, **revient demain… et découvre une autre muse**.
3. Le partage de score en emojis (intégré) fait le reste sur X/Twitter, où le contenu
   charme circule librement.
4. Compte X du jeu : tease quotidien de la photo floutée à 10 h (l'heure de Bonjour
   Madame, le clin d'œil que les trentenaires français comprendront).

Recrutement des premières muses : DM aux créatrices FR petites/moyennes (1-50k abonnés)
sur X/Insta. Pitch : « jeu quotidien, audience FR, on met ta photo soft + tes liens en
avant toute la journée, gratuit, tu gardes tous tes droits ». Viser 30 muses = 1 mois de
contenu. Les petites créatrices cherchent activement de la visibilité gratuite.

## ⚖️ Checklist légale (à faire AVANT le lancement public)

- [ ] **Consentement écrit** de chaque créatrice : photo fournie par elle, cession de droit
      de diffusion non exclusive et révocable, copie de pièce d'identité prouvant sa
      majorité. Un mail clair + un PDF d'une page suffisent pour démarrer.
- [ ] **Rester soft** : lingerie, suggestif, artistique. Pas de nudité explicite ni de
      caractère pornographique → on reste hors du champ de la vérification d'âge ARCOM
      (loi SREN). C'est la ligne rouge du projet : la faire valider une fois par un avocat
      (~300 €) avec 3-4 photos types comme référence.
- [ ] **Age gate 18+** (déjà intégré) + mention dans les CGU.
- [ ] **Mentions légales** réelles (éditeur, hébergeur) — modal déjà en place, à compléter.
- [ ] **RGPD** : v1 n'a aucune collecte (stats en localStorage uniquement) → pas de bannière
      cookies nécessaire tant que tu n'ajoutes pas d'analytics. Si analytics : Plausible
      plutôt que GA.
- [ ] **Retrait express** : engagement de dépublication sous 24 h si une muse retire son
      consentement. Ça doit figurer dans l'accord.

## 🗺️ Roadmap

- **v1 (ce prototype)** : statique, puzzles embarqués, parfait pour valider avec 5-10
  muses réelles et mesurer la rétention.
- **v2** : petit backend FastAPI sur Railway (même stack que Synqio) — admin d'upload
  pour planifier les muses, dictionnaire de validation des mots, stats serveur,
  flou re-généré côté serveur (pour que la photo nette ne soit pas téléchargeable
  avant d'avoir gagné — en v1 elle est dans le DOM, c'est un prototype).
- **v3** : archive premium via Stripe, espace créatrice self-service, classements.

---

*Prototype : les 10 profils inclus sont fictifs, les dégradés remplacent les photos.
Aucune photo réelle ne doit être mise en ligne sans l'accord écrit décrit ci-dessus.*
