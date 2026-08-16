"""
ai_enrichment_monetique.py — Classification IA dédiée "Support Monétique".

Traitement indépendant de UC1 (ai_enrichment.py) : classe les tickets où
service_subcategory == "Support Monétique" selon un référentiel métier figé
(3 macro-catégories iTop, 17 thèmes fins avec responsabilité Métier/BIAT-IT/
Les deux), à partir de ticket_title + ticket_description — PAS de
root_cause_raw/solution comme UC1.

Module entièrement optionnel et indépendant : si MONETIQUE_AI_ENABLED=False ou
GROQ_API_KEY absente, classify_monetique() ne fait aucun appel réseau (colonnes
monetique_* à NaN) — comportement symétrique à enrich_dataframe() pour UC1.

RÈGLE DE RÉDACTION (non négociable, héritée de UC1) : redact() est appliqué à
tout champ envoyé à l'API. On n'envoie jamais une ligne complète : uniquement
ticket_title + ticket_description (rédigés).

CACHE INCRÉMENTAL — voir classify_monetique() pour le détail des cas
(nouveau / déjà classé / texte vidé). En flux normal, une référence déjà
présente dans le cache n'est pas reclassée, même si son texte a évolué.
"""

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ai_enrichment import (
    _CHARS_PER_TOKEN,
    _GROQ_PRICE_PER_1K_TOKENS_USD,
    _call_groq_batch,
    _hash_text,
    _load_cache,
    _save_cache,
    GroqClient,
    redact,
)
from config import Config
from logger_config import setup_logger

logger = setup_logger(__name__)

_MONETIQUE_SUBCATEGORY = "Support Monétique"   # valeur contrôlée iTop, identique
                                                # à la clé SUBCATEGORY_MANAGERS de config.py


# ══════════════════════════════════════════════════════════════════════════════
# Référentiel métier (figé — ne pas reformuler les libellés)
# ══════════════════════════════════════════════════════════════════════════════

MONETIQUE_MACRO_CATEGORIES: dict[str, str] = {
    "Réclamations Clients (Service Monétique)": "1.2 Support et Services Informatiques (IT) → 1.3 Applications Bancaires (IT) → Réclamations Clients (Service Monétique)",
    "Demande d'investigation (Service Monétique)": "1.2 Support et Services Informatiques (IT) → 3.4 Demandes de Services IT → Demande d'investigation (Service Monétique)",
    "Demande d'extraction de données": "1.2 Support et Services Informatiques (IT) → 3.4 Demandes de Services IT → Demande d'extraction de données",
}

# Chaque thème reçoit une macro_category (non fournie explicitement par le
# tableau métier source, qui ne lie que theme -> responsabilite/actions) —
# affectation faite par analogie avec le chemin iTop le plus proche : les
# thèmes orientés réclamation/litige client vont sous "Réclamations Clients",
# les thèmes orientés investigation/opération technique sous "Demande
# d'investigation", les demandes de données sous "Demande d'extraction de
# données". À ajuster si le métier valide un autre découpage.
MONETIQUE_THEMES: list[dict] = [
    {
        "theme": "Ajout / gestion MCC",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "Métier",
        "action_metier": "Ajout via écran dédié",
        "action_it": "Création/maintenance de l'écran (RM8)",
    },
    {
        "theme": "Mise à jour Pack MXP",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Ticket Helpdesk transféré à l'équipe Production",
    },
    {
        "theme": "Gestion Compte/Client (compte inexistant)",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Procédure et traitement par BIAT IT",
    },
    {
        "theme": "Remboursement anticipé (Tempo / Flexy)",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Tempo : BIAT IT direct — Flexy : procédure existante BIAT IT",
    },
    {
        "theme": "Litige / Chargeback",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Qualification selon la sous-catégorie",
        "action_it": "Traitement selon le besoin identifié",
    },
    {
        "theme": "Fonctionnement TPE / Carte",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Première qualification métier",
        "action_it": "Expertise technique si nécessaire",
    },
    {
        "theme": "Règlement affilié (encaissement)",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Première analyse métier",
        "action_it": "Analyse approfondie monétique / optimisation (RM8/RM9)",
    },
    {
        "theme": "AMEX",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Contrôle fichier/bordereau avec SMT, transmission à BIAT IT",
        "action_it": "Vérification intégration et crédit commerçant",
    },
    {
        "theme": "E-commerce",
        "macro_category": "Réclamations Clients (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Analyse des écarts à partir des états fournis",
        "action_it": "Fourniture des états des fichiers reçus/intégrés/non reçus",
    },
    {
        "theme": "Blocage intégration journée (nationale/internationale)",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Expertise technique requise",
    },
    {
        "theme": "Différence comptable après intégration journée internationale",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Première analyse Métier Monétique/Comptabilité",
        "action_it": "Analyse technique et correction si nécessaire",
    },
    {
        "theme": "Restauration transaction archivée",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Possible si écran dédié mis à disposition",
        "action_it": "Création d'écran(s) dédiés pour autonomie métier",
    },
    {
        "theme": "Recyclage transactions rejetées",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Expertise technique requise (Mastercard)",
    },
    {
        "theme": "Modification solde SWIFT",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Expertise technique requise",
    },
    {
        "theme": "Demande de données (base, hors réglementaire)",
        "macro_category": "Demande d'extraction de données",
        "responsabilite": "Les deux",
        "action_metier": "Consultation en lecture seule sur environnement dédié",
        "action_it": "Mise en place de l'environnement dédié",
    },
    {
        "theme": "Demande de données (réglementaire)",
        "macro_category": "Demande d'extraction de données",
        "responsabilite": "BIAT-IT",
        "action_metier": None,
        "action_it": "Expertise technique requise",
    },
    {
        "theme": "Demande d'investigation (transaction / chargeback / reversal)",
        "macro_category": "Demande d'investigation (Service Monétique)",
        "responsabilite": "Les deux",
        "action_metier": "Première analyse avec transmission des références/justificatifs",
        "action_it": "Investigation technique et analyse détaillée",
    },
]

_MONETIQUE_FALLBACK_THEME = "Autre / à qualifier"

# Entrée spéciale hors référentiel des 17 thèmes (donc PAS dans MONETIQUE_THEMES,
# qui doit rester à 17 éléments) — utilisée à la fois quand le LLM répond
# explicitement "Autre / à qualifier" et quand il hallucine un thème inconnu.
_AUTRE_A_QUALIFIER_ENTRY: dict = {
    "theme": _MONETIQUE_FALLBACK_THEME,
    "macro_category": None,
    "responsabilite": "À qualifier",
    "action_metier": None,
    "action_it": None,
}

_THEME_LOOKUP: dict[str, dict] = {t["theme"]: t for t in MONETIQUE_THEMES}
_THEME_LOOKUP[_MONETIQUE_FALLBACK_THEME] = _AUTRE_A_QUALIFIER_ENTRY


# ══════════════════════════════════════════════════════════════════════════════
# Prompt système et validation de sortie LLM
# ══════════════════════════════════════════════════════════════════════════════

_ALLOWED_THEMES_TEXT = ", ".join([t["theme"] for t in MONETIQUE_THEMES] + [_MONETIQUE_FALLBACK_THEME])
_ALLOWED_MACRO_TEXT = ", ".join(MONETIQUE_MACRO_CATEGORIES.keys())

_SYSTEM_PROMPT_MONETIQUE = (
    "Tu es un classificateur de tickets d'incidents IT bancaires spécialisés "
    "'Support Monétique' (cartes, TPE, chargebacks, règlements affiliés). "
    f"Macro-catégories possibles : {_ALLOWED_MACRO_TEXT}. "
    "Thèmes possibles (choisis EXACTEMENT un des libellés suivants, sans jamais "
    f"en inventer un nouveau) : {_ALLOWED_THEMES_TEXT}. "
    "Regroupe les formulations proches vers le thème du référentiel le plus "
    f"proche. Si aucun thème ne correspond avec une confiance suffisante, "
    f"réponds '{_MONETIQUE_FALLBACK_THEME}' avec une confiance basse. "
    "Réponds UNIQUEMENT avec un objet JSON, sans texte autour."
)


def _monetique_fallback_result() -> dict:
    entry = _AUTRE_A_QUALIFIER_ENTRY
    return {
        "macro_category": entry["macro_category"],
        "theme": entry["theme"],
        "responsabilite": entry["responsabilite"],
        "action_metier": entry["action_metier"],
        "action_it": entry["action_it"],
        "confiance": 0.0,
        "resume": "",
    }


def _validate_monetique_classification(result: Any) -> dict:
    """Valide/normalise une classification Monétique unitaire.

    Seul `theme` est réellement retenu de la réponse LLM ; macro_category/
    responsabilite/action_metier/action_it sont TOUJOURS dérivés par lookup
    dans le référentiel — jamais recopiés depuis la sortie du modèle. Garantit
    au niveau du code qu'aucune valeur hors référentiel ne peut être écrite
    pour ces 4 champs, quelle que soit la réponse du LLM.
    """
    theme = result.get("theme") if isinstance(result, dict) else None
    entry = _THEME_LOOKUP.get(theme)

    if entry is None:
        if theme is not None:
            logger.warning(
                f"Thème IA hors référentiel reçu : '{theme}' — repli sur '{_MONETIQUE_FALLBACK_THEME}'."
            )
        entry = _AUTRE_A_QUALIFIER_ENTRY
        confiance = 0.0
    else:
        try:
            confiance = float(result.get("confiance", 0.0))
        except (TypeError, ValueError):
            confiance = 0.0
        confiance = min(max(confiance, 0.0), 1.0)

    return {
        "macro_category": entry["macro_category"],
        "theme": entry["theme"],
        "responsabilite": entry["responsabilite"],
        "action_metier": entry["action_metier"],
        "action_it": entry["action_it"],
        "confiance": confiance,
        "resume": str(result.get("resume") or "") if isinstance(result, dict) else "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Construction du texte / hash de contenu
# ══════════════════════════════════════════════════════════════════════════════

def _build_monetique_text(row: pd.Series) -> str:
    """Concatène et rédige ticket_title + ticket_description d'un ticket
    (PAS root_cause_raw/solution, contrairement à UC1)."""
    parts = []
    for col in ("ticket_title", "ticket_description"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            parts.append(redact(str(val)))
    return " | ".join(parts).strip()


def _normalize_for_hash(value: Any) -> str:
    """Trim + espaces multiples réduits + minuscule — normalisation légère
    pour éviter de re-classifier à cause d'un simple espace/casse en trop."""
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).lower()


def _monetique_content_hash(row: pd.Series) -> str:
    """content_hash = sha256(Object normalisé + '|' + Description normalisée)."""
    title = _normalize_for_hash(row.get("ticket_title"))
    description = _normalize_for_hash(row.get("ticket_description"))
    return _hash_text(f"{title}|{description}")


# ══════════════════════════════════════════════════════════════════════════════
# Appels Groq — unitaire et par lot
# ══════════════════════════════════════════════════════════════════════════════

def _classify_monetique_unitary(client: GroqClient, text: str) -> dict:
    if not text.strip():
        return _monetique_fallback_result()
    user_prompt = (
        "Classe ce ticket Monétique. Réponds avec l'objet JSON "
        '{"macro_categorie": "...", "theme": "...", "confiance": 0.0-1.0, '
        '"resume": "<max 15 mots>"}.\n'
        f"Texte du ticket : {text}"
    )
    try:
        result = client.chat_json(_SYSTEM_PROMPT_MONETIQUE, user_prompt)
    except Exception as exc:
        logger.warning(f"Classification Monétique unitaire échouée : {type(exc).__name__}.")
        return _monetique_fallback_result()
    return _validate_monetique_classification(result)


def _classify_monetique_batch(client: GroqClient, texts: list[str]) -> list[dict]:
    """Classifie un lot de tickets Monétique en un seul appel (coût / 10)."""
    numbered = "\n".join(f"{i + 1}. {t if t.strip() else '(vide)'}" for i, t in enumerate(texts))
    user_prompt = (
        f"Classe les {len(texts)} tickets Monétique suivants, dans l'ordre. Réponds avec "
        '{"resultats": [{"macro_categorie": "...", "theme": "...", "confiance": 0.0-1.0, '
        '"resume": "<max 15 mots>"}, ...]} — le tableau "resultats" doit contenir '
        f"exactement {len(texts)} éléments, dans le même ordre que la liste ci-dessous.\n\n{numbered}"
    )
    return _call_groq_batch(
        client, _SYSTEM_PROMPT_MONETIQUE, user_prompt, n_items=len(texts),
        validate_item=_validate_monetique_classification,
        unitary_fn=lambda: [_classify_monetique_unitary(client, t) for t in texts],
        fallback_result=_monetique_fallback_result(),
        label="Lot Monétique",
    )


# ══════════════════════════════════════════════════════════════════════════════
# Cache — même fichier physique que UC1 (cache/ai_classifications.json),
# namespace réservé "__monetique__"
# ══════════════════════════════════════════════════════════════════════════════

_MONETIQUE_CACHE_KEY = "__monetique__"


def _load_monetique_cache() -> dict[str, dict]:
    return _load_cache().get(_MONETIQUE_CACHE_KEY, {})


def _save_monetique_cache(monetique_cache: dict[str, dict]) -> None:
    # Relit le fichier complet avant d'écrire : évite d'écraser des entrées
    # UC1 sauvegardées entre-temps dans le même run.
    full = _load_cache()
    full[_MONETIQUE_CACHE_KEY] = monetique_cache
    _save_cache(full)


def _get_cached_monetique_classification(
    cache: dict[str, dict],
    reference: str,
) -> tuple[str | None, dict | None]:
    """Retourne une classification existante pour une référence.

    Compatibilité ascendante : accepte aussi les anciennes clés au format
    `reference:hash` si elles existent encore dans le cache.
    """
    if reference in cache:
        return reference, cache[reference]

    prefix = f"{reference}:"
    for key, value in cache.items():
        if key.startswith(prefix):
            return key, value

    return None, None


def _set_cached_monetique_classification(cache: dict[str, dict], reference: str, result: dict) -> None:
    cache[reference] = result


# ══════════════════════════════════════════════════════════════════════════════
# Application au DataFrame
# ══════════════════════════════════════════════════════════════════════════════

def _apply_monetique_classification(df: pd.DataFrame, idx: Any, result: dict) -> None:
    df.at[idx, "monetique_macro_category"] = result["macro_category"]
    df.at[idx, "monetique_theme"] = result["theme"]
    df.at[idx, "monetique_responsabilite"] = result["responsabilite"]
    df.at[idx, "monetique_action_metier"] = result["action_metier"]
    df.at[idx, "monetique_action_it"] = result["action_it"]
    df.at[idx, "monetique_ai_confidence"] = float(result["confiance"])


def _empty_monetique_columns(df: pd.DataFrame) -> pd.DataFrame:
    for col in (
        "monetique_macro_category", "monetique_theme", "monetique_responsabilite",
        "monetique_action_metier", "monetique_action_it",
    ):
        df[col] = pd.NA
    df["monetique_ai_confidence"] = float("nan")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Point d'entrée public
# ══════════════════════════════════════════════════════════════════════════════

def classify_monetique(
    df: pd.DataFrame,
    cfg: Config,
    dry_run: bool = False,
    limit: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Classification IA dédiée "Support Monétique" — ajoute monetique_macro_category,
    monetique_theme, monetique_responsabilite, monetique_action_metier,
    monetique_action_it, monetique_ai_confidence au DataFrame de faits.

    Miroir du contrat de ai_enrichment.enrich_dataframe() (UC1), mais
    indépendant : flag MONETIQUE_AI_ENABLED séparé de AI_ENRICHMENT_ENABLED,
    cache séparé (namespace dédié dans le même fichier), colonnes séparées.

    Seuls les tickets service_subcategory == "Support Monétique" sont dans le
    périmètre. Les 6 colonnes sont toujours ajoutées (NaN hors périmètre, NaN
    aussi si désactivé/sans clé/aucun ticket éligible) — ce module ne doit
    jamais faire échouer le pipeline.

     Cache incrémental basé sur la référence :
        1. Pas d'entrée cache pour cette référence -> classifier.
        2. Entrée présente -> réutiliser, zéro appel API.
        3. Entrée présente mais titre + description désormais vides -> ne PAS
            appeler l'API, colonnes remises à NaN, entrée cache PRÉSERVÉE (audit),
            WARN loggé.

    Returns:
        (df_enrichi, stats) — stats contient les compteurs pour le rapport
        qualité (n_monetique_total, n_monetique_sampled, n_classified,
        n_cache_hit, n_api_calls, n_high_confidence, n_autre_a_qualifier,
        n_stale_cache_text_emptied, responsabilite_counts, estimated_tokens,
        estimated_cost_usd, estimated_cost_usd_total_projected).
    """
    df = df.copy()
    stats: dict[str, Any] = {
        "n_monetique_total": 0, "n_monetique_sampled": 0, "n_classified": 0,
        "n_cache_hit": 0, "n_api_calls": 0, "n_high_confidence": 0,
        "n_autre_a_qualifier": 0, "n_stale_cache_text_emptied": 0,
        "estimated_tokens": 0, "estimated_cost_usd": 0.0,
        "estimated_cost_usd_total_projected": 0.0,
        "responsabilite_counts": {},
    }

    if not dry_run and not cfg.monetique_ai_enabled:
        logger.info("Classification Monétique désactivée (MONETIQUE_AI_ENABLED=False) — colonnes à NaN.")
        return _empty_monetique_columns(df), stats

    if not cfg.groq_api_key:
        logger.info("GROQ_API_KEY absente — classification Monétique ignorée, colonnes à NaN.")
        return _empty_monetique_columns(df), stats

    df = _empty_monetique_columns(df)

    if "service_subcategory" not in df.columns:
        return df, stats

    mask = df["service_subcategory"] == _MONETIQUE_SUBCATEGORY
    monetique_idx_all = df.index[mask].tolist()
    stats["n_monetique_total"] = len(monetique_idx_all)

    monetique_idx = monetique_idx_all[:limit] if limit is not None else monetique_idx_all
    stats["n_monetique_sampled"] = len(monetique_idx)

    if not monetique_idx:
        return df, stats

    client = GroqClient(cfg)
    cache = _load_monetique_cache()

    def _record(idx: Any, reference: str, result: dict) -> None:
        _apply_monetique_classification(df, idx, result)
        stats["n_classified"] += 1
        if result["theme"] == _MONETIQUE_FALLBACK_THEME:
            stats["n_autre_a_qualifier"] += 1
        if result["confiance"] >= cfg.monetique_confidence_threshold:
            stats["n_high_confidence"] += 1
        resp = result["responsabilite"]
        stats["responsabilite_counts"][resp] = stats["responsabilite_counts"].get(resp, 0) + 1

    to_call_idx: list[Any] = []
    to_call_texts: list[str] = []
    to_call_refs: list[str] = []

    for idx in monetique_idx:
        row = df.loc[idx]
        reference = str(row.get("reference", idx))
        text = _build_monetique_text(row)
        _, cached = _get_cached_monetique_classification(cache, reference)

        if not text.strip():
            # Cas 4 — texte désormais vide : pas d'appel API, colonnes déjà
            # NaN par défaut, cache préservé (pas supprimé) pour audit.
            if cached is not None:
                logger.warning(
                    f"Object/Description vidés pour {reference}, classification précédente invalidée"
                )
                stats["n_stale_cache_text_emptied"] += 1
            continue

        if cached is not None:
            # Cas 2 — référence déjà analysée, on réutilise.
            stats["n_cache_hit"] += 1
            _record(idx, reference, cached)
        else:
            # Cas 1 — aucune entrée pour cette référence.
            to_call_idx.append(idx)
            to_call_texts.append(text)
            to_call_refs.append(reference)

    cache_updates: dict[str, dict] = {}
    batch_size = max(cfg.ai_batch_size, 1)
    for start in range(0, len(to_call_idx), batch_size):
        b_idx = to_call_idx[start:start + batch_size]
        b_texts = to_call_texts[start:start + batch_size]
        b_refs = to_call_refs[start:start + batch_size]

        results = _classify_monetique_batch(client, b_texts)
        stats["n_api_calls"] += 1

        batch_updates: dict[str, dict] = {}
        for idx, text, reference, result in zip(b_idx, b_texts, b_refs, results):
            batch_updates[reference] = {
                "macro_category": result["macro_category"],
                "theme": result["theme"],
                "responsabilite": result["responsabilite"],
                "action_metier": result["action_metier"],
                "action_it": result["action_it"],
                "confiance": result["confiance"],
                "classified_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            }
            _record(idx, reference, result)
            stats["estimated_tokens"] += (len(text) + 200) // _CHARS_PER_TOKEN

        # Sauvegarde après CHAQUE lot — voir le commentaire équivalent dans
        # ai_enrichment.py : un job CI tué sur timeout ne doit pas perdre le
        # travail déjà classifié en mémoire.
        if not dry_run and batch_updates:
            cache_updates.update(batch_updates)
            cache.update(batch_updates)
            _save_monetique_cache(cache)

    n_new = len(to_call_idx)
    avg_tokens = (stats["estimated_tokens"] / n_new) if n_new else 0.0
    stats["estimated_cost_usd"] = round(stats["estimated_tokens"] / 1000 * _GROQ_PRICE_PER_1K_TOKENS_USD, 4)

    remaining = max(stats["n_monetique_total"] - stats["n_monetique_sampled"], 0)
    projected_tokens = stats["estimated_tokens"] + remaining * avg_tokens
    stats["estimated_cost_usd_total_projected"] = round(
        projected_tokens / 1000 * _GROQ_PRICE_PER_1K_TOKENS_USD, 4
    )

    logger.info(
        f"Classification Monétique : {stats['n_classified']}/{stats['n_monetique_sampled']} classifié(s) "
        f"({stats['n_cache_hit']} depuis cache, {stats['n_api_calls']} appel(s) API), "
        f"{stats['n_autre_a_qualifier']} 'Autre / à qualifier', "
        f"coût estimé de ce run ~{stats['estimated_cost_usd']}$."
    )
    return df, stats
