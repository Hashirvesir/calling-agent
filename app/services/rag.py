"""RAG (Retrieval-Augmented Generation) support for script-based calling agents.

At startup, ScriptRAG loads every .txt/.md file from the scripts directory,
chunks them sentence-by-sentence, and pre-computes OpenAI embeddings so
retrieval is just a numpy dot product (< 1 ms).  The only network hop at
call-time is a single 





 call to embed the user's query
(~100 ms), which runs before the LLM call so it does not add to wall-clock
latency.

RAGContextInjector is a Pipecat FrameProcessor that sits between the user
context aggregator and the LLM service.  For each LLMContextFrame it:
  1. Strips any stale RAG system message from the previous turn.
  2. Retrieves the top-k most relevant script chunks for the latest user turn.
  3. Inserts a short system message with that context just before the user
     message so the LLM can answer from the script.
"""

import re
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger

from pipecat.frames.frames import Frame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


_RAG_MARKER = "__rag_ctx__"
_CONV_MARKER = "__conv_state__"

def _format_target_fields(extraction_fields: list) -> list[str]:
    """Turn a script's extraction_fields into human-readable target labels.

    Accepts list[str] or list[dict] ({name, description}). Domain-agnostic —
    works for any agent (real estate, loans, support, delivery, …).
    """
    labels: list[str] = []
    for item in extraction_fields or []:
        if isinstance(item, str):
            name = item.strip()
            if name:
                labels.append(name)
        elif isinstance(item, dict):
            name = (item.get("name") or "").strip()
            desc = (item.get("description") or "").strip()
            if name:
                labels.append(f"{name} ({desc})" if desc else name)
    return labels


class ScriptRAG:
    """Offline-index RAG over a directory of plain-text/markdown scripts."""

    def __init__(self, scripts_dir: str = "scripts"):
        self._scripts_dir = Path(scripts_dir)
        self._chunks: list[str] = []
        self._embeddings: Optional[np.ndarray] = None
        self._client = None
        self._cache: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def initialize(self, openai_api_key: str) -> None:
        """Load scripts and pre-compute all chunk embeddings.  Call once at startup."""
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=openai_api_key)
        self._load_scripts()
        if self._chunks:
            await self._embed_all()
        else:
            logger.warning("No script chunks found — RAG will return empty context.")

    @classmethod
    async def from_content(cls, content: str, openai_api_key: str) -> "ScriptRAG":
        """Create a ScriptRAG instance from raw text content (DB-loaded scripts)."""
        from openai import AsyncOpenAI

        instance = cls(scripts_dir="")
        instance._client = AsyncOpenAI(api_key=openai_api_key)
        instance._chunk_text(content)
        if instance._chunks:
            await instance._embed_all()
        else:
            logger.warning("Script content produced no chunks — RAG will return empty context.")
        return instance

    async def retrieve(self, query: str, top_k: int = 3, min_score: float = 0.25) -> str:
        """Return the top-k most relevant script chunks for *query*.

        The best-ranked chunk is ALWAYS returned — chunks come from this agent's
        own script, so even a weak semantic match is still on-topic for the
        business. min_score only filters ranks 2..k. (A hard threshold on rank 1
        used to silently return "" whenever a new script's wording didn't closely
        match the caller's phrasing, and the bot then answered generically as if
        no script existed.)

        The query embedding is cached so repeated or similar turns are free.
        """
        if not self._chunks or self._embeddings is None:
            return ""

        q_emb = await self._get_query_embedding(query)
        # OpenAI text-embedding-3-small vectors are unit-normalised → dot == cosine
        scores: np.ndarray = self._embeddings @ q_emb
        top_idx = np.argsort(scores)[::-1][:top_k]
        results = [
            self._chunks[i]
            for rank, i in enumerate(top_idx)
            if rank == 0 or scores[i] >= min_score
        ]
        if scores[top_idx[0]] < min_score:
            logger.info(
                f"RAG: weak match (top score {scores[top_idx[0]]:.2f}) for query "
                f"{query[:60]!r} — injecting best chunk anyway."
            )
        return "\n\n".join(results)

    @property
    def loaded(self) -> bool:
        return bool(self._chunks) and self._embeddings is not None

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_scripts(self) -> None:
        if not self._scripts_dir.exists():
            logger.warning(f"Scripts directory not found: {self._scripts_dir.resolve()}")
            return
        for ext in ("*.txt", "*.md"):
            for path in sorted(self._scripts_dir.glob(ext)):
                text = path.read_text(encoding="utf-8")
                before = len(self._chunks)
                self._chunk_text(text)
                logger.info(f"Loaded {len(self._chunks) - before} chunks from '{path.name}'")
        logger.info(f"Total chunks across all scripts: {len(self._chunks)}")

    def _chunk_text(self, text: str, max_words: int = 100, overlap: int = 20) -> None:
        """Sentence-aware chunking with word cap and overlap."""
        # Split on sentence-ending punctuation (Latin . ! ? + Urdu/Arabic ؟ and
        # the Urdu full-stop "۔" U+06D4 — without it Urdu scripts never split and
        # collapse into one giant chunk, breaking top-k retrieval).
        sentences = re.split(r"(?<=[.!?؟۔])\s+", text.strip())
        current: list[str] = []
        current_len = 0

        for sentence in sentences:
            words = sentence.split()
            if not words:
                continue
            if current_len + len(words) > max_words and current:
                self._chunks.append(" ".join(current))
                # Overlap: keep the tail of the previous chunk
                current = current[-overlap:]
                current_len = len(current)
            current.extend(words)
            current_len += len(words)

        if current:
            self._chunks.append(" ".join(current))

    async def _embed_all(self) -> None:
        logger.info(f"Pre-computing OpenAI embeddings for {len(self._chunks)} chunks…")
        all_emb: list[list[float]] = []
        # text-embedding-3-small supports up to 2048 inputs per request
        batch_size = 200
        for i in range(0, len(self._chunks), batch_size):
            batch = self._chunks[i : i + batch_size]
            resp = await self._client.embeddings.create(
                model="text-embedding-3-small",
                input=batch,
            )
            all_emb.extend(e.embedding for e in resp.data)
        self._embeddings = np.array(all_emb, dtype=np.float32)
        logger.info("Script embeddings pre-computed and cached in memory.")

    async def warm_connection(self) -> None:
        """Fire a throwaway embeddings call purely to open/warm the HTTP
        connection to OpenAI before the caller's first turn needs one for
        real. The pooled connection from _embed_all() (run at agent-build
        time, often minutes/hours before a call) is long since closed by
        call time, so the first real retrieve() of every call was paying a
        full TCP+TLS handshake (~1-1.5s) on top of the actual embedding
        request — this absorbs that cost during greeting playback instead,
        while the caller has nothing to say yet. Best-effort: any failure
        here just means the first real call pays the handshake cost as
        before, so it must never raise."""
        if self._client is None:
            return
        try:
            await self._client.embeddings.create(model="text-embedding-3-small", input=" ")
        except Exception as exc:
            logger.debug(f"RAG connection warm-up failed (harmless): {exc}")

    async def _get_query_embedding(self, query: str) -> np.ndarray:
        if query not in self._cache:
            resp = await self._client.embeddings.create(
                model="text-embedding-3-small",
                input=[query],
            )
            q_emb = np.array(resp.data[0].embedding, dtype=np.float32)
            # Bounded LRU: evict oldest when cache exceeds 64 entries
            if len(self._cache) >= 64:
                del self._cache[next(iter(self._cache))]
            self._cache[query] = q_emb
        return self._cache[query]


def build_conv_state_message(target_fields: list[str], response_language: str = "") -> Optional[dict]:
    """Build the 'what to collect' reminder system message, or None if the
    agent's script defines no extraction fields. Shared by the live-call
    RAGContextInjector and the dashboard's agent-test widget so both send the
    LLM byte-identical instructions."""
    if not target_fields:
        return None
    if response_language == "English":
        body = (
            f"{_CONV_MARKER}\n"
            "In this call you need to collect the following information from the caller:\n"
            + "\n".join(f"- {f}" for f in target_fields)
            + "\nDo not re-ask for information already provided in the conversation; "
            "ask only for what is still missing."
        )
    else:
        body = (
            f"{_CONV_MARKER}\n"
            "اس کال میں آپ کو کالر سے یہ معلومات اکٹھی کرنی ہیں:\n"
            + "\n".join(f"- {f}" for f in target_fields)
            + "\nگفتگو میں جو معلومات پہلے آ چکی ہو وہ دوبارہ نہ پوچھیں، "
            "صرف باقی ماندہ معلومات پوچھیں۔"
        )
    return {"role": "system", "content": body}


def build_rag_message(context_text: str, response_language: str = "") -> Optional[dict]:
    """Build the 'answer from this script context' system message, or None if
    there's no context to inject. Shared by RAGContextInjector and the
    dashboard's agent-test widget — see build_conv_state_message."""
    if not context_text:
        return None
    lang_clause = (
        f"Reply ONLY in {response_language}, "
        "even if the reference script is in another language."
        if response_language
        else "اسی زبان میں جواب دیں جس میں صارف بات کر رہا ہے۔"
    )
    # The wrapper itself must be in the reply language. When it was always
    # Urdu, an English-locked agent received an Urdu instruction every turn
    # and intermittently drifted into Urdu.
    if response_language == "English":
        body = (
            f"{_RAG_MARKER}\n"
            "Below are the relevant parts of the reference script. "
            f"Answer from these. {lang_clause}\n\n"
            f"{context_text}"
        )
    else:
        body = (
            f"{_RAG_MARKER}\n"
            "ذیل میں reference script کے متعلقہ حصے ہیں۔ "
            f"انہی سے جواب دیں۔ {lang_clause}\n\n"
            f"{context_text}"
        )
    return {"role": "system", "content": body}


def strip_rag_and_conv_messages(messages: list[dict]) -> list[dict]:
    """Drop any RAG-context / conv-state system messages — used before
    re-injecting fresh ones for the current turn (both here and in
    RAGContextInjector)."""
    return [m for m in messages if not _is_rag_msg(m) and not _is_conv_state_msg(m)]


class RAGContextInjector(FrameProcessor):
    """Pipecat processor that injects retrieved script context before each LLM call.

    Place this between the user context aggregator and the LLM service:

        user_aggregator → RAGContextInjector → llm

    On each LLMContextFrame it strips any stale RAG message from the previous
    turn, retrieves fresh context for the current user query, and injects it as
    a system message immediately before the user's latest message.
    """

    def __init__(self, rag: ScriptRAG | None = None, top_k: int = 3, extraction_fields: list | None = None, response_language: str = "", rag_task=None, **kwargs):
        super().__init__(**kwargs)
        # `rag` may be ready already, OR `rag_task` is a background build we resolve
        # lazily on the first user turn — so the initial greeting is never blocked
        # by embedding time (RAG is usually done by the time the caller speaks).
        self._rag = rag
        self._rag_task = rag_task
        self._top_k = top_k
        # Domain-agnostic: target fields come from the agent's own script config.
        self._target_fields: list[str] = _format_target_fields(extraction_fields or [])
        # The language the bot must reply in (agent's default_language, e.g.
        # "English"). When set, the RAG context tells the LLM to answer in this
        # language instead of mirroring the caller's language.
        self._response_language: str = response_language

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            # Resolve the background RAG build on first use (greeting already played).
            if self._rag is None and self._rag_task is not None:
                try:
                    self._rag = await self._rag_task
                except Exception as exc:
                    logger.warning(f"RAG build failed — answering without script context: {exc}")
                finally:
                    self._rag_task = None

            messages: list[dict] = frame.context.get_messages()

            # 1. Remove stale RAG and conversation-state messages from previous turn
            clean = strip_rag_and_conv_messages(messages)

            # 2. Find the latest plain-text user message
            user_text: Optional[str] = None
            for m in reversed(clean):
                if m.get("role") != "user":
                    continue
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_text = content
                break

            # 3. Remind the LLM which fields this agent must collect. The LLM
            #    tracks what's already answered from the conversation history
            #    itself — no domain-specific keyword matching needed.
            conv_state_msg = build_conv_state_message(self._target_fields, self._response_language)
            if conv_state_msg:
                # Insert at position 1 (right after the main system prompt)
                clean.insert(1, conv_state_msg)

            # 4. Retrieve and inject RAG script context
            if user_text and self._rag is not None and self._rag.loaded:
                context_text = await self._rag.retrieve(user_text, top_k=self._top_k)
                rag_msg = build_rag_message(context_text, self._response_language)
                if rag_msg:
                    # Insert just before the last user message
                    for i in range(len(clean) - 1, -1, -1):
                        if clean[i].get("role") == "user":
                            clean.insert(i, rag_msg)
                            break
                    logger.debug(f"RAG injected context ({len(context_text)} chars) for query: {user_text[:60]!r}")

            frame.context.set_messages(clean)

        await self.push_frame(frame, direction)


def _is_rag_msg(msg: dict) -> bool:
    content = msg.get("content", "")
    return isinstance(content, str) and content.startswith(_RAG_MARKER)


def _is_conv_state_msg(msg: dict) -> bool:
    content = msg.get("content", "")
    return isinstance(content, str) and content.startswith(_CONV_MARKER)
