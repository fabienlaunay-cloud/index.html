"""Garde-fous de l'offre unique à 45 €.

Deux catalogues décrivent les plans : `SIGNUP_PLANS` (ce qu'on vend) et
`PLAN_QUOTAS` (ce qui fait tourner les comptes). Ils ont divergé par le passé ;
ces tests rendent la divergence impossible à livrer.
"""
from app.services import billing, usage


def test_une_seule_offre_souscriptible():
    assert list(billing.SIGNUP_PLANS) == ["standard"]
    assert len(billing.public_plans()) == 1


def test_offre_a_45_sans_setup_ni_engagement():
    p = billing.public_plans()[0]
    assert (p["monthly"], p["amount"], p["setup"]) == (45, 45, 0)
    assert p["interval"] == "month"
    assert p["commitment_months"] == 0
    # Rien à payer en plus le premier mois : c'est l'argument de l'offre.
    assert p["first_payment"] == 45


def test_quota_annonce_et_quota_applique_concordent():
    """Le texte de la page de tarifs doit décrire le quota réellement appliqué."""
    p = billing.public_plans()[0]
    q = usage.PLAN_QUOTAS[p["slug"]]
    assert q["skus"] == 100 and q["images"] == 200
    assert p["skus"] == "100 SKU / mois"
    assert q["price_monthly"] == p["monthly"]
    assert q["price_setup"] == p["setup"]
    assert not q["annual_pool"]


def test_anciennes_offres_conservees_pour_les_comptes_existants():
    """Fermées à la vente, mais toujours décrites : des comptes les portent."""
    for ancien in ("starter", "business", "scale", "maintenance"):
        assert ancien in usage.PLAN_QUOTAS
        assert ancien not in billing.SIGNUP_PLANS


def test_aucun_plan_ne_propose_d_upgrade():
    """Offre unique : plus de palier au-dessus, y compris pour les anciens."""
    for plan in usage.PLAN_QUOTAS:
        assert usage.get_upgrade_suggestion(plan) is None


def test_le_plan_par_defaut_existe_des_deux_cotes():
    assert billing.DEFAULT_PLAN == usage.DEFAULT_PLAN == "standard"
    assert usage.DEFAULT_PLAN in usage.PLAN_QUOTAS
    assert billing.DEFAULT_PLAN in billing.SIGNUP_PLANS


def test_le_webhook_reconnait_le_montant_de_l_offre():
    from app.routes.stripe_webhook import _AMOUNT_TO_PLAN
    assert _AMOUNT_TO_PLAN[4500] == "standard"
