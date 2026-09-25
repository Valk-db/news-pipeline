"""Automated fact-checking for source reliability scoring."""

import hashlib
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
import logging

from src.shared.llm import get_llm_client
from src.schema.models import FactCheckRecord, FactCheckRecord as FCRecord
from src.utils.ingest_stats import STATS

logger = logging.getLogger(__name__)


@dataclass
class Claim:
    """Extracted claim from an article."""
    text: str
    claim_hash: str
    entities: List[str]
    position: int  # Character position in article
    context: str  # Surrounding text
    claim_type: str  # "statistic", "quote", "prediction", "causal", "definition"


class FactChecker:
    """
    Multi-source fact-checker for reliability scoring.

    Sources:
    1. ClaimReview schema (schema.org/ClaimReview) - structured fact-checks
    2. ClaimBuster API - automated claim detection and verification
    3. LLM Verification - cross-referencing with tier-1 consensus
    4. Manual review queue
    """

    def __init__(self):
        self.claimbuster_api_key = None  # Optional
        self.cache = {}  # Simple in-memory cache

    async def check_claim(self, claim: Claim, source_domain: str) -> Dict[str, Any]:
        """
        Check a single claim against multiple fact-check sources.

        Returns:
            Dict with verdict, confidence, explanation, fact_checker
        """
        claim_hash = claim.claim_hash

        # Check cache first
        if claim_hash in self.cache:
            return self.cache[claim_hash]

        # Try sources in order of reliability
        results = []

        # 1. ClaimReview (structured fact-checks from known fact-checkers)
        # This would query a database of ClaimReview markup
        # For now, placeholder

        # 2. ClaimBuster API (if configured)
        if self.claimbuster_api_key:
            cb_result = await self._check_claimbuster(claim)
            if cb_result:
                results.append(cb_result)

        # 3. LLM Verification (cross-reference with tier-1 consensus)
        llm_result = await self._check_llm_verification(claim)
        if llm_result:
            results.append(llm_result)

        # 4. Internal knowledge base (previously verified claims)
        kb_result = await self._check_knowledge_base(claim_hash)
        if kb_result:
            results.append(kb_result)

        # Aggregate results
        final_result = self._aggregate_results(results)

        # Cache result
        self.cache[claim_hash] = final_result

        STATS.record("fact_check", "checked")
        return final_result

    async def _check_claimbuster(self, claim: Claim) -> Optional[Dict[str, Any]]:
        """Check claim using ClaimBuster API."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://claimbuster-api.example.com/v1/score",
                    json={"text": claim.text},
                    headers={"Authorization": f"Bearer {self.claimbuster_api_key}"},
                )
                if response.status_code == 200:
                    data = response.json()
                    score = data.get("score", 0)  # 0-1 scale

                    if score > 0.7:
                        verdict = FCRecord.Verdict.TRUE
                    elif score > 0.4:
                        verdict = FCRecord.Verdict.MIXED
                    else:
                        verdict = FCRecord.Verdict.FALSE

                    return {
                        "verdict": verdict,
                        "confidence": int(score * 100),
                        "fact_checker": FCRecord.FactChecker.CLAIMBUSTER,
                        "explanation": f"ClaimBuster score: {score:.2f}",
                    }
        except Exception as e:
            logger.warning(f"ClaimBuster check failed: {e}")

        return None

    async def _check_llm_verification(self, claim: Claim) -> Optional[Dict[str, Any]]:
        """Verify claim using LLM against tier-1 consensus."""
        llm = await get_llm_client()

        prompt = f"""Fact-check this claim by evaluating its veracity based on general knowledge and journalistic standards.

Claim: "{claim.text}"
Context: "{claim.context}"
Entities involved: {", ".join(claim.entities) if claim.entities else "None"}

Return a JSON object with:
- "verdict": one of "true", "mostly_true", "mixed", "mostly_false", "false", "unverifiable"
- "confidence": 0-100
- "explanation": brief reasoning (max 200 chars)
- "evidence_needed": what would be needed to verify conclusively

Only return the JSON object."""

        try:
            response = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500,
            )

            content = response["choices"][0]["message"]["content"].strip()
            result = json.loads(content)

            verdict_map = {
                "true": FCRecord.Verdict.TRUE,
                "mostly_true": FCRecord.Verdict.MOSTLY_TRUE,
                "mixed": FCRecord.Verdict.MIXED,
                "mostly_false": FCRecord.Verdict.MOSTLY_FALSE,
                "false": FCRecord.Verdict.FALSE,
                "unverifiable": FCRecord.Verdict.UNVERIFIED,
            }

            return {
                "verdict": verdict_map.get(result.get("verdict", "unverifiable"), FCRecord.Verdict.UNVERIFIED),
                "confidence": result.get("confidence", 50),
                "fact_checker": FCRecord.FactChecker.LLM_VERIFIER,
                "explanation": result.get("explanation", ""),
            }

        except Exception as e:
            logger.warning(f"LLM verification failed: {e}")
            return None

    async def _check_knowledge_base(self, claim_hash: str) -> Optional[Dict[str, Any]]:
        """Check internal knowledge base of previously verified claims."""
        # This would query a database of previous fact-checks
        # For now, return None
        return None

    def _aggregate_results(self, results: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate multiple fact-check results into final verdict."""
        if not results:
            return {
                "verdict": FCRecord.Verdict.UNVERIFIED,
                "confidence": 0,
                "fact_checker": FCRecord.FactChecker.LLM_VERIFIER,
                "explanation": "No fact-check sources available",
            }

        # Weight by fact-checker reliability
        weights = {
            FCRecord.FactChecker.CLAIMREVIEW: 1.0,
            FCRecord.FactChecker.CLAIMBUSTER: 0.8,
            FCRecord.FactChecker.LLM_VERIFIER: 0.7,
            FCRecord.FactChecker.MANUAL: 1.0,
        }

        # Convert verdicts to numeric scores
        verdict_scores = {
            FCRecord.Verdict.TRUE: 1.0,
            FCRecord.Verdict.MOSTLY_TRUE: 0.75,
            FCRecord.Verdict.MIXED: 0.5,
            FCRecord.Verdict.MOSTLY_FALSE: 0.25,
            FCRecord.Verdict.FALSE: 0.0,
            FCRecord.Verdict.UNVERIFIED: 0.5,
        }

        weighted_score = 0
        total_weight = 0
        verdicts = []

        for r in results:
            fc = r.get("fact_checker", FCRecord.FactChecker.LLM_VERIFIER)
            weight = weights.get(fc, 0.5)
            verdict = r.get("verdict", FCRecord.Verdict.UNVERIFIED)
            confidence = r.get("confidence", 50) / 100

            score = verdict_scores.get(verdict, 0.5)
            weighted_score += score * weight * confidence
            total_weight += weight * confidence
            verdicts.append(verdict)

        if total_weight == 0:
            final_score = 0.5
        else:
            final_score = weighted_score / total_weight

        # Convert back to verdict
        if final_score >= 0.875:
            final_verdict = FCRecord.Verdict.TRUE
        elif final_score >= 0.625:
            final_verdict = FCRecord.Verdict.MOSTLY_TRUE
        elif final_score >= 0.375:
            final_verdict = FCRecord.Verdict.MIXED
        elif final_score >= 0.125:
            final_verdict = FCRecord.Verdict.MOSTLY_FALSE
        else:
            final_verdict = FCRecord.Verdict.FALSE

        avg_confidence = sum(r.get("confidence", 50) for r in results) / len(results)

        return {
            "verdict": final_verdict,
            "confidence": int(avg_confidence),
            "fact_checker": FCRecord.FactChecker.LLM_VERIFIER,
            "explanation": f"Aggregated from {len(results)} sources",
        }


async def extract_claims_from_article(
    article_text: str,
    title: str,
    entities: Dict[str, List[str]],
) -> List[Claim]:
    """
    Extract verifiable claims from an article using LLM.

    Args:
        article_text: Full article body text
        title: Article title
        entities: Extracted entities dict {"PERSON": [...], "ORG": [...], "GPE": [...]}

    Returns:
        List of Claim objects
    """
    llm = get_llm_client()

    # Flatten entities
    all_entities = []
    for ent_list in entities.values():
        all_entities.extend(ent_list)

    # Truncate for context
    text_for_llm = article_text[:6000]

    prompt = f"""Extract verifiable factual claims from this news article.

Article Title: {title}
Entities mentioned: {", ".join(all_entities[:20]) if all_entities else "None"}

Article Text:
{text_for_llm}

Return a JSON array of claim objects. Each claim should have:
- "text": The exact claim text (max 200 chars)
- "type": One of "statistic", "quote", "prediction", "causal", "definition", "attribution"
- "entities": Array of entity names from the article that are central to this claim
- "context": Surrounding sentence for context (max 150 chars)

Focus on claims that can be verified as true/false:
- Specific numbers, statistics, percentages
- Attributed quotes from named sources
- Predictions about future events
- Causal claims ("X caused Y")
- Definitive statements about events

Do NOT include:
- Opinions, analysis, speculation
- Vague statements
- Background context

Maximum 10 claims. Only return the JSON array."""

    try:
        response = await llm.chat.completions.create(
            model=llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
        )

        content = response.choices[0].message.content.strip()
        claims_data = json.loads(content)

        claims = []
        for c in claims_data:
                claim_text = c.get("text", "").strip()
                if not claim_text:
                    continue

                # Generate hash for dedup
                claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()

                claims.append(Claim(
                    text=claim_text,
                    claim_hash=claim_hash,
                    entities=c.get("entities", []),
                    position=c.get("position", 0),
                    context=c.get("context", ""),
                    claim_type=c.get("type", "attribution"),
                ))

        return claims

    except Exception as e:
        logger.error(f"Claim extraction failed: {e}")
        return []


async def fact_check_article(
    session,
    article_id: str,
    article_text: str,
    title: str,
    source_domain: str,
    entities: Dict[str, List[str]],
) -> List[FactCheckRecord]:
    """
    Fact-check all claims in an article and store results.

    Args:
        session: Database session
        article_id: Article UUID
        article_text: Article body text
        title: Article title
        source_domain: Source domain
        entities: Article entities

    Returns:
        List of FactCheckRecord objects created
    """
    # Extract claims
    claims = await extract_claims_from_article(article_text, title, entities)

    if not claims:
        return []

    checker = FactChecker()
    fact_checks = []

    for claim in claims:
        result = await checker.check_claim(claim, source_domain)

        fc = FactCheckRecord(
            source_domain=source_domain,
            article_id=article_id,
            claim=claim.text,
            claim_hash=claim.claim_hash,
            verdict=result["verdict"],
            confidence=result["confidence"],
            fact_checker=result["fact_checker"],
            fact_checker_url=None,
            explanation=result.get("explanation"),
            claim_date=datetime.now(timezone.utc),  # Would be article publish date
        )

        session.add(fc)
        fact_checks.append(fc)

    await session.commit()
    return fact_checks