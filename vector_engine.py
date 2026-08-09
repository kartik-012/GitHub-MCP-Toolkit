import math
import re
from collections import Counter
from typing import List, Dict, Any, Tuple


class VectorEngine:
    """
    Lightweight TF-IDF Vector Space Model & Cosine Similarity Engine.
    Provides semantic vector matching and similarity scoring across issue texts
    without external heavy binary C-dependencies.
    """

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Tokenize and normalize text into lowercase word tokens."""
        text = text.lower()
        tokens = re.findall(r'\b[a-z0-9]+\b', text)
        # Filter basic stop words
        stopwords = {
            "a", "an", "the", "and", "or", "but", "if", "because", "as", "what",
            "which", "this", "that", "these", "those", "then", "just", "so", "than",
            "such", "both", "through", "about", "for", "is", "of", "to", "in", "it"
        }
        return [t for t in tokens if t not in stopwords and len(t) > 1]

    @classmethod
    def compute_cosine_similarity(cls, text1: str, text2: str) -> float:
        """Compute cosine similarity score (0.0 to 1.0) between two text strings."""
        tokens1 = cls._tokenize(text1)
        tokens2 = cls._tokenize(text2)

        if not tokens1 or not tokens2:
            return 0.0

        vec1 = Counter(tokens1)
        vec2 = Counter(tokens2)

        intersection = set(vec1.keys()) & set(vec2.keys())
        numerator = sum(vec1[x] * vec2[x] for x in intersection)

        sum1 = sum(vec1[x] ** 2 for x in vec1.keys())
        sum2 = sum(vec2[x] ** 2 for x in vec2.keys())
        denominator = math.sqrt(sum1) * math.sqrt(sum2)

        if not denominator:
            return 0.0

        return round(numerator / denominator, 4)

    @classmethod
    def rank_documents(cls, query: str, documents: List[Dict[str, Any]], top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Rank a list of issue documents by cosine similarity against query.
        Each doc must have 'title' and optional 'body'.
        """
        scored_docs = []
        for doc in documents:
            doc_text = f"{doc.get('title', '')} {doc.get('body', '')}"
            score = cls.compute_cosine_similarity(query, doc_text)
            if score > 0.0:
                doc_copy = dict(doc)
                doc_copy["similarity_score"] = score
                scored_docs.append(doc_copy)

        scored_docs.sort(key=lambda x: x["similarity_score"], reverse=True)
        return scored_docs[:top_k]
