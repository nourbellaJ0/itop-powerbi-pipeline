"""
ai_enrichment.py — Enrichissement optionnel des incidents via l'API Groq.

UC1 : classification des causes racines / solutions par LLM (vient compléter
    la classification regex ROOT_CAUSE_PATTERNS de transform.py).
    En flux normal, un ticket déjà présent dans le cache n'est pas
    reclassé : seules les références encore jamais analysées partent à l'IA.
      Modèle économique (cfg.groq_model, ex: llama-3.1-8b-instant) — appelé
      en lots de 10 sur l'ensemble des tickets éligibles.
UC4 : synthèse exécutive mensuelle générée à partir d'agrégats chiffrés.
      Modèle de meilleure qualité (cfg.groq_model_summary, ex: openai/gpt-oss-120b)
      — un seul appel par mois.

Module entièrement optionnel : si AI_ENRICHMENT_ENABLED=False ou GROQ_API_KEY
absente, enrich_dataframe() ne fait aucun appel réseau (colonnes IA à NaN,
un simple log INFO) — le pipeline se comporte exactement comme avant.

RÈGLE DE RÉDACTION (non négociable) : redact() est appliqué à tout champ
envoyé à l'API. On n'envoie jamais une ligne complète : uniquement
root_cause_raw + solution (rédigés) pour UC1, uniquement des agrégats
chiffrés pour UC4. Jamais reference, agent_name, requester_org.
"""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import requests

from config import Config
from logger_config import setup_logger
from transform import ROOT_CAUSE_PATTERNS, _ROOT_CAUSE_FALLBACK

logger = setup_logger(__name__)

_CACHE_DIR = Path(__file__).parent / "cache"
_CACHE_FILE = _CACHE_DIR / "ai_classifications.json"

# Estimation grossière (~4 caractères/token) et tarif approximatif — à ajuster
# selon la tarification Groq en vigueur (et selon le modèle réellement utilisé,
# le tarif variant fortement entre un petit modèle UC1 et un grand modèle UC4).
# Utilisée uniquement à titre indicatif (dry-run et rapport qualité), jamais
# pour une facturation réelle.
_CHARS_PER_TOKEN = 4
_GROQ_PRICE_PER_1K_TOKENS_USD = 0.002

CATEGORIES: list[str] = list(ROOT_CAUSE_PATTERNS.keys()) + [_ROOT_CAUSE_FALLBACK]
_INDETERMINE = "Indéterminé"


# ══════════════════════════════════════════════════════════════════════════════
# Rédaction — masquage des données sensibles avant tout envoi à l'API
# ══════════════════════════════════════════════════════════════════════════════

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
_RE_REF   = re.compile(r"\b(?:MAT|FT)\d+\b", re.IGNORECASE)
_RE_RIB   = re.compile(r"\bTN59\d+\b|\b\d{20}\b", re.IGNORECASE)
_RE_TEL   = re.compile(
    r"(?:\+216|00216)[\s.-]?\d{2}[\s.-]?\d{3}[\s.-]?\d{3}\b"
    r"|\b\d{2}[ .-]\d{3}[ .-]\d{3}\b"
)
_RE_NUM   = re.compile(r"\b\d{8,}\b")


def redact(text: Any) -> str:
    """Masque emails, RIB/IBAN, téléphones, références internes et numéros de
    compte par des placeholders, avant tout envoi à l'API Groq.

    Ordre d'application important : du plus spécifique (REF/RIB/TEL) au plus
    générique (NUM) — sinon le motif générique masquerait partiellement un
    motif plus spécifique (ex : FT26054344260278 → [REF], pas FT[NUM]).
    """
    if text is None or (not isinstance(text, (list, dict)) and pd.isna(text)):
        return ""
    s = str(text)
    s = _RE_EMAIL.sub("[EMAIL]", s)
    s = _RE_REF.sub("[REF]", s)
    s = _RE_RIB.sub("[RIB]", s)
    s = _RE_TEL.sub("[TEL]", s)
    s = _RE_NUM.sub("[NUM]", s)
    return s


# ══════════════════════════════════════════════════════════════════════════════
# Client Groq — REST compatible OpenAI (https://api.groq.com/openai/v1)
# ══════════════════════════════════════════════════════════════════════════════

# Plafond appliqué à l'en-tête Retry-After : évite qu'un lot ne bloque le
# pipeline pendant des minutes/heures si Groq signale un quota journalier
# épuisé — au-delà, on préfère échouer vite (WARN + Indéterminé) et laisser
# le prochain run réessayer.
_MAX_RETRY_AFTER_SECONDS = 60.0


def _parse_retry_after(resp: requests.Response) -> float | None:
    """Lit l'en-tête Retry-After (secondes) renvoyé par Groq sur un 429/5xx.

    Groq respecte le standard HTTP et indique précisément combien de temps
    attendre avant de réessayer — bien plus fiable qu'un backoff exponentiel
    arbitraire côté client, en particulier pour les limites par minute/jour.
    """
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SECONDS)


class GroqAPIError(Exception):
    """Levée en cas d'échec durable de l'API Groq (après épuisement des retries)."""


class GroqClient:
    """Client minimal pour POST {base_url}/chat/completions avec réponse JSON forcée.

    Robustesse : retry exponentiel (3 tentatives) sur 429/5xx et erreurs réseau,
    timeout 30s, rate limiting respectant GROQ_MAX_REQ_PER_MIN *et*
    GROQ_MAX_TOKENS_PER_MIN. La clé API n'est jamais loggée ; les prompts/
    réponses complets ne sont pas loggés non plus.

    Le TPM (tokens/minute) est souvent la contrainte réellement bloquante —
    bien plus stricte que le RPM sur le tier gratuit Groq (ex: 6 000 TPM pour
    llama-3.1-8b-instant, largement dépassé par ~7 appels/min de lots de 10
    tickets alors que le RPM autoriserait 30 appels/min). On suit donc une
    fenêtre glissante de 60s des tokens consommés (comptés via le champ
    "usage" de la réponse Groq) et on attend avant tout appel qui la ferait
    dépasser, en plus du pacing par nombre de requêtes.

    Le modèle peut être surchargé à l'instanciation (UC4 utilise un modèle
    différent — plus qualitatif — de celui d'UC1, appelé bien plus souvent).
    """

    def __init__(self, cfg: Config, model: str | None = None) -> None:
        self._api_key = cfg.groq_api_key
        self._model = model or cfg.groq_model
        self._base_url = cfg.groq_base_url
        self._min_interval = 60.0 / max(cfg.groq_max_req_per_min, 1)
        self._last_call_ts = 0.0
        self._max_tokens_per_min = max(cfg.groq_max_tokens_per_min, 1)
        self._token_window: list[tuple[float, int]] = []

    def _throttle_requests(self) -> None:
        elapsed = time.monotonic() - self._last_call_ts
        wait = self._min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def _estimate_prompt_tokens(self, system_prompt: str, user_prompt: str) -> int:
        # +300 de marge pour la réponse JSON attendue (non connue avant l'appel).
        return (len(system_prompt) + len(user_prompt)) // _CHARS_PER_TOKEN + 300

    def _record_tokens(self, n_tokens: int) -> None:
        self._token_window.append((time.monotonic(), max(n_tokens, 0)))

    def _wait_for_token_budget(self, estimated_tokens: int) -> None:
        """Attend si nécessaire pour rester sous GROQ_MAX_TOKENS_PER_MIN sur une
        fenêtre glissante de 60s, plutôt que de découvrir le 429 après coup."""
        now = time.monotonic()
        self._token_window = [(t, n) for t, n in self._token_window if now - t < 60]
        used = sum(n for _, n in self._token_window)
        if used + estimated_tokens <= self._max_tokens_per_min:
            return
        if not self._token_window:
            return
        oldest_ts = self._token_window[0][0]
        wait = 60.0 - (time.monotonic() - oldest_ts)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
            self._token_window = [(t, n) for t, n in self._token_window if now - t < 60]

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: float = 0.0) -> Any:
        """Appelle l'API Groq en mode JSON forcé, avec retry exponentiel.

        Retourne l'objet JSON parsé de la réponse (content du 1er choix).
        Lève GroqAPIError si les 3 tentatives échouent.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        estimated_tokens = self._estimate_prompt_tokens(system_prompt, user_prompt)

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            self._wait_for_token_budget(estimated_tokens)
            self._throttle_requests()
            self._last_call_ts = time.monotonic()
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=30)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning(f"Groq API — erreur réseau (tentative {attempt}/3) : {type(exc).__name__}.")
                time.sleep(2 ** attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = GroqAPIError(f"HTTP {resp.status_code}")
                wait = _parse_retry_after(resp)
                if wait is None:
                    wait = float(2 ** attempt)
                logger.warning(
                    f"Groq API — HTTP {resp.status_code} (tentative {attempt}/3) "
                    f"— nouvelle tentative dans {wait:.0f}s."
                )
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                raise GroqAPIError(f"Groq API — HTTP {resp.status_code} non récupérable.")

            data = resp.json()
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            self._record_tokens(usage.get("total_tokens") or estimated_tokens)
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)

        raise GroqAPIError(f"Echec après 3 tentatives : {last_exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Appel Groq générique pour un lot — partagé entre tous les cas d'usage
# (UC1, classification Monétique, …)
# ══════════════════════════════════════════════════════════════════════════════

def _call_groq_batch(
    client: GroqClient,
    system_prompt: str,
    user_prompt: str,
    n_items: int,
    validate_item: Callable[[Any], dict],
    unitary_fn: Callable[[], list[dict]],
    fallback_result: dict,
    results_key: str = "resultats",
    label: str = "Lot",
) -> list[dict]:
    """Appelle Groq en JSON forcé pour un lot, avec les deux replis génériques
    communs à tous les cas d'usage :

    - Echec API durable (GroqAPIError, 429/5xx épuisés) -> [fallback_result] *
      n_items, SANS retraitement unitaire (l'API est déjà en échec pour ce lot —
      inutile, et coûteux en quota, de retenter n_items appels qui échoueraient
      probablement pareil).
    - HTTP 200 mais tableau `results_key` de longueur inattendue (glitch
      ponctuel du modèle, pas un problème de quota) -> unitary_fn() (le repli
      unitaire a des chances de réussir).
    - Succès avec la bonne longueur -> [validate_item(x) for x in items].
    """
    try:
        result = client.chat_json(system_prompt, user_prompt)
    except GroqAPIError as exc:
        logger.warning(
            f"{label} de {n_items} ticket(s) — échec API durable ({exc}) — "
            "tickets marqués en repli (pas de retraitement unitaire)."
        )
        return [dict(fallback_result) for _ in range(n_items)]

    items = result.get(results_key) if isinstance(result, dict) else None
    if not isinstance(items, list) or len(items) != n_items:
        got = len(items) if isinstance(items, list) else "N/A"
        logger.warning(
            f"{label} de {n_items} ticket(s) — longueur inattendue ({got} pour "
            f"{n_items} envoyés) — retraitement en unitaire."
        )
        return unitary_fn()

    return [validate_item(item) for item in items]


# ══════════════════════════════════════════════════════════════════════════════
# UC1 — Classification des causes / solutions
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT_UC1 = (
    "Tu es un classificateur de tickets d'incidents IT bancaires. "
    "Catégories possibles (choisis EXACTEMENT un des libellés suivants) : "
    f"{', '.join(CATEGORIES)}. "
    "Réponds UNIQUEMENT avec un objet JSON, sans texte autour. "
    "La 'categorie' doit être obligatoirement l'une des catégories listées "
    f"ci-dessus, ou '{_INDETERMINE}' si le texte fourni est vide ou inexploitable "
    "(dans ce cas, confiance doit être 0)."
)


def _build_uc1_text(row: pd.Series) -> str:
    """Concatène et rédige les champs root_cause_raw + solution d'un ticket."""
    parts = []
    for col in ("root_cause_raw", "solution"):
        val = row.get(col)
        if pd.notna(val) and str(val).strip():
            parts.append(redact(str(val)))
    return " | ".join(parts).strip()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cache_key(reference: str, text: str) -> str:
    return f"{reference}:{_hash_text(text)}"


def _cache_reference_key(reference: str) -> str:
    return str(reference)


def _get_cached_classification(cache: dict[str, dict], reference: str) -> tuple[str | None, dict | None]:
    """Retourne la première classification trouvée pour une référence.

    Compatibilité ascendante : accepte les anciennes clés `reference:hash` et
    les nouvelles clés `reference`.
    """
    reference_key = _cache_reference_key(reference)
    fallback_key: str | None = None
    fallback_value: dict | None = None

    for key, value in cache.items():
        if key == reference_key:
            return key, value
        if key.startswith(f"{reference_key}:"):
            fallback_key = key
            fallback_value = value

    return fallback_key, fallback_value


def _set_cached_classification(cache: dict[str, dict], reference: str, result: dict) -> None:
    cache[_cache_reference_key(reference)] = result


def _load_cache() -> dict[str, dict]:
    if not _CACHE_FILE.exists():
        return {}
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Cache IA illisible ({exc}) — reconstruction depuis zéro.")
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _validate_classification(result: Any) -> dict:
    """Valide/normalise une classification unitaire ; repli sur Indéterminé si invalide."""
    if not isinstance(result, dict):
        return {"categorie": _INDETERMINE, "confiance": 0.0, "resume": ""}

    categorie = result.get("categorie")
    if categorie not in CATEGORIES and categorie != _INDETERMINE:
        logger.warning(f"Catégorie IA hors liste reçue : '{categorie}' — repli sur '{_INDETERMINE}'.")
        categorie = _INDETERMINE

    try:
        confiance = float(result.get("confiance", 0.0))
    except (TypeError, ValueError):
        confiance = 0.0
    confiance = min(max(confiance, 0.0), 1.0)
    if categorie == _INDETERMINE:
        confiance = 0.0

    resume = str(result.get("resume") or "")
    return {"categorie": categorie, "confiance": confiance, "resume": resume}


def _classify_unitary(client: GroqClient, text: str) -> dict:
    if not text.strip():
        return {"categorie": _INDETERMINE, "confiance": 0.0, "resume": ""}
    user_prompt = (
        "Classe ce ticket. Réponds avec l'objet JSON "
        '{"categorie": "...", "confiance": 0.0-1.0, "resume": "<max 15 mots>"}.\n'
        f"Texte du ticket : {text}"
    )
    try:
        result = client.chat_json(_SYSTEM_PROMPT_UC1, user_prompt)
    except Exception as exc:
        logger.warning(f"Classification unitaire échouée : {type(exc).__name__}.")
        return {"categorie": _INDETERMINE, "confiance": 0.0, "resume": ""}
    return _validate_classification(result)


def _classify_batch(client: GroqClient, texts: list[str]) -> list[dict]:
    """Classifie un lot de tickets en un seul appel (coût / 10)."""
    numbered = "\n".join(f"{i + 1}. {t if t.strip() else '(vide)'}" for i, t in enumerate(texts))
    user_prompt = (
        f"Classe les {len(texts)} tickets suivants, dans l'ordre. Réponds avec "
        '{"resultats": [{"categorie": "...", "confiance": 0.0-1.0, '
        '"resume": "<max 15 mots>"}, ...]} — le tableau "resultats" doit contenir '
        f"exactement {len(texts)} éléments, dans le même ordre que la liste ci-dessous.\n\n{numbered}"
    )
    return _call_groq_batch(
        client, _SYSTEM_PROMPT_UC1, user_prompt, n_items=len(texts),
        validate_item=_validate_classification,
        unitary_fn=lambda: [_classify_unitary(client, t) for t in texts],
        fallback_result={"categorie": _INDETERMINE, "confiance": 0.0, "resume": ""},
        label="Lot",
    )


def _apply_classification(df: pd.DataFrame, idx: Any, result: dict) -> None:
    df.at[idx, "ai_cause_category"] = result["categorie"]
    df.at[idx, "ai_cause_confidence"] = float(result["confiance"])
    df.at[idx, "ai_summary"] = result["resume"]


def _empty_ai_columns(df: pd.DataFrame) -> pd.DataFrame:
    df["ai_cause_category"] = pd.NA
    df["ai_cause_confidence"] = float("nan")
    df["ai_summary"] = pd.NA
    df["root_cause_category_final"] = df.get("root_cause_category", pd.NA)
    return df


def enrich_dataframe(
    df: pd.DataFrame,
    cfg: Config,
    dry_run: bool = False,
    limit: int | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    UC1 — Ajoute ai_cause_category, ai_cause_confidence, ai_summary et
    root_cause_category_final au DataFrame de faits (avant modeling).

    Consolidation : root_cause_category_final = ai_cause_category si
    confiance >= AI_CONFIDENCE_THRESHOLD, sinon root_cause_category (regex),
    sinon NaN.

    Si l'IA est désactivée (flag ou clé absente) et que dry_run=False : les
    colonnes sont mises à NaN, aucun appel réseau n'est effectué, un simple
    log INFO est émis. Le pipeline ne doit jamais échouer à cause de ce module.

    Args:
        df:      DataFrame de faits nettoyé (post-transform, avant modeling).
        cfg:     Configuration du pipeline (clé, modèle, seuils…).
        dry_run: Si True, limite le traitement et ne touche pas au cache définitif.
        limit:   Nombre maximum de tickets éligibles à traiter (utilisé par --ai-dry-run).

    Returns:
        (df_enrichi, stats) — stats contient les compteurs pour le rapport qualité
        et l'estimation de coût (n_eligible_total, n_eligible_sampled, n_classified,
        n_cache_hit, n_api_calls, n_high_confidence, n_indetermine,
        estimated_tokens, estimated_cost_usd, estimated_cost_usd_total_projected).
    """
    df = df.copy()
    stats = {
        "n_eligible_total": 0, "n_eligible_sampled": 0, "n_classified": 0,
        "n_cache_hit": 0, "n_api_calls": 0, "n_high_confidence": 0,
        "n_indetermine": 0, "estimated_tokens": 0, "estimated_cost_usd": 0.0,
        "estimated_cost_usd_total_projected": 0.0,
    }

    if not dry_run and not cfg.ai_enrichment_enabled:
        logger.info("Enrichissement IA désactivé (AI_ENRICHMENT_ENABLED=False) — colonnes IA à NaN.")
        return _empty_ai_columns(df), stats

    if not cfg.groq_api_key:
        logger.info("GROQ_API_KEY absente — enrichissement IA ignoré, colonnes IA à NaN.")
        return _empty_ai_columns(df), stats

    df = _empty_ai_columns(df)

    root_cause = df.get("root_cause_raw", pd.Series(dtype=object, index=df.index))
    solution = df.get("solution", pd.Series(dtype=object, index=df.index))
    eligible_mask = root_cause.notna() | solution.notna()
    eligible_idx_all = df.index[eligible_mask].tolist()
    stats["n_eligible_total"] = len(eligible_idx_all)

    eligible_idx = eligible_idx_all[:limit] if limit is not None else eligible_idx_all
    stats["n_eligible_sampled"] = len(eligible_idx)

    if not eligible_idx:
        return df, stats

    client = GroqClient(cfg)
    cache = _load_cache()
    cache_updates: dict[str, dict] = {}

    to_call_idx: list[Any] = []
    to_call_texts: list[str] = []

    for idx in eligible_idx:
        text = _build_uc1_text(df.loc[idx])
        reference = str(df.loc[idx].get("reference", idx))
        key, cached = _get_cached_classification(cache, reference)
        if cached is not None:
            stats["n_cache_hit"] += 1
            _apply_classification(df, idx, cached)
            stats["n_classified"] += 1
            if cached["categorie"] == _INDETERMINE:
                stats["n_indetermine"] += 1
            if cached["confiance"] >= cfg.ai_confidence_threshold:
                stats["n_high_confidence"] += 1
        else:
            to_call_idx.append(idx)
            to_call_texts.append(text)

    batch_size = max(cfg.ai_batch_size, 1)
    for start in range(0, len(to_call_idx), batch_size):
        batch_idx = to_call_idx[start:start + batch_size]
        batch_texts = to_call_texts[start:start + batch_size]
        results = _classify_batch(client, batch_texts)
        stats["n_api_calls"] += 1
        for idx, text, result in zip(batch_idx, batch_texts, results):
            reference = str(df.loc[idx].get("reference", idx))
            key = _cache_reference_key(reference)
            cache_updates[key] = result
            _apply_classification(df, idx, result)
            stats["n_classified"] += 1
            if result["categorie"] == _INDETERMINE:
                stats["n_indetermine"] += 1
            if result["confiance"] >= cfg.ai_confidence_threshold:
                stats["n_high_confidence"] += 1
            stats["estimated_tokens"] += (len(text) + 200) // _CHARS_PER_TOKEN

    if not dry_run and cache_updates:
        for reference, result in cache_updates.items():
            _set_cached_classification(cache, reference, result)
        cache.update(cache_updates)
        _save_cache(cache)

    n_new = len(to_call_idx)
    avg_tokens = (stats["estimated_tokens"] / n_new) if n_new else 0.0
    stats["estimated_cost_usd"] = round(stats["estimated_tokens"] / 1000 * _GROQ_PRICE_PER_1K_TOKENS_USD, 4)

    remaining = max(stats["n_eligible_total"] - stats["n_eligible_sampled"], 0)
    projected_tokens = stats["estimated_tokens"] + remaining * avg_tokens
    stats["estimated_cost_usd_total_projected"] = round(
        projected_tokens / 1000 * _GROQ_PRICE_PER_1K_TOKENS_USD, 4
    )

    # Consolidation : ai_cause_category si confiance >= seuil, sinon regex, sinon NaN
    regex_col = df.get("root_cause_category", pd.Series(pd.NA, index=df.index))
    conf = pd.to_numeric(df["ai_cause_confidence"], errors="coerce")
    use_ai = conf >= cfg.ai_confidence_threshold
    df["root_cause_category_final"] = regex_col
    df.loc[use_ai, "root_cause_category_final"] = df.loc[use_ai, "ai_cause_category"]

    logger.info(
        f"Enrichissement IA UC1 : {stats['n_classified']}/{stats['n_eligible_sampled']} classifié(s) "
        f"({stats['n_cache_hit']} depuis cache, {stats['n_api_calls']} appel(s) API), "
        f"{stats['n_indetermine']} indéterminé(s), coût estimé de ce run ~{stats['estimated_cost_usd']}$."
    )
    return df, stats


# ══════════════════════════════════════════════════════════════════════════════
# UC4 — Synthèse exécutive mensuelle
# ══════════════════════════════════════════════════════════════════════════════

def _compute_monthly_aggregates(df: pd.DataFrame) -> dict | None:
    """Calcule en pandas les agrégats du dernier mois complet. Aucune donnée
    ligne par ligne n'est incluse — uniquement des chiffres agrégés."""
    if "created_at" not in df.columns or df["created_at"].dropna().empty:
        return None

    today = pd.Timestamp.now().normalize()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - pd.Timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    prev_month_end = last_month_start - pd.Timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)

    created = df["created_at"]
    cur = df[(created >= last_month_start) & (created <= last_month_end)]
    prev = df[(created >= prev_month_start) & (created <= prev_month_end)]

    volume = len(cur)
    volume_prev = len(prev)
    variation_pct = round((volume - volume_prev) / volume_prev * 100, 1) if volume_prev else None

    nature_counts = cur["nature"].value_counts().to_dict() if "nature" in cur.columns else {}

    if "service_subcategory" in cur.columns:
        not_generic = ~cur["service_subcategory"].astype(str).str.contains(
            "ticket.*chaud", case=False, na=False, regex=True
        )
        top_subcategories = cur.loc[not_generic, "service_subcategory"].value_counts().head(3).to_dict()
    else:
        top_subcategories = {}

    n_critical = int(cur["is_critical"].fillna(0).astype(float).sum()) if "is_critical" in cur.columns else None
    backlog = int(df["is_open"].fillna(0).astype(float).sum()) if "is_open" in df.columns else None

    cause_col = "root_cause_category_final" if "root_cause_category_final" in cur.columns else "root_cause_category"
    top_causes = cur[cause_col].value_counts().head(3).to_dict() if cause_col in cur.columns else {}

    return {
        "month_label": last_month_start.strftime("%Y-%m"),
        "volume_total": volume,
        "volume_mois_precedent": volume_prev,
        "variation_pct": variation_pct,
        "repartition_nature": nature_counts,
        "top_sous_categories_hors_ticket_a_chaud": top_subcategories,
        "nb_critiques": n_critical,
        "backlog_fin_de_mois": backlog,
        "top_causes": top_causes,
    }


_SYSTEM_PROMPT_UC4 = (
    "Tu es un rédacteur de synthèses exécutives pour un rapport mensuel "
    "d'incidents IT bancaire. Réponds UNIQUEMENT avec l'objet JSON "
    '{"synthese": "<texte en français, 150 mots maximum>"}.'
)


def generate_monthly_summary(df: pd.DataFrame, cfg: Config) -> str | None:
    """
    UC4 — Calcule les agrégats du dernier mois complet et génère, en un seul
    appel Groq (modèle cfg.groq_model_summary), le commentaire exécutif du
    rapport mensuel.

    N'envoie jamais de données ligne par ligne — uniquement les agrégats
    chiffrés retournés par _compute_monthly_aggregates().

    Returns:
        Le texte de synthèse, ou None si l'IA est désactivée, la clé absente,
        les agrégats non calculables, ou l'appel API échoue.
    """
    if not cfg.ai_enrichment_enabled or not cfg.groq_api_key:
        logger.info("Synthèse mensuelle IA ignorée (désactivée ou clé absente).")
        return None

    aggregates = _compute_monthly_aggregates(df)
    if aggregates is None:
        logger.warning("Synthèse mensuelle IA impossible : agrégats non calculables (created_at absent).")
        return None

    client = GroqClient(cfg, model=cfg.groq_model_summary)
    user_prompt = (
        "Rédige en français, en 150 mots maximum, le commentaire exécutif d'un "
        "rapport mensuel d'incidents IT bancaire à partir de ces chiffres. "
        "Ton factuel, pas de recommandations inventées, ne cite que les chiffres fournis.\n\n"
        f"Chiffres : {json.dumps(aggregates, ensure_ascii=False)}"
    )
    try:
        result = client.chat_json(_SYSTEM_PROMPT_UC4, user_prompt)
        summary = str(result.get("synthese", "")).strip() if isinstance(result, dict) else ""
    except Exception as exc:
        logger.warning(f"Echec génération synthèse mensuelle IA : {type(exc).__name__}.")
        return None

    return summary or None


def build_ai_insights_row(summary_text: str, month_label: str) -> pd.DataFrame:
    """Construit la ligne ai_insights (month, generated_at, summary_text)."""
    return pd.DataFrame([{
        "month": month_label,
        "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "summary_text": summary_text,
    }])


def append_ai_insights(previous: pd.DataFrame | None, new_row: pd.DataFrame) -> pd.DataFrame:
    """Fusionne la nouvelle synthèse avec l'historique existant (mode append)."""
    if previous is None or previous.empty:
        return new_row
    return pd.concat([previous, new_row], ignore_index=True)


def build_monthly_insights_row(df: pd.DataFrame, cfg: Config) -> pd.DataFrame | None:
    """Orchestration UC4 complète : agrégats → synthèse → ligne ai_insights.

    Retourne None si non applicable (IA désactivée, agrégats impossibles,
    ou échec de génération) — auquel cas main.py ne touche pas à ai_insights.
    """
    aggregates = _compute_monthly_aggregates(df)
    if aggregates is None:
        return None
    summary = generate_monthly_summary(df, cfg)
    if not summary:
        return None
    return build_ai_insights_row(summary, aggregates["month_label"])
