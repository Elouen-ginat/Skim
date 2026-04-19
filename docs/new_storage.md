Prior art per storage kind
Kind	Established protocol / lib	Notes
KV	(no dominant protocol) — Dapr, Django cache, cachetools all roll their own	Keep your own floor here.
Relational + schema + validation	SQLModel (Pydantic + SQLAlchemy, one class)	Exactly solves the "one class declares schema + indexes + validation" problem for SQL. Also: Piccolo, Edgy, Tortoise, Prisma-py.
Document / JSON-indexed	Beanie / ODMantic (Mongo), native Pydantic+DynamoDB ODMs	Inner Settings class declares indexes/uniqueness.
Vector	langchain_core.vectorstores.VectorStore (Pinecone, Chroma, Weaviate, Qdrant, pgvector, FAISS, Milvus, LanceDB, Turbopuffer…); LlamaIndex VectorStore; Haystack DocumentStore	Mature, 20+ backends each. Do not invent a new one.
Full-text / search	Haystack DocumentStore, Meilisearch/Typesense/ES SDKs	Fewer abstract layers — adopt Haystack if you need portability.
Time-series	No great portable layer — InfluxDB client, Timescale = SQL, Prometheus remote-write	Usually SQL (Timescale) covers this via the relational tier.
Object blob	fsspec covers S3/GCS/Azure/local uniformly	Use it; don't wrap S3 yourself.
Graph	Neo4j / GQL drivers	Niche — skip until asked.
Generic "multi-backend state store"	Dapr state stores	Has Skaal's exact KV-facade-over-Postgres problem. Useful as a cautionary tale.
Revised proposal
Skaal's actual value-add is declarative selection + solver + migration engine, not the storage protocols themselves. So: adopt per-tier protocols, keep the tier surface narrow, collapse Map/Collection only at the KV tier.

1. KV tier — collapse to one class (this was the original proposal)
Single Store[T]. Map/Collection become aliases. No change to backends. This is the only tier where Skaal invents its own shape, because nothing dominant exists and your KV backends (Local/Redis/SQLite/Dynamo-as-KV) are already solid.

2. Relational tier — delegate to SQLModel

from sqlmodel import SQLModel, Field

@app.relational(durability="persistent", read_latency="< 20ms")
class User(SQLModel, table=True):
    id: int = Field(primary_key=True)
    email: str = Field(index=True, unique=True)
    address: "Address" = Relationship(...)
Solver picks a SQL backend (postgres, cloud-sql-postgres, sqlite).
postgres_backend.py hosts a SQLAlchemy engine keyed on the SQLModel.metadata; ensure_schema() runs SQLModel.metadata.create_all (or Alembic when you add real migrations).
You kill the skaal_kv(ns, key, value JSONB) facade entirely for this tier — real tables, real indexes, real joins. Nested Pydantic models get flattened the SQLModel way (FK or JSON column per field, declarer's choice).
3. Vector tier — wrap LangChain's VectorStore

@app.vector(dim=1536, metric="cosine")
class EmbeddedDocs(VectorStore[Doc]):         # thin Pydantic-typed wrapper
    pass

await EmbeddedDocs.add([doc1, doc2])
hits = await EmbeddedDocs.similarity_search("query", k=5)
Skaal provides:

A typed wrapper that carries T: BaseModel so metadata is validated both ways.
A solver axis (backend ∈ {pgvector, pinecone, chroma, qdrant, weaviate, faiss-local}) chosen by the same constraint machinery you use today (latency, durability, scale, region).
Zero new vector-search code — the underlying langchain_* adapter does the work.
langchain-core is ~200 KB, optional extra — fine on [vector].

4. Search / full-text tier — defer
Add when needed, most likely wrapping Haystack's DocumentStore. Don't build it speculatively.

5. Blob tier — fsspec, not a Store

@app.blob(durability="archive")
class Uploads: ...              # resolves to s3://, gs://, file:// via fsspec
Different enough from Store[T] (stream semantics, no keys-as-lookup) that it gets its own small surface.

6. Don't touch
Stream/queue (Kafka, NATS, Redis Streams) — not storage, already covered by Skaal's channel plugin entry-point at pyproject.toml:101.
Graph — skip until a user asks.
What this changes vs. the previous proposal
__skaal_indexes__ / __skaal_unique__ go away. They were reinventing SQLModel's Field(index=True, unique=True). Relational Store users write SQLModel directly; the Skaal storage decorator only adds the solver/plan/migration metadata on top.
QueryableBackend protocol goes away. The relational tier uses a SQLAlchemy session, not a widened KV protocol. Much smaller surface, no parallel query DSL to maintain.
Vector is a first-class tier, wrapping an existing ecosystem instead of bolted onto KV.
Capability-tier gating still matters for the solver: a @app.relational class cannot resolve to local or redis; a @app.vector class cannot resolve to sqlite. That's where Skaal's z3 layer earns its keep.
Concrete call
If you agree with the direction, I'd split the work:

PR-1 (small, no new deps): collapse Map/Collection → Store[T] at the KV tier. Pure refactor.
PR-2: add @app.relational + SQLModel integration + real Postgres DDL. Drops the JSONB facade for SQL-resolved stores.
PR-3: add @app.vector + LangChain VectorStore adapter + pgvector/Chroma catalog entries.
Which of those three is actually worth doing for you right now? The KV collapse is free; the relational and vector tiers are real design commitments and depend on whether your users are asking for SQL schemas or RAG workloads.
