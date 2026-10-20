import math
import re
from collections import Counter
from typing import List, Dict, Any, Optional, Tuple


class VectorEngine:
    """
    Pure-Python TF-IDF Vector Space Model & Cosine Similarity Engine.
    Implements:
      1. Sublinear Term Frequency scaling: TF(t, d) = 1 + log(count(t, d))
      2. Smoothed Inverse Document Frequency: IDF(t, D) = log((1 + |D|) / (1 + df(t, D))) + 1
      3. 2x Title-Field Boosting: prioritizes title matches over body matches
      4. Lightweight morphological normalization: unifies common suffixes (-s, -es, -ing, -ed)
    """

    STOPWORDS = {
        "a", "an", "the", "and", "or", "but", "if", "because", "as", "what",
        "which", "this", "that", "these", "those", "then", "just", "so", "than",
        "such", "both", "through", "about", "for", "is", "of", "to", "in", "it",
        "on", "with", "by", "at", "from", "be", "are", "was", "were", "has", "have",
    }

    @classmethod
    def _stem(cls, token: str) -> str:
        """Lightweight morphological normalization for English technical tokens."""
        if len(token) <= 4:
            return token
        for suffix in ("ing", "ed", "es", "s"):
            if token.endswith(suffix) and len(token) - len(suffix) >= 3:
                return token[: -len(suffix)]
        return token

    @classmethod
    def _tokenize(cls, text: str) -> List[str]:
        """Tokenize (including splitting snake_case/kebab-case), remove stopwords, and stem."""
        if not text:
            return []
        raw_tokens = re.findall(r"[a-z0-9]+", text.lower())
        return [
            cls._stem(t)
            for t in raw_tokens
            if t not in cls.STOPWORDS and len(t) > 1
        ]

    @classmethod
    def compute_idf(cls, tokenized_docs: List[List[str]]) -> Dict[str, float]:
        """
        Compute smoothed Inverse Document Frequency across a document corpus:
            IDF(t) = log((1 + N) / (1 + df(t))) + 1.0
        Rare technical terms receive higher weight; ubiquitous corpus terms are down-weighted.
        """
        n_docs = len(tokenized_docs)
        if n_docs == 0:
            return {}

        df_counts: Counter = Counter()
        for tokens in tokenized_docs:
            for unique_token in set(tokens):
                df_counts[unique_token] += 1

        return {
            token: round(math.log((1.0 + n_docs) / (1.0 + df)) + 1.0, 6)
            for token, df in df_counts.items()
        }

    @classmethod
    def compute_cosine_similarity(
        cls,
        text1: str,
        text2: str,
        idf_weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Compute TF-IDF cosine similarity (0.0 to 1.0) between two texts.
        Uses sublinear TF scaling `(1 + log(tf)) * idf`.
        """
        tokens1 = cls._tokenize(text1)
        tokens2 = cls._tokenize(text2)

        if not tokens1 or not tokens2:
            return 0.0

        tf1 = Counter(tokens1)
        tf2 = Counter(tokens2)

        def weight(token: str, count: int) -> float:
            tf_val = 1.0 + math.log(count) if count > 0 else 0.0
            idf_val = idf_weights.get(token, 1.0) if idf_weights else 1.0
            return tf_val * idf_val

        vec1 = {t: weight(t, c) for t, c in tf1.items()}
        vec2 = {t: weight(t, c) for t, c in tf2.items()}

        intersection = set(vec1.keys()) & set(vec2.keys())
        numerator = sum(vec1[x] * vec2[x] for x in intersection)

        sum1 = sum(v ** 2 for v in vec1.values())
        sum2 = sum(v ** 2 for v in vec2.values())
        denominator = math.sqrt(sum1) * math.sqrt(sum2)

        if not denominator:
            return 0.0

        return round(numerator / denominator, 4)

    @classmethod
    def rank_documents(
        cls,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Rank issue documents against `query` using corpus-weighted TF-IDF cosine similarity.
        Title tokens are boosted 2x relative to body tokens to prioritize title matches.
        """
        if not query or not documents:
            return []

        query_tokens = cls._tokenize(query)
        if not query_tokens:
            return []

        # Build corpus with 2x title boosting
        doc_texts: List[str] = []
        tokenized_corpus: List[List[str]] = [query_tokens]
        for doc in documents:
            title = doc.get("title", "")
            body = doc.get("body", "")
            boosted_text = f"{title} {title} {body}"
            doc_texts.append(boosted_text)
            tokenized_corpus.append(cls._tokenize(boosted_text))

        idf_weights = cls.compute_idf(tokenized_corpus)

        scored_docs = []
        for doc, doc_text in zip(documents, doc_texts):
            score = cls.compute_cosine_similarity(query, doc_text, idf_weights=idf_weights)
            if score > 0.0:
                doc_copy = dict(doc)
                doc_copy["similarity_score"] = score
                scored_docs.append(doc_copy)

        scored_docs.sort(key=lambda x: x["similarity_score"], reverse=True)
        return scored_docs[:top_k]

    @classmethod
    def compute_lexical_score(cls, query: str, doc: Dict[str, Any]) -> float:
        """
        Compute exact lexical / technical-identifier match score (0.0 to 1.0).
        Captures exact `#issue_number` references, underscored/hyphenated error codes
        (e.g. `ERR_OAUTH_401`, `CVE-2025-1001`), and exact phrase matches.
        """
        if not query or not doc:
            return 0.0

        q_lower = query.strip().lower()
        title = str(doc.get("title", "")).lower()
        body = str(doc.get("body", "")).lower()
        combined = f"{title} {body}"

        score = 0.0

        # 1. Exact #issue_number reference check (e.g. "#42" or "issue 42")
        doc_num = doc.get("number")
        if doc_num is not None:
            num_matches = re.findall(r"#(\d+)\b|\bissue\s+#?(\d+)\b", q_lower)
            referenced_nums = {int(a or b) for a, b in num_matches if (a or b)}
            if int(doc_num) in referenced_nums:
                score += 0.65

        # 2. Exact contiguous phrase match in title or body
        if len(q_lower) >= 3:
            if q_lower in title:
                score += 0.50
            elif q_lower in body:
                score += 0.30

        # 3. Exact technical identifier / token overlap (preserving _, -, .)
        raw_q_terms = [
            t for t in re.findall(r"[a-z0-9][a-z0-9_.-]*", q_lower)
            if t not in cls.STOPWORDS and len(t) > 1
        ]
        if raw_q_terms:
            matched_in_title = sum(1 for t in raw_q_terms if t in title)
            matched_in_body = sum(1 for t in raw_q_terms if t in combined)
            title_ratio = matched_in_title / len(raw_q_terms)
            body_ratio = matched_in_body / len(raw_q_terms)
            score += (0.35 * title_ratio) + (0.20 * body_ratio)

        return round(min(1.0, score), 4)

    @classmethod
    def hybrid_rrf_search(
        cls,
        query: str,
        documents: List[Dict[str, Any]],
        top_k: int = 5,
        rrf_k: int = 60,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid Retrieval using Reciprocal Rank Fusion (RRF):
            RRF(d) = 1 / (k + rank_semantic(d)) + 1 / (k + rank_lexical(d))
        Combines smoothed corpus TF-IDF cosine ranking with exact lexical / error-code
        matching so both conceptual queries and exact identifiers rank accurately.
        """
        if not query or not documents:
            return []

        sem_ranked = cls.rank_documents(query, documents, top_k=len(documents))

        # Index documents by stable key (repo + number + index fallback)
        doc_map: Dict[str, Dict[str, Any]] = {}
        sem_rank_map: Dict[str, int] = {}
        sem_score_map: Dict[str, float] = {}

        def _doc_key(d: Dict[str, Any], fallback_idx: int = 0) -> str:
            return f"{d.get('repo', '')}:{d.get('number', fallback_idx)}:{d.get('title', '')}"

        for idx, d in enumerate(documents):
            doc_map[_doc_key(d, idx)] = d

        for rank_idx, d in enumerate(sem_ranked, start=1):
            k = _doc_key(d)
            sem_rank_map[k] = rank_idx
            sem_score_map[k] = d.get("similarity_score", 0.0)

        lex_scored: List[tuple[str, float]] = []
        for idx, d in enumerate(documents):
            k = _doc_key(d, idx)
            l_score = cls.compute_lexical_score(query, d)
            if l_score > 0.0:
                lex_scored.append((k, l_score))
        lex_scored.sort(key=lambda x: x[1], reverse=True)
        lex_rank_map = {k: rank_idx for rank_idx, (k, _) in enumerate(lex_scored, start=1)}
        lex_score_map = dict(lex_scored)

        candidate_keys = set(sem_rank_map.keys()) | set(lex_rank_map.keys())
        if not candidate_keys:
            return []

        max_possible_rrf = 2.0 / (rrf_k + 1)
        fused_results: List[Dict[str, Any]] = []

        for k in candidate_keys:
            base_doc = dict(doc_map[k])
            r_sem = sem_rank_map.get(k)
            r_lex = lex_rank_map.get(k)

            raw_rrf = 0.0
            if r_sem is not None:
                raw_rrf += 1.0 / (rrf_k + r_sem)
            if r_lex is not None:
                raw_rrf += 1.0 / (rrf_k + r_lex)

            normalized_rrf = round(raw_rrf / max_possible_rrf, 4)
            sem_s = sem_score_map.get(k, 0.0)
            lex_s = lex_score_map.get(k, 0.0)

            base_doc["similarity_score"] = round(max(sem_s, lex_s), 4)
            base_doc["semantic_score"] = sem_s
            base_doc["lexical_score"] = lex_s
            base_doc["rrf_score"] = normalized_rrf
            fused_results.append(base_doc)

        fused_results.sort(
            key=lambda x: (x["rrf_score"], x["similarity_score"], x["lexical_score"]),
            reverse=True,
        )
        return fused_results[:top_k]

