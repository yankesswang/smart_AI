"""知識庫檢索（RAG 的 R）。

刻意不依賴外部向量資料庫或斷詞套件，改用「CJK 字元 bigram + 拉丁詞」的 TF-IDF 餘弦相似度：

* 中文不需要斷詞模型也能有合理召回（bigram 對術語如「軸承」「冷卻」「振動」很有效）。
* 完全確定性，Demo 每次結果一樣，Benchmark 才有意義。
* 沒有網路相依，離線就能跑。

真實導入時把 ``KnowledgeBase`` 換成向量資料庫即可，介面（``search``）不變。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .corpus import MAINTENANCE_HISTORY, MANUALS, MaintenanceCase, ManualDoc

_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-_.]*|\d+(?:\.\d+)?")
_CJK_RUN = re.compile(r"[一-鿿]+")


def tokenize(text: str) -> list[str]:
    """拉丁詞照詞切，CJK 用字元 unigram + bigram。"""
    tokens = [m.group(0).lower() for m in _LATIN.finditer(text)]
    for run in _CJK_RUN.findall(text):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class Chunk:
    chunk_id: str
    source: str            # manual / sop / history
    ref: str               # MAN-A-3.2 / MH-014
    title: str
    text: str
    machine_ids: tuple[str, ...]
    fault_ids: tuple[str, ...]


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.chunk.ref,
            "title": self.chunk.title,
            "source": self.chunk.source,
            "score": round(self.score, 4),
            "text": self.chunk.text,
            "fault_ids": list(self.chunk.fault_ids),
        }


class KnowledgeBase:
    """對 Manual / SOP / Maintenance History 做 TF-IDF 檢索。"""

    def __init__(
        self,
        manuals: Iterable[ManualDoc] = MANUALS,
        history: Iterable[MaintenanceCase] = MAINTENANCE_HISTORY,
    ) -> None:
        self.manuals = tuple(manuals)
        self.history = tuple(history)
        self.chunks: list[Chunk] = []
        self._build_chunks()
        self._tf: list[Counter[str]] = [Counter(tokenize(c.title + "。" + c.text)) for c in self.chunks]
        self._idf: dict[str, float] = self._compute_idf()
        self._norms: list[float] = [self._norm(tf) for tf in self._tf]

    # -- 建立索引 ---------------------------------------------------------------------
    def _build_chunks(self) -> None:
        for doc in self.manuals:
            # 以段落切塊，讓引用可以指到「手冊的哪一段」而不是整份文件。
            paragraphs = [p.strip() for p in doc.body.split("\n") if p.strip()]
            for idx, para in enumerate(paragraphs, start=1):
                self.chunks.append(
                    Chunk(
                        chunk_id=f"{doc.ref}#{idx}",
                        source=doc.doc_type,
                        ref=doc.ref,
                        title=doc.title,
                        text=para,
                        machine_ids=doc.machine_ids,
                        fault_ids=doc.fault_ids,
                    )
                )
        for case in self.history:
            self.chunks.append(
                Chunk(
                    chunk_id=case.case_id,
                    source="history",
                    ref=case.case_id,
                    title=f"維修紀錄 {case.case_id}（{case.machine_id}）",
                    text=case.as_text(),
                    machine_ids=(case.machine_id,),
                    fault_ids=(case.diagnosed_fault,),
                )
            )

    def _compute_idf(self) -> dict[str, float]:
        n = len(self.chunks)
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        return {term: math.log((n + 1) / (count + 1)) + 1.0 for term, count in df.items()}

    def _norm(self, tf: Counter[str]) -> float:
        return math.sqrt(sum((count * self._idf.get(term, 1.0)) ** 2 for term, count in tf.items())) or 1.0

    # -- 查詢 -------------------------------------------------------------------------
    def search(
        self,
        query: str,
        top_k: int = 5,
        machine_id: str | None = None,
        sources: tuple[str, ...] | None = None,
    ) -> list[RetrievedChunk]:
        q_tf = Counter(tokenize(query))
        if not q_tf:
            return []
        q_norm = self._norm(q_tf)
        scored: list[RetrievedChunk] = []
        for idx, chunk in enumerate(self.chunks):
            if sources and chunk.source not in sources:
                continue
            if machine_id and chunk.machine_ids and machine_id not in chunk.machine_ids:
                continue
            tf = self._tf[idx]
            dot = sum(
                count * self._idf.get(term, 1.0) * tf.get(term, 0) * self._idf.get(term, 1.0)
                for term, count in q_tf.items()
                if term in tf
            )
            if dot <= 0:
                continue
            scored.append(RetrievedChunk(chunk, dot / (q_norm * self._norms[idx])))
        scored.sort(key=lambda r: (-r.score, r.chunk.chunk_id))
        return scored[:top_k]

    def fault_affinity(self, query: str, fault_ids: Iterable[str], top_k: int = 24) -> dict[str, float]:
        """查詢文字對每個候選故障的「文件支持度」，用於診斷時的證據加權。

        用「每個來源取最高分」而不是「加總」：某個故障剛好有比較多筆歷史紀錄，
        不應該因為筆數多就贏過另一個有一段精準手冊描述的故障。
        手冊/SOP 權重高於歷史紀錄，因為手冊才寫著鑑別診斷的關鍵點。
        """
        fault_ids = list(fault_ids)
        hits = self.search(query, top_k=top_k)
        best_doc = {fid: 0.0 for fid in fault_ids}
        best_hist = {fid: 0.0 for fid in fault_ids}
        for hit in hits:
            bucket = best_hist if hit.chunk.source == "history" else best_doc
            for fid in hit.chunk.fault_ids:
                if fid in bucket:
                    bucket[fid] = max(bucket[fid], hit.score)
        raw = {fid: 0.65 * best_doc[fid] + 0.35 * best_hist[fid] for fid in fault_ids}
        best = max(raw.values(), default=0.0)
        if best <= 0:
            return {fid: 0.0 for fid in raw}
        return {fid: value / best for fid, value in raw.items()}

    def machine_fault_prior(self, machine_id: str, fault_ids: Iterable[str], half_life_days: float = 240.0) -> dict[str, float]:
        """由 Maintenance History 算出「這台機器得這種病」的先驗機率。

        這是歷史資料在診斷中真正有用的地方：它是數字，不是文字相似度。
        近期案例權重較高（指數衰減），避免兩年前的故障主導判斷。
        """
        fault_ids = list(fault_ids)
        weights = {fid: 0.0 for fid in fault_ids}
        for case in self.history:
            if case.machine_id != machine_id or case.diagnosed_fault not in weights:
                continue
            weights[case.diagnosed_fault] += 0.5 ** (case.days_ago / half_life_days)
        total = sum(weights.values())
        if total <= 0:
            # 沒有歷史紀錄時退回均勻先驗，不要假裝知道。
            return {fid: 1.0 / len(fault_ids) for fid in fault_ids}
        return {fid: w / total for fid, w in weights.items()}

    def cases_for_fault(self, fault_id: str, machine_id: str | None = None, limit: int = 3) -> list[MaintenanceCase]:
        cases = [
            c
            for c in self.history
            if c.diagnosed_fault == fault_id and (machine_id is None or c.machine_id == machine_id)
        ]
        cases.sort(key=lambda c: c.days_ago)
        return cases[:limit]

    def sop_for_fault(self, fault_id: str) -> list[ManualDoc]:
        return [d for d in self.manuals if d.doc_type == "sop" and fault_id in d.fault_ids]

    def safety_sops(self) -> list[ManualDoc]:
        return [d for d in self.manuals if d.ref.startswith("SOP-SF")]

    def stats(self) -> dict[str, int]:
        return {
            "manuals": len(self.manuals),
            "history_cases": len(self.history),
            "chunks": len(self.chunks),
            "vocabulary": len(self._idf),
        }


_DEFAULT_KB: KnowledgeBase | None = None


def default_kb() -> KnowledgeBase:
    global _DEFAULT_KB
    if _DEFAULT_KB is None:
        _DEFAULT_KB = KnowledgeBase()
    return _DEFAULT_KB


__all__ = ["KnowledgeBase", "Chunk", "RetrievedChunk", "tokenize", "default_kb"]
